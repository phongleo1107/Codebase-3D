"""GitHub client: preflight, redirect handling, and credential withholding.

No test here touches the network. Every HTTP interaction is served by respx,
which swaps httpx's transport, and `getaddrinfo` is stubbed per test on top of
the suite-wide block in conftest.py — so a check that forgot to stub the
resolver fails loudly rather than resolving a real name.

The load-bearing test in this file is
`test_download_request_carries_no_authorization`. It is the assertion
docs/SECURITY.md has been owing since the egress guard landed: the guard can
refuse a hostile redirect target, but if the token rode along on the download
request, a single bypass would hand the operator's credential to whoever
controlled that host.
"""

import socket
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from app.config import get_settings
from app.errors import (
    AppError,
    ErrorCode,
    RepositoryNotFoundError,
    RepositoryTooLargeError,
    UpstreamUnavailableError,
)
from app.fetch.github import (
    BASE_HEADERS,
    GITHUB_API_ROOT,
    RepoMetadata,
    create_client,
    download_request,
    get_download_url,
    get_repo_metadata,
)

OWNER = "facebook"
NAME = "react"
SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
REPO_URL = f"{GITHUB_API_ROOT}/repos/{OWNER}/{NAME}"
TARBALL_URL = f"{REPO_URL}/tarball/main"
CODELOAD = f"https://codeload.github.com/{OWNER}/{NAME}/legacy.tar.gz/{SHA}"
CODELOAD_BRANCH = f"https://codeload.github.com/{OWNER}/{NAME}/legacy.tar.gz/refs/heads/main"

# A fake, for asserting where a credential does and does not travel.
TOKEN = "ghp_faketokenfortestsonly0000000000000000"

PUBLIC_IP = "140.82.121.4"
LOOPBACK_IP = "127.0.0.1"


