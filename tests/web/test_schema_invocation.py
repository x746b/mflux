import sys

import pytest

from mflux.web.adapters import ADAPTERS
from mflux.web.invocation import Invocation, InvocationError
from mflux.web.paths import PathGuard
from mflux.web.schema import FormSchema

pytestmark = pytest.mark.fast


@pytest.fixture(scope="module")
def schema():
    return FormSchema()


@pytest.fixture
def guard(tmp_path):
    (tmp_path / "models/Qwen_Qwen-Image-2.1").mkdir(parents=True)
    (tmp_path / "out/.uploads").mkdir(parents=True)
    return PathGuard(tmp_path / "out", tmp_path / "out/.uploads", [tmp_path / "models"], [])


def build(payload, guard, schema, tmp_path):
    return Invocation.from_payload(payload, guard, schema, tmp_path / "out" / "img")


@pytest.mark.parametrize("command", list(ADAPTERS))
def test_every_adapter_has_a_full_schema(schema, command):
    spec = schema.command(command)
    assert spec["available"], spec
    flags = {f["flag"] for f in spec["fields"]}
    assert "--prompt" in flags
    assert not flags & FormSchema.BLOCKED_FLAGS
    assert spec["models"], "the model picker needs at least one built-in model"


@pytest.mark.parametrize("command", list(ADAPTERS))
def test_builtin_models_are_accepted_by_their_command(schema, guard, tmp_path, command):
    for model in schema.command(command)["models"]:
        payload = {
            "command": command,
            "model": {"source": "builtin", "value": model["name"]},
            "options": {"--prompt": "x"},
        }
        build(payload, guard, schema, tmp_path).validate()


def test_scheduler_choices_are_registry_names_only(schema):
    from mflux.models.common.schedulers import SCHEDULER_REGISTRY

    choices = schema.fields_by_flag("mflux-generate-qwen-2.1")["--scheduler"]["choices"]
    assert {"linear", "flow_match_euler_discrete"} <= set(choices)
    assert all(choice in SCHEDULER_REGISTRY and "." not in choice for choice in choices)


def test_readme_command_round_trips_through_the_real_parser(schema, guard, tmp_path):
    model_dir = tmp_path / "models/Qwen_Qwen-Image-2.1"
    payload = {
        "command": "mflux-generate-qwen-2.1",
        "model": {"source": "local", "value": str(model_dir)},
        "options": {
            "--base-model": "qwen-image-2.1",
            "--prompt": "A ceramic teapot",
            "--width": "1024",
            "--height": "1024",
            "--steps": 40,
        },
        "seeds": ["42", "43"],
    }
    invocation = build(payload, guard, schema, tmp_path)
    args = invocation.parse()
    assert args.model_path == str(model_dir.resolve())
    assert args.base_model == "qwen-image-2.1"
    assert (args.width, args.height, args.steps, args.seed) == (1024, 1024, 40, [42, 43])
    assert args.metadata is True
    assert "{seed}" in args.output and args.output.startswith(str(tmp_path / "out"))
    assert "mflux-generate-qwen-2.1 --base-model=qwen-image-2.1" in invocation.shell_command()


def test_parse_leaves_process_state_untouched(schema, guard, tmp_path):
    from mflux.utils.generated_image import GeneratedImage
    from mflux.utils.image_util import ImageUtil

    before = (list(sys.argv), GeneratedImage.model_path, ImageUtil.embed_metadata_enabled)
    payload = {
        "command": "mflux-generate-qwen-2.1",
        "model": {"source": "local", "value": str(tmp_path / "models/Qwen_Qwen-Image-2.1")},
        "options": {"--prompt": "x"},
    }
    build(payload, guard, schema, tmp_path).parse()
    assert (list(sys.argv), GeneratedImage.model_path, ImageUtil.embed_metadata_enabled) == before


