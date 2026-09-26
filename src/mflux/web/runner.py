import gc
import io
import json
import logging
import queue
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from mflux.web.adapters import AdapterError, ModelCacheKey
from mflux.web.invocation import Invocation, InvocationError

logger = logging.getLogger(__name__)


class JobCancelled(Exception): ...


@dataclass
class Job:
    invocation: Invocation
    payload: dict
    preview_every: int = 0
    id: str = field(default_factory=lambda: secrets.token_hex(8))
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    images: list[str] = field(default_factory=list)
    error: str | None = None
    messages: list[str] = field(default_factory=list)
    preview_jpeg: bytes | None = None
    events: list[dict] = field(default_factory=list)
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    TERMINAL = ("done", "error", "cancelled")

    def emit(self, kind: str, **data) -> None:
        with self._lock:
            self.events.append({"seq": len(self.events), "type": kind, "time": time.time(), **data})

    def events_since(self, seq: int) -> list[dict]:
        with self._lock:
            return self.events[seq:]

    @property
    def finished(self) -> bool:
        return self.status in Job.TERMINAL

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "command": self.invocation.command,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "images": list(self.images),
            "error": self.error,
            "messages": list(self.messages),
            "shell_command": self.invocation.shell_command(),
            "prompt": (self.payload.get("options") or {}).get("--prompt", ""),
        }


class WebProgressCallback:
    # Registered once per cached model; forwards to whichever job the runner is executing.
    def __init__(self, runner: "JobRunner", model, latent_creator):
        self.runner = runner
        self.model = model
        self.latent_creator = latent_creator

    def call_before_loop(self, seed, prompt, latents, config, **kwargs) -> None:
        job = self.runner.current_job
        if job is None:
            return
        battery_saver = self.runner.battery_saver
        if battery_saver is not None:
            battery_saver.call_before_loop()
        job.emit("seed_start", seed=seed, total=config.num_inference_steps, start=config.init_time_step)

    def call_in_loop(self, t, seed, prompt, latents, config, time_steps) -> None:
        job = self.runner.current_job
        if job is None:
            return
        if job.cancel_requested.is_set():
            from mflux.utils.exceptions import StopImageGenerationException

            raise StopImageGenerationException(f"Cancelled at step {t + 1}/{config.num_inference_steps}")
        info = time_steps.format_dict if time_steps is not None else {}
        job.emit(
            "progress",
            seed=seed,
            step=t + 1,
            total=config.num_inference_steps,
            elapsed=info.get("elapsed"),
            rate=info.get("rate"),
        )
        if job.preview_every and (t + 1) % job.preview_every == 0 and t + 1 < config.num_inference_steps:
            try:
                job.preview_jpeg = self._preview(latents, config, seed, prompt)
                job.emit("preview", seed=seed, step=t + 1)
            except Exception as exc:  # noqa: BLE001 -- a failed preview must not kill the run
                logger.warning("Preview decode failed: %s", exc)
                job.preview_every = 0

    def call_after_loop(self, seed, prompt, latents, config) -> None:
        job = self.runner.current_job
        if job is not None:
            job.emit("decoding", seed=seed)

    def _preview(self, latents, config, seed, prompt) -> bytes:
        # Same decode path as StepwiseHandler, downscaled to a JPEG for the browser.
        from mflux.utils.image_util import ImageUtil

        unpacked = self.latent_creator.unpack_latents(latents=latents, height=config.height, width=config.width)
        vae = self.model.vae
        if hasattr(vae, "decode_packed_latents") and unpacked.shape[1] > getattr(vae, "latent_channels", 32):
            decoded = vae.decode_packed_latents(unpacked)
        else:
            decoded = vae.decode(unpacked)
        image = ImageUtil.to_image(
            decoded_latents=decoded,
            config=config,
            seed=seed,
            prompt=prompt,
            quantization=self.model.bits,
            generation_time=0,
        ).image
        image.thumbnail((512, 512))
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=80)
        return buffer.getvalue()


