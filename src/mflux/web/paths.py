import re
from pathlib import Path


class PathRejected(ValueError): ...


class PathGuard:
    HF_REPO_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
    HF_COLLECTION_PATTERN = re.compile(
        r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*:[A-Za-z0-9_.-]+\.safetensors$"
    )
    FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$")

    def __init__(self, output_dir: Path, upload_dir: Path, models_dirs: list[Path], lora_dirs: list[Path]):
        self.output_dir = output_dir.expanduser().resolve()
        self.upload_dir = upload_dir.expanduser().resolve()
        self.models_dirs = [d.expanduser().resolve() for d in models_dirs]
        self.lora_dirs = [d.expanduser().resolve() for d in lora_dirs]

    def output_file(self, name: str) -> Path:
        if not PathGuard.FILENAME_PATTERN.match(name) or ".." in name:
            raise PathRejected(f"Invalid image name: {name!r}")
        return self._confined(self.output_dir / name, [self.output_dir])

    def upload_file(self, name: str) -> Path:
        if not PathGuard.FILENAME_PATTERN.match(name):
            raise PathRejected(f"Invalid upload id: {name!r}")
        path = self._confined(self.upload_dir / name, [self.upload_dir])
        if not path.is_file():
            raise PathRejected(f"Upload not found: {name!r}")
        return path

    def local_model(self, value: str) -> Path:
        path = self._confined(Path(value).expanduser(), self.models_dirs)
        if not path.is_dir():
            raise PathRejected(f"Model directory not found: {value}")
        return path

    def lora(self, value: str) -> str:
        if PathGuard.HF_REPO_PATTERN.match(value) or PathGuard.HF_COLLECTION_PATTERN.match(value):
            PathGuard.reject_if_local_shadow(value)
            return value
        if Path(value).expanduser().is_absolute():
            path = self._confined(Path(value).expanduser(), self.lora_dirs)
            if not path.is_file():
                raise PathRejected(f"LoRA file not found: {value}")
            return str(path)
        if PathGuard.FILENAME_PATTERN.match(value):
            # A bare name is resolved by the LoRA library (mflux-lora-library), which only
            # looks inside its own configured roots.
            PathGuard.reject_if_local_shadow(value)
            return value
        raise PathRejected(f"LoRA must be a HuggingFace repo, a library name, or a file under --lora-dir: {value}")

    def hf_repo(self, value: str) -> str:
        if not PathGuard.HF_REPO_PATTERN.match(value):
            raise PathRejected(f"Not a HuggingFace repo id (org/name): {value}")
        PathGuard.reject_if_local_shadow(value)
        return value

    def list_local_models(self) -> list[dict]:
        found = []
        for root in self.models_dirs:
            if not root.is_dir():
                continue
            found.extend(
                {"name": child.name, "path": str(child)}
                for child in sorted(root.iterdir())
                if child.is_dir() and not child.name.startswith(".")
            )
        return found

    def list_local_loras(self) -> list[dict]:
        found = []
        for root in self.lora_dirs:
            if not root.is_dir():
                continue
            for child in sorted(root.rglob("*.safetensors")):
                resolved = child.resolve()
                if self._is_within(resolved, [root]):
                    found.append({"name": str(child.relative_to(root)), "path": str(resolved)})
        return found

    @staticmethod
    def reject_if_local_shadow(value: str) -> None:
        # mflux loads a local directory before trying the Hub, so an "org/name" string that
        # also exists relative to the working directory would escape the configured roots.
        if Path(value.split(":", 1)[0]).exists():
            raise PathRejected(f"{value!r} matches a local path; use an absolute path under an allowed directory")

    def _confined(self, path: Path, roots: list[Path]) -> Path:
        if not roots:
            raise PathRejected("No directory is configured for this kind of path")
        resolved = path.resolve()
        if not self._is_within(resolved, roots):
            raise PathRejected(f"Path is outside the allowed directories: {path}")
        return resolved

    @staticmethod
    def _is_within(path: Path, roots: list[Path]) -> bool:
        return any(path == root or root in path.parents for root in roots)
