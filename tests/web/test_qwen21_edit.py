from types import SimpleNamespace

import pytest
from PIL import Image

from mflux.web.adapters import Qwen21EditAdapter
from mflux.web.invocation import Invocation, InvocationError
from mflux.web.schema import FormSchema
from tests.web.test_app import client_for, csrf, make_app, png_bytes

pytestmark = pytest.mark.fast
COMMAND = "mflux-generate-qwen-2.1-edit"


@pytest.fixture
def web(tmp_path):
    return make_app(tmp_path)


def invocation(web, options=None, references=None, **extra):
    return Invocation.from_payload(
        {
            "command": COMMAND,
            "options": {"--prompt": "Use image 1", **(options or {})},
            "references": references,
            **extra,
        },
        web.guard,
        web.schema,
        web.settings.output_dir / "edit",
    )


def test_edit_schema_exposes_references_and_kv_cache_but_no_loras():
    schema = FormSchema()
    fields = schema.fields_by_flag(COMMAND)
    assert fields["--image-paths"]["widget"] == "special"
    assert fields["--use-kv-cache"]["default"] is True
    assert not {"--lora", "--bake-lora", "--image", "--verbose"} & fields.keys()
    assert schema.command(COMMAND)["dimension_step"] == 32
    assert schema.command(COMMAND)["max_references"] == 10


def test_reference_uploads_round_trip_in_order_and_survive_history_clear(web):
    client = client_for(web)
    ids = [
        client.post("/api/uploads", files={"file": ("ref.png", png_bytes(), "image/png")}, headers=csrf(client)).json()[
            "id"
        ]
        for _ in range(3)
    ]
    body = {"command": COMMAND, "options": {"--prompt": "Combine images"}, "references": ids[::-1]}
    queued = client.post("/api/generate", json=body, headers=csrf(client))
    assert queued.status_code == 200, queued.text
    args = web.runner.jobs()[0].invocation.parse()
    assert [path.name for path in args.image_paths] == ids[::-1]
    assert web.runner.active_uploads() == set(ids)
    client.delete("/api/jobs", headers=csrf(client))
    assert all((web.settings.upload_dir / name).exists() for name in ids)
    web.runner.cancel(queued.json()["id"])
    client.delete("/api/jobs", headers=csrf(client))
    assert not any((web.settings.upload_dir / name).exists() for name in ids)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"--width": "1000"}, "multiples of 32"),
        ({"--height": "16"}, "multiples of 32"),
        ({"--output-resolution": 1000}, "multiple of 32"),
        ({"--output-resolution": 100000}, "between"),
        ({"--steps": 1}, "at least two steps"),
        ({"--guidance": 0.9}, "at least 1"),
        ({"--scheduler": "flow_match_euler_discrete"}, "linear Euler"),
    ],
)
def test_edit_validation_runs_before_loading(web, options, message):
    with pytest.raises(InvocationError, match=message):
        invocation(web, options).validate()


@pytest.mark.parametrize("references", ["ref.png", [None], ["missing.png"] * 11])
def test_invalid_reference_lists_are_rejected(web, references):
    with pytest.raises(InvocationError):
        invocation(web, references=references)


def test_reference_ids_must_refer_to_existing_uploads(web):
    with pytest.raises(InvocationError):
        invocation(web, references=["missing.png"])


def test_text_generation_command_rejects_references(web):
    with pytest.raises(InvocationError, match="does not take reference images"):
        invocation(web, references=["ref.png"], command="mflux-generate-qwen-2.1")


def test_edit_rejects_loras(web):
    with pytest.raises(InvocationError, match="does not take LoRAs"):
        invocation(web, loras=[{"path": "example"}])


def test_edit_forwards_cli_parameters_and_bounds_prompt_cache(web):
    inv = invocation(web, {"--guidance": 2, "--use-kv-cache": False, "--output-resolution": 512})
    args = inv.parse()
    inv.adapter.validate(args)
    calls = []
    model = SimpleNamespace(
        prompt_cache={str(i): i for i in range(20)}, generate_image=lambda **kwargs: calls.append(kwargs) or "image"
    )
    assert inv.adapter.generate(model, args, 42, args.prompt) == "image"
    assert calls == [
        {
            "seed": 42,
            "prompt": "Use image 1",
            "negative_prompt": "",
            "width": None,
            "height": None,
            "num_inference_steps": 40,
            "guidance": 2,
            "image_paths": [],
            "output_resolution": 512,
            "use_kv_cache": False,
        }
    ]
    assert list(model.prompt_cache) == ["16", "17", "18", "19"]


def test_edit_uses_last_reference_for_scaled_dimensions(web):
    name = "a" * 16 + ".png"
    Image.new("RGBA", (320, 640)).save(web.settings.upload_dir / name)
    inv = invocation(web, {"--width": "2x", "--height": "auto"}, [name])
    args = inv.parse()
    inv.adapter.validate(args)
    assert inv.adapter._dimensions(args) == (640, 640)


def test_automatic_dimensions_cannot_bypass_web_limit(web):
    name = "b" * 16 + ".png"
    Image.new("RGB", (32, 4096)).save(web.settings.upload_dir / name)
    with pytest.raises(InvocationError, match="at most 8192"):
        invocation(web, references=[name]).validate()


def test_edit_loader_matches_upstream_constructor(web, monkeypatch):
    import mflux.models.qwen21.reference as reference

    seen = []
    monkeypatch.setattr(reference, "QwenImage21Edit", lambda **kwargs: seen.append(kwargs))
    args = invocation(web, {"--quantize": "4"}).parse()
    Qwen21EditAdapter().load(args)
    assert seen[0]["quantize"] == 4
    assert seen[0]["model_path"] is None
    assert set(seen[0]) == {"quantize", "model_path", "model_config"}


def test_edit_runner_uses_real_adapter_with_fake_weights(web, monkeypatch):
    import mflux.models.qwen21.reference as reference
    from mflux.web.runner import JobRunner
    from tests.web.test_runner import FakeAdapter, FakeModel, wait

    class EditModel(FakeModel):
        def __init__(self, **kwargs):
            super().__init__()
            self.prompt_cache = {}

        def generate_image(self, **kwargs):
            args = SimpleNamespace(steps=kwargs["num_inference_steps"])
            return FakeAdapter().generate(self, args, kwargs["seed"], kwargs["prompt"])

    monkeypatch.setattr(reference, "QwenImage21Edit", EditModel)
    runner = JobRunner(max_memory_gb=2)
    runner.start()
    try:
        for _ in range(2):
            job = wait(runner.submit(invocation(web, {"--steps": 2}), {}))
            assert job.status == "done", job.error
        assert "model_cached" in [event["type"] for event in job.events]
    finally:
        runner.stop()
