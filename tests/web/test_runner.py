import time
from argparse import Namespace
from pathlib import Path

import pytest

from mflux.callbacks.callback_registry import CallbackRegistry
from mflux.web.adapters import AdapterError
from mflux.web.runner import JobRunner

pytestmark = pytest.mark.fast


class FakeConfig:
    def __init__(self, steps):
        self.num_inference_steps = steps
        self.init_time_step = 0


class FakeImage:
    def save(self, path, export_json_metadata=False, overwrite=False):
        Path(path).write_bytes(b"png")
        if export_json_metadata:
            Path(path).with_suffix(".metadata.json").write_text("{}")


class FakeModel:
    def __init__(self):
        self.callbacks = CallbackRegistry()
        self.tiling_config = None


class FakeAdapter:
    def __init__(self, fail_on_load=False, step_delay=0.0):
        self.loads = 0
        self.fail_on_load = fail_on_load
        self.step_delay = step_delay
        self.started = False

    def validate(self, args):
        if args.prompt == "invalid":
            raise AdapterError("bad combination")

    def load(self, args):
        self.loads += 1
        if self.fail_on_load:
            raise RuntimeError("weights missing")
        return FakeModel()

    def latent_creator(self):
        return None

    def generate(self, model, args, seed, prompt):
        config = FakeConfig(args.steps)
        for callback in model.callbacks.before_loop_callbacks():
            callback.call_before_loop(seed=seed, prompt=prompt, latents=None, config=config)
        for t in range(args.steps):
            self.started = True
            time.sleep(self.step_delay)
            for callback in model.callbacks.in_loop_callbacks():
                callback.call_in_loop(t=t, seed=seed, prompt=prompt, latents=None, config=config, time_steps=None)
        for callback in model.callbacks.after_loop_callbacks():
            callback.call_after_loop(seed=seed, prompt=prompt, latents=None, config=config)
        return FakeImage()


class FakeInvocation:
    def __init__(
        self,
        adapter,
        output_dir,
        model="qwen-image-2.1",
        prompt="teapot",
        seeds=(1,),
        steps=3,
        low_ram=False,
        **overrides,
    ):
        self.command = "mflux-generate-qwen-2.1"
        self.adapter = adapter
        self._args = dict(
            model=model,
            model_path=None,
            base_model=None,
            quantize=None,
            lora_paths=None,
            lora_scales=None,
            bake_lora=True,
            prompt=prompt,
            prompt_file=None,
            seed=list(seeds),
            steps=steps,
            low_ram=low_ram,
            mlx_cache_limit_gb=None,
            vae_tiling=False,
            vae_tile_size=None,
            battery_percentage_stop_limit=5,
            no_metadata=False,
            output=str(output_dir / "img_{seed}.png"),
            web_messages=["a warning"],
        )
        self._args.update(overrides)

    def parse(self):
        return Namespace(**self._args)

    def shell_command(self):
        return "mflux-generate-qwen-2.1 --prompt=teapot"


@pytest.fixture
def runner():
    runner = JobRunner(cache_size=1)
    runner.start()
    yield runner
    runner.stop()


def wait(job, timeout=10):
    deadline = time.time() + timeout
    while not job.finished and time.time() < deadline:
        time.sleep(0.01)
    assert job.finished, f"job stuck in {job.status}"
    return job


def event_types(job):
    return [event["type"] for event in job.events]


def test_job_runs_saves_images_and_sidecars(runner, tmp_path):
    job = wait(runner.submit(FakeInvocation(FakeAdapter(), tmp_path, seeds=(7, 8)), {"command": "x"}))
    assert job.status == "done", job.error
    assert job.images == ["img_7.png", "img_8.png"]
    assert (tmp_path / "img_7.web.json").exists() and (tmp_path / "img_7.metadata.json").exists()
    assert job.messages == ["a warning"]
    types = event_types(job)
    assert types[:4] == ["queued", "started", "loading", "loaded"]
    assert types.count("progress") == 6 and types[-1] == "done"
    progress = [e for e in job.events if e["type"] == "progress"]
    assert [(e["step"], e["total"]) for e in progress[:3]] == [(1, 3), (2, 3), (3, 3)]


def test_second_job_reuses_cached_model(runner, tmp_path):
    adapter = FakeAdapter()
    wait(runner.submit(FakeInvocation(adapter, tmp_path), {}))
    second = wait(runner.submit(FakeInvocation(adapter, tmp_path, prompt="another"), {}))
    assert adapter.loads == 1
    assert "model_cached" in event_types(second)
    assert runner.status()["cached_models"][0]["model"] == "qwen-image-2.1"


def test_different_model_evicts_with_cache_size_one(runner, tmp_path):
    adapter = FakeAdapter()
    wait(runner.submit(FakeInvocation(adapter, tmp_path), {}))
    wait(runner.submit(FakeInvocation(adapter, tmp_path, model="other"), {}))
    wait(runner.submit(FakeInvocation(adapter, tmp_path), {}))
    assert adapter.loads == 3
    assert len(runner.status()["cached_models"]) == 1


def test_low_ram_jobs_are_never_cached(runner, tmp_path, monkeypatch):
    from mflux.callbacks.callback_manager import CallbackManager

    monkeypatch.setattr(CallbackManager, "register_callbacks", staticmethod(lambda **kwargs: None))
    adapter = FakeAdapter()
    wait(runner.submit(FakeInvocation(adapter, tmp_path, low_ram=True), {}))
    wait(runner.submit(FakeInvocation(adapter, tmp_path, low_ram=True), {}))
    assert adapter.loads == 2
    assert runner.status()["cached_models"] == []


def test_cancel_running_job_stops_between_steps(runner, tmp_path):
    adapter = FakeAdapter(step_delay=0.05)
    job = runner.submit(FakeInvocation(adapter, tmp_path, steps=200), {})
    while not adapter.started:
        time.sleep(0.01)
    assert runner.cancel(job.id)
    wait(job)
    assert job.status == "cancelled"
    assert event_types(job).count("progress") < 200


def test_cancel_queued_job_never_runs(runner, tmp_path):
    slow = FakeAdapter(step_delay=0.05)
    first = runner.submit(FakeInvocation(slow, tmp_path, steps=20), {})
    queued_adapter = FakeAdapter()
    queued = runner.submit(FakeInvocation(queued_adapter, tmp_path), {})
    assert runner.cancel(queued.id)
    runner.cancel(first.id)
    wait(first)
    time.sleep(0.1)
    assert queued.status == "cancelled"
    assert queued_adapter.loads == 0


def test_load_failure_is_reported_and_runner_keeps_going(runner, tmp_path):
    failed = wait(runner.submit(FakeInvocation(FakeAdapter(fail_on_load=True), tmp_path), {}))
    assert failed.status == "error" and "weights missing" in failed.error
    ok = wait(runner.submit(FakeInvocation(FakeAdapter(), tmp_path), {}))
    assert ok.status == "done"


def test_adapter_validation_error_is_reported_plainly(runner, tmp_path):
    job = wait(runner.submit(FakeInvocation(FakeAdapter(), tmp_path, prompt="invalid"), {}))
    assert job.status == "error"
    assert job.error == "bad combination"


def test_existing_file_is_not_overwritten(runner, tmp_path):
    (tmp_path / "img_1.png").write_bytes(b"old")
    job = wait(runner.submit(FakeInvocation(FakeAdapter(), tmp_path), {}))
    assert job.images == ["img_1_1.png"]
    assert (tmp_path / "img_1.png").read_bytes() == b"old"
