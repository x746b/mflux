import math
import os
import subprocess
import sys


class MemoryBudgetExceeded(RuntimeError): ...


class MemoryGuard:
    GB = 1024**3
    PROMPT_CACHE_SIZE = 4

    def __init__(self, max_memory_gb: float | None = None):
        self.limit_gb = self.resolve_limit(max_memory_gb)
        self.limit = int(self.limit_gb * self.GB)
        self.cache_limit = self.limit // 4
        self.baseline: int | None = None

    @staticmethod
    def resolve_limit(value: float | None) -> float:
        if value is not None:
            if not math.isfinite(value) or value <= 0:
                raise ValueError("--max-memory-gb must be a finite number greater than zero")
            return value
        if sys.platform == "darwin":
            ram = int(subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.memsize"], text=True))
        else:
            ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        return ram * 0.75 / MemoryGuard.GB

    def install(self) -> None:
        import mlx.core as mx

        mx.set_memory_limit(self.limit)
        self.set_cache_limit()

    def set_cache_limit(self, requested_gb: float | None = None) -> None:
        import mlx.core as mx

        # CLI cache limits use decimal GB; the server budget and memory meter use GiB.
        limit = self.cache_limit if requested_gb is None else min(self.cache_limit, int(requested_gb * 1000**3))
        mx.set_cache_limit(limit)

    def check(self) -> None:
        import mlx.core as mx

        active = mx.get_active_memory()
        if active > self.limit:
            raise MemoryBudgetExceeded(
                f"MLX active memory {active / self.GB:.1f} GB exceeds the {self.limit_gb:g} GB server budget. "
                "The job was stopped, queued jobs cancelled, and cached models will be unloaded. "
                "Try fewer reference images, a smaller resolution, quantization, or --cache-size 0."
            )

    def retained_growth(self) -> bool:
        import mlx.core as mx

        active = mx.get_active_memory()
        if active > self.limit:
            return True
        if self.baseline is None:
            self.baseline = active
            return False
        return active - self.baseline > max(2 * self.GB, self.baseline * 0.1)

    @staticmethod
    def trim_prompt_cache(model) -> None:
        cache = getattr(model, "prompt_cache", None)
        if isinstance(cache, dict):
            while len(cache) > MemoryGuard.PROMPT_CACHE_SIZE:
                cache.pop(next(iter(cache)))
