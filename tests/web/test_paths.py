import os

import pytest

from mflux.web.paths import PathGuard, PathRejected

pytestmark = pytest.mark.fast


@pytest.fixture
def guard(tmp_path):
    for name in ("out", "out/.uploads", "models/qwen", "loras/sub", "outside"):
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / "loras/sub/style.safetensors").write_bytes(b"x")
    (tmp_path / "outside/secret.safetensors").write_bytes(b"x")
    (tmp_path / "out/.uploads/abc.png").write_bytes(b"x")
    return PathGuard(tmp_path / "out", tmp_path / "out/.uploads", [tmp_path / "models"], [tmp_path / "loras"])


@pytest.mark.parametrize("name", ["../x.png", "..", "/etc/passwd", "a/b.png", ".hidden", ""])
def test_output_file_rejects_escapes(guard, name):
    with pytest.raises(PathRejected):
        guard.output_file(name)


def test_output_file_accepts_plain_names(guard, tmp_path):
    assert guard.output_file("20260926_abc.png") == (tmp_path / "out/20260926_abc.png").resolve()


def test_local_model_must_live_under_models_dir(guard, tmp_path):
    assert guard.local_model(str(tmp_path / "models/qwen")) == (tmp_path / "models/qwen").resolve()
    for bad in (str(tmp_path / "outside"), str(tmp_path / "models/../outside"), "/etc"):
        with pytest.raises(PathRejected):
            guard.local_model(bad)


def test_symlink_out_of_models_dir_is_rejected(guard, tmp_path):
    os.symlink(tmp_path / "outside", tmp_path / "models/link")
    with pytest.raises(PathRejected):
        guard.local_model(str(tmp_path / "models/link"))


def test_no_models_dir_means_no_local_models(tmp_path):
    guard = PathGuard(tmp_path, tmp_path, [], [])
    with pytest.raises(PathRejected):
        guard.local_model(str(tmp_path))


def test_lora_rules(guard, tmp_path):
    assert guard.lora(str(tmp_path / "loras/sub/style.safetensors")).endswith("style.safetensors")
    assert guard.lora("org/some-lora") == "org/some-lora"
    assert guard.lora("org/repo:file.safetensors") == "org/repo:file.safetensors"
    assert guard.lora("my-library-lora") == "my-library-lora"
    for bad in (str(tmp_path / "outside/secret.safetensors"), "../outside/secret.safetensors", "~/.ssh/id_rsa"):
        with pytest.raises(PathRejected):
            guard.lora(bad)


def test_repo_id_that_shadows_a_relative_path_is_rejected(guard, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "org").mkdir()
    (tmp_path / "org/repo").mkdir()
    with pytest.raises(PathRejected):
        guard.hf_repo("org/repo")
    assert guard.hf_repo("other/repo") == "other/repo"


def test_upload_lookup(guard):
    assert guard.upload_file("abc.png").name == "abc.png"
    for bad in ("missing.png", "../abc.png", "/etc/passwd"):
        with pytest.raises(PathRejected):
            guard.upload_file(bad)


def test_listing(guard):
    assert [m["name"] for m in guard.list_local_models()] == ["qwen"]
    assert [lora["name"] for lora in guard.list_local_loras()] == ["sub/style.safetensors"]
