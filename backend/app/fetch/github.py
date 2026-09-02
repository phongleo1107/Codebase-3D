"""GitHub API client — the first code in this project that can open a socket.

Two steps of the ingestion sequence in docs/ARCHITECTURE.md live here:

``get_repo_metadata``  Preflight ``GET /repos/{owner}/{repo}``: canonical
                       spelling, default branch, and the reported size, so an
                       oversized repository is refused *before* any archive
                       byte moves.
``get_download_url``   ``GET /repos/{owner}/{repo}/tarball/{ref}`` with
                       redirects disabled, the single ``Location`` pushed
                       through :mod:`app.security.net_guard`, and the approved
                       URL handed back for the streaming download (Task 5).

Nothing here downloads an archive. The download itself is built by
:func:`download_request` and sent by ``app/fetch/archive.py``, which does not
exist yet.

Three properties of the client are security controls, not configuration
(docs/SECURITY.md, "Network / SSRF"):

``follow_redirects=False``  Every hop is inspected. httpx would otherwise
                            chase a ``Location`` to an internal address before
                            any code of ours saw it.
``trust_env=False``         ``HTTP_PROXY``/``ALL_PROXY``/``NO_PROXY`` and
                            ``SSL_CERT_FILE`` in the process environment cannot
                            redirect or downgrade our egress.
No default ``Authorization``  The token is attached per request, and only to
                            ``api.github.com``. The codeload download is
                            structurally incapable of carrying it: there is no
                            client-level header for a later request to inherit.

That last point is deliberately architectural rather than procedural. The
obvious implementation — set the header on the client, delete it before the
download — leaves a window in which the credential is one forgotten ``del``
away from an attacker-influenced host. Here the header does not exist unless a
call site names it, and the only call site that names it is the request to
``api.github.com``.

Failures collapse to two errors and never carry upstream detail. ``404`` and
``403`` both become :class:`~app.errors.RepositoryNotFoundError`, so a
configured token cannot turn this into a private-repository existence oracle.
Everything else — a wrong status, a missing or refused ``Location``, a
malformed body, a transport failure — becomes
:class:`~app.errors.UpstreamUnavailableError` (502). A refused redirect is a
statement about GitHub's response, not about the URL the user submitted; that
one already passed :func:`app.security.url_validation.parse_github_url`.

Request URLs are logged; the token never is.
"""

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, NoReturn
from urllib.parse import quote, urlsplit

import httpx

from app.config import get_settings
from app.errors import (
    RepositoryNotFoundError,
    RepositoryTooLargeError,
    UpstreamUnavailableError,
)
from app.security.net_guard import assert_public_ip, validate_download_url

logger = logging.getLogger(__name__)

GITHUB_API_ROOT = "https://api.github.com"

# Sent on every request. Authorization is conspicuously absent — see the module
# docstring; adding it here would defeat the credential-withholding control.
BASE_HEADERS: dict[str, str] = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "codebase-2d",
}

# Any 3xx carrying a Location is treated the same way: validated, never
# followed. The set is explicit so a 3xx *without* redirect semantics (304)
# does not silently become a download attempt.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# GitHub logins and repository names, restated at this boundary. The canonical
# spelling comes back from the API and is interpolated into later URLs, so it is
# held to the same character set the user-facing grammar enforces.
_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,100}")
# One slash-separated ref component: "main", "refs", "heads", "feat", ...
_REF_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,255}")
# An abbreviated or full commit SHA, as it appears in a codeload path.
_SHA_PATTERN = re.compile(r"[0-9a-f]{7,40}")


@dataclass(frozen=True, slots=True)
class RepoMetadata:
    """What the preflight learned about a repository.

    ``owner`` and ``name`` are GitHub's *canonical* spelling, not the user's:
    the API is case-insensitive on lookup and returns the real case, and every
    later URL and node path should use that rather than whatever was typed.
    """

    owner: str
    name: str
    default_branch: str
    size_kb: int
    private: bool
    archived: bool


def _reject_upstream() -> NoReturn:
    """One exception, one static message, no echo of the upstream response."""
    raise UpstreamUnavailableError() from None