def repo_payload(**overrides: Any) -> dict[str, Any]:
    """A minimal, well-formed /repos response."""
    payload: dict[str, Any] = {
        "name": NAME,
        "owner": {"login": OWNER},
        "default_branch": "main",
        "size": 1024,
        "private": False,
        "archived": False,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def resolves_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every name resolve to one globally routable address."""
    _patch_resolver(monkeypatch, PUBLIC_IP)


@pytest.fixture
def resolves_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every name resolve to loopback — a hostile or poisoned resolver."""
    _patch_resolver(monkeypatch, LOOPBACK_IP)


def _patch_resolver(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    def fake_getaddrinfo(*args: object, **kwargs: object) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    """One client shared across both requests, as the pipeline will use it."""
    with create_client() as active:
        yield active


# --------------------------------------------------------------------------
# Client configuration — three properties that are controls, not settings
# --------------------------------------------------------------------------


def test_client_does_not_follow_redirects(client: httpx.Client) -> None:
    assert client.follow_redirects is False


def test_client_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """trust_env=False: HTTP_PROXY and friends cannot reroute our egress."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    with create_client() as active:
        assert active.trust_env is False
        # The proxy env vars are read at construction time when trust_env is
        # true, so an empty mounts table is the observable consequence.
        assert not [key for key in active._mounts if key.pattern != ""]


def test_client_holds_no_authorization_header(client: httpx.Client) -> None:
    """The structural half of the credential control.

    A client with no Authorization default cannot leak one to a later request,
    no matter which host that request goes to.
    """
    assert "authorization" not in {key.lower() for key in client.headers}
    assert "authorization" not in {key.lower() for key in BASE_HEADERS}


def test_client_timeouts_come_from_settings(client: httpx.Client) -> None:
    settings = get_settings()
    assert client.timeout.connect == settings.GITHUB_CONNECT_TIMEOUT_S
    assert client.timeout.read == settings.GITHUB_READ_TIMEOUT_S


# --------------------------------------------------------------------------
# get_repo_metadata
# --------------------------------------------------------------------------


@respx.mock
def test_metadata_success(client: httpx.Client) -> None:
    route = respx.get(REPO_URL).mock(return_value=httpx.Response(200, json=repo_payload()))

    metadata = get_repo_metadata(OWNER, NAME, None, client=client)

    assert metadata == RepoMetadata(
        owner=OWNER,
        name=NAME,
        default_branch="main",
        size_kb=1024,
        private=False,
        archived=False,
    )
    assert route.called


@respx.mock
def test_metadata_returns_githubs_canonical_casing(client: httpx.Client) -> None:
    """GitHub is case-insensitive on lookup; the canonical spelling wins."""
    respx.get(f"{GITHUB_API_ROOT}/repos/FaceBook/ReAcT").mock(
        return_value=httpx.Response(200, json=repo_payload())
    )

    metadata = get_repo_metadata("FaceBook", "ReAcT", None, client=client)

    assert (metadata.owner, metadata.name) == (OWNER, NAME)


@respx.mock
def test_metadata_sends_token_when_configured(client: httpx.Client) -> None:
    route = respx.get(REPO_URL).mock(return_value=httpx.Response(200, json=repo_payload()))

    get_repo_metadata(OWNER, NAME, TOKEN, client=client)

    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"


@respx.mock
def test_metadata_sends_no_token_when_unset(client: httpx.Client) -> None:
    route = respx.get(REPO_URL).mock(return_value=httpx.Response(200, json=repo_payload()))

    get_repo_metadata(OWNER, NAME, None, client=client)

    assert "authorization" not in route.calls.last.request.headers


@respx.mock
def test_metadata_works_without_a_supplied_client() -> None:
    """The client argument is an optimization; the functions own one otherwise."""
    respx.get(REPO_URL).mock(return_value=httpx.Response(200, json=repo_payload()))

    assert get_repo_metadata(OWNER, NAME).default_branch == "main"


@respx.mock
def test_metadata_rejects_oversized_repository_before_any_download(
    client: httpx.Client,
) -> None:
    limit = get_settings().MAX_REPO_API_SIZE_KB
    respx.get(REPO_URL).mock(
        return_value=httpx.Response(200, json=repo_payload(size=limit + 1))
    )
    # Any codeload traffic at all would mean the preflight failed to gate it.
    codeload = respx.get(url__startswith="https://codeload.github.com")

    with pytest.raises(RepositoryTooLargeError):
        get_repo_metadata(OWNER, NAME, None, client=client)

    assert not codeload.called


@respx.mock
def test_metadata_accepts_a_repository_exactly_at_the_limit(client: httpx.Client) -> None:
    limit = get_settings().MAX_REPO_API_SIZE_KB
    respx.get(REPO_URL).mock(return_value=httpx.Response(200, json=repo_payload(size=limit)))

    assert get_repo_metadata(OWNER, NAME, None, client=client).size_kb == limit


@respx.mock
def test_metadata_size_limit_is_read_from_settings_at_call_time(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tightening the limit in the environment must actually be enforced."""
    get_settings.cache_clear()
    monkeypatch.setenv("MAX_REPO_API_SIZE_KB", "10")
    try:
        respx.get(REPO_URL).mock(return_value=httpx.Response(200, json=repo_payload(size=11)))
        with pytest.raises(RepositoryTooLargeError):
            get_repo_metadata(OWNER, NAME, None, client=client)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("status", [403, 404])
@respx.mock
def test_metadata_collapses_403_and_404(client: httpx.Client, status: int) -> None:
    """A configured token must not become a private-repo existence oracle."""
    respx.get(REPO_URL).mock(return_value=httpx.Response(status, json={"message": "Not Found"}))

    with pytest.raises(RepositoryNotFoundError) as excinfo:
        get_repo_metadata(OWNER, NAME, TOKEN, client=client)

    assert excinfo.value.code is ErrorCode.REPOSITORY_NOT_FOUND


@respx.mock
def test_403_and_404_are_indistinguishable(client: httpx.Client) -> None:
    """Same type, same status, same body — byte for byte."""
    bodies = []
    for status in (403, 404):
        respx.get(REPO_URL).mock(return_value=httpx.Response(status))
        with pytest.raises(AppError) as excinfo:
            get_repo_metadata(OWNER, NAME, TOKEN, client=client)
        bodies.append((type(excinfo.value), excinfo.value.body("req-1")))
    assert bodies[0] == bodies[1]


@pytest.mark.parametrize("status", [200, 301, 401, 429, 500, 502, 503])
@respx.mock
def test_metadata_maps_other_statuses_to_upstream_unavailable(
    client: httpx.Client, status: int
) -> None:
    # 200 is included deliberately: a 200 with a body that is not a repository
    # object is upstream nonsense, not a successful preflight.
    body = "not json at all" if status == 200 else ""
    respx.get(REPO_URL).mock(return_value=httpx.Response(status, text=body))

    with pytest.raises(UpstreamUnavailableError):
        get_repo_metadata(OWNER, NAME, None, client=client)


MALFORMED_PAYLOADS: list[Any] = [
    # Wrong container types.
    [],
    "a string",
    42,
    None,
    # Missing fields.
    {"name": NAME, "owner": {"login": OWNER}, "size": 1, "private": False, "archived": False},
    {"owner": {"login": OWNER}, "default_branch": "main", "size": 1, "private": False},
    # Wrong field types.
    {**{"name": NAME, "owner": {"login": OWNER}, "default_branch": "main"},
     "size": "1024", "private": False, "archived": False},
    {**{"name": NAME, "owner": {"login": OWNER}, "default_branch": "main"},
     "size": 1, "private": "no", "archived": False},
    {**{"name": NAME, "owner": {"login": OWNER}, "default_branch": "main"},
     "size": -1, "private": False, "archived": False},
    # owner is not an object.
    {"name": NAME, "owner": OWNER, "default_branch": "main",
     "size": 1, "private": False, "archived": False},
    # Empty strings.
    {"name": "", "owner": {"login": OWNER}, "default_branch": "main",
     "size": 1, "private": False, "archived": False},
    # Names and branches outside the accepted character set — these are
    # interpolated into later URLs, so upstream does not get to widen them.
    {"name": "../../etc/passwd", "owner": {"login": OWNER}, "default_branch": "main",
     "size": 1, "private": False, "archived": False},
    {"name": NAME, "owner": {"login": "a/b"}, "default_branch": "main",
     "size": 1, "private": False, "archived": False},
    {"name": NAME, "owner": {"login": OWNER}, "default_branch": "../../../x",
     "size": 1, "private": False, "archived": False},
    {"name": NAME, "owner": {"login": OWNER}, "default_branch": "..",
     "size": 1, "private": False, "archived": False},
]


@pytest.mark.parametrize("payload", MALFORMED_PAYLOADS, ids=range(len(MALFORMED_PAYLOADS)))
@respx.mock
def test_metadata_rejects_malformed_body(client: httpx.Client, payload: Any) -> None:
    respx.get(REPO_URL).mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(UpstreamUnavailableError):
        get_repo_metadata(OWNER, NAME, None, client=client)


@respx.mock
def test_metadata_reports_private_and_archived(client: httpx.Client) -> None:
    respx.get(REPO_URL).mock(
        return_value=httpx.Response(200, json=repo_payload(private=True, archived=True))
    )

    metadata = get_repo_metadata(OWNER, NAME, None, client=client)

    assert (metadata.private, metadata.archived) == (True, True)


@respx.mock
def test_metadata_transport_failure_becomes_upstream_unavailable(
    client: httpx.Client,
) -> None:
    respx.get(REPO_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    with pytest.raises(UpstreamUnavailableError):
        get_repo_metadata(OWNER, NAME, None, client=client)


HOSTILE_SEGMENTS = ["../../etc", "a/b", "", "..", ".", "a%2fb", "a b", "a?b", "a#b"]


@pytest.mark.parametrize("segment", HOSTILE_SEGMENTS, ids=HOSTILE_SEGMENTS or None)
@respx.mock
def test_hostile_owner_or_name_never_reaches_the_wire(
    client: httpx.Client, segment: str
) -> None:
    """Path segments are validated before the URL is built, not after."""
    catch_all = respx.get(url__startswith=GITHUB_API_ROOT).mock(
        return_value=httpx.Response(200, json=repo_payload())
    )

    with pytest.raises(UpstreamUnavailableError):
        get_repo_metadata(segment, NAME, None, client=client)
    with pytest.raises(UpstreamUnavailableError):
        get_repo_metadata(OWNER, segment, None, client=client)

    assert not catch_all.called


# --------------------------------------------------------------------------
# get_download_url
# --------------------------------------------------------------------------


def redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


@respx.mock
@pytest.mark.usefixtures("resolves_public")
def test_download_url_success(client: httpx.Client) -> None:
    respx.get(TARBALL_URL).mock(return_value=redirect(CODELOAD))

    url, sha = get_download_url(OWNER, NAME, "main", None, client=client)

    # Returned byte-identical, so the download fetches exactly what was validated.
    assert url == CODELOAD
    assert sha == SHA


@respx.mock
@pytest.mark.usefixtures("resolves_public")
def test_download_url_has_no_sha_when_the_redirect_pins_a_branch(
    client: httpx.Client,
) -> None:
    """A branch ref redirects to .../refs/heads/main, which names no commit.

    The authoritative SHA comes from the tar root during extraction; this
    function must not invent one.
    """
    respx.get(TARBALL_URL).mock(return_value=redirect(CODELOAD_BRANCH))

    url, sha = get_download_url(OWNER, NAME, "main", None, client=client)

    assert (url, sha) == (CODELOAD_BRANCH, None)


@respx.mock
@pytest.mark.usefixtures("resolves_public")
def test_download_url_never_follows_the_redirect(client: httpx.Client) -> None:
    respx.get(TARBALL_URL).mock(return_value=redirect(CODELOAD))
    codeload = respx.get(url__startswith="https://codeload.github.com")

    get_download_url(OWNER, NAME, "main", None, client=client)

    assert not codeload.called


@respx.mock
@pytest.mark.usefixtures("resolves_public")
def test_download_url_sends_token_to_api_github(client: httpx.Client) -> None:
    route = respx.get(TARBALL_URL).mock(return_value=redirect(CODELOAD))

    get_download_url(OWNER, NAME, "main", TOKEN, client=client)

    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"


@respx.mock
@pytest.mark.usefixtures("resolves_public")
def test_download_url_encodes_a_nested_ref(client: httpx.Client) -> None:
    route = respx.get(f"{REPO_URL}/tarball/refs/heads/feat/nested").mock(
        return_value=redirect(CODELOAD)
    )

    get_download_url(OWNER, NAME, "refs/heads/feat/nested", None, client=client)

    assert route.called


HOSTILE_REFS = ["../../../etc/passwd", "..", "a/../b", "", "a b", "main?x=1", "main#f"]


@pytest.mark.parametrize("ref", HOSTILE_REFS, ids=range(len(HOSTILE_REFS)))
@respx.mock
def test_hostile_ref_never_reaches_the_wire(client: httpx.Client, ref: str) -> None:
    catch_all = respx.get(url__startswith=GITHUB_API_ROOT).mock(
        return_value=redirect(CODELOAD)
    )

    with pytest.raises(UpstreamUnavailableError):
        get_download_url(OWNER, NAME, ref, None, client=client)

    assert not catch_all.called


REFUSED_LOCATIONS = [
    # The suffix attack the equality allowlist exists to defeat.
    f"https://codeload.github.com.evil.com/{OWNER}/{NAME}/legacy.tar.gz/{SHA}",
    "https://evil.com/legacy.tar.gz",
    # Scheme downgrade, protocol-relative, and relative Locations.
    f"http://codeload.github.com/{OWNER}/{NAME}/legacy.tar.gz/{SHA}",
    f"//codeload.github.com/{OWNER}/{NAME}/legacy.tar.gz/{SHA}",
    f"/{OWNER}/{NAME}/legacy.tar.gz/{SHA}",
    "file:///etc/passwd",
    # Straight at the internal network.
    "https://169.254.169.254/latest/meta-data/",
    "https://127.0.0.1/legacy.tar.gz",
]


@pytest.mark.parametrize("location", REFUSED_LOCATIONS, ids=REFUSED_LOCATIONS)
@respx.mock
def test_refused_redirect_target_is_never_requested(
    client: httpx.Client, location: str
) -> None:
    """A host the guard refuses must produce a 502 and zero further traffic."""
    respx.get(TARBALL_URL).mock(return_value=redirect(location))
    followed = respx.get(url__startswith="https://").mock(
        return_value=httpx.Response(200, content=b"pwned")
    )

    with pytest.raises(UpstreamUnavailableError):
        get_download_url(OWNER, NAME, "main", None, client=client)

    assert not followed.called


@respx.mock
@pytest.mark.usefixtures("resolves_loopback")
def test_allowlisted_host_resolving_to_loopback_is_refused(client: httpx.Client) -> None:
    """The name passes; the destination does not. Both checks are required."""
    respx.get(TARBALL_URL).mock(return_value=redirect(CODELOAD))

    with pytest.raises(UpstreamUnavailableError):
        get_download_url(OWNER, NAME, "main", None, client=client)


@respx.mock
def test_unresolvable_host_is_refused(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(TARBALL_URL).mock(return_value=redirect(CODELOAD))

    def failing_getaddrinfo(*args: object, **kwargs: object) -> list[Any]:
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", failing_getaddrinfo)

    with pytest.raises(UpstreamUnavailableError):
        get_download_url(OWNER, NAME, "main", None, client=client)


@pytest.mark.parametrize("status", [200, 204, 304, 400, 403, 404, 500])
@respx.mock
def test_non_redirect_status_is_upstream_unavailable(
    client: httpx.Client, status: int
) -> None:
    """Note the deliberate difference from the preflight: 403/404 here is not a
    missing repository — the preflight already found it — so it does not
    collapse to REPOSITORY_NOT_FOUND."""
    respx.get(TARBALL_URL).mock(return_value=httpx.Response(status))

    with pytest.raises(UpstreamUnavailableError):
        get_download_url(OWNER, NAME, "main", None, client=client)


@pytest.mark.parametrize("status", [200, 201, 304, 400, 500])
@respx.mock
def test_location_on_a_non_redirect_status_is_not_followed(
    client: httpx.Client, status: int
) -> None:
    """A Location header only means "redirect" on a redirect status.

    Without this, `200 OK` plus a `Location` would be read as a download
    target. 304 is the reason the accepted set is enumerated rather than a
    `300 <= status < 400` range: it carries no redirect semantics.
    """
    respx.get(TARBALL_URL).mock(
        return_value=httpx.Response(status, headers={"Location": CODELOAD})
    )

    with pytest.raises(UpstreamUnavailableError):
        get_download_url(OWNER, NAME, "main", None, client=client)


@respx.mock
def test_redirect_without_location_is_upstream_unavailable(client: httpx.Client) -> None:
    respx.get(TARBALL_URL).mock(return_value=httpx.Response(302))

    with pytest.raises(UpstreamUnavailableError):
        get_download_url(OWNER, NAME, "main", None, client=client)


@respx.mock
def test_download_url_transport_failure_is_upstream_unavailable(
    client: httpx.Client,
) -> None:
    respx.get(TARBALL_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(UpstreamUnavailableError):
        get_download_url(OWNER, NAME, "main", None, client=client)


@respx.mock
@pytest.mark.usefixtures("resolves_public")
def test_every_refusal_body_is_identical(client: httpx.Client) -> None:
    """No rejection may hint at which check tripped or echo upstream detail."""
    cases = [
        redirect("https://codeload.github.com.evil.com/x"),
        redirect("http://codeload.github.com/x"),
        httpx.Response(500),
        httpx.Response(302),
    ]
    bodies = set()
    for response in cases:
        respx.get(TARBALL_URL).mock(return_value=response)
        with pytest.raises(AppError) as excinfo:
            get_download_url(OWNER, NAME, "main", None, client=client)
        assert isinstance(excinfo.value, UpstreamUnavailableError)
        bodies.add(str(excinfo.value.body("req-1")))
    assert len(bodies) == 1


# --------------------------------------------------------------------------
# The credential rule — the assertion SECURITY.md has been owing
# --------------------------------------------------------------------------


@respx.mock
@pytest.mark.usefixtures("resolves_public")
def test_download_request_carries_no_authorization(client: httpx.Client) -> None:
    """The token reaches api.github.com and stops there.

    Both requests go through the same client, in the order the pipeline will
    make them, with a token configured throughout. The codeload request is the
    one that must be bare: if the host allowlist were ever bypassed, an
    Authorization header here would hand the operator's credential to whatever
    host the attacker redirected us to.
    """
    api = respx.get(TARBALL_URL).mock(return_value=redirect(CODELOAD))
    codeload = respx.get(CODELOAD).mock(return_value=httpx.Response(200, content=b"tar"))

    url, _ = get_download_url(OWNER, NAME, "main", TOKEN, client=client)
    client.send(download_request(client, url))

    assert api.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"

    sent = codeload.calls.last.request
    assert sent.url.host == "codeload.github.com"
    assert "authorization" not in {key.lower() for key in sent.headers}
    # Not just absent under that name — the value must appear nowhere at all.
    assert TOKEN not in str(dict(sent.headers))


@respx.mock
def test_download_request_strips_an_inherited_authorization_header() -> None:
    """Belt and braces, against a client this module did not build.

    create_client() never sets an Authorization default, so this can only
    happen if a caller supplies its own client. The download must still be
    bare.
    """
    codeload = respx.get(CODELOAD).mock(return_value=httpx.Response(200))

    with httpx.Client(
        follow_redirects=False,
        trust_env=False,
        headers={**BASE_HEADERS, "Authorization": f"Bearer {TOKEN}"},
    ) as tainted:
        tainted.send(download_request(tainted, CODELOAD))

    assert "authorization" not in {key.lower() for key in codeload.calls.last.request.headers}


@respx.mock
@pytest.mark.usefixtures("resolves_public")
def test_token_is_not_carried_across_calls_on_a_shared_client(
    client: httpx.Client,
) -> None:
    """A tokened call must not leave a credential on the client for the next one."""
    respx.get(REPO_URL).mock(return_value=httpx.Response(200, json=repo_payload()))
    tarball = respx.get(TARBALL_URL).mock(return_value=redirect(CODELOAD))

    get_repo_metadata(OWNER, NAME, TOKEN, client=client)
    get_download_url(OWNER, NAME, "main", None, client=client)

    assert "authorization" not in {key.lower() for key in client.headers}
    assert "authorization" not in tarball.calls.last.request.headers
