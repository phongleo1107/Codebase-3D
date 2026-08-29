"""Egress guard: host allowlist, resolved-IP globality, and redirect handling.

No test here touches the network. `getaddrinfo` is stubbed per test on top of
the suite-wide block in conftest.py, and every HTTP interaction runs through
`httpx.MockTransport`, which never opens a socket.
"""

import socket

import httpx
import pytest

from app.errors import AppError, ErrorCode, UpstreamUnavailableError
from app.security.net_guard import (
    ALLOWED_DOWNLOAD_HOSTS,
    assert_public_ip,
    validate_download_url,
)
from tests.conftest import NetworkAccessAttempted

CODELOAD = "https://codeload.github.com/facebook/react/tar.gz/a1b2c3d"


# --------------------------------------------------------------------------
# validate_download_url
# --------------------------------------------------------------------------

ACCEPTED_LOCATIONS: list[str] = [
    CODELOAD,
    # codeload redirect targets legitimately carry a signed query string.
    "https://codeload.github.com/o/r/tar.gz/sha?token=abc123",
    # Scheme, host, and the explicit default port are case/format variations of
    # the same destination.
    "https://CODELOAD.GitHub.COM/o/r/tar.gz/sha",
    "HTTPS://codeload.github.com/o/r/tar.gz/sha",
    "https://codeload.github.com:443/o/r/tar.gz/sha",
    # A single trailing root dot is stripped before the equality test.
    "https://codeload.github.com./o/r/tar.gz/sha",
]


@pytest.mark.parametrize("location", ACCEPTED_LOCATIONS, ids=ACCEPTED_LOCATIONS)
def test_accepts_allowlisted_download_url(location: str) -> None:
    # Returned unchanged, so the caller fetches exactly what was validated.
    assert validate_download_url(location) == location


REJECTED_LOCATIONS: list[str] = [
    # --- the suffix attack the allowlist exists to defeat ---
    "https://codeload.github.com.evil.com/o/r/tar.gz/sha",
    "https://codeload.github.com.evil.com./o/r/tar.gz/sha",
    "https://evil.com/codeload.github.com/o/r/tar.gz/sha",
    "https://xcodeload.github.com/o/r/tar.gz/sha",
    "https://codeload.github.evil.com/o/r/tar.gz/sha",
    "https://evil.com/tar.gz",
    # Two trailing dots is not a hostname; only one is stripped.
    "https://codeload.github.com../o/r/tar.gz/sha",
    # --- wrong scheme ---
    "http://codeload.github.com/o/r/tar.gz/sha",
    "ftp://codeload.github.com/o/r/tar.gz/sha",
    "file:///etc/passwd",
    "javascript:alert(1)",
    # --- protocol-relative and relative Location values ---
    "//evil.com/o/r/tar.gz/sha",
    "//codeload.github.com/o/r/tar.gz/sha",
    "/o/r/tar.gz/sha",
    "o/r/tar.gz/sha",
    "",
    # --- ports other than 443 ---
    "https://codeload.github.com:22/o/r/tar.gz/sha",
    "https://codeload.github.com:80/o/r/tar.gz/sha",
    "https://codeload.github.com:8080/o/r/tar.gz/sha",
    "https://codeload.github.com:notaport/o/r/tar.gz/sha",
    # --- userinfo ---
    "https://user:pass@codeload.github.com/o/r/tar.gz/sha",
    "https://codeload.github.com@evil.com/o/r/tar.gz/sha",
    "https://codeload.github.com\\@evil.com/o/r/tar.gz/sha",
    # --- private and loopback destinations named directly ---
    "https://127.0.0.1/o/r/tar.gz/sha",
    "https://localhost/o/r/tar.gz/sha",
    "https://[::1]/o/r/tar.gz/sha",
    "https://169.254.169.254/latest/meta-data/",
    # --- malformed bracketed hosts: urlsplit raises ValueError and quotes the
    #     offending host, so these must be caught, not propagated ---
    "https://[internal-admin.corp]/o/r/tar.gz/sha",
    "https://[::1/o/r/tar.gz/sha",
    "https://[]/o/r/tar.gz/sha",
    "https://[1:2:3]/o/r/tar.gz/sha",
    # --- control characters: header splicing, and the urlsplit deletion
    #     differential that would otherwise make these parse as codeload ---
    "https://codeload.github.com/o/r\x00",
    "https://codel\toad.github.com/o/r/tar.gz/sha",
    "https://evil.com\t/o/r",
    "https://codeload.github.com/tar.gz\r\nX-Injected: 1",
    "https://codeload.github.com/tar.gz\nSet-Cookie: a=b",
    # --- homograph ---
    "https://codeload.githуb.com/o/r/tar.gz/sha",  # noqa: RUF001
]