def create_client() -> httpx.Client:
    """Build the only kind of HTTP client this service is allowed to use.

    Callers that make both requests should reuse one client so the TLS
    connection to ``api.github.com`` is not renegotiated; passing it as the
    ``client`` argument below does that. A client created here never holds a
    credential, so reuse cannot leak one across hosts.
    """
    settings = get_settings()
    return httpx.Client(
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(
            connect=settings.GITHUB_CONNECT_TIMEOUT_S,
            read=settings.GITHUB_READ_TIMEOUT_S,
            write=settings.GITHUB_CONNECT_TIMEOUT_S,
            pool=settings.GITHUB_CONNECT_TIMEOUT_S,
        ),
        limits=httpx.Limits(
            max_connections=settings.MAX_GITHUB_CONNECTIONS,
            max_keepalive_connections=settings.MAX_GITHUB_CONNECTIONS,
        ),
        headers=BASE_HEADERS,
    )


@contextmanager
def _client_scope(client: httpx.Client | None) -> Iterator[httpx.Client]:
    """Use the caller's client, or own one for the duration of the call."""
    if client is not None:
        yield client
        return
    with create_client() as owned:
        yield owned


def _api_auth_headers(token: str | None) -> dict[str, str]:
    """The Authorization header for ``api.github.com``, and nowhere else."""
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _check_segment(value: str) -> str:
    """Refuse an owner or repository name outside the accepted character set."""
    if not _SEGMENT_PATTERN.fullmatch(value) or value in {".", ".."}:
        _reject_upstream()
    return value


def _segment(value: str) -> str:
    """Percent-encode one path segment, or refuse it.

    Encoding alone is not enough: ``..`` survives ``quote`` untouched because
    both characters are unreserved, so the character set is checked first.
    """
    return quote(_check_segment(value), safe="")


def _encode_ref(ref: str) -> str:
    """Encode a git ref for interpolation into an API path.

    A ref legitimately contains slashes (``refs/heads/feat/nested``), so the
    separators are preserved and each component is validated on its own. Refs
    reaching here have already passed ``parse_github_url`` or come from
    GitHub's own ``default_branch``; the check is a boundary restatement, not a
    substitute for that one.
    """
    # str.split never returns an empty list, so an empty ref arrives here as
    # [""] and is refused by the component pattern rather than silently
    # producing ".../tarball/".
    return "/".join(_ref_component(component) for component in ref.split("/"))


def _ref_component(component: str) -> str:
    if not _REF_COMPONENT_PATTERN.fullmatch(component):
        _reject_upstream()
    if ".." in component or component.startswith(".") or component.endswith("."):
        _reject_upstream()
    return quote(component, safe="")


def _repo_api_url(owner: str, name: str) -> str:
    return f"{GITHUB_API_ROOT}/repos/{_segment(owner)}/{_segment(name)}"