def test_steps_default_to_the_model(schema, guard, tmp_path):
    args = build({"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "x"}}, guard, schema, tmp_path).parse()
    assert args.steps == 40


def test_prompt_that_looks_like_a_flag_stays_a_prompt(schema, guard, tmp_path):
    args = build(
        {"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "--steps 3"}}, guard, schema, tmp_path
    ).parse()
    assert args.prompt == "--steps 3"
    assert args.steps == 40


def test_boolean_optional_flag_at_default_adds_nothing(schema, guard, tmp_path):
    invocation = build(
        {"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "x", "--bake-lora": True}},
        guard,
        schema,
        tmp_path,
    )
    assert not any("bake-lora" in token for token in invocation.argv)
    assert invocation.parse().bake_lora is True


def test_boolean_optional_flag_emits_negative_form(schema, guard, tmp_path):
    payload = {
        "command": "mflux-generate-qwen-2.1",
        "options": {"--prompt": "x", "--bake-lora": False, "--low-ram": True},
    }
    invocation = build(payload, guard, schema, tmp_path)
    assert "--no-bake-lora" in invocation.argv and "--low-ram" in invocation.argv
    args = invocation.parse()
    assert args.bake_lora is False and args.low_ram is True


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"command": "mflux-generate-train", "options": {"--prompt": "x"}}, "not available"),
        ({"command": "mflux-generate-qwen-2.1", "options": {}}, "prompt is required"),
        ({"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "-"}}, "stdin"),
        ({"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "x", "--output": "/etc/x.png"}}, "not allowed"),
        (
            {"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "x", "--prompt-file": "/etc/passwd"}},
            "not allowed",
        ),
        (
            {"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "x", "--config-from-metadata": "/x.json"}},
            "not allowed",
        ),
        ({"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "x", "--model": "/etc"}}, "Unsupported"),
        (
            {"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "x", "--scheduler": "os.system"}},
            "must be one of",
        ),
        (
            {
                "command": "mflux-generate-qwen-2.1",
                "options": {"--prompt": "x", "--scheduler": "mflux.models.common.schedulers.LinearScheduler"},
            },
            "must be one of",
        ),
        ({"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "x", "--quantize": "7"}}, "must be one of"),
        ({"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "x", "--steps": "40; rm"}}, "must be a number"),
        ({"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "x", "--steps": 2.5}}, "whole number"),
        ({"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "x", "--width": "1024 --low-ram"}}, "pixels"),
        ({"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "x"}, "seeds": ["1; ls"]}, "Invalid seed"),
        (
            {
                "command": "mflux-generate-qwen-2.1",
                "options": {"--prompt": "x"},
                "model": {"source": "builtin", "value": "dev"},
            },
            "not a model",
        ),
        (
            {
                "command": "mflux-generate-qwen-2.1",
                "options": {"--prompt": "x"},
                "model": {"source": "local", "value": "/etc"},
            },
            "outside",
        ),
        (
            {
                "command": "mflux-generate-qwen-2.1",
                "options": {"--prompt": "x"},
                "model": {"source": "hf", "value": "../../etc"},
            },
            "HuggingFace",
        ),
        (
            {
                "command": "mflux-generate-qwen-2.1",
                "options": {"--prompt": "x"},
                "image": {"upload": "../../etc/passwd"},
            },
            "Invalid upload",
        ),
        (
            {"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "x"}, "loras": [{"path": "/etc/passwd"}]},
            "No directory is configured",
        ),
    ],
)
def test_rejections(schema, guard, tmp_path, payload, message):
    with pytest.raises(InvocationError, match=message):
        build(payload, guard, schema, tmp_path)


def test_parser_errors_surface_without_usage_noise(schema, guard, tmp_path):
    invocation = build(
        {"command": "mflux-generate-qwen-2.1", "options": {"--prompt": "x", "--vae-tile-size": 130}},
        guard,
        schema,
        tmp_path,
    )
    with pytest.raises(InvocationError) as info:
        invocation.validate()
    assert str(info.value) == "argument --vae-tile-size: '130' must be a multiple of 16"


def test_family_restriction_is_enforced_before_loading(schema, guard, tmp_path):
    payload = {"command": "mflux-generate-ernie-image-turbo", "options": {"--prompt": "x", "--guidance": 3.0}}
    with pytest.raises(InvocationError, match="Guidance"):
        build(payload, guard, schema, tmp_path).validate()
