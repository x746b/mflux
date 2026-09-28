import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mflux.web.app import WebApp
from mflux.web.auth import WebAuth
from mflux.web.chat import PromptChat
from mflux.web.settings import WebSettings

ASYNC_CLIENT = httpx.AsyncClient


class ChatRoutesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.settings = WebSettings(output_dir=Path(directory.name), secret_key="private-secret")

    def client(self):
        return TestClient(WebApp(self.settings, runner=object()).app, base_url="http://127.0.0.1:8001")

    def test_config_reports_only_presence(self):
        os.environ["OPENAI_API_KEY"] = "private-openai-key"
        response = self.client().get("/api/chat/config")
        self.assertTrue(response.json()["configured"])
        self.assertEqual(response.json()["default_model"], "gpt-6-luna")
        self.assertNotIn("private-openai-key", response.text)
        self.assertNotIn("private-secret", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_authentication_and_csrf(self):
        self.settings.api_key_hash = WebAuth.hash_key("test-key")
        client = self.client()
        self.assertEqual(client.get("/api/chat/config").status_code, 401)
        self.assertEqual(client.post("/api/chat", json={}).status_code, 401)
        self.settings.api_key_hash = None
        client = self.client()
        self.assertEqual(client.post("/api/chat", json={}).status_code, 403)

    def test_missing_key_and_bad_requests(self):
        client = self.client()
        headers = {"X-MFlux-CSRF": client.get("/api/session").json()["csrf"]}
        payload = {"messages": [{"role": "user", "content": "A rainy street"}]}
        self.assertEqual(client.post("/api/chat", json=payload, headers=headers).status_code, 503)
        self.assertEqual(client.post("/api/chat", json=[], headers=headers).status_code, 400)
        self.assertEqual(client.post("/api/chat", content="{bad", headers=headers).status_code, 400)
        self.assertEqual(client.post("/api/chat", content="x" * 320001, headers=headers).status_code, 413)

    def test_validation_and_defaults(self):
        request = {"messages": [{"role": "user", "content": "A rainy street"}]}
        result = PromptChat.validate(request)
        self.assertEqual(result["model"], "gpt-6-luna")
        self.assertFalse(result["store"])
        self.assertEqual(result["reasoning"], {"effort": "low"})
        self.assertEqual(result["max_output_tokens"], 4096)
        self.assertNotIn("tools", result)
        self.assertEqual(PromptChat.validate({**request, "model": "gpt-6-sol"})["model"], "gpt-6-sol")
        for invalid in [
            {**request, "model": "other"}, {**request, "model": []},
            {**request, "instructions": "x" * 4001},
            {"messages": [{"role": "system", "content": "test"}]},
            {"messages": [{"role": "user", "content": " "}]},
            {"messages": [{"role": "user", "content": "x" * 12001}]},
            {"messages": request["messages"] * 25},
            {"messages": [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 11000} for i in range(5)]},
        ]:
            with self.subTest(invalid=list(invalid)):
                with self.assertRaises(HTTPException):
                    PromptChat.validate(invalid)

    def test_authenticated_post_streams_ndjson(self):
        os.environ["OPENAI_API_KEY"] = "private-openai-key"
        client = self.client()
        headers = {"X-MFlux-CSRF": client.get("/api/session").json()["csrf"]}
        events = (
            'data: {"type":"response.output_text.delta","delta":"A rainy street"}\n\n'
            'data: {"type":"response.completed","response":{"usage":{"input_tokens":4,"output_tokens":3}}}\n\n'
        )
        with patch("mflux.web.chat.httpx.AsyncClient", side_effect=lambda **kw: ASYNC_CLIENT(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=events)), **kw)):
            response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "An idea"}]}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/x-ndjson", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual([json.loads(line)["type"] for line in response.text.splitlines()], ["delta", "done"])


class ChatStreamTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def events(*items):
        return "".join("data: " + json.dumps(item) + "\n\n" for item in items)

    async def collect(self, handler):
        with patch("mflux.web.chat.httpx.AsyncClient", side_effect=lambda **kw: ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw)):
            payload = PromptChat.validate({"messages": [{"role": "user", "content": "A rainy street"}]})
            return [json.loads(item) async for item in PromptChat().stream(payload, "secret-key")]

    async def test_stream_and_payload(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
            self.assertEqual(request.headers["authorization"], "Bearer secret-key")
            body = json.loads(request.content)
            self.assertFalse(body["store"])
            self.assertTrue(body["stream"])
            return httpx.Response(200, text=self.events(
                {"type": "response.output_text.delta", "delta": "Rain "},
                {"type": "response.output_text.delta", "delta": "in Prague"},
                {"type": "response.completed", "response": {"usage": {"input_tokens": 10, "output_tokens": 5}, "secret": "hidden"}},
            ))
        events = await self.collect(handler)
        self.assertEqual("".join(e.get("text", "") for e in events), "Rain in Prague")
        self.assertEqual(events[-1], {"type": "done", "usage": {"input_tokens": 10, "output_tokens": 5}})
        self.assertNotIn("hidden", json.dumps(events))

    async def test_provider_errors_are_sanitized(self):
        for status in (400, 401, 403, 404, 429, 500):
            with self.subTest(status=status):
                events = await self.collect(lambda request: httpx.Response(status, text="secret-key private diagnostic"))
                self.assertEqual(events[0]["type"], "error")
                self.assertNotIn("secret-key", json.dumps(events))

    async def test_timeout_and_incomplete_stream(self):
        def timeout(request):
            raise httpx.ReadTimeout("secret-key")
        self.assertEqual((await self.collect(timeout))[0]["type"], "error")
        for body in ["data: not-json\n\n", "data: []\n\n", self.events({"type": "response.incomplete"}), ""]:
            events = await self.collect(lambda request: httpx.Response(200, text=body))
            self.assertEqual(events[-1]["type"], "error")

    async def test_busy_and_cancellation_release_resources(self):
        class Stream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
                await asyncio.Event().wait()

            async def aclose(self):
                self.closed = True

        stream = Stream()
        chat = PromptChat()
        with patch("mflux.web.chat.httpx.AsyncClient", side_effect=lambda **kw: ASYNC_CLIENT(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)), **kw)):
            first = chat.stream({}, "secret-key")
            self.assertEqual(json.loads(await anext(first))["type"], "delta")
            busy = [json.loads(item) async for item in chat.stream({}, "secret-key")]
            self.assertEqual(busy[0]["type"], "error")
            await first.aclose()
            self.assertTrue(stream.closed)
            self.assertFalse(chat.lock.locked())
