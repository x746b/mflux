import time
from argparse import Namespace

import mlx.core as mx
import pytest

from mflux.web.adapters import ModelCacheKey
from mflux.web.runner import JobRunner
from tests.web.test_runner import FakeAdapter, FakeInvocation, wait

pytestmark = pytest.mark.fast


class BufferChurningAdapter(FakeAdapter):
    # Leaves freed MLX buffers behind, like activations do during a real generation.
    def generate(self, model, args, seed, prompt):
        for _ in range(3):
            mx.eval(mx.ones((1024, 1024)) * 2)
        return super().generate(model, args, seed, prompt)


@pytest.fixture
def runner():
    runner = JobRunner(cache_size=1, idle_unload_seconds=0)
    runner.start()
    yield runner
    runner.stop()


def wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.02)
    return predicate()


def test_buffer_cache_is_released_after_every_job(runner, tmp_path):
    job = wait(runner.submit(FakeInvocation(BufferChurningAdapter(), tmp_path), {}))
    assert job.status == "done", job.error
    assert wait_until(lambda: mx.get_cache_memory() == 0)


def test_per_job_cache_limit_does_not_outlive_the_job(runner, tmp_path):
    default = mx.set_cache_limit(0)
    mx.set_cache_limit(default)
    wait(runner.submit(FakeInvocation(FakeAdapter(), tmp_path, mlx_cache_limit_gb=0.5), {}))
    time.sleep(0.05)
    assert mx.set_cache_limit(default) == default


def test_toggling_bake_without_loras_reuses_the_loaded_model(runner, tmp_path):
    adapter = FakeAdapter()
    for bake in (True, False, True):
        wait(runner.submit(FakeInvocation(adapter, tmp_path, bake_lora=bake), {}))
    assert adapter.loads == 1


def test_bake_and_scales_still_matter_with_loras():
    def key(**kwargs):
        base = dict(
            model=None,
            model_path="/m",
            base_model=None,
            quantize=None,
            lora_paths=["a"],
            lora_scales=[1.0],
            bake_lora=True,
        )
        return ModelCacheKey.of("cmd", Namespace(**{**base, **kwargs}))

    assert key() != key(bake_lora=False)
    assert key() != key(lora_scales=[0.5])
    assert key(lora_paths=[], bake_lora=True) == key(lora_paths=[], bake_lora=False, lora_scales=[0.3])


def test_idle_model_is_unloaded(tmp_path):
    runner = JobRunner(cache_size=1, idle_unload_seconds=0.2)
    runner.start()
    try:
        adapter = FakeAdapter()
        wait(runner.submit(FakeInvocation(adapter, tmp_path), {}))
        assert runner.status()["cached_models"]
        assert 0 < runner.status()["unload_in"] <= 0.2
        assert wait_until(lambda: runner.status()["cached_models"] == [])
        assert runner.status()["unload_in"] is None
        wait(runner.submit(FakeInvocation(adapter, tmp_path), {}))
        assert adapter.loads == 2
    finally:
        runner.stop()


def test_unload_request_frees_the_model(runner, tmp_path):
    assert runner.request_unload() is False
    wait(runner.submit(FakeInvocation(FakeAdapter(), tmp_path), {}))
    assert runner.request_unload() is True
    assert wait_until(lambda: runner.status()["cached_models"] == [])


def test_status_reports_memory(runner):
    assert set(runner.status()["memory"]) == {"active_gb", "cache_gb", "peak_gb"}


def test_qwen21_geometry_cache_is_bounded():
    import mlx.nn as nn

    from mflux.models.qwen21.model.qwen21_transformer.qwen21_transformer import Qwen21Transformer

    transformer = Qwen21Transformer.__new__(Qwen21Transformer)
    nn.Module.__init__(transformer)
    transformer._geometry_cache = {}
    transformer.pos_embed = lambda text_len, height, width: (mx.zeros((text_len,)), mx.zeros((text_len,)))
    for text_len in range(1, 50):
        transformer._geometry(text_len=text_len, latent_height=4, latent_width=4, encoder_hidden_states_mask=None)
    assert len(transformer._geometry_cache) == Qwen21Transformer.GEOMETRY_CACHE_SIZE
    assert (49, 4, 4) in transformer._geometry_cache
    assert (1, 4, 4) not in transformer._geometry_cache


def test_budget_and_cache_cap_are_installed_and_restored(monkeypatch, tmp_path):
    limits, caches = [], []
    monkeypatch.setattr(mx, "set_memory_limit", limits.append)
    monkeypatch.setattr(mx, "set_cache_limit", caches.append)
    runner = JobRunner(max_memory_gb=8)
    runner.start()
    try:
        for requested, expected in [(100, 2 * 1024**3), (0.5, 500_000_000)]:
            wait(runner.submit(FakeInvocation(FakeAdapter(), tmp_path, mlx_cache_limit_gb=requested), {}))
            assert caches[-2:] == [expected, 2 * 1024**3]
        assert limits == [8 * 1024**3]
        assert runner.status()["max_memory_gb"] == 8
    finally:
        runner.stop()


