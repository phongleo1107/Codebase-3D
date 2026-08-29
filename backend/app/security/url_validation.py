"""Repository URL grammar — the outermost trust boundary (PRD §10 "SSRF").

The only shapes accepted are::

    https://github.com/<owner>/<repo>
    https://github.com/<owner>/<repo>/tree/<ref>

tolerating a trailing ``/``, a ``.git`` suffix, and a ``www.`` prefix. Everything
else is rejected, including every host that is not literally ``github.com`` or
``www.github.com``. This is an allowlist, not a filter: there is no expression of
"looks like GitHub" anywhere below, only string equality against
:data:`_ALLOWED_HOSTS`, because ``endswith`` and substring tests are what
``github.com.evil.com`` and ``evil.com/github.com/o/r`` exist to defeat.

Parsing is *not* the first step. ``urllib.parse.urlsplit`` silently deletes tab,
CR, and LF from anywhere in the input — verified on CPython 3.14.7, where
``urlsplit("https://gith\\tub.com/o/r").hostname`` is ``'github.com'``. A caller
that logged or displayed the raw string would therefore disagree with the parser
about what host was requested. Every byte is screened before ``urlsplit`` sees
it, so no such differential exists.

Nothing here touches the network or resolves a name; egress is
:mod:`app.security.net_guard`'s job.
"""

import re
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import urlsplit

from app.config import get_settings
from app.errors import InvalidRepositoryUrlError

# Compared by equality, never by suffix. Adding a host here widens the trust
# boundary, so it takes a security review, not a convenience patch.
_ALLOWED_HOSTS: frozenset[str] = frozenset({"github.com", "www.github.com"})

# C0 controls, space, DEL, and backslash. The controls close the urlsplit
# deletion differential described in the module docstring; the backslash closes
# the browser-vs-stdlib one, where a browser reads "https://github.com\@evil.com"
# as a path under github.com and urlsplit reads the host as evil.com.
# net_guard.py screens upstream Location headers with a deliberately separate
# copy of this rule — the two boundaries are allowed to diverge.
_FORBIDDEN_CHARS = re.compile(r"[\x00-\x20\x7f\\]")

# GitHub logins: 1-39 chars, alphanumeric and hyphens, no leading/trailing
# hyphen. Repository names: 1-100 chars of [A-Za-z0-9._-]. Both deliberately
# exclude "%", so a percent-encoded traversal such as "%2e%2e" is rejected as a
# character-set violation and never decoded.
_OWNER_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_REPO_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,100}")
# One slash-separated component of a git ref.
_REF_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,255}")


@dataclass(frozen=True, slots=True)
class RepoRef:
    """A validated repository coordinate.

    ``owner`` and ``name`` keep the case they were given; GitHub is
    case-insensitive here and the metadata preflight returns the canonical
    spelling. ``ref`` is ``None`` when the URL named no branch, in which case
    the caller uses the repository's default branch.
    """

    owner: str
    name: str
    ref: str | None


def _reject() -> NoReturn:
    """Every rejection is indistinguishable.

    One exception type, one static message, no detail about the input — a
    caller cannot turn this function into an oracle, and no rejected string can
    reach a response body. ``from None`` suppresses any exception being handled
    at the call site, which would otherwise carry the offending value into the
    logged traceback.
    """
    raise InvalidRepositoryUrlError() from None