@pytest.mark.parametrize("location", REJECTED_LOCATIONS, ids=[repr(x) for x in REJECTED_LOCATIONS])
def test_rejects_non_allowlisted_download_url(location: str) -> None:
    with pytest.raises(UpstreamUnavailableError):
        validate_download_url(location)


FUZZ_PARTS = ["", "[", "]", ":", "@", "/", "\\", ".", "%", "?", "#", "-", "a", "1", "::", "\x00"]
FUZZ_TEMPLATES = [
    "https://{}{}{}/tar.gz",
    "https://codeload.github.com{}{}{}/tar.gz",
    "https://[{}{}{}]/tar.gz",
    "{}{}{}",
]


def test_only_the_typed_error_ever_escapes() -> None:
    """A Location header is upstream-controlled, so nothing untyped may escape.

    `urlsplit` raises `ValueError` on a malformed bracketed host and quotes the
    host in the message; propagating that would leak an upstream-controlled
    string into a traceback.
    """
    for template in FUZZ_TEMPLATES:
        for first in FUZZ_PARTS:
            for second in FUZZ_PARTS:
                for third in FUZZ_PARTS:
                    candidate = template.format(first, second, third)
                    try:
                        validate_download_url(candidate)
                    except UpstreamUnavailableError:
                        pass
                    except Exception as exc:
                        pytest.fail(
                            f"{type(exc).__name__} escaped validate_download_url for "
                            f"{candidate!r} (only UpstreamUnavailableError may)"
                        )


def test_download_rejection_is_a_typed_app_error() -> None:
    with pytest.raises(AppError) as caught:
        validate_download_url("https://codeload.github.com.evil.com/x")
    error = caught.value
    assert isinstance(error, UpstreamUnavailableError)
    assert error.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert error.status_code == 502


@pytest.mark.parametrize(
    "location", [*REJECTED_LOCATIONS, "https://codeload.github.com.evil.com/secret-path"]
)
def test_download_rejection_never_echoes_the_location(location: str) -> None:
    with pytest.raises(UpstreamUnavailableError) as caught:
        validate_download_url(location)
    body = caught.value.body("fixed-request-id")
    assert body == {
        "error": {
            "code": "UPSTREAM_UNAVAILABLE",
            "message": UpstreamUnavailableError.message,
            "requestId": "fixed-request-id",
        }
    }
    assert "evil" not in repr(body)


def test_allowlist_is_exactly_one_host() -> None:
    """ADR-007's source preview adds raw.githubusercontent.com as one more line.

    Until it does, that host is not fetchable — asserted here so the widening is
    a deliberate edit to this test, not a silent one.
    """
    assert isinstance(ALLOWED_DOWNLOAD_HOSTS, frozenset)
    assert set(ALLOWED_DOWNLOAD_HOSTS) == {"codeload.github.com"}
    with pytest.raises(UpstreamUnavailableError):
        validate_download_url("https://raw.githubusercontent.com/o/r/sha/src/index.ts")


# --------------------------------------------------------------------------
# assert_public_ip
# --------------------------------------------------------------------------

# Mirrors typeshed's getaddrinfo return type exactly, including the
# `tuple[int, bytes]` sockaddr form that motivates the guard in net_guard.
AddrInfo = tuple[
    socket.AddressFamily,
    socket.SocketKind,
    int,
    str,
    tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes],
]


def _addrinfo(*addresses: str) -> list[AddrInfo]:
    """Build a getaddrinfo-shaped result for the given literal addresses."""
    infos: list[AddrInfo] = []
    for address in addresses:
        if ":" in address:
            infos.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 443, 0, 0)))
        else:
            infos.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)))
    return infos


class _StubResolver:
    """A getaddrinfo stand-in that records the names it was asked about."""

    def __init__(self, infos: list[AddrInfo]) -> None:
        self.infos = infos
        self.calls: list[str] = []

    def __call__(self, host: str, port: object, *args: object, **kwargs: object) -> list[AddrInfo]:
        self.calls.append(host)
        return self.infos


def _resolve_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> _StubResolver:
    resolver = _StubResolver(_addrinfo(*addresses))
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    return resolver


