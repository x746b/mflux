import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from mflux.web.app import WebApp
from mflux.web.auth import WebAuth
from mflux.web.settings import WebSettings


class SettingsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.token_path = self.root / "hf-token"
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch("huggingface_hub.constants.HF_TOKEN_PATH", str(self.token_path)))

    def client(self, **overrides):
        settings = WebSettings(
            output_dir=self.root / "outputs",
            models_dirs=[self.root / "models"],
            lora_dirs=[self.root / "loras"],
            secret_key="test-secret",
            **overrides,
        )
        runner = SimpleNamespace(
            idle_unload_seconds=300,
            status=lambda: {
                "cached_models": [{"model": "test-model"}],
                "max_memory_gb": 24,
                "max_cache_gb": 6,
            },
        )
        return TestClient(WebApp(settings, runner=runner).app, base_url="http://127.0.0.1:8001")

    def test_missing_and_blank_tokens(self):
        self.assertEqual(WebSettings.huggingface_status(), {"status": "missing", "source": None})
        self.token_path.write_text(" \n")
        os.environ["HF_TOKEN"] = " "
        self.assertEqual(WebSettings.huggingface_status()["status"], "missing")

    def test_saved_login_and_environment_precedence(self):
        self.token_path.write_text("hf_saved_secret")
        self.assertEqual(WebSettings.huggingface_status(), {"status": "detected", "source": "login"})
        os.environ["HF_TOKEN"] = "hf_environment_secret"
        self.assertEqual(WebSettings.huggingface_status(), {"status": "detected", "source": "environment"})
        response = self.client().get("/api/settings")
        self.assertNotIn("hf_saved_secret", response.text)
        self.assertNotIn("hf_environment_secret", response.text)
        self.assertNotIn("test-secret", response.text)

    def test_legacy_environment_token(self):
        os.environ["HUGGING_FACE_HUB_TOKEN"] = "hf_legacy_secret"
        self.assertEqual(WebSettings.huggingface_status(), {"status": "detected", "source": "environment"})

    def test_unreadable_token(self):
        with patch.object(Path, "read_text", side_effect=PermissionError("private file")):
            self.assertEqual(WebSettings.huggingface_status(), {"status": "unreadable", "source": None})

    def test_runtime_and_refresh(self):
        client = self.client()
        response = client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        runtime = response.json()["runtime"]
        self.assertEqual(runtime["models_dirs"], [str((self.root / "models").resolve())])
        self.assertEqual(runtime["lora_dirs"], [str((self.root / "loras").resolve())])
        self.assertEqual(runtime["output_dir"], str((self.root / "outputs").resolve()))
        self.assertEqual(runtime["max_memory_gb"], 24)
        self.assertEqual(runtime["max_cache_gb"], 6)
        self.assertEqual(runtime["idle_unload_minutes"], 5)
        self.assertEqual(runtime["cached_models"], [{"model": "test-model"}])
        self.token_path.write_text("hf_new_login")
        self.assertEqual(client.get("/api/settings").json()["huggingface"]["status"], "detected")

    def test_settings_follow_authentication(self):
        client = self.client(api_key_hash=WebAuth.hash_key("test-api-key"))
        self.assertEqual(client.get("/api/settings").status_code, 401)
        self.assertEqual(client.get("/settings", follow_redirects=False).status_code, 303)
        headers = {"Authorization": "Bearer test-api-key"}
        self.assertEqual(client.get("/api/settings", headers=headers).status_code, 200)
        self.assertEqual(client.get("/settings", headers=headers).status_code, 200)

    def test_settings_page_and_shared_navigation(self):
        client = self.client()
        for page in ("/", "/gallery", "/settings"):
            response = client.get(page)
            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/settings"', response.text)
            self.assertIn('/static/appearance.js?v=', response.text)
        page = client.get("/settings").text
        self.assertIn("hf auth login", page)
        self.assertNotIn('type="password"', page)
        self.assertIn('id="appearance-theme"', page)
