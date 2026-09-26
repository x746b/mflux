import argparse
import logging
import os
import sys
from pathlib import Path

from mflux.web.network import NetworkPolicy
from mflux.web.settings import DEFAULT_CONFIG_PATH, DEFAULT_HOST, DEFAULT_OUTPUT_DIR, DEFAULT_PORT, WebSettings

INSTALL_HINT = "mflux-web needs the 'web' extra: uv tool install --force 'mflux[web]'"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve a local web UI for mflux image generation.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Bind address (default: {DEFAULT_HOST}). Anything but loopback requires an API key.")  # fmt: off
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT}).")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help=f"Where generated images are written; the UI cannot write anywhere else (default: {DEFAULT_OUTPUT_DIR}).")  # fmt: off
    parser.add_argument("--models-dir", type=Path, action="append", default=[], help="Directory whose subdirectories are local model checkpoints. Repeatable.")  # fmt: off
    parser.add_argument("--lora-dir", type=Path, action="append", default=[], help="Directory searched for local .safetensors LoRAs. Repeatable.")  # fmt: off
    parser.add_argument("--cache-size", type=int, default=1, help="How many loaded models to keep in memory between runs (default: 1; 0 reloads every run).")  # fmt: off
    parser.add_argument("--api-key", default=None, help="API key for login. Prefer MFLUX_WEB_API_KEY or --api-key-file: command lines are visible to other local users.")  # fmt: off
    parser.add_argument("--api-key-file", type=Path, default=None, help="Read the API key from this file.")
    parser.add_argument("--require-auth", action="store_true", help="Require login on loopback too. Without a key, the first visit offers to create one.")  # fmt: off
    parser.add_argument("--allowed-host", action="append", default=[], help="Extra Host header name to accept (e.g. a Tailscale or reverse-proxy name). Repeatable.")  # fmt: off
    parser.add_argument("--tls-cert", type=Path, default=None, help="Serve HTTPS with this certificate (PEM).")
    parser.add_argument("--tls-key", type=Path, default=None, help="Private key for --tls-cert (PEM).")
    parser.add_argument("--behind-https", action="store_true", help="A TLS-terminating proxy sits in front: mark session cookies Secure.")  # fmt: off
    parser.add_argument("--max-upload-mb", type=int, default=50, help="Largest init image accepted (default: 50).")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("MFLUX_WEB_CONFIG", DEFAULT_CONFIG_PATH)), help=f"State file for the session secret and a first-run key (default: {DEFAULT_CONFIG_PATH}).")  # fmt: off
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        import uvicorn

        from mflux.web.app import WebApp
        from mflux.web.auth import WebAuth
    except ImportError as exc:
        parser.exit(1, f"{INSTALL_HINT}\n({exc})\n")

    if (args.tls_cert is None) != (args.tls_key is None):
        parser.error("--tls-cert and --tls-key must be given together")

    api_key = args.api_key or os.environ.get("MFLUX_WEB_API_KEY")
    if args.api_key_file is not None:
        api_key = args.api_key_file.read_text().strip()
    if api_key and (problem := WebAuth.validate_new_key(api_key)):
        parser.error(problem)

    settings = WebSettings(
        host=args.host,
        port=args.port,
        output_dir=args.output_dir.expanduser().resolve(),
        models_dirs=[d.expanduser().resolve() for d in args.models_dir],
        lora_dirs=[d.expanduser().resolve() for d in args.lora_dir],
        cache_size=args.cache_size,
        require_auth=args.require_auth,
        api_key_hash=WebAuth.hash_key(api_key) if api_key else None,
        allowed_hosts=args.allowed_host,
        tls_certfile=args.tls_cert,
        tls_keyfile=args.tls_key,
        behind_https=args.behind_https,
        max_upload_mb=args.max_upload_mb,
        config_path=args.config.expanduser(),
    )
    settings.load_persisted_api_key()
    settings.ensure_secret_key()

    if problem := NetworkPolicy.startup_error(
        settings.host, settings.auth_configured, settings.allowed_hosts, settings.behind_https
    ):
        parser.exit(2, f"mflux-web: {problem}\n")

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(WebCli.banner(settings), file=sys.stderr)
    config = uvicorn.Config(
        WebApp(settings).app,
        host=settings.host,
        port=settings.port,
        log_level=args.log_level,
        ssl_certfile=str(settings.tls_certfile) if settings.tls_certfile else None,
        ssl_keyfile=str(settings.tls_keyfile) if settings.tls_keyfile else None,
        proxy_headers=False,
    )
    # uvicorn.Config sets up its loggers on construction, so the filter goes on afterwards.
    if args.log_level != "debug":
        logging.getLogger("uvicorn.access").addFilter(QuietAccessLog())
    uvicorn.Server(config).run()


class QuietAccessLog(logging.Filter):
    # The page polls status and loads thumbnails constantly; only --log-level debug shows them.
    NOISY_PREFIXES = (
        "/api/status",
        "/api/session",
        "/api/jobs",
        "/api/images/",
        "/api/gallery",
        "/static/",
        "/favicon.svg",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3:
            return True
        method, path = str(args[1]), str(args[2])
        if method != "GET":
            return True
        return not path.startswith(QuietAccessLog.NOISY_PREFIXES)


class WebCli:
    @staticmethod
    def banner(settings: WebSettings) -> str:
        scheme = "https" if settings.tls_certfile else "http"
        shown_host = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
        lines = [f"mflux-web: {scheme}://{shown_host}:{settings.port}", f"  outputs: {settings.output_dir}"]
        lines.extend(f"  models:  {directory}" for directory in settings.models_dirs)
        lines.extend(f"  loras:   {directory}" for directory in settings.lora_dirs)
        if settings.auth_configured or settings.require_auth:
            lines.append("  auth:    login required")
        else:
            lines.append("  auth:    none (loopback only)")
        if not NetworkPolicy.is_loopback_host(settings.host) and not (settings.tls_certfile or settings.behind_https):
            lines.append(
                "  WARNING: serving plain HTTP beyond loopback; the API key and images cross the network unencrypted. "
                "Use --tls-cert/--tls-key, or a TLS proxy such as `tailscale serve` with --behind-https."
            )
        return "\n".join(lines)


if __name__ == "__main__":
    main()