class JobRunner:
    MAX_JOBS_KEPT = 200
    # Queue sentinel: model memory may only be touched on the worker thread that owns MLX.
    UNLOAD = "unload"
    GB = 1024**3

    def __init__(self, cache_size: int = 1, idle_unload_seconds: float = 600):
        self.cache_size = max(cache_size, 0)
        self.idle_unload_seconds = max(idle_unload_seconds, 0)
        self.current_job: Job | None = None
        self.battery_saver = None
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._queue: "queue.Queue[Job | str | None]" = queue.Queue()
        self._models: OrderedDict[tuple, object] = OrderedDict()
        self._jobs_lock = threading.Lock()
        self._thread = threading.Thread(target=self._work, name="mflux-web-worker", daemon=True)
        self._loading: str | None = None
        self._last_used = time.monotonic()
        self._default_cache_limit: int | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(None)

    def submit(self, invocation: Invocation, payload: dict, preview_every: int = 0) -> Job:
        job = Job(invocation=invocation, payload=payload, preview_every=max(int(preview_every or 0), 0))
        with self._jobs_lock:
            self._jobs[job.id] = job
            while len(self._jobs) > JobRunner.MAX_JOBS_KEPT:
                oldest_id, oldest = next(iter(self._jobs.items()))
                if not oldest.finished:
                    break
                self._jobs.pop(oldest_id)
        job.emit("queued", position=self.queue_position(job))
        self._queue.put(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        with self._jobs_lock:
            return list(self._jobs.values())

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.finished:
            return False
        job.cancel_requested.set()
        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = time.time()
            job.emit("cancelled", message="Removed from queue")
        return True

    def clear_history(self) -> int:
        # Finished jobs only: running and queued ones still have a worker or client attached.
        with self._jobs_lock:
            finished = [job_id for job_id, job in self._jobs.items() if job.finished]
            for job_id in finished:
                self._jobs.pop(job_id)
        return len(finished)

    def active_uploads(self) -> set[str]:
        uploads = set()
        for job in self.jobs():
            image = (job.payload or {}).get("image") or {}
            if not job.finished and image.get("upload"):
                uploads.add(str(image["upload"]))
        return uploads

    def request_unload(self) -> bool:
        if not self._models:
            return False
        self._queue.put(JobRunner.UNLOAD)
        return True

    def queue_position(self, job: Job) -> int:
        waiting = [j for j in self.jobs() if j.status == "queued"]
        return waiting.index(job) + 1 if job in waiting else 0

    def status(self) -> dict:
        return {
            "current_job": self.current_job.id if self.current_job else None,
            "queued": sum(1 for j in self.jobs() if j.status == "queued"),
            "loading": self._loading,
            "cached_models": [ModelCacheKey.describe(key) for key in list(self._models)],
            "cache_size": self.cache_size,
            "memory": JobRunner.memory_snapshot(),
            "unload_in": self._seconds_until_idle_unload(),
        }

    @staticmethod
    def memory_snapshot() -> dict:
        import mlx.core as mx

        return {
            "active_gb": round(mx.get_active_memory() / JobRunner.GB, 2),
            "cache_gb": round(mx.get_cache_memory() / JobRunner.GB, 2),
            "peak_gb": round(mx.get_peak_memory() / JobRunner.GB, 2),
        }

    def _work(self) -> None:
        import mlx.core as mx

        # MLX has no getter for the cache limit; read it by setting and restoring it.
        self._default_cache_limit = mx.set_cache_limit(0)
        mx.set_cache_limit(self._default_cache_limit)
        while True:
            try:
                job = self._queue.get(timeout=self._seconds_until_idle_unload())
            except queue.Empty:
                self._unload_all(f"idle for {self.idle_unload_seconds / 60:g} min")
                continue
            if job is None:
                return
            if isinstance(job, str) and job == JobRunner.UNLOAD:
                self._unload_all("unload requested")
                continue
            if job.cancel_requested.is_set():
                continue
            mx.reset_peak_memory()
            self.current_job = job
            job.status = "running"
            job.started_at = time.time()
            job.emit("started")
            try:
                self._run(job)
                job.status = "done"
                job.emit("done", images=job.images)
            except JobCancelled as exc:
                job.status = "cancelled"
                job.emit("cancelled", message=str(exc), images=job.images)
            except Exception as exc:  # noqa: BLE001 -- every failure is reported on the job
                job.status = "error"
                job.error = JobRunner._describe_error(exc)
                if not isinstance(exc, (InvocationError, AdapterError)):
                    logger.exception("Job %s failed", job.id)
                job.emit("error", message=job.error, images=job.images)
            finally:
                job.finished_at = time.time()
                self.current_job = None
                self.battery_saver = None
                self._after_job(job)

    def _after_job(self, job: Job) -> None:
        import mlx.core as mx

        # MLX keeps freed buffers for reuse, by default up to most of the machine's memory.
        # A long-lived server must hand them back after every job, or each new size or
        # prompt length leaves more behind and the process looks like it holds two models.
        if self._default_cache_limit is not None:
            mx.set_cache_limit(self._default_cache_limit)
        peak_gb = mx.get_peak_memory() / JobRunner.GB
        gc.collect()
        mx.clear_cache()
        self._last_used = time.monotonic()
        memory = JobRunner.memory_snapshot()
        loaded = ", ".join(ModelCacheKey.describe(key)["model"] or key[0] for key in self._models) or "none"
        logger.info(
            "Job %s %s in %.1fs · peak %.1f GB · now %.1f GB active · models loaded: %s",
            job.id,
            job.status,
            (job.finished_at or time.time()) - (job.started_at or job.created_at),
            peak_gb,
            memory["active_gb"],
            loaded,
        )

    def _seconds_until_idle_unload(self) -> float | None:
        if not self._models or not self.idle_unload_seconds or self.current_job is not None:
            return None
        return max(self._last_used + self.idle_unload_seconds - time.monotonic(), 0.01)

    def _unload_all(self, reason: str) -> None:
        if not self._models:
            return
        names = ", ".join(ModelCacheKey.describe(key)["model"] or key[0] for key in self._models)
        self._models.clear()
        JobRunner._release_memory()
        logger.info("Unloaded %s (%s) · now %.1f GB active", names, reason, JobRunner.memory_snapshot()["active_gb"])

    def _run(self, job: Job) -> None:
        import mlx.core as mx

        from mflux.callbacks.instances.battery_saver import BatterySaver
        from mflux.utils.exceptions import StopImageGenerationException
        from mflux.utils.prompt_util import PromptUtil

        invocation = job.invocation
        adapter = invocation.adapter
        args = invocation.parse()
        adapter.validate(args)
        job.messages.extend(args.web_messages)
        if args.mlx_cache_limit_gb is not None:
            mx.set_cache_limit(int(args.mlx_cache_limit_gb * 1000**3))
        self.battery_saver = BatterySaver(battery_percentage_stop_limit=args.battery_percentage_stop_limit)

        cacheable = not args.low_ram and self.cache_size > 0
        model = self._cached_model(job, args) if cacheable else self._uncached_model(job, args)
        self._apply_tiling(model, args)
        try:
            for index, seed in enumerate(args.seed):
                if job.cancel_requested.is_set():
                    raise JobCancelled("Cancelled")
                job.emit("seed_queued", seed=seed, index=index, count=len(args.seed))
                try:
                    image = adapter.generate(model, args, seed, PromptUtil.read_prompt(args))
                except StopImageGenerationException as exc:
                    raise JobCancelled(str(exc)) from exc
                job.images.append(self._save(job, image, args, seed))
                job.emit("image", name=job.images[-1], seed=seed)
        finally:
            if not cacheable:
                del model
                JobRunner._release_memory()

    def _cached_model(self, job: Job, args):
        key = ModelCacheKey.of(job.invocation.command, args)
        if key in self._models:
            self._models.move_to_end(key)
            job.emit("model_cached", model=ModelCacheKey.describe(key))
            return self._models[key]["model"]
        while len(self._models) >= self.cache_size:
            _, evicted = self._models.popitem(last=False)
            del evicted
            JobRunner._release_memory()
        model = self._load(job, args, key)
        self._models[key] = {"model": model, "tiling": getattr(model, "tiling_config", None)}
        return model

    def _uncached_model(self, job: Job, args):
        from mflux.callbacks.callback_manager import CallbackManager

        # Low-RAM runs use the CLI's own MemorySaver, which deletes encoders and transformer
        # as it goes, so the model cannot be reused and the cache is emptied first.
        self._models.clear()
        JobRunner._release_memory()
        key = ModelCacheKey.of(job.invocation.command, args)
        model = self._load(job, args, key)
        CallbackManager.register_callbacks(
            args=args, model=model, latent_creator=job.invocation.adapter.latent_creator()
        )
        return model

    def _load(self, job: Job, args, key: tuple):
        described = ModelCacheKey.describe(key)
        job.emit("loading", model=described)
        self._loading = described["model"] or job.invocation.command
        started = time.time()
        try:
            model = job.invocation.adapter.load(args)
        finally:
            self._loading = None
        model.callbacks.register(WebProgressCallback(self, model, job.invocation.adapter.latent_creator()))
        job.emit("loaded", model=described, seconds=round(time.time() - started, 1))
        return model

    def _apply_tiling(self, model, args) -> None:
        from mflux.models.common.vae.tiling_config import TilingConfig

        entry = next((e for e in self._models.values() if e["model"] is model), None)
        if args.vae_tiling or args.vae_tile_size is not None:
            model.tiling_config = TilingConfig(vae_decode_tile_size=args.vae_tile_size or 512)
        elif entry is not None:
            model.tiling_config = entry["tiling"]

    def _save(self, job: Job, image, args, seed: int) -> str:
        from mflux.utils.image_util import ImageUtil

        with Invocation.LOCK:
            Invocation.apply_globals(args)
            path = ImageUtil.resolve_output_path(args.output.format(seed=seed))
            image.save(path=path, export_json_metadata=True, overwrite=True)
        sidecar = {"command": job.invocation.command, "payload": job.payload, "seed": seed, "job": job.id}
        Path(path).with_suffix(".web.json").write_text(json.dumps(sidecar, indent=2))
        return Path(path).name

    @staticmethod
    def _release_memory() -> None:
        import mlx.core as mx

        gc.collect()
        mx.clear_cache()

    @staticmethod
    def _describe_error(exc: Exception) -> str:
        from mflux.utils.exceptions import MFluxUserException

        if isinstance(exc, (InvocationError, AdapterError, MFluxUserException)):
            return str(exc)
        return f"{type(exc).__name__}: {exc}"
