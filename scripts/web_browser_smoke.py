# Run with Playwright in a separate venv; server Python must have mflux[web] installed.

import argparse
import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


class BrowserSmoke:
    COMMAND = "mflux-generate-qwen-2.1-edit"

    def __init__(self, args):
        self.args = args
        self.work = args.work_dir.resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.process = None
        self.log = None

    def run(self) -> None:
        from playwright.sync_api import expect, sync_playwright

        # A fresh wheel environment may spend several seconds importing model CLI schemas.
        expect.set_options(timeout=30000)
        try:
            self._start("old")
            with sync_playwright() as playwright:
                browser = playwright.firefox.launch_persistent_context(
                    str(self.work / "profile"),
                    headless=True,
                    viewport={"width": 1440, "height": 1050},
                    firefox_user_prefs={"browser.cache.disk.enable": True},
                )
                try:
                    page = browser.pages[0]
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(self.url)
                    expect(page.locator("#command")).to_have_value("mflux-generate-qwen-2.1")
                    page.locator('[data-flag="--prompt"]').fill("A saved text-generation draft")
                    before = self._get("/__test/assets")
                    page.evaluate("fetch('/static/generate.js').then(r => r.text())")
                    assert self._get("/__test/assets") == before, "Previous script was not cached"
                    self._stop()
                    self._start("new")
                    page.goto(self.url + "/?upgrade=1")
                    expect(page.locator("#command")).to_have_value("mflux-generate-qwen-2.1")
                    page.select_option("#command", self.COMMAND)
                    expect(page.locator("#references-section")).to_be_visible()
                    expect(page.locator("#lora-section")).to_be_hidden()
                    expect(page.locator("#img2img-section")).to_be_hidden()
                    page.locator('[data-flag="--prompt"]').fill("Combine image 1 and image 2")
                    # Known tiny PNGs; nothing runs model inference in this server.
                    files = [self.work / f"reference-{i}.png" for i in range(3)]
                    uploads = []
                    page.on(
                        "response",
                        lambda response: (
                            uploads.append(response.json()["id"])
                            if response.url.endswith("/api/uploads") and response.status == 200
                            else None
                        ),
                    )
                    page.locator("#reference-images").set_input_files(files)
                    expect(page.locator(".reference-card")).to_have_count(3)
                    page.get_by_role("button", name="Remove image 2", exact=True).click()
                    expect(page.locator(".reference-card")).to_have_count(2)
                    assert page.locator(".reference-card span").all_text_contents() == ["Image 1", "Image 2"]
                    page.select_option("#command", "mflux-generate-qwen-2.1")
                    expect(page.locator("#references-section")).to_be_hidden()
                    expect(page.locator('[data-flag="--prompt"]')).to_have_value("A saved text-generation draft")
                    page.select_option("#command", self.COMMAND)
                    expect(page.locator(".reference-card")).to_have_count(2)
                    page.select_option("#aspect-ratio", "16:9")
                    assert int(page.locator('[data-flag="--width"]').input_value()) % 32 == 0
                    assert int(page.locator('[data-flag="--height"]').input_value()) % 32 == 0
                    with page.expect_response(lambda r: r.url.endswith("/api/generate")) as generated:
                        page.locator("#generate").click()
                    assert generated.value.status == 200, generated.value.text()
                    payload = generated.value.request.post_data_json
                    assert payload["references"] == [uploads[0], uploads[2]]
                    assert payload["image"] is None and payload["loras"] == []
                    assert "--image-paths" in generated.value.json()["shell_command"]
                    # Uploaded files used by this queued job survive history clearing.
                    page.on("dialog", lambda dialog: dialog.accept())
                    page.locator("#clear-history").click()
                    expect(page.locator(".reference-card")).to_have_count(0)
                    assert (self.work / "out/.uploads" / uploads[0]).exists()
                    assert (self.work / "out/.uploads" / uploads[2]).exists()
                    assert not (self.work / "out/.uploads" / uploads[1]).exists()
                    # Verify init uploads are preserved through saved-family restoration too.
                    page.select_option("#command", "mflux-generate-qwen-2.1")
                    page.locator("#img2img-section summary").click()
                    page.locator("#init-image").set_input_files(files[0])
                    expect(page.locator("#init-preview")).to_be_visible()
                    page.select_option("#command", self.COMMAND)
                    page.select_option("#command", "mflux-generate-qwen-2.1")
                    expect(page.locator("#init-preview")).to_be_visible()
                    page.locator("#clear-history").click()
                    expect(page.locator("#init-preview")).to_be_hidden()
                    assert not errors, errors
                    assets = self._get("/__test/assets")
                    assert any(path.startswith("/static/generate.js?v=") for path in assets)
                    page.select_option("#command", self.COMMAND)
                    page.screenshot(path=str(self.work / "qwen21-edit-ui.png"), full_page=True)
                    print(
                        "PASS: warm Firefox cache upgrade, ordered uploads/removal, saved-family switching, "
                        "dimension snapping, queued references, history clearing; no model inference"
                    )
                finally:
                    browser.close()
        finally:
            self._stop()

    @staticmethod
    def serve(args) -> None:
        import uvicorn
        from fastapi import Request
        from PIL import Image

        import mflux.web.app as app_module
        from mflux.web.runner import JobRunner
        from mflux.web.settings import WebSettings

        work = args.work_dir.resolve()
        if args.serve == "old":
            assets = work / "old-web"
            repo = Path(__file__).resolve().parents[1]
            names = subprocess.check_output(
                [
                    "git",
                    "ls-tree",
                    "-r",
                    "--name-only",
                    args.baseline,
                    "src/mflux/web/static",
                    "src/mflux/web/templates",
                ],
                cwd=repo,
                text=True,
            ).splitlines()
            for name in names:
                target = assets / Path(name).relative_to("src/mflux/web")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(subprocess.check_output(["git", "show", f"{args.baseline}:{name}"], cwd=repo))
            app_module.WEB_DIR = assets
        for index in range(3):
            Image.new("RGBA", (32, 32), (index * 70, 100, 150, 180)).save(work / f"reference-{index}.png")
        runner = JobRunner(max_memory_gb=2)
        # Exercise validation, uploads and queuing, but never start the inference worker.
        runner.start = lambda: None
        settings = WebSettings(output_dir=work / "out", config_path=work / "web.json", secret_key="test" * 16)
        app = app_module.WebApp(settings, runner=runner).app
        counts = {}

        @app.middleware("http")
        async def track_assets(request: Request, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/static/"):
                path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                counts[path] = counts.get(path, 0) + 1
                if args.serve == "old":
                    # Force a stale-script scenario even when the old checkout was just created.
                    response.headers["Cache-Control"] = "public, max-age=86400"
            return response

        @app.get("/__test/assets")
        def asset_counts():
            return counts

        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")

    def _get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=2) as response:
            return json.load(response)

    def _start(self, mode):
        self.log = (self.work / f"{mode}-server.log").open("w")
        self.process = subprocess.Popen(
            [
                str(self.args.server_python),
                str(Path(__file__).resolve()),
                "--serve",
                mode,
                "--port",
                str(self.port),
                "--work-dir",
                str(self.work),
                "--baseline",
                self.args.baseline,
            ],
            stdout=self.log,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
        for _ in range(120):
            if self.process.poll() is not None:
                raise RuntimeError(f"Server exited; inspect {self.work / (mode + '-server.log')}")
            try:
                self._get("/api/session")
                return
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        raise TimeoutError("Server did not start")

    def _stop(self):
        if self.process is not None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--baseline", default="cc1f1ff")
    parser.add_argument("--serve", choices=("old", "new"))
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.serve:
        BrowserSmoke.serve(args)
    else:
        BrowserSmoke(args).run()


if __name__ == "__main__":
    main()
