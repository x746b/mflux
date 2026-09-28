import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001
DEFAULT_OUTPUT_DIR = Path.home() / "AI" / "mflux-web" / "outputs"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "mflux" / "web.json"


@dataclass
class WebSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    output_dir: Path = DEFAULT_OUTPUT_DIR
    models_dirs: list[Path] = field(default_factory=list)
    lora_dirs: list[Path] = field(default_factory=list)
    cache_size: int = 1
    idle_unload_minutes: float = 10
    max_memory_gb: float | None = None
    require_auth: bool = False
    api_key_hash: str | None = None
    secret_key: str = ""
    allowed_hosts: list[str] = field(default_factory=list)
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None
    behind_https: bool = False
    max_upload_mb: int = 50
    config_path: Path = DEFAULT_CONFIG_PATH

    @property
    def secure_cookies(self) -> bool:
        return self.behind_https or self.tls_certfile is not None

    @property
    def auth_configured(self) -> bool:
        return self.api_key_hash is not None

    @property
    def upload_dir(self) -> Path:
        return self.output_dir / ".uploads"

    @staticmethod
    def load_persisted(config_path: Path) -> dict:
        if not config_path.exists():
            return {}
        return json.loads(config_path.read_text())

    def persist(self) -> None:
        # Only state the server creates itself lives here: the cookie-signing secret and a
        # key set through the first-run page. Paths and bind options stay on the command line.
        data = WebSettings.load_persisted(self.config_path)
        data["secret_key"] = self.secret_key
        if self.api_key_hash is not None:
            data["api_key_hash"] = self.api_key_hash
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.config_path.with_suffix(".tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp_path, self.config_path)
        os.chmod(self.config_path, 0o600)

    def ensure_secret_key(self) -> None:
        persisted = WebSettings.load_persisted(self.config_path)
        env_secret = os.environ.get("MFLUX_WEB_SECRET_KEY")
        if env_secret:
            self.secret_key = env_secret
            return
        if persisted.get("secret_key"):
            self.secret_key = persisted["secret_key"]
            return
        self.secret_key = secrets.token_hex(32)
        self.persist()

    def load_persisted_api_key(self) -> None:
        if self.api_key_hash is None:
            self.api_key_hash = WebSettings.load_persisted(self.config_path).get("api_key_hash")

    @staticmethod
    def huggingface_status() -> dict:
        from huggingface_hub.constants import HF_TOKEN_PATH

        for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
            if os.environ.get(name, "").strip():
                return {"status": "detected", "source": "environment"}
        try:
            detected = bool(Path(HF_TOKEN_PATH).read_text().strip())
        except FileNotFoundError:
            detected = False
        except (OSError, UnicodeError):
            return {"status": "unreadable", "source": None}
        return {"status": "detected" if detected else "missing", "source": "login" if detected else None}