def _get(client: httpx.Client, url: str, headers: dict[str, str]) -> httpx.Response:
    """Issue one GET, converting every transport failure into one 502.

    ``httpx.HTTPError`` covers timeouts, connection errors, protocol errors, and
    the too-many-redirects case. The URL is safe to log; the headers are not,
    and are never logged.
    """
    try:
        response = client.get(url, headers=headers)
    except httpx.HTTPError:
        logger.warning("GitHub request failed: GET %s", url, exc_info=True)
        _reject_upstream()
    logger.info("GET %s -> %s", url, response.status_code)
    return response


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Decode a JSON object body, or refuse it.

    The body is upstream data: a non-JSON payload (a proxy's error page, say)
    must not surface as a bare ``JSONDecodeError``, and a JSON *array* or
    scalar must not reach the field readers as something that happens to
    support ``.get``.
    """
    try:
        payload = response.json()
    except ValueError:
        _reject_upstream()
    if not isinstance(payload, dict):
        _reject_upstream()
    return payload


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        _reject_upstream()
    return value


def _require_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    # Not `isinstance(value, int)`: bool is a subclass of int, but the reverse
    # coercion would read `size: 0` as False somewhere down the line.
    if not isinstance(value, bool):
        _reject_upstream()
    return value


def _require_non_negative_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _reject_upstream()
    return value


def get_repo_metadata(
    owner: str,
    name: str,
    token: str | None = None,
    *,
    client: httpx.Client | None = None,
) -> RepoMetadata:
    """Preflight a repository, refusing it if GitHub reports it as oversized.

    Raises :class:`~app.errors.RepositoryNotFoundError` for both ``404`` and
    ``403``, :class:`~app.errors.RepositoryTooLargeError` past
    ``MAX_REPO_API_SIZE_KB``, and
    :class:`~app.errors.UpstreamUnavailableError` for anything else.
    """
    url = _repo_api_url(owner, name)
    with _client_scope(client) as active:
        response = _get(active, url, _api_auth_headers(token))

    if response.status_code in {403, 404}:
        # Collapsed deliberately. With a token configured, "403 forbidden" and
        # "404 missing" would otherwise distinguish a private repository that
        # exists from one that does not.
        raise RepositoryNotFoundError()
    if response.status_code != 200:
        _reject_upstream()

    payload = _json_object(response)
    owner_payload = payload.get("owner")
    if not isinstance(owner_payload, dict):
        _reject_upstream()

    metadata = RepoMetadata(
        # Re-validated through _segment because these are interpolated into the
        # tarball URL and later into node paths.
        owner=_check_segment(_require_str(owner_payload, "login")),
        name=_check_segment(_require_str(payload, "name")),
        default_branch=_require_str(payload, "default_branch"),
        size_kb=_require_non_negative_int(payload, "size"),
        private=_require_bool(payload, "private"),
        archived=_require_bool(payload, "archived"),
    )
    # Fails now rather than at URL-construction time, so a hostile branch name
    # cannot be carried around as a valid-looking RepoMetadata.
    _encode_ref(metadata.default_branch)

    if metadata.size_kb > get_settings().MAX_REPO_API_SIZE_KB:
        logger.info("repository refused as oversized: %s KiB", metadata.size_kb)
        raise RepositoryTooLargeError()

    return metadata


def get_download_url(
    owner: str,
    name: str,
    ref: str,
    token: str | None = None,
    *,
    client: httpx.Client | None = None,
) -> tuple[str, str | None]:
    """Resolve the tarball redirect to an approved download URL.

    Returns ``(url, commit_sha)``. ``commit_sha`` is ``None`` unless the
    redirect target pins one — GitHub redirects a branch ref to
    ``.../legacy.tar.gz/refs/heads/main``, which names no commit. The
    authoritative SHA is harvested from the tar root directory during
    extraction (docs/ARCHITECTURE.md, ingestion step 5); this is a hint, not a
    replacement for that.

    The redirect is validated, never followed: the ``Location`` goes through
    :func:`~app.security.net_guard.validate_download_url` for the host
    allowlist and then through
    :func:`~app.security.net_guard.assert_public_ip` for the resolved
    destination. Both raise :class:`~app.errors.UpstreamUnavailableError`, as
    does every other failure here.
    """
    url = f"{_repo_api_url(owner, name)}/tarball/{_encode_ref(ref)}"
    with _client_scope(client) as active:
        response = _get(active, url, _api_auth_headers(token))

    if response.status_code not in _REDIRECT_STATUSES:
        _reject_upstream()

    location = response.headers.get("Location")
    if not location:
        _reject_upstream()

    # Host allowlist by string equality. Returns the input unchanged, so what
    # is fetched is exactly what was validated.
    target = validate_download_url(location)

    # Total here only because validate_download_url already parsed this exact
    # string without raising; wrapped anyway so a future divergence between the
    # two cannot escape as a bare ValueError quoting the upstream host.
    try:
        host = urlsplit(target).hostname
    except ValueError:
        _reject_upstream()
    if host is None:
        _reject_upstream()

    # An allowlisted name is not a verified destination — the resolver is
    # attacker-influenceable.
    assert_public_ip(host)

    return target, _sha_from_download_url(target)


def _sha_from_download_url(url: str) -> str | None:
    """Read a commit SHA out of a codeload path, if the redirect pinned one."""
    last = urlsplit(url).path.rstrip("/").rpartition("/")[2]
    return last if _SHA_PATTERN.fullmatch(last) else None


def download_request(client: httpx.Client, url: str) -> httpx.Request:
    """Build the credential-free GET for an already-approved download URL.

    Sending it is ``app/fetch/archive.py``'s job (Task 5). Building it here
    keeps the credential rule in one place and makes it testable: the assertion
    that no ``Authorization`` reaches ``codeload.github.com`` runs against this
    request, not against a hand-rolled one in a test.

    ``url`` must be the value returned by :func:`get_download_url`. This
    function does not re-validate it; it is not a second guard.
    """
    request = client.build_request("GET", url)
    # Belt and braces. create_client() sets no Authorization default, so there
    # is normally nothing to remove — but a caller-supplied client is outside
    # this module's control, and a credential reaching codeload is exactly the
    # failure this pop exists to make impossible.
    request.headers.pop("Authorization", None)
    return request
