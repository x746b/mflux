import asyncio
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from starlette.requests import Request
from starlette.responses import JSONResponse

from mflux.web.app import WebApp
from mflux.web.auth import LoginThrottle
from mflux.web.limits import RequestBodyLimitMiddleware
from mflux.web.runner import JobQueueFull, JobRunner
from mflux.web.settings import WebSettings


class AuthenticationLimitsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.web = WebApp(WebSettings(secret_key="test-secret", api_key_hash="test-hash"), runner=object())

    async def test_verification_runs_off_event_loop(self):
        loop_thread = threading.get_ident()
        thread_ids = []
        with patch.object(self.web.auth, "verify_api_key", side_effect=lambda key: thread_ids.append(threading.get_ident()) or True):
            self.assertEqual(await self.web._verify_key("test", "client"), (True, 0))
        self.assertNotEqual(thread_ids[0], loop_thread)

    async def test_backoff_prevents_verification(self):
        for _ in range(LoginThrottle.FREE_ATTEMPTS):
            self.web.throttle.record_failure("client")
        with patch.object(self.web.auth, "verify_api_key") as verify:
            valid, retry = await self.web._verify_key("test", "client")
        self.assertFalse(valid)
        self.assertGreater(retry, 0)
        verify.assert_not_called()

    async def test_concurrency_slots_survive_request_cancellation(self):
        release = threading.Event()
        started = [threading.Event(), threading.Event()]

        def verify(key):
            started[int(key)].set()
            release.wait(timeout=5)
            return False

        with patch.object(self.web.auth, "verify_api_key", side_effect=verify):
            tasks = [asyncio.create_task(self.web._verify_key(str(i), "client")) for i in range(2)]
            try:
                for event in started:
                    self.assertTrue(await asyncio.to_thread(event.wait, 2))
                self.assertEqual(await self.web._verify_key("extra", "other"), (False, 1))
                tasks[0].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await tasks[0]
                self.assertTrue(self.web._auth_slots.locked())
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.gather(*list(self.web._auth_tasks), return_exceptions=True)
                await asyncio.sleep(0)
            self.assertFalse(self.web._auth_slots.locked())
            self.assertEqual(self.web.throttle._failures["client"][0], 2)


class BodyLimitsTest(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, chunks, limit=8, headers=None, app=None, path="/api/validate"):
        output = []
        delivered = []

        async def receive():
            return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}

        async def sink(scope, receive, send):
            request = Request(scope, receive)
            async for chunk in request.stream():
                delivered.append(chunk)
            await JSONResponse({"ok": True})(scope, receive, send)

        async def send(message):
            output.append(message)

        middleware = RequestBodyLimitMiddleware(app or sink, max_upload_mb=1)
        middleware.JSON_LIMIT = limit
        middleware.upload_limit = limit
        await middleware({"type": "http", "method": "POST", "path": path, "headers": headers or []}, receive, send)
        return output, delivered

    async def test_streamed_bytes_are_bounded(self):
        output, delivered = await self.invoke([b"1234", b"56789"])
        self.assertEqual(output[0]["status"], 413)
        self.assertEqual(delivered, [b"1234"])

    async def test_boundary_and_declared_size(self):
        output, delivered = await self.invoke([b"1234", b"5678"])
        self.assertEqual(output[0]["status"], 200)
        self.assertEqual(b"".join(delivered), b"12345678")
        output, delivered = await self.invoke([b"123456789"], headers=[(b"content-length", b"9")])
        self.assertEqual(output[0]["status"], 413)
        self.assertEqual(delivered, [])

    async def test_multipart_files_close_on_limit(self):
        from starlette import formparsers

        opened = []
        original = formparsers.SpooledTemporaryFile

        def track(*args, **kwargs):
            handle = original(*args, **kwargs)
            opened.append(handle)
            return handle

        async def parse(scope, receive, send):
            await Request(scope, receive).form()

        first = b'--test\r\nContent-Disposition: form-data; name="file"; filename="image.png"\r\nContent-Type: image/png\r\n\r\nx'
        with patch.object(formparsers, "SpooledTemporaryFile", side_effect=track):
            output, _ = await self.invoke(
                [first, b"x" * 20], limit=len(first) + 5, app=parse, path="/api/uploads",
                headers=[(b"content-type", b"multipart/form-data; boundary=test")],
            )
        self.assertEqual(output[0]["status"], 413)
        self.assertTrue(opened)
        self.assertTrue(all(handle.closed for handle in opened))


class ResourceRoutesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.web = WebApp(WebSettings(output_dir=self.root, secret_key="test-secret"), runner=object())
        self.client = TestClient(self.web.app, base_url="http://127.0.0.1:8001")
        self.headers = {"X-MFlux-CSRF": self.client.get("/api/session").json()["csrf"]}

    def test_small_upload_still_works(self):
        image = io.BytesIO()
        Image.new("RGB", (2, 2)).save(image, format="PNG")
        response = self.client.post("/api/uploads", files={"file": ("image.png", image.getvalue(), "image/png")}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.web.settings.upload_dir / response.json()["id"]).is_file())

    def test_json_limit_and_security_headers(self):
        with patch.object(RequestBodyLimitMiddleware, "AUTH_LIMIT", 8):
            response = self.client.post("/api/login", json={"api_key": "test"}, headers=self.headers)
        self.assertEqual(response.status_code, 413)
        self.assertIn("Content-Security-Policy", response.headers)

    def test_streamed_json_limit_through_app(self):
        with patch.object(RequestBodyLimitMiddleware, "AUTH_LIMIT", 8):
            response = self.client.post("/api/login", content=iter([b'{"api_key":', b'"test"}']), headers=self.headers)
        self.assertEqual(response.status_code, 413)
        self.assertIn("Content-Security-Policy", response.headers)

    def test_bearer_and_login_share_backoff(self):
        self.web.auth.api_key_hash = "test-hash"
        client = self.client._transport.client[0]
        for _ in range(LoginThrottle.FREE_ATTEMPTS):
            self.web.throttle.record_failure(client)
        with patch.object(self.web.auth, "verify_api_key") as verify:
            response = self.client.get("/api/session", headers={"Authorization": "Bearer test"})
            self.assertEqual(response.status_code, 429)
            self.assertIn("retry-after", response.headers)
            response = self.client.post("/api/login", json={"api_key": "test"}, headers=self.headers)
            self.assertEqual(response.status_code, 429)
            verify.assert_not_called()

    def test_normal_login_and_bearer_csrf(self):
        self.web.auth.set_api_key("test-password-123")
        response = self.client.post("/api/login", json={"api_key": "test-password-123"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.client.get("/api/session").json()["authenticated"])
        with patch.object(self.web.auth, "verify_api_key", wraps=self.web.auth.verify_api_key) as verify:
            response = self.client.post("/api/logout", headers={"Authorization": "Bearer test-password-123"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(verify.call_count, 1)

    def test_queue_full_returns_retryable_response(self):
        self.web.runner = SimpleNamespace(submit=lambda *args, **kwargs: (_ for _ in ()).throw(JobQueueFull("Queue full")))
        with patch.object(self.web, "_invocation", return_value=SimpleNamespace(validate=lambda: None)):
            response = self.client.post("/api/generate", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 429)
        self.assertIn("retry-after", response.headers)


class QueueLimitsTest(unittest.TestCase):
    def test_concurrent_admission_is_bounded(self):
        runner = JobRunner(max_memory_gb=1)

        def submit(_):
            try:
                return runner.submit(None, {})
            except JobQueueFull:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(submit, range(32)))
        self.assertEqual(sum(job is not None for job in jobs), runner.MAX_PENDING_JOBS)
        self.assertEqual(len(runner.jobs()), runner.MAX_PENDING_JOBS)
        first = next(job for job in jobs if job)
        runner.cancel(first.id)
        with self.assertRaises(JobQueueFull):
            runner.submit(None, {})
        runner._queue.get_nowait()
        runner.submit(None, {})
        runner.stop()
        with self.assertRaises(JobQueueFull):
            runner.submit(None, {})

    def test_history_prunes_finished_jobs_behind_active_job(self):
        runner = JobRunner(max_memory_gb=1)
        active = runner.submit(None, {})
        runner._queue.get_nowait()
        active.status = "running"
        with patch.object(JobRunner, "MAX_JOBS_KEPT", 3):
            for _ in range(6):
                job = runner.submit(None, {})
                runner._queue.get_nowait()
                job.status, job.finished_at = "done", 1
        self.assertEqual(len(runner.jobs()), 3)
        self.assertIs(runner.get(active.id), active)
