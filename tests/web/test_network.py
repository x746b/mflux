import pytest

from mflux.web.network import NetworkPolicy

pytestmark = pytest.mark.fast


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.8.9.10", "::ffff:127.0.0.1", "LOCALHOST."]
)
def test_loopback_hosts(host):
    assert NetworkPolicy.is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "mac.local", "", None, "127.0.0.1.evil.com"])
def test_non_loopback_hosts(host):
    assert not NetworkPolicy.is_loopback_host(host)


def test_loopback_without_key_starts():
    assert NetworkPolicy.startup_error("127.0.0.1", auth_configured=False, allowed_hosts=[], behind_proxy=False) is None


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::"])
def test_public_bind_without_key_refuses(host):
    assert "API key" in NetworkPolicy.startup_error(host, auth_configured=False, allowed_hosts=[], behind_proxy=False)


def test_public_bind_with_key_starts():
    assert NetworkPolicy.startup_error("0.0.0.0", auth_configured=True, allowed_hosts=[], behind_proxy=False) is None


@pytest.mark.parametrize(("allowed", "proxy"), [(["mac.tailnet.ts.net"], False), ([], True)])
def test_proxied_loopback_without_key_refuses(allowed, proxy):
    assert NetworkPolicy.startup_error("127.0.0.1", False, allowed, proxy) is not None


@pytest.mark.parametrize("header", ["127.0.0.1:8001", "localhost:8001", "[::1]:8001", "localhost"])
def test_loopback_bind_accepts_loopback_host_headers(header):
    assert NetworkPolicy.host_header_allowed(header, "127.0.0.1", [])


@pytest.mark.parametrize("header", ["evil.example:8001", "192.168.1.5:8001", None, ""])
def test_loopback_bind_rejects_rebinding_host_headers(header):
    assert not NetworkPolicy.host_header_allowed(header, "127.0.0.1", [])


def test_allowed_host_is_accepted_on_loopback_bind():
    assert NetworkPolicy.host_header_allowed("mac.tailnet.ts.net", "127.0.0.1", ["mac.tailnet.ts.net"])


def test_public_bind_restricts_only_when_names_listed():
    assert NetworkPolicy.host_header_allowed("anything:8001", "0.0.0.0", [])
    assert not NetworkPolicy.host_header_allowed("anything:8001", "0.0.0.0", ["mac.local"])
    assert NetworkPolicy.host_header_allowed("mac.local:8001", "0.0.0.0", ["mac.local"])
