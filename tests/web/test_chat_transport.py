import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from mflux.web.chat import PromptChat
from mflux.web.chat_stream import BoundedEventStream

ASYNC_CLIENT = httpx.AsyncClient


class SmallEventStream(BoundedEventStream):
    CHUNK_BYTES = 3
    MAX_LINE_BYTES = 32
    MAX_EVENT_BYTES = 64
    MAX_RESPONSE_BYTES = 128


class ResponseChunks:
    def __init__(self, chunks, encoding=""):
        self.chunks = chunks
        self.headers = {"content-encoding": encoding}
        self.reads = 0

    async def aiter_bytes(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk


class EventBoundsTest(unittest.IsolatedAsyncioTestCase):
    async def test_split_unicode_crlf_and_multiline_events(self):
        data = '\ufeffdata: café\r\ndata: rain\r\n\r\n: comment\r\rdata: end\n\n'.encode()
        response = ResponseChunks([bytes([byte]) for byte in data])
        self.assertEqual([event async for event in SmallEventStream.events(response)], ["café\nrain", "end"])

    async def test_exact_line_boundary_and_unterminated_line_limit(self):
        with patch.object(SmallEventStream, "MAX_LINE_BYTES", 10):
            self.assertEqual(
                [event async for event in SmallEventStream.events(ResponseChunks([b"data: abcd\n\n"]))], ["abcd"]
            )
            with self.assertRaises(ValueError):
                _ = [event async for event in SmallEventStream.events(ResponseChunks([b"data:", b" abcde"]))]

    async def test_event_size_and_total_size_limits(self):
        with patch.object(SmallEventStream, "MAX_EVENT_BYTES", 12):
            with self.assertRaises(ValueError):
                _ = [event async for event in SmallEventStream.events(ResponseChunks([b"data: a\ndata: b\n\n"]))]
        with patch.object(SmallEventStream, "MAX_RESPONSE_BYTES", 10):
            response = ResponseChunks([b":ok\n\n"] * 3)
            with self.assertRaises(ValueError):
                _ = [event async for event in SmallEventStream.events(response)]

    async def test_compression_rejected_before_body_read(self):
        response = ResponseChunks([b"unused"], encoding="gzip")
        with self.assertRaises(ValueError):
            _ = [event async for event in SmallEventStream.events(response)]
        self.assertEqual(response.reads, 0)


class EndpointTransportTest(unittest.TestCase):
    def test_http_only_for_loopback_and_https_for_remote(self):
        with patch.dict(os.environ, {}, clear=True):
            for base in ("http://127.0.0.1:8000/v1", "http://[::1]:8000/v1", "http://localhost:8000/v1"):
                os.environ["OMLX_BASE_URL"] = base
                self.assertTrue(PromptChat.endpoint("omlx").startswith("http://"))
            for base in ("http://192.168.1.10:8000/v1", "http://inference.example/v1"):
                os.environ["OMLX_BASE_URL"] = base
                with self.assertRaises(HTTPException) as error:
                    PromptChat.endpoint("omlx")
                self.assertIn("HTTPS", error.exception.detail)
            os.environ["OMLX_BASE_URL"] = "https://inference.example/v1"
            self.assertEqual(PromptChat.endpoint("omlx"), "https://inference.example/v1/chat/completions")


class WaitingStream(httpx.AsyncByteStream):
    def __init__(self, progress=False, tokens=False):
        self.progress = progress
        self.tokens = tokens
        self.closed = asyncio.Event()

    async def __aiter__(self):
        while self.progress or self.tokens:
            if self.tokens:
                yield b'data: {"type":"response.output_text.delta","delta":"Hi"}\n\n'
            else:
                yield b": progress\n\n"
            await asyncio.sleep(0.001)
        await asyncio.Event().wait()
        yield b""

    async def aclose(self):
        self.closed.set()


class DeadlineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.object(PromptChat, "RESPONSE_DEADLINES", {"openai": 0.03, "omlx": 0.03}))

    def client(self, handler):
        return patch(
            "mflux.web.chat.httpx.AsyncClient",
            side_effect=lambda **kw: ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw),
        )

    async def collect(self, chat, provider="openai"):
        return [json.loads(event) async for event in chat.stream({}, "test-key", provider)]

    async def test_deadline_with_and_without_upstream_progress(self):
        for provider in ("openai", "omlx"):
            for progress in (False, True):
                with self.subTest(provider=provider, progress=progress):
                    upstream = WaitingStream(progress=progress)
                    chat = PromptChat()
                    with self.client(lambda request: httpx.Response(200, stream=upstream)):
                        events = await asyncio.wait_for(self.collect(chat, provider), 1)
                    self.assertIn("time limit", events[-1]["message"])
                    self.assertTrue(upstream.closed.is_set())
                    self.assertFalse(chat.lock.locked())

    async def test_slow_consumer_releases_lock_and_next_request_works(self):
        upstream = WaitingStream(tokens=True)
        chat = PromptChat()
        with self.client(lambda request: httpx.Response(200, stream=upstream)):
            stream = chat.stream({}, "test-key")
            self.assertEqual(json.loads(await asyncio.wait_for(anext(stream), 1))["type"], "delta")
            await asyncio.wait_for(upstream.closed.wait(), 1)
            await asyncio.sleep(0)
            self.assertFalse(chat.lock.locked())
        completed = 'data: {"type":"response.completed","response":{"usage":{}}}\n\n'
        with self.client(lambda request: httpx.Response(200, text=completed)):
            self.assertEqual((await asyncio.wait_for(self.collect(chat), 1))[-1]["type"], "done")
        await stream.aclose()
        self.assertFalse(chat.lock.locked())

    async def test_cancel_before_first_event(self):
        upstream = WaitingStream()
        chat = PromptChat()
        with self.client(lambda request: httpx.Response(200, stream=upstream)):
            stream = chat.stream({}, "test-key")
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            await stream.aclose()
        self.assertFalse(chat.lock.locked())

    async def test_parser_failure_closes_response_and_sanitizes_error(self):
        class InvalidStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b"data: normal-text"

            async def aclose(self):
                self.closed = True

        upstream = InvalidStream()
        chat = PromptChat()
        with patch.object(BoundedEventStream, "MAX_LINE_BYTES", 8):
            with self.client(lambda request: httpx.Response(200, stream=upstream)):
                events = await self.collect(chat)
        self.assertEqual(events[-1]["type"], "error")
        self.assertNotIn("normal-text", events[-1]["message"])
        self.assertTrue(upstream.closed)
        self.assertFalse(chat.lock.locked())
