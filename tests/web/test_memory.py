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