def parse_github_url(raw: str) -> RepoRef:
    """Validate a user-supplied repository URL.

    Raises :class:`~app.errors.InvalidRepositoryUrlError` on anything that is
    not exactly an accepted GitHub repository URL.
    """
    # Bound the work before doing any. The limit lives in Settings so that
    # tightening it in the environment is enforced here, not just in the
    # request model.
    if not raw or len(raw) > get_settings().MAX_URL_LENGTH:
        _reject()

    # Homograph defence. A Cyrillic lookalike of "i" or "o" inside "github.com"
    # is a different host that renders identically, and IDNA would encode it to
    # a punycode label we would then have to reason about. Refusing non-ASCII
    # outright means we never have to. The literal homographs live in the tests.
    if not raw.isascii():
        _reject()

    # Only surrounding whitespace is forgiven (pasted URLs collect it). The
    # order matters and is load-bearing: str.strip() removes every character
    # whose isspace() is true, including U+00A0 and U+3000, so stripping first
    # would silently launder "\u00a0https://github.com/o/r" into a clean ASCII
    # URL and defeat the check above. Because that check has already run, strip()
    # here can only remove ASCII whitespace.
    candidate = raw.strip()
    if _FORBIDDEN_CHARS.search(candidate):
        _reject()

    # urlsplit is not total: it raises ValueError on a malformed bracketed host
    # ("https://[evil.com]/o/r", "https://[::1/o/r"), and that message quotes the
    # offending host back. Letting it escape would break both the typed-error
    # contract and the rule that no rejection echoes its input.
    try:
        parts = urlsplit(candidate)
    except ValueError:
        _reject()

    # urlsplit lowercases the scheme, so this accepts "HTTPS://" and rejects
    # "http:", "ftp:", "file:", "javascript:", and the schemeless forms
    # "//evil.com/o/r" and "git@github.com:o/r".
    if parts.scheme != "https":
        _reject()

    # No accepted form carries a query or fragment. A bare trailing "?" or "#"
    # is the one exception: urlsplit cannot tell an empty component from an
    # absent one, and both round-trip to the same canonical URL.
    if parts.query or parts.fragment:
        _reject()

    # Userinfo makes "https://github.com@evil.com/o/r" read as GitHub to a
    # human and as evil.com to the parser.
    #
    # This and the port check below are both subsumed by the netloc-equality
    # check further down — mutation testing confirms no input reaches only one
    # of them. They are kept because a boundary this important should name the
    # threats it defends against, and because they become the sole defence the
    # moment anyone relaxes that equality (to permit an explicit :443, say).
    if "@" in parts.netloc:
        _reject()

    try:
        port = parts.port
    except ValueError:
        # A non-numeric port. The ValueError message quotes the offending
        # value, so it is dropped rather than chained.
        _reject()
    if port is not None:
        _reject()

    # hostname is already lowercased and unbracketed by urlsplit; a trailing
    # dot is NOT stripped, so "github.com." fails the equality test. That is
    # intentional at this boundary: the user typed it, and the accepted forms
    # do not include it.
    host = parts.hostname
    if host is None or host not in _ALLOWED_HOSTS:
        _reject()

    # The authority must be the host and nothing else. The checks above already
    # cover userinfo and a numeric port, but urlsplit reports an *empty* port
    # ("https://github.com:/o/r") as None, and a bracketed literal loses its
    # brackets in .hostname. Comparing the whole authority closes both without
    # depending on either detail.
    if parts.netloc.lower() != host:
        _reject()

    return _parse_path(parts.path)


def _parse_path(path: str) -> RepoRef:
    """Split an already host-validated path into owner, repo, and ref."""
    # An assertion rather than a control: urlsplit guarantees an authority-form
    # URL has a path that is either "" or starts with "/", and "" is caught by
    # the segment count below. Stated so the slice further down cannot silently
    # start reading from the wrong offset if that ever stops being true.
    if not path.startswith("/"):
        _reject()
    # Exactly one trailing slash is forgiven. A second one leaves an empty
    # final segment, which no pattern below matches.
    path = path.removesuffix("/")

    segments = path[1:].split("/")
    if len(segments) < 2:
        _reject()

    owner = segments[0]
    # A .git suffix is stripped before validation, so a repository literally
    # named ".git" reduces to the empty string and is rejected.
    name = segments[1].removesuffix(".git")
    if not _OWNER_PATTERN.fullmatch(owner) or not _REPO_PATTERN.fullmatch(name):
        _reject()
    # "." and ".." satisfy the character set but become node IDs and are echoed
    # back to /api/source, so they are excluded by name.
    if name in {".", ".."}:
        _reject()

    ref: str | None = None
    if len(segments) > 2:
        # The only accepted continuation is /tree/<ref>, where <ref> may itself
        # contain slashes ("feat/nested-name").
        if segments[2] != "tree" or len(segments) < 4:
            _reject()
        ref = _validated_ref(segments[3:])

    return RepoRef(owner=owner, name=name, ref=ref)


def _validated_ref(components: list[str]) -> str:
    """Join ref path components after enforcing a strict subset of git-check-ref-format.

    The ref is interpolated into an api.github.com path, so it is held to the
    same standard as the owner and repository: a fixed character set, no
    traversal, and no empty component.
    """
    for component in components:
        if not _REF_COMPONENT_PATTERN.fullmatch(component):
            _reject()
        if ".." in component or component.startswith(".") or component.endswith("."):
            _reject()
        if component.endswith(".lock"):
            _reject()
    return "/".join(components)
