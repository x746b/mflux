import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from mflux.web.app import WebApp
from mflux.web.schema import FormSchema
from mflux.web.settings import WebSettings


class StartupProgressTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.web = WebApp(WebSettings(secret_key="test-secret"), runner=object())

    async def test_progress_remains_available_during_schema_build(self):
        started, release = threading.Event(), threading.Event()

        def commands(progress):
            progress("mflux-generate", 0, 2)
            progress("mflux-generate-z-image", 1, 2)
            started.set()
            if not release.wait(2):
                raise TimeoutError("Test worker not released")
            return [{"command": "first"}, {"command": "second"}]

        self.web.schema = SimpleNamespace(commands=commands)
        phases = []

        def scan():
            with self.web._discovery_lock:
                phases.append(self.web._discovery["phase"])
            return []

        with patch.object(self.web.guard, "list_local_models", side_effect=scan):
            with patch.object(self.web.guard, "list_local_loras", side_effect=scan):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.web.app), base_url="http://127.0.0.1:8001") as client:
                    self.assertEqual((await client.get("/api/commands/progress")).json()["phase"], "waiting")
                    pending = asyncio.create_task(client.get("/api/commands"))
                    try:
                        self.assertTrue(await asyncio.to_thread(started.wait, 1))
                        response = await asyncio.wait_for(client.get("/api/commands/progress"), 1)
                        progress = response.json()
                        self.assertEqual(progress["command"], "mflux-generate-z-image")
                        self.assertEqual((progress["completed"], progress["total"]), (1, 4))
                        self.assertNotIn("started_at", progress)
                        self.assertEqual(response.headers["cache-control"], "no-store")
                    finally:
                        release.set()
                        await pending
                    progress = (await client.get("/api/commands/progress")).json()
                    self.assertEqual(progress["phase"], "done")
                    self.assertEqual(progress["completed"], progress["total"])
        self.assertEqual(phases, ["models", "loras"])

    async def test_progress_requires_authentication(self):
        self.web.auth.set_api_key("test-password")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.web.app), base_url="http://127.0.0.1:8001") as client:
            self.assertEqual((await client.get("/api/commands/progress")).status_code, 401)

    async def test_failure_is_reported_without_exception_details(self):
        with patch.object(self.web.schema, "commands", side_effect=RuntimeError("private diagnostic")):
            with self.assertRaises(RuntimeError):
                self.web._discover_commands()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.web.app), base_url="http://127.0.0.1:8001") as client:
            response = await client.get("/api/commands/progress")
        self.assertEqual(response.json()["phase"], "error")
        self.assertNotIn("private diagnostic", response.text)

    def test_schema_callback_preserves_cache(self):
        schema = FormSchema()
        updates = []
        with patch.object(FormSchema, "_build", return_value={"available": True}) as build:
            first = schema.commands(progress=lambda *args: updates.append(args))
            second = schema.commands()
        self.assertEqual(first, second)
        self.assertEqual(build.call_count, len(first))
        self.assertEqual(len(updates), len(first))
        self.assertEqual(updates[-1][1:], (len(first) - 1, len(first)))
