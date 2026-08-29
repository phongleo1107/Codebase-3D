"""Egress guard — the only place an outbound host is approved.

Two independent checks, deliberately kept as two functions because they answer
two different questions and the caller needs both:

``validate_download_url``   Is this *name* one we are willing to talk to?
``assert_public_ip``        Does that name resolve to a *destination* on the
                            public internet?

Neither performs a request. The HTTP client (``app/fetch/``, not yet written)
must call both before issuing a redirect-following fetch, with
``follow_redirects=False`` so that every hop passes through here, and without an
``Authorization`` header on the download itself (docs/SECURITY.md, "Credential
leak to a redirect target").

Host approval is string equality against a one-element allowlist. ``endswith``
is never used: ``codeload.github.com.evil.com`` ends with the allowlisted name
and is a host the attacker controls.

Failures raise :class:`~app.errors.UpstreamUnavailableError` (502). A refused
redirect is a statement about GitHub's response, not about the URL the user
submitted — that one already passed
:func:`app.security.url_validation.parse_github_url` — and the static message
tells an attacker nothing about which check tripped.
"""

import ipaddress
import re
import socket
from typing import NoReturn
from urllib.parse import urlsplit

from app.errors import UpstreamUnavailableError

# The set of hosts this service will fetch from. ADR-007's source preview adds
# "raw.githubusercontent.com" as one more line here and changes nothing else.
ALLOWED_DOWNLOAD_HOSTS: frozenset[str] = frozenset(
    {
        "codeload.github.com",
    }
)

# 443 is the only port ever expected; None means the URL omitted it.
_ALLOWED_PORT = 443

# See the same rule in url_validation.py for the reasoning. It is duplicated
# rather than shared because these guard different inputs — a user-submitted URL
# and an upstream Location header — and must be free to diverge without one
# boundary silently loosening the other.
_FORBIDDEN_CHARS = re.compile(r"[\x00-\x20\x7f\\]")


def _reject() -> NoReturn:
    """One exception, one static message, no echo of the offending value."""
    raise UpstreamUnavailableError() from None


def validate_download_url(location: str) -> str:
    """Approve an absolute https URL on an allowlisted host, or raise.

    Returns the input unchanged so the caller fetches exactly the string that
    was validated, with no room for a re-serialization to reintroduce something
    this function rejected.
    """
    # A Location header is upstream-controlled data. Screening it before
    # urlsplit closes the same tab/CR/LF deletion differential described in
    # url_validation, which here would also be a response-splitting artifact.
    if not location or not location.isascii() or _FORBIDDEN_CHARS.search(location):
        _reject()

    # urlsplit is not total: a malformed bracketed host ("https://[internal.corp]/x",
    # "https://[::1/x") raises ValueError, and the message quotes the host back.
    # An escape here would break the typed-error contract and echo an
    # upstream-controlled string into a traceback.
    try:
        parts = urlsplit(location)
    except ValueError:
        _reject()

    # Exactly https. This single check kills "http://", "file://",
    # "javascript:", the protocol-relative "//evil.com/x", and every relative
    # Location ("/owner/repo/tar.gz/sha"), all of which parse with no scheme.
    if parts.scheme != "https":
        _reject()

    if "@" in parts.netloc:
        _reject()

    try:
        port = parts.port
    except ValueError:
        _reject()
    if port is not None and port != _ALLOWED_PORT:
        _reject()

    host = parts.hostname
    if host is None:
        _reject()
    # urlsplit already lowercases; restated so the control is legible on its own
    # rather than resting on a stdlib detail. removesuffix drops exactly one
    # trailing root dot, so the malformed "codeload.github.com.." still fails.
    host = host.lower().removesuffix(".")
    if host not in ALLOWED_DOWNLOAD_HOSTS:
        _reject()

    # No query/fragment check: codeload redirect targets legitimately carry a
    # signed query string, and neither component can change the destination.
    return location


def assert_public_ip(host: str) -> None:
    """Require that every address ``host`` resolves to is on the public internet.

    An allowlisted name is not enough on its own: DNS is attacker-influenceable
    and a hostile or poisoned resolver can point a legitimate name at loopback
    or at a cloud metadata endpoint. *Every* returned address must pass, not
    just the first, so a resolver cannot hide 127.0.0.1 behind a public A
    record.

    This narrows the window rather than closing it — the connection is made by
    name afterwards, so a resolver that answers differently the second time is
    not caught here. Closing that fully needs connect-by-IP with SNI, which is
    out of scope for v1 and recorded as a residual risk.
    """
    try:
        infos = socket.getaddrinfo(host, _ALLOWED_PORT, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        # gaierror is an OSError; an over-long or IDNA-hostile name raises
        # UnicodeError instead. Both mean "no verified destination".
        _reject()
    if not infos:
        _reject()

    for info in infos:
        address = info[4][0]
        if not isinstance(address, str):
            # A sockaddr shape we did not ask for. Fail closed rather than
            # guess at what the kernel would connect to.
            _reject()
        _assert_global_address(address)


def _assert_global_address(address: str) -> None:
    """Reject one resolved address unless it is a routable public destination."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        _reject()

    # ::ffff:127.0.0.1 is loopback wearing an IPv6 costume; judge the address
    # the kernel will actually use. On CPython 3.14.7 this is belt-and-braces —
    # IPv6Address.is_global/is_private/is_reserved already delegate to
    # .ipv4_mapped when it is set — but doing it here means the guard states its
    # own rule instead of depending on that delegation continuing to exist.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    # is_global alone is not sufficient. Verified on CPython 3.14.7:
    #   ip_address("::127.0.0.1").is_global     -> True   (IPv4-compatible, RFC 4291)
    #   ip_address("64:ff9b::7f00:1").is_global -> True   (NAT64, RFC 6052)
    #   ip_address("224.0.0.1").is_global       -> True   (multicast)
    # The first two embed an IPv4 destination that is_global never inspects. Both
    # sit under ::/8, which is in _IPv6Constants._reserved_networks, so
    # is_reserved catches them; it also catches IPv4 240/4. Note that ::ffff:0:0/96
    # is *also* numerically under ::/8 — mapped addresses escape only because of
    # the delegation described above, which is why the unmapping must stay above
    # this check and not below it. Multicast is not a TCP destination at all.
    if not ip.is_global or ip.is_reserved or ip.is_multicast:
        _reject()

    # ...and one more, found by sweeping all 65 536 IPv6 /16 prefixes against the
    # three predicates above: fec0::/10, the site-local range deprecated by
    # RFC 3879. CPython's _reserved_networks stops at fe00::/9 and its
    # _private_networks resumes at fe80::/10, so the block between them is
    # is_global=True, is_reserved=False, is_multicast=False. Its neighbours
    # fe80::/10 and fc00::/7 are both correctly rejected; this range alone is not.
    # The sweep found no other gap.
    #
    # The isinstance test is load-bearing, not decorative: IPv4Address has no
    # is_site_local attribute, so an unguarded access would raise AttributeError
    # on every IPv4 address and escape this module untyped.
    if isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local:
        _reject()
