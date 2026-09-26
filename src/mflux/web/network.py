import ipaddress


class NetworkPolicy:
    LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1", "[::1]")

    @staticmethod
    def is_loopback_host(value: str | None) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        candidate = value.strip().rstrip(".").lower()
        if candidate.startswith("[") and candidate.endswith("]"):
            candidate = candidate[1:-1]
        if candidate == "localhost":
            return True
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            return False
        if address.is_loopback:
            return True
        return isinstance(address, ipaddress.IPv6Address) and bool(
            address.ipv4_mapped and address.ipv4_mapped.is_loopback
        )

    @staticmethod
    def startup_error(host: str, auth_configured: bool, allowed_hosts: list[str], behind_proxy: bool) -> str | None:
        if auth_configured:
            return None
        if not NetworkPolicy.is_loopback_host(host):
            return (
                f"Refusing to bind to non-loopback host {host!r} without an API key. "
                "Pass --api-key (or set MFLUX_WEB_API_KEY), or bind to 127.0.0.1."
            )
        # A loopback bind behind a reverse proxy (tailscale serve, Caddy) is reachable from
        # wherever the proxy is, so it needs a key just like a public bind.
        exposed_names = [h for h in allowed_hosts if not NetworkPolicy.is_loopback_host(h)]
        if exposed_names or behind_proxy:
            return (
                "Refusing to serve through a proxy (--allowed-host / --behind-https) without an API key. "
                "Pass --api-key (or set MFLUX_WEB_API_KEY)."
            )
        return None

    @staticmethod
    def host_header_allowed(host_header: str | None, bind_host: str, allowed_hosts: list[str]) -> bool:
        # A loopback server must only answer to loopback names: otherwise a web page whose
        # domain re-resolves to 127.0.0.1 (DNS rebinding) becomes same-origin with this UI.
        if not host_header:
            return False
        hostname = NetworkPolicy._strip_port(host_header).lower()
        if hostname in (h.lower() for h in allowed_hosts):
            return True
        if NetworkPolicy.is_loopback_host(bind_host):
            return NetworkPolicy.is_loopback_host(hostname)
        # Non-loopback binds always require authentication, so the host name is only
        # restricted when the operator listed explicit names.
        return not allowed_hosts

    @staticmethod
    def _strip_port(host_header: str) -> str:
        value = host_header.strip()
        if value.startswith("["):
            end = value.find("]")
            return value[: end + 1] if end != -1 else value
        if value.count(":") == 1:
            return value.split(":", 1)[0]
        return value
