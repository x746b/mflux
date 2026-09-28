import json
import os
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mflux.web.app import WebApp
from mflux.web.chat import PromptChat
from mflux.web.settings import WebSettings

ASYNC_CLIENT = httpx.AsyncClient


class OmlxChatTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "OMLX_API_KEY": "local-test-key",
            "OPENAI_API_KEY": "cloud-test-key",
            "OMLX_MFLUX_MODEL": "test-local-model",
        }, clear=True))
        self.body = {"provider": "omlx", "messages": [{"role": "user", "content": "A rainy street"}]}

    async def collect(self, content):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, text=content)

        with patch("mflux.web.chat.httpx.AsyncClient", side_effect=lambda **kw: ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw)):
            events = [json.loads(item) async for item in PromptChat().stream(PromptChat.validate(self.body), "local-test-key", "omlx")]
        return events, requests

    @staticmethod
    def sse(*items):
        return "".join("data: " + (item if isinstance(item, str) else json.dumps(item)) + "\n\n" for item in items)

    def test_configuration_has_no_credentials(self):
        status = PromptChat.status()
        self.assertTrue(status["providers"]["omlx"]["configured"])
        self.assertEqual(status["providers"]["omlx"]["default_model"], "test-local-model")
        self.assertNotIn("local-test-key", json.dumps(status))
        self.assertNotIn("cloud-test-key", json.dumps(status))
        del os.environ["OMLX_API_KEY"]
        self.assertFalse(PromptChat.status()["providers"]["omlx"]["configured"])

    def test_missing_model_and_invalid_provider_are_rejected(self):
        for change in ({"provider": "other"}, {"model": "another-model"}):
            with self.assertRaises(HTTPException):
                PromptChat.validate({**self.body, **change})
        del os.environ["OMLX_MFLUX_MODEL"]
        with self.assertRaises(HTTPException):
            PromptChat.validate(self.body)

    def test_base_url_normalization_and_validation(self):
        self.assertEqual(PromptChat.endpoint("omlx"), "http://127.0.0.1:8000/v1/chat/completions")
        os.environ["OMLX_BASE_URL"] = "http://localhost:9000/"
        self.assertEqual(PromptChat.endpoint("omlx"), "http://127.0.0.1:9000/v1/chat/completions")
        os.environ["OMLX_BASE_URL"] = "https://inference.example/v1/"
        self.assertEqual(PromptChat.endpoint("omlx"), "https://inference.example/v1/chat/completions")
        os.environ["OMLX_BASE_URL"] = "https://name:private-value@inference.example/v1"
        status = PromptChat.status()["providers"]["omlx"]
        self.assertFalse(status["configured"])
        self.assertNotIn("private-value", json.dumps(status))

    def test_shared_instructions_and_protocol_specific_fields(self):
        local = PromptChat.validate({**self.body, "instructions": "Natural lighting"})
        cloud = PromptChat.validate({**self.body, "provider": "openai", "instructions": "Natural lighting"})
        self.assertEqual(local["messages"][0], {"role": "system", "content": cloud["instructions"]})
        self.assertEqual(local["max_tokens"], cloud["max_output_tokens"])
        self.assertTrue(local["stream_options"]["include_usage"])
        self.assertFalse(local["chat_template_kwargs"]["enable_thinking"])
        self.assertNotIn("reasoning", local)
        self.assertNotIn("store", local)

    async def test_stream_usage_and_credential_isolation(self):
        content = self.sse(
            {"choices": [{"delta": {"reasoning_content": "not chat text"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "A rainy "}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "street"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 4}},
            "[DONE]",
        )
        events, requests = await self.collect(content)
        self.assertEqual("".join(e.get("text", "") for e in events), "A rainy street")
        self.assertEqual(events[-1], {"type": "done", "usage": {"input_tokens": 12, "output_tokens": 4}})
        self.assertEqual(requests[0].headers["authorization"], "Bearer local-test-key")
        self.assertNotIn("cloud-test-key", str(requests[0].headers))
        self.assertEqual(str(requests[0].url), "http://127.0.0.1:8000/v1/chat/completions")

    async def test_no_usage_and_incomplete_replies(self):
        events, _ = await self.collect(self.sse({"choices": [{"delta": {"content": "Hi"}, "finish_reason": "stop"}]}, "[DONE]"))
        self.assertEqual(events[-1]["usage"], {"input_tokens": None, "output_tokens": None})
        for content in (
            self.sse({"choices": [{"delta": {"content": "Partial"}, "finish_reason": "length"}]}, "[DONE]"),
            self.sse("[DONE]"),
            self.sse({"error": {"message": "local-test-key"}}),
        ):
            events, _ = await self.collect(content)
            self.assertEqual(events[-1]["type"], "error")
            self.assertNotIn("local-test-key", json.dumps(events))

    def test_route_selects_provider_credentials(self):
        web = WebApp(WebSettings(secret_key="test-secret"), runner=object())
        client = TestClient(web.app, base_url="http://127.0.0.1:8001")
        headers = {"X-MFlux-CSRF": client.get("/api/session").json()["csrf"]}
        selected = []

        async def stream(payload, key, provider):
            selected.append((key, provider, payload["model"]))
            yield '{"type":"done","usage":{}}\n'

        with patch.object(web.chat, "stream", side_effect=stream):
            response = client.post("/api/chat", json=self.body, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(selected, [("local-test-key", "omlx", "test-local-model")])
        del os.environ["OMLX_API_KEY"]
        self.assertEqual(client.post("/api/chat", json=self.body, headers=headers).status_code, 503)