def test_the_suite_blocks_real_name_resolution() -> None:
    """conftest's autouse guard is not a no-op."""
    with pytest.raises(NetworkAccessAttempted):
        socket.getaddrinfo("codeload.github.com", 443)


def test_unstubbed_resolution_fails_loudly_rather_than_being_swallowed() -> None:
    """`assert_public_ip` catches OSError, not everything.

    The block raises a RuntimeError, so it travels straight through the guard.
    That makes every passing test above provably dependent on its stub: none of
    them can be quietly passing because resolution failed.
    """
    with pytest.raises(NetworkAccessAttempted):
        assert_public_ip("codeload.github.com")


PRIVATE_ADDRESSES: list[str] = [
    "127.0.0.1",
    "127.0.0.53",
    "10.0.0.1",
    "10.255.255.255",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",
    "0.0.0.0",  # noqa: S104
    "100.64.0.1",
    "::1",
    "::",
    "fe80::1",
    "fc00::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:169.254.169.254",
    "::ffff:192.168.1.1",
    "::ffff:0.0.0.0",
    "::ffff:224.0.0.1",
    "::ffff:240.0.0.1",
    # is_global returns True for each of the next three; see the comment in
    # net_guard._assert_global_address. (240.0.0.1 below is is_global False —
    # it is here for the reserved-IPv4 case, not as an is_global counterexample.)
    "::127.0.0.1",
    "64:ff9b::7f00:1",
    "224.0.0.1",
    "240.0.0.1",
    # fec0::/10, deprecated IPv6 site-local (RFC 3879). is_global is True and
    # neither is_reserved nor is_multicast covers it — the one gap an exhaustive
    # sweep of all 65 536 IPv6 /16 prefixes found in the three predicates above.
    "fec0::1",
    "fec0:0:0:ffff::1",
    "fed0::1",
    "feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
]


@pytest.mark.parametrize("address", PRIVATE_ADDRESSES, ids=PRIVATE_ADDRESSES)
def test_rejects_non_public_resolved_address(address: str, monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = _resolve_to(monkeypatch, address)
    with pytest.raises(UpstreamUnavailableError):
        assert_public_ip("codeload.github.com")
    # Proves the stub was used, so this test cannot pass by never resolving.
    assert resolver.calls == ["codeload.github.com"]


PUBLIC_ADDRESSES: list[str] = [
    "140.82.121.4",
    "8.8.8.8",
    "2606:4700::1111",
    "2a00:1450:4001::1",
    "::ffff:140.82.121.4",
]


@pytest.mark.parametrize("address", PUBLIC_ADDRESSES, ids=PUBLIC_ADDRESSES)
def test_accepts_public_resolved_address(address: str, monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = _resolve_to(monkeypatch, address)
    assert_public_ip("codeload.github.com")
    assert resolver.calls == ["codeload.github.com"]


def test_every_resolved_address_must_pass_not_just_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver cannot hide loopback behind a leading public A record."""
    _resolve_to(monkeypatch, "140.82.121.4", "127.0.0.1")
    with pytest.raises(UpstreamUnavailableError):
        assert_public_ip("codeload.github.com")

    _resolve_to(monkeypatch, "140.82.121.4", "2606:4700::1111")
    assert_public_ip("codeload.github.com")


def test_empty_resolution_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _StubResolver([]))
    with pytest.raises(UpstreamUnavailableError):
        assert_public_ip("codeload.github.com")


def test_resolution_failure_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*args: object, **kwargs: object) -> list[AddrInfo]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", failing)
    with pytest.raises(UpstreamUnavailableError):
        assert_public_ip("codeload.github.com")


def test_unparseable_resolved_address_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed if a resolver returns something that is not an IP at all."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _StubResolver([(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 443))]),
    )
    with pytest.raises(UpstreamUnavailableError):
        assert_public_ip("codeload.github.com")


def test_non_string_sockaddr_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sockaddr shape we never asked for must fail closed, not be inspected.

    getaddrinfo's result type also covers address families whose sockaddr is
    `(int, bytes)` rather than `(str, int)`. That integer matters: without the
    type guard it reaches `ipaddress.ip_address`, which happily accepts an int
    and reads it as a packed IPv4 address. 0x8C527904 would come back as the
    perfectly global 140.82.121.4 and the check would pass on a value that was
    never an address at all.
    """
    protocol_number = 0x8C527904
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _StubResolver([(socket.AF_INET, socket.SOCK_STREAM, 6, "", (protocol_number, b"\x00"))]),
    )
    with pytest.raises(UpstreamUnavailableError):
        assert_public_ip("codeload.github.com")


def test_ip_rejection_never_echoes_the_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, "169.254.169.254")
    with pytest.raises(UpstreamUnavailableError) as caught:
        assert_public_ip("codeload.github.com")
    body = caught.value.body("fixed-request-id")
    assert "169.254" not in repr(body)
    assert body["error"]["message"] == UpstreamUnavailableError.message