@pytest.mark.parametrize("phase", ["before", "step", "decode"])
def test_budget_abort_unloads_model_and_releases_logged_tracebacks(monkeypatch, tmp_path, caplog, phase):
    import weakref

    from tests.web.test_runner import FakeConfig, FakeModel

    active = [0]
    refs = []

    class HoardingAdapter(FakeAdapter):
        def load(self, args):
            model = FakeModel()
            refs.append(weakref.ref(model))
            return model

        def generate(self, model, args, seed, prompt):
            active[0] = 3 * 1024**3
            config = FakeConfig(3)
            callback = model.callbacks.before_loop_callbacks()[0]
            if phase == "before":
                callback.call_before_loop(seed, prompt, None, config)
            elif phase == "step":
                callback.call_in_loop(0, seed, prompt, None, config, None)
            else:
                callback.call_after_loop(seed, prompt, None, config)
            raise AssertionError("memory guard did not stop the job")

    monkeypatch.setattr(mx, "get_active_memory", lambda: active[0])
    runner = JobRunner(max_memory_gb=2)
    runner.start()
    try:
        failed = wait(runner.submit(FakeInvocation(HoardingAdapter(), tmp_path), {}))
        assert failed.status == "error"
        assert "exceeds the 2 GB server budget" in failed.error
        assert runner.status()["cached_models"] == []
        assert refs[0]() is None
        assert all(record.exc_info is None for record in caplog.records)
        assert all(not isinstance(arg, BaseException) for record in caplog.records for arg in (record.args or ()))
        active[0] = 0
        assert wait(runner.submit(FakeInvocation(FakeAdapter(), tmp_path), {})).status == "done"
    finally:
        runner.stop()


def test_cumulative_retained_growth_evicts_before_next_queued_job(monkeypatch, tmp_path):
    active = [0]

    class LeakingAdapter(FakeAdapter):
        def load(self, args):
            active[0] = 4 * 1024**3
            return super().load(args)

        def generate(self, model, args, seed, prompt):
            active[0] += 1024**3
            return super().generate(model, args, seed, prompt)

    monkeypatch.setattr(mx, "get_active_memory", lambda: active[0])
    runner = JobRunner(max_memory_gb=20)
    adapter = LeakingAdapter()
    runner.start()
    try:
        jobs = [runner.submit(FakeInvocation(adapter, tmp_path), {}) for _ in range(5)]
        for job in jobs:
            assert wait(job).status == "done"
        assert adapter.loads == 2
        assert any("retained MLX memory" in message for message in jobs[3].messages)
    finally:
        runner.stop()


def test_memory_cli_help_and_invalid_budgets():
    from mflux.web.cli import build_parser
    from mflux.web.memory import MemoryGuard

    assert "75%" in build_parser().format_help()
    for value in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="finite number greater than zero"):
            MemoryGuard(value)


@pytest.mark.parametrize("low_ram", [True, False])
def test_cli_callbacks_cannot_raise_cache_limit(monkeypatch, tmp_path, low_ram):
    from mflux.callbacks.callback_manager import CallbackManager

    seen = []
    monkeypatch.setattr(CallbackManager, "register_callbacks", lambda **kwargs: seen.append(kwargs["args"]))
    runner = JobRunner(cache_size=0, max_memory_gb=2)
    runner.start()
    try:
        job = wait(runner.submit(FakeInvocation(FakeAdapter(), tmp_path, low_ram=low_ram, mlx_cache_limit_gb=100), {}))
        assert job.status == "done", job.error
        assert seen[0].mlx_cache_limit_gb * 1000**3 == runner.memory.cache_limit
    finally:
        runner.stop()


def test_prompt_cache_is_bounded_across_text_generation_jobs(tmp_path):
    class PromptCachingAdapter(FakeAdapter):
        def load(self, args):
            model = super().load(args)
            model.prompt_cache = {}
            return model

        def generate(self, model, args, seed, prompt):
            model.prompt_cache[prompt] = seed
            return super().generate(model, args, seed, prompt)

    runner = JobRunner(max_memory_gb=2)
    runner.start()
    try:
        adapter = PromptCachingAdapter()
        for index in range(7):
            assert wait(runner.submit(FakeInvocation(adapter, tmp_path, prompt=str(index)), {})).status == "done"
        assert adapter.loads == 1
        assert list(next(iter(runner._models.values()))["model"].prompt_cache) == ["3", "4", "5", "6"]
    finally:
        runner.stop()


def test_budget_breach_cancels_remaining_queue(monkeypatch, tmp_path):
    from mflux.web.memory import MemoryBudgetExceeded

    class OverBudgetAdapter(FakeAdapter):
        def generate(self, model, args, seed, prompt):
            raise MemoryBudgetExceeded("budget exceeded")

    runner = JobRunner(max_memory_gb=2)
    failed = runner.submit(FakeInvocation(OverBudgetAdapter(), tmp_path), {})
    adapter = FakeAdapter()
    queued = runner.submit(FakeInvocation(adapter, tmp_path), {})
    runner.start()
    try:
        assert wait(failed).status == "error"
        assert wait(queued).status == "cancelled"
        assert adapter.loads == 0
        assert "memory budget" in queued.events[-1]["message"]
    finally:
        runner.stop()