# --------------------------------------------------------------------------
# Redirect handling
#
# The real policy will live in app/fetch/ (not written yet). The helper below
# is a stand-in for it, present so the guard can be exercised against actual
# httpx redirect responses. What is under test is validate_download_url; the
# helper only supplies the shape of the call.
# --------------------------------------------------------------------------

TARBALL_URL = "https://api.github.com/repos/facebook/react/tarball/main"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _download_with_one_redirect(client: httpx.Client, url: str) -> httpx.Response:
    response = client.get(url)
    if response.status_code not in _REDIRECT_STATUSES:
        return response

    location = response.headers.get("location")
    if location is None:
        raise UpstreamUnavailableError()

    followed = client.get(validate_download_url(location))
    if followed.status_code in _REDIRECT_STATUSES:
        # Exactly one redirect is permitted.
        raise UpstreamUnavailableError()
    return followed


def _client(routes: dict[str, tuple[int, dict[str, str]]], seen: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        requested = str(request.url)
        seen.append(requested)
        if requested not in routes:
            pytest.fail(f"unexpected request to {requested}")
        status, headers = routes[requested]
        return httpx.Response(status_code=status, headers=headers, content=b"tarball-bytes")

    # follow_redirects=False is httpx's default; stated explicitly because the
    # whole design depends on it. trust_env=False keeps proxy environment
    # variables out of the picture.
    return httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False
    )


def test_single_redirect_to_codeload_is_followed() -> None:
    seen: list[str] = []
    routes = {
        TARBALL_URL: (302, {"location": CODELOAD}),
        CODELOAD: (200, {}),
    }
    with _client(routes, seen) as client:
        response = _download_with_one_redirect(client, TARBALL_URL)
    assert response.status_code == 200
    assert response.content == b"tarball-bytes"
    assert seen == [TARBALL_URL, CODELOAD]


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.com/o/r/tar.gz/sha",
        "https://codeload.github.com.evil.com/o/r/tar.gz/sha",
        "http://codeload.github.com/o/r/tar.gz/sha",
        "//evil.com/o/r/tar.gz/sha",
        "/o/r/tar.gz/sha",
        "https://169.254.169.254/latest/meta-data/",
    ],
    ids=[
        "attacker-host",
        "suffix-of-allowlisted-host",
        "downgraded-to-http",
        "protocol-relative",
        "relative",
        "metadata-endpoint",
    ],
)
def test_redirect_to_disallowed_target_is_refused(location: str) -> None:
    """And refused *before* the second request is issued."""
    seen: list[str] = []
    routes = {TARBALL_URL: (302, {"location": location})}
    with _client(routes, seen) as client, pytest.raises(UpstreamUnavailableError):
        _download_with_one_redirect(client, TARBALL_URL)
    assert seen == [TARBALL_URL]


def test_second_redirect_is_refused() -> None:
    """One hop is permitted; a redirect chain is not."""
    second = "https://codeload.github.com/o/r/tar.gz/second"
    seen: list[str] = []
    routes = {
        TARBALL_URL: (302, {"location": CODELOAD}),
        CODELOAD: (302, {"location": second}),
    }
    with _client(routes, seen) as client, pytest.raises(UpstreamUnavailableError):
        _download_with_one_redirect(client, TARBALL_URL)
    assert seen == [TARBALL_URL, CODELOAD]


def test_redirect_without_location_is_refused() -> None:
    seen: list[str] = []
    routes: dict[str, tuple[int, dict[str, str]]] = {TARBALL_URL: (302, {})}
    with _client(routes, seen) as client, pytest.raises(UpstreamUnavailableError):
        _download_with_one_redirect(client, TARBALL_URL)
    assert seen == [TARBALL_URL]


def test_httpx_does_not_follow_redirects_on_its_own() -> None:
    """If this ever fails, the guard is being bypassed by the client itself."""
    seen: list[str] = []
    routes = {TARBALL_URL: (302, {"location": "https://evil.com/x"})}
    with _client(routes, seen) as client:
        response = client.get(TARBALL_URL)
    assert response.status_code == 302
    assert seen == [TARBALL_URL]
