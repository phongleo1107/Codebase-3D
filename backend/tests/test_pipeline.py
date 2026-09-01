"""The analysis pipeline: the first code path that runs a whole repository.

Every other test file in this suite exercises one module against a fixture it
constructed. This one exercises the *join* — a `RepoRef` in, a
`RepositoryAnalysis` out, through the real GitHub client, the real archive
reader, and the real parser, with only the transport replaced.

No test here touches the network: respx swaps httpx's transport, `getaddrinfo`
is stubbed per test on top of the suite-wide block in conftest.py, and the
tarballs come from `tests/fixtures/tarballs.py`.

Four groups carry most of the weight:

*The credential rule* — `test_download_carries_no_authorization` repeats the
assertion `tests/test_github_client.py` makes, but through the pipeline, which
is the code that actually sends the request. The client test proves
`download_request` builds a bare request; this one proves nothing between there
and the wire puts a token back.

*The stream* — `iter_raw`, not `iter_bytes`, and `stream=True`. Both are
security controls (see `app/analysis/pipeline.py`) and both are pinned
behaviourally rather than by inspecting the call.

*The grammar split* — asserted by the imports it costs, not by the mapping. A
test that checked `_BY_EXTENSION[".ts"] is _TS_GRAMMAR` would restate the code;
these check that picking the wrong grammar silently loses a dependency edge.

*The two caps* — `MAX_SOURCE_FILES` and `MAX_ARCHIVE_MEMBERS` are different
limits at different layers, and a test asserts they cannot be conflated.
"""

import logging
import socket
import tarfile
import time
from collections.abc import Iterator
from dataclasses import fields
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.analysis import pipeline as pipeline_module
from app.analysis.deadline import Deadline
from app.analysis.parser import parse_source
from app.analysis.pipeline import (
    JAVASCRIPT,
    SKIP_FILTERED,
    SKIP_UNSUPPORTED,
    TYPESCRIPT,
    RepositoryAnalysis,
    analyze_repository,
)
from app.analysis.routes import detect_routes
from app.config import Settings
from app.errors import (
    AnalysisTimeoutError,
    ArchiveRejectedError,
    NoSupportedFilesError,
    RepositoryNotFoundError,
    UpstreamUnavailableError,
)
from app.fetch.archive import iter_source_files
from app.fetch.github import GITHUB_API_ROOT, create_client
from app.security.url_validation import RepoRef
from tests.fixtures.tarballs import ROOT, TarMember, chunked, make_source_tar, make_tar, noise

OWNER = "acme"
NAME = "widgets"
SHA = "a1b2c3d"  # the SHA carried by tests.fixtures.tarballs.ROOT
REPO_URL = f"{GITHUB_API_ROOT}/repos/{OWNER}/{NAME}"
CODELOAD = f"https://codeload.github.com/{OWNER}/{NAME}/legacy.tar.gz/refs/heads/main"

# A fake, for asserting where a credential does and does not travel.
TOKEN = "ghp_faketokenfortestsonly0000000000000000"

PUBLIC_IP = "140.82.121.4"

REPO = RepoRef(owner=OWNER, name=NAME, ref=None)
SETTINGS = Settings()


def tarball_url(ref: str = "main") -> str:
    return f"{REPO_URL}/tarball/{ref}"


def repo_payload(**overrides: Any) -> dict[str, Any]:
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


@pytest.fixture(autouse=True)
def resolves_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every name resolves to one globally routable address.

    Autouse because `assert_public_ip` runs on the redirect in every test here;
    without it the suite-wide network block fires instead and every failure
    looks like an SSRF rejection.
    """

    def fake_getaddrinfo(*args: object, **kwargs: object) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    """One client for all three requests, exactly as the pipeline uses it."""
    with create_client() as active:
        yield active


def serve(
    tarball: bytes | Iterator[bytes],
    *,
    payload: dict[str, Any] | None = None,
    ref: str = "main",
    download: httpx.Response | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, respx.Route]:
    """Mock the whole three-request conversation: preflight, redirect, download."""
    return {
        "repo": respx.get(REPO_URL).mock(
            return_value=httpx.Response(
                200, json=payload if payload is not None else repo_payload()
            )
        ),
        "tarball": respx.get(tarball_url(ref)).mock(
            return_value=httpx.Response(302, headers={"Location": CODELOAD})
        ),
        "download": respx.get(CODELOAD).mock(
            return_value=download
            if download is not None
            else httpx.Response(200, content=tarball, headers=headers or {})
        ),
    }


def run(
    tarball: bytes | Iterator[bytes],
    *,
    client: httpx.Client,
    settings: Settings = SETTINGS,
    repo: RepoRef = REPO,
    **kwargs: Any,
) -> RepositoryAnalysis:
    serve(tarball, **kwargs)
    return analyze_repository(repo, settings=settings, client=client)


def paths(analysis: RepositoryAnalysis) -> list[str]:
    return [str(f.path) for f in analysis.files]


def specifiers(analysis: RepositoryAnalysis, path: str) -> list[str]:
    for source in analysis.files:
        if str(source.path) == path:
            return sorted(ref.specifier for ref in source.imports)
    raise AssertionError(f"{path} is not in the analysis")


# --------------------------------------------------------------------------
# The join: a whole repository, end to end
# --------------------------------------------------------------------------

REALISTIC = {
    "package.json": b'{"name":"widgets"}',
    "README.md": b"# widgets\n",
    "src/index.ts": b"import { a } from './a';\nimport react from 'react';\n",
    "src/a.ts": b"export const a = 1;\n",
    "src/App.tsx": b"import './a';\nexport const App = () => <div>hi</div>;\n",
    "src/legacy.js": b"const x = require('./a');\n",
}


@respx.mock
def test_analyzes_a_repository_end_to_end(client: httpx.Client) -> None:
    """One repository through every module in the ingestion path.

    Nothing in this project had ever done this before the pipeline existed.
    """
    analysis = run(make_source_tar(REALISTIC), client=client)

    assert paths(analysis) == ["src/index.ts", "src/a.ts", "src/App.tsx", "src/legacy.js"]
    assert analysis.commit_sha == SHA
    assert (analysis.owner, analysis.name, analysis.ref) == (OWNER, NAME, "main")
    assert analysis.truncated is False

    assert specifiers(analysis, "src/index.ts") == ["./a", "react"]
    assert specifiers(analysis, "src/App.tsx") == ["./a"]
    assert specifiers(analysis, "src/legacy.js") == ["./a"]

    # package.json and README.md have no grammar; they are counted, not dropped
    # silently.
    assert analysis.skipped[SKIP_UNSUPPORTED] == 2


@respx.mock
def test_all_three_requests_are_made_in_order(client: httpx.Client) -> None:
    routes = serve(make_source_tar({"a.ts": b"1"}))

    analyze_repository(REPO, settings=SETTINGS, client=client)

    assert all(route.called for route in routes.values())
    assert [call.request.url.host for call in respx.calls] == [
        "api.github.com",
        "api.github.com",
        "codeload.github.com",
    ]


@respx.mock
def test_uses_githubs_canonical_spelling_not_the_users(client: httpx.Client) -> None:
    """The preflight's owner/name win, and they are what the tarball URL uses."""
    respx.get(f"{GITHUB_API_ROOT}/repos/AcMe/WiDgEtS").mock(
        return_value=httpx.Response(200, json=repo_payload())
    )
    respx.get(tarball_url()).mock(
        return_value=httpx.Response(302, headers={"Location": CODELOAD})
    )
    respx.get(CODELOAD).mock(
        return_value=httpx.Response(200, content=make_source_tar({"a.ts": b"1"}))
    )

    analysis = analyze_repository(
        RepoRef(owner="AcMe", name="WiDgEtS", ref=None), settings=SETTINGS, client=client
    )

    assert (analysis.owner, analysis.name) == (OWNER, NAME)


@respx.mock
def test_uses_the_default_branch_when_the_url_names_no_ref(client: httpx.Client) -> None:
    routes = serve(
        make_source_tar({"a.ts": b"1"}),
        payload=repo_payload(default_branch="trunk"),
        ref="trunk",
    )

    analysis = analyze_repository(REPO, settings=SETTINGS, client=client)

    assert routes["tarball"].called
    assert analysis.ref == "trunk"


@respx.mock
def test_uses_the_ref_from_the_url_when_it_names_one(client: httpx.Client) -> None:
    routes = serve(make_source_tar({"a.ts": b"1"}), ref="v2")

    analysis = analyze_repository(
        RepoRef(owner=OWNER, name=NAME, ref="v2"), settings=SETTINGS, client=client
    )

    assert routes["tarball"].called
    assert analysis.ref == "v2"


# --------------------------------------------------------------------------
# The credential rule, asserted against the request that is actually sent
# --------------------------------------------------------------------------


@respx.mock
def test_download_carries_no_authorization(client: httpx.Client) -> None:
    """The token reaches api.github.com and stops there.

    `tests/test_github_client.py` proves `download_request` *builds* a bare
    request. This proves the pipeline *sends* one: both API calls carry the
    credential, the codeload GET does not, under any header name and with the
    token's value searched for across every header value.

    If the redirect host allowlist were ever bypassed, an Authorization header
    here would hand the operator's credential to whatever host the attacker
    redirected us to.
    """
    routes = serve(make_source_tar({"a.ts": b"1"}))

    analyze_repository(REPO, settings=Settings(GITHUB_TOKEN=SecretStr(TOKEN)), client=client)

    assert routes["repo"].calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert routes["tarball"].calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"

    sent = routes["download"].calls.last.request
    assert sent.url.host == "codeload.github.com"
    assert "authorization" not in {key.lower() for key in sent.headers}
    # Not merely absent under that name — the value must appear nowhere at all.
    assert TOKEN not in str(dict(sent.headers))


@respx.mock
def test_no_token_configured_sends_no_authorization_anywhere(client: httpx.Client) -> None:
    routes = serve(make_source_tar({"a.ts": b"1"}))

    analyze_repository(REPO, settings=Settings(GITHUB_TOKEN=None), client=client)

    for route in routes.values():
        assert "authorization" not in {key.lower() for key in route.calls.last.request.headers}


# --------------------------------------------------------------------------
# The download stream
# --------------------------------------------------------------------------


@respx.mock
def test_reads_the_raw_stream_so_a_content_encoding_cannot_expand_it(
    client: httpx.Client,
) -> None:
    """`iter_raw()`, not `iter_bytes()` — and the difference is observable.

    httpx transparently gunzips a response carrying `Content-Encoding: gzip`
    before any of our meters see a byte. Measured on httpx 0.28.1: a 52-byte
    body served with that header comes back from `iter_bytes()` as its 1700
    decoded bytes. Every budget in `fetch/archive.py` — `MAX_DOWNLOAD_BYTES`,
    and the compression-ratio guard's *denominator* — is defined on wire bytes,
    so an upstream that sets the header gets a free unmetered decompression
    pass and quietly changes what two controls measure.

    A tarball is already gzip. Served with the header set, `iter_raw()` hands
    the reader the gzip stream it expects and the analysis succeeds; under
    `iter_bytes()` httpx would strip that layer and the reader would be handed
    a bare tar, which is not a gzip stream at all.
    """
    analysis = run(
        make_source_tar({"src/a.ts": b"import './b';\n"}),
        client=client,
        headers={"Content-Encoding": "gzip"},
    )

    assert paths(analysis) == ["src/a.ts"]


@respx.mock
def test_download_is_streamed_rather_than_buffered(client: httpx.Client) -> None:
    """`stream=True` is a control, not a tuning knob.

    Without it httpx reads the whole body into memory before returning, so
    `MAX_DOWNLOAD_BYTES` would be enforced against bytes already buffered and
    ADR-003's bounded-memory claim would be false. Here the source-file cap
    stops the analysis early, and most of the archive must still be unsent.
    """
    payload = make_source_tar({f"src/f{i}.ts": noise(4096, seed=i) for i in range(200)})
    served = 0

    def counting() -> Iterator[bytes]:
        nonlocal served
        for chunk in chunked(payload, 4096):
            served += len(chunk)
            yield chunk

    analysis = run(counting(), client=client, settings=Settings(MAX_SOURCE_FILES=2))

    assert analysis.truncated is True
    assert 0 < served < len(payload)


@respx.mock
def test_download_response_is_closed_after_stopping_early(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `closing()` around the streamed response, on the path that needs it.

    A fully-drained stream is closed by httpx itself, so asserting closure after
    a complete analysis proves nothing — mutation testing caught exactly that:
    deleting `closing(response)` left the first version of this test green. The
    case that distinguishes them is stopping early, which is what
    `MAX_SOURCE_FILES` does; a response abandoned mid-body stays open and holds
    its connection until the collector gets to it.
    """
    responses: list[httpx.Response] = []
    real_send = httpx.Client.send

    def spy(self: httpx.Client, *args: Any, **kwargs: Any) -> httpx.Response:
        response = real_send(self, *args, **kwargs)
        responses.append(response)
        return response

    monkeypatch.setattr(httpx.Client, "send", spy)

    payload = make_source_tar({f"src/f{i}.ts": noise(4096, seed=i) for i in range(200)})
    analysis = run(payload, client=client, settings=Settings(MAX_SOURCE_FILES=2))

    assert analysis.truncated is True
    assert responses and all(response.is_closed for response in responses)


@respx.mock
def test_a_tainted_caller_supplied_client_still_downloads_without_credentials() -> None:
    """`download_request` is not decoration, because the client is a parameter.

    `create_client` sets no Authorization default, so on the normal path
    building the request by hand would be indistinguishable — that is ADR-009
    working, and mutation testing confirms it. What makes the call load-bearing
    is that `analyze_repository` accepts a client it did not build. Here it is
    handed one with a credential baked in, and the codeload GET must still be
    bare.
    """
    routes = serve(make_source_tar({"a.ts": b"1"}))

    with httpx.Client(
        follow_redirects=False,
        trust_env=False,
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as tainted:
        analyze_repository(REPO, settings=SETTINGS, client=tainted)

    sent = routes["download"].calls.last.request
    assert "authorization" not in {key.lower() for key in sent.headers}
    assert TOKEN not in str(dict(sent.headers))


@respx.mock
@pytest.mark.parametrize("status", [200, 302, 404, 500], ids=["ok", "redirect", "missing", "error"])
def test_download_status_other_than_200_is_upstream_unavailable(
    client: httpx.Client, status: int
) -> None:
    """A second redirect is not part of the validated path, so it is not followed."""
    tarball = make_source_tar({"a.ts": b"1"})
    serve(
        tarball,
        download=httpx.Response(
            status,
            content=tarball if status == 200 else b"",
            headers={"Location": "https://evil.example/x"} if status == 302 else {},
        ),
    )

    if status == 200:
        assert analyze_repository(REPO, settings=SETTINGS, client=client).files
        return
    with pytest.raises(UpstreamUnavailableError):
        analyze_repository(REPO, settings=SETTINGS, client=client)


# --------------------------------------------------------------------------
# The deadline — one per request, and it is not preemption (ADR-010)
# --------------------------------------------------------------------------


def _spy_deadlines(monkeypatch: pytest.MonkeyPatch) -> list[Deadline]:
    """Record the Deadline object each stage is handed.

    Three stages take one as of ADR-021, not two: the archive reader, the parse,
    and route detection. `extract_imports` is deliberately absent — it no longer
    takes a deadline at all, because the check that used to sit in front of its
    query moved into `parse_source`, where it guards every query run over the
    tree rather than only that one.
    """
    seen: list[Deadline] = []
    real_iter = iter_source_files
    real_parse = parse_source
    real_detect = detect_routes

    def iter_spy(raw: Any, limits: Any, deadline: Deadline, info: Any = None) -> Any:
        seen.append(deadline)
        return real_iter(raw, limits, deadline, info)

    def parse_spy(
        source: Any, path: Any, language: Any, deadline: Deadline, settings: Any = None
    ) -> Any:
        seen.append(deadline)
        return real_parse(source, path, language, deadline, settings)

    def detect_spy(tree: Any, path: Any, deadline: Deadline, settings: Any = None) -> Any:
        seen.append(deadline)
        return real_detect(tree, path, deadline, settings)

    monkeypatch.setattr(pipeline_module, "iter_source_files", iter_spy)
    monkeypatch.setattr(pipeline_module, "parse_source", parse_spy)
    monkeypatch.setattr(pipeline_module, "detect_routes", detect_spy)
    return seen


@respx.mock
def test_exactly_one_deadline_is_constructed_and_shared(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same frozen object reaches both consumers.

    Identity, not equality: two `Deadline`s built a millisecond apart would
    compare unequal but a stage that built its own from `ANALYSIS_TIMEOUT_S`
    would still have awarded itself a fresh 60 seconds. The point of the frozen
    dataclass is that there is one budget for the request, so the test asserts
    there is one object.
    """
    seen = _spy_deadlines(monkeypatch)

    run(make_source_tar({"a.ts": b"1", "b.ts": b"2", "c.ts": b"3"}), client=client)

    # One from the archive reader, then a parse and a route detection per file.
    assert len(seen) == 7
    assert len({id(deadline) for deadline in seen}) == 1


@respx.mock
def test_expired_deadline_aborts_before_any_file_is_parsed(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy_deadlines(monkeypatch)

    with pytest.raises(AnalysisTimeoutError):
        run(
            make_source_tar({"a.ts": b"1", "b.ts": b"2"}),
            client=client,
            settings=Settings(ANALYSIS_TIMEOUT_S=0),
        )

    # The archive reader was handed the deadline and refused at its first
    # member; the parser was never reached.
    assert len(seen) == 1


@respx.mock
def test_deadline_stops_the_next_file_not_the_running_one(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-010's consequence, pinned so nobody reads the deadline as preemption.

    The clock is frozen until the first file is completely done with, then jumps
    past the budget. The first file is *not* interrupted — it completes, its
    imports are extracted and its routes detected — and the run aborts on the
    *second* file. There is no in-parse timeout in tree-sitter 0.26.0, so the
    granularity really is one whole file; a hostile file can still hold this
    thread for a few seconds after the budget is gone.

    The clock is advanced from a `detect_routes` spy because that is now the
    last thing done to a file's tree (ADR-021). Advancing it from the import
    spy instead would abort *inside* the first file, at route detection's own
    deadline check, and would therefore stop testing what this test is named
    for.
    """
    elapsed = {"jumped": False}
    # `app/analysis/deadline.py` does `import time` and calls `time.monotonic()`,
    # so patching the module attribute is what the Deadline actually reads.
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0 if elapsed["jumped"] else 0.0)

    parsed: list[str] = []
    real_detect = detect_routes

    def detect_spy(tree: Any, path: Any, deadline: Any, settings: Any = None) -> Any:
        result = list(real_detect(tree, path, deadline, settings))
        parsed.append(str(path))
        elapsed["jumped"] = True
        return iter(result)

    monkeypatch.setattr(pipeline_module, "detect_routes", detect_spy)

    with pytest.raises(AnalysisTimeoutError):
        run(
            make_source_tar({"a.ts": b"import './x';\n", "b.ts": b"import './y';\n"}),
            client=client,
            settings=Settings(ANALYSIS_TIMEOUT_S=60),
        )

    assert parsed == ["a.ts"]


# --------------------------------------------------------------------------
# The secret filter — TODO item 1's first half
# --------------------------------------------------------------------------

SECRET_FILES = {
    ".env": b"API_KEY=hunter2\n",
    ".env.production": b"API_KEY=hunter2\n",
    "certs/server.crt": b"-----BEGIN CERTIFICATE-----\n",
    "src/id_rsa.ts": b"import './leak';\n",
    "deploy/private.pem": b"-----BEGIN RSA PRIVATE KEY-----\n",
    "node_modules/left-pad/index.js": b"import './vendored';\n",
    "dist/bundle.js": b"import './generated';\n",
}


@respx.mock
def test_secret_and_excluded_paths_never_become_files(client: httpx.Client) -> None:
    analysis = run(make_source_tar({**SECRET_FILES, "src/app.ts": b"1"}), client=client)

    assert paths(analysis) == ["src/app.ts"]
    assert analysis.skipped[SKIP_FILTERED] == len(SECRET_FILES)


@respx.mock
def test_secret_paths_never_reach_the_parser(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filter runs before the grammar test, so hostile bytes are not parsed.

    The three paths here all have a supported extension, so an implementation
    that filtered *after* choosing a grammar would still have handed a vendored
    bundle and a file named `id_rsa.ts` to tree-sitter.

    Spying on `parse_source` rather than on `extract_imports` makes this the
    stronger claim it always meant to be: the filtered bytes never reach the
    *parser*, so no consumer of the tree — imports, routes, or whatever is added
    next — can see them either. Under the old arrangement a second reader could
    have been added with its own parse and this test would have stayed green.
    """
    seen: list[str] = []
    real_parse = parse_source

    def parse_spy(source: Any, path: Any, *args: Any) -> Any:
        seen.append(str(path))
        return real_parse(source, path, *args)

    monkeypatch.setattr(pipeline_module, "parse_source", parse_spy)

    run(
        make_source_tar(
            {
                "src/id_rsa.ts": b"1",
                "node_modules/left-pad/index.js": b"1",
                "dist/bundle.js": b"1",
                "src/app.ts": b"1",
            }
        ),
        client=client,
    )

    assert seen == ["src/app.ts"]


@respx.mock
def test_ordinary_source_that_a_sloppier_filter_would_eat_survives(
    client: httpx.Client,
) -> None:
    """The load-bearing half of the secret filter, asserted through the pipeline.

    `monkey.ts` dies to a substring search for `key`; `src/secrets.ts` dies if
    the exact-name rule becomes a prefix; `server.ts` dies if `server.*` is read
    literally; `src/build.ts` dies if the directory rules are applied to the
    final component.
    """
    allowed = ["monkey.ts", "keyboard.tsx", "src/secrets.ts", "server.ts", "src/build.ts"]
    analysis = run(make_source_tar(dict.fromkeys(allowed, b"1")), client=client)

    assert paths(analysis) == allowed
    assert SKIP_FILTERED not in analysis.skipped


# --------------------------------------------------------------------------
# Grammar selection — asserted by the import it costs to get it wrong
# --------------------------------------------------------------------------


@respx.mock
def test_ts_file_gets_the_typescript_grammar(client: httpx.Client) -> None:
    """`<T>expr` is a type assertion in .ts and an opening JSX tag in .tsx.

    Under the TSX grammar the phantom element swallows the rest of the file into
    an ERROR node and the second import is lost. This is why the split exists;
    the cost of getting it wrong is a missing dependency edge, not a crash.
    """
    source = b"import a from './a';\nconst x = <Foo>bar;\nimport b from './b';\n"
    analysis = run(make_source_tar({"src/cast.ts": source}), client=client)

    assert specifiers(analysis, "src/cast.ts") == ["./a", "./b"]


@respx.mock
def test_tsx_file_gets_the_tsx_grammar(client: httpx.Client) -> None:
    """The same failure in the other direction, so neither choice is arbitrary."""
    source = (
        b"import a from './a';\n"
        b'const el = <div className="x">hi</div>;\n'
        b"import b from './b';\n"
    )
    analysis = run(make_source_tar({"src/App.tsx": source}), client=client)

    assert specifiers(analysis, "src/App.tsx") == ["./a", "./b"]


SUPPORTED = [
    ("src/a.ts", TYPESCRIPT),
    ("src/a.tsx", TYPESCRIPT),
    ("src/a.mts", TYPESCRIPT),
    ("src/a.cts", TYPESCRIPT),
    ("src/a.js", JAVASCRIPT),
    ("src/a.jsx", JAVASCRIPT),
    ("src/a.mjs", JAVASCRIPT),
    ("src/a.cjs", JAVASCRIPT),
]


@respx.mock
@pytest.mark.parametrize(("path", "language"), SUPPORTED, ids=[p for p, _ in SUPPORTED])
def test_every_supported_extension_is_parsed_and_labelled(
    client: httpx.Client, path: str, language: str
) -> None:
    analysis = run(make_source_tar({path: b"import './dep';\n"}), client=client)

    assert paths(analysis) == [path]
    assert analysis.files[0].language == language
    assert specifiers(analysis, path) == ["./dep"]


UNSUPPORTED = [
    "package.json",
    "README.md",
    "styles.css",
    "script.py",
    "logo.svg",
    # Neighbours of the supported set, so widening it stays deliberate: `.cts`
    # and `.mts` joined it on 2026-09-01 and these did not.
    "src/a.ets",
    "src/a.ts.bak",
    # No extension at all.
    "Makefile",
]


@respx.mock
@pytest.mark.parametrize("path", UNSUPPORTED, ids=UNSUPPORTED)
def test_unsupported_extensions_are_skipped_and_counted(client: httpx.Client, path: str) -> None:
    analysis = run(make_source_tar({path: b"import './dep';\n", "src/a.ts": b"1"}), client=client)

    assert paths(analysis) == ["src/a.ts"]
    assert analysis.skipped[SKIP_UNSUPPORTED] == 1


@respx.mock
def test_extension_matching_is_case_insensitive(client: httpx.Client) -> None:
    """A repository is case-preserving; the machine that produced it often is not."""
    analysis = run(
        make_source_tar({"src/A.TS": b"import './a';\n", "src/B.JsX": b"import './b';\n"}),
        client=client,
    )

    assert paths(analysis) == ["src/A.TS", "src/B.JsX"]


@respx.mock
def test_extension_matching_does_not_case_fold(client: httpx.Client) -> None:
    """`.lower()`, not `.casefold()` — the one place the narrower fold is safer.

    U+017F LATIN SMALL LETTER LONG S casefolds to "s" but lowercases to itself,
    so under `casefold` a file whose extension is ".t" + U+017F would be handed
    to the TypeScript grammar. Everywhere else in this project the widening fold
    is correct because it widens a *rejection*; here it would widen an
    *acceptance*, which is the direction that lets untrusted bytes into the
    parser.
    """
    # Written as an escape, not a literal: the character is near-indistinguishable
    # from an "f" in a diff, which is the whole premise. Same convention as the
    # fullwidth homoglyphs in tests/test_archive.py.
    long_s = "src/a.t\u017f"
    assert long_s.casefold() == "src/a.ts" and long_s.lower() != "src/a.ts"
    analysis = run(
        make_source_tar({long_s: b"import './a';\n", "src/real.ts": b"1"}),
        client=client,
    )

    assert paths(analysis) == ["src/real.ts"]
    assert analysis.skipped[SKIP_UNSUPPORTED] == 1


# --------------------------------------------------------------------------
# MAX_SOURCE_FILES — the parse cap, which is not the archive's member cap
# --------------------------------------------------------------------------


@respx.mock
def test_source_file_cap_truncates_and_says_so(client: httpx.Client) -> None:
    analysis = run(
        make_source_tar({f"src/f{i}.ts": b"1" for i in range(5)}),
        client=client,
        settings=Settings(MAX_SOURCE_FILES=2),
    )

    assert len(analysis.files) == 2
    assert analysis.truncated is True


@respx.mock
def test_truncated_is_false_when_everything_fits(client: httpx.Client) -> None:
    analysis = run(
        make_source_tar({f"src/f{i}.ts": b"1" for i in range(3)}),
        client=client,
        settings=Settings(MAX_SOURCE_FILES=3),
    )

    assert len(analysis.files) == 3
    assert analysis.truncated is False


@respx.mock
def test_source_file_cap_counts_files_after_filtering(client: httpx.Client) -> None:
    """3000 PNGs must not exhaust the budget for source.

    The cap is checked after the secret filter and the extension test, so it
    bounds the files that would become nodes rather than the members the
    archive happened to contain.
    """
    members = {f"assets/img{i}.png": b"\x89PNG" for i in range(10)}
    members[".env"] = b"SECRET=1\n"
    members["src/one.ts"] = b"import './x';\n"
    members["src/two.ts"] = b"import './y';\n"

    analysis = run(make_source_tar(members), client=client, settings=Settings(MAX_SOURCE_FILES=2))

    assert paths(analysis) == ["src/one.ts", "src/two.ts"]
    assert analysis.truncated is False


@respx.mock
def test_source_file_cap_is_not_the_archive_member_cap(client: httpx.Client) -> None:
    """Two different limits at two different layers; neither stands in for the other.

    `MAX_ARCHIVE_MEMBERS` is a statement about the tarball, enforced before
    anything is filtered, and exceeding it rejects the whole archive.
    `MAX_SOURCE_FILES` is a statement about how much we will parse, and
    exceeding it truncates. A test that only checked "too many files is
    refused" would not notice one being wired to the other.
    """
    tarball = make_source_tar({f"src/f{i}.ts": b"1" for i in range(5)})

    # Past the member cap: rejected outright, even with parse headroom to spare.
    with pytest.raises(ArchiveRejectedError):
        run(
            tarball,
            client=client,
            settings=Settings(MAX_ARCHIVE_MEMBERS=3, MAX_SOURCE_FILES=3000),
        )

    # Past the parse cap only: truncated, and not an error.
    respx.reset()
    analysis = run(
        tarball,
        client=client,
        settings=Settings(MAX_ARCHIVE_MEMBERS=50_000, MAX_SOURCE_FILES=3),
    )
    assert len(analysis.files) == 3
    assert analysis.truncated is True


# --------------------------------------------------------------------------
# MAX_IMPORTS — the bound on the phase that has no clock (ADR-019)
# --------------------------------------------------------------------------
#
# `resolve_imports` runs after this module has spent the whole 60 s budget and
# takes no `Deadline`, so the only thing bounding it is how many imports leave
# `_analyze`. `MAX_SOURCE_FILES` does not bound that — it caps files, and one
# file can hold tens of thousands of imports. These tests exercise the cap at
# three digits; the measurement that motivated it (1 002 000 imports, 78.7 s)
# is a benchmark and deliberately not a test.


def many_imports(count: int) -> bytes:
    """One file carrying ``count`` unresolvable relative imports.

    Unresolvable and relative on purpose: that is the worst case the cap is
    sized against — each one tries every candidate extension before failing,
    ~65x the cost of resolving a bare package name.
    """
    return b"".join(b"import './missing%d';\n" % i for i in range(count))


@respx.mock
def test_import_cap_truncates_and_says_so(client: httpx.Client) -> None:
    analysis = run(
        make_source_tar({"src/many.ts": many_imports(10_000)}),
        client=client,
        settings=Settings(MAX_IMPORTS=500),
    )

    assert analysis.import_count == 500
    assert analysis.truncated is True
    assert analysis.imports_truncated is True


@respx.mock
def test_both_flags_are_false_when_every_import_fits(client: httpx.Client) -> None:
    analysis = run(
        make_source_tar({"src/many.ts": many_imports(50)}),
        client=client,
        settings=Settings(MAX_IMPORTS=500),
    )

    assert analysis.import_count == 50
    assert analysis.truncated is False
    assert analysis.imports_truncated is False


@respx.mock
def test_import_cap_stops_mid_file_and_keeps_the_partial_file(client: httpx.Client) -> None:
    """A single file can exceed the cap by itself, so the stop lands inside one.

    The file stays in the analysis with the imports collected so far. Dropping
    it would delete a node other files may legitimately import — and its bytes
    and line count are not made untrue by a short import list. `truncated` is
    what says the list is short.
    """
    analysis = run(
        make_source_tar({"src/many.ts": many_imports(10_000)}),
        client=client,
        settings=Settings(MAX_IMPORTS=7),
    )

    assert paths(analysis) == ["src/many.ts"]
    assert len(analysis.files[0].imports) == 7
    # The file itself is reported honestly; only the imports were cut.
    assert analysis.files[0].loc == 10_000
    assert analysis.imports_truncated is True


@respx.mock
def test_import_cap_abandons_the_rest_of_the_repository(client: httpx.Client) -> None:
    """The whole point: reaching the cap stops parsing, it does not merely trim.

    A cap that kept parsing and discarded the overflow would leave every later
    file's parse cost on the clock for nothing. Breaking abandons the generator
    and therefore the download, exactly as `MAX_SOURCE_FILES` does.
    """
    analysis = run(
        make_source_tar(
            {
                "src/a.ts": many_imports(10_000),
                "src/b.ts": b"import './a';\n",
                "src/c.ts": b"import './a';\n",
            }
        ),
        client=client,
        settings=Settings(MAX_IMPORTS=100),
    )

    assert paths(analysis) == ["src/a.ts"]
    assert analysis.import_count == 100


@respx.mock
def test_import_cap_is_a_total_across_files_not_a_per_file_limit(client: httpx.Client) -> None:
    """Four imports each over five files is twenty imports, not four.

    A per-file reading of the cap would let 3000 files carry 3000 times it,
    which is the exact hole this closes.
    """
    analysis = run(
        make_source_tar({f"src/f{i}.ts": many_imports(4) for i in range(5)}),
        client=client,
        settings=Settings(MAX_IMPORTS=10),
    )

    assert analysis.import_count == 10
    assert paths(analysis) == ["src/f0.ts", "src/f1.ts", "src/f2.ts"]
    assert analysis.imports_truncated is True


@respx.mock
def test_the_import_cap_is_not_recorded_as_a_file_skip(client: httpx.Client) -> None:
    """`skipped` counts files that produced no node; the capped file produced one.

    Folding an import-cap marker in there would inflate `skippedFiles` with
    something that is not a file skip — the mistake `_NON_FILE_SKIPS` already
    exists to prevent for directory entries.
    """
    analysis = run(
        make_source_tar({"src/many.ts": many_imports(10_000), "README.md": b"# hi\n"}),
        client=client,
        settings=Settings(MAX_IMPORTS=5),
    )

    assert analysis.imports_truncated is True
    assert set(analysis.skipped) <= {SKIP_FILTERED, SKIP_UNSUPPORTED}
    assert analysis.skipped_files == analysis.skipped.get(SKIP_UNSUPPORTED, 0)


@respx.mock
def test_the_file_cap_and_the_import_cap_are_distinguishable(client: httpx.Client) -> None:
    """Both set `truncated`; only one sets `imports_truncated`.

    They have different consequences downstream — the file cap drops whole
    files off the end of archive order, the import cap can also leave the last
    file present with a partial import list — so a consumer that cannot tell
    them apart cannot describe what it is showing.
    """
    tarball = make_source_tar({f"src/f{i}.ts": many_imports(3) for i in range(5)})

    by_files = run(tarball, client=client, settings=Settings(MAX_SOURCE_FILES=2))
    assert by_files.truncated is True
    assert by_files.imports_truncated is False

    respx.reset()
    by_imports = run(tarball, client=client, settings=Settings(MAX_IMPORTS=4))
    assert by_imports.truncated is True
    assert by_imports.imports_truncated is True


@respx.mock
def test_import_count_is_what_the_resolver_will_be_handed(client: httpx.Client) -> None:
    """`import_count` is derived, not stored, so it cannot drift from `files`.

    It is also the length of the sequence `resolve_imports` returns — the
    number the cap is actually about.
    """
    analysis = run(
        make_source_tar({"src/a.ts": many_imports(6), "src/b.ts": many_imports(4)}),
        client=client,
    )

    assert analysis.import_count == 10
    assert analysis.import_count == sum(len(f.imports) for f in analysis.files)


# --------------------------------------------------------------------------
# Skip counting — the tally nothing below this module could keep
# --------------------------------------------------------------------------


@respx.mock
def test_archive_skips_are_folded_into_the_counts(client: httpx.Client) -> None:
    """`archive.py` computed these and only logged them; the pipeline publishes them."""
    payload = make_tar(
        [
            TarMember(name=ROOT, type=tarfile.DIRTYPE, mode=0o755),
            TarMember(name=f"{ROOT}/src", type=tarfile.DIRTYPE, mode=0o755),
            TarMember(name=f"{ROOT}/src/app.ts", data=b"import './a';\n"),
            TarMember(name=f"{ROOT}/src/link.ts", type=tarfile.SYMTYPE, linkname="/etc/passwd"),
            TarMember(name=f"{ROOT}/src/huge.ts", data=b"x" * 4096),
            TarMember(name=f"{ROOT}/README.md", data=b"#\n"),
            TarMember(name=f"{ROOT}/.env", data=b"K=v\n"),
        ]
    )

    analysis = run(payload, client=client, settings=Settings(MAX_MEMBER_BYTES=1024))

    assert paths(analysis) == ["src/app.ts"]
    assert analysis.skipped == {
        "symlink": 1,
        "member_size": 1,
        SKIP_UNSUPPORTED: 1,
        SKIP_FILTERED: 1,
    }
    assert analysis.skipped_files == 4


@respx.mock
def test_directory_entries_are_not_counted_as_skipped_files(client: httpx.Client) -> None:
    """A tarball has a member per directory; they are not files that went missing."""
    payload = make_tar(
        [
            TarMember(name=ROOT, type=tarfile.DIRTYPE, mode=0o755),
            TarMember(name=f"{ROOT}/a", type=tarfile.DIRTYPE, mode=0o755),
            TarMember(name=f"{ROOT}/a/b", type=tarfile.DIRTYPE, mode=0o755),
            TarMember(name=f"{ROOT}/a/b/app.ts", data=b"1"),
        ]
    )

    analysis = run(payload, client=client)

    assert paths(analysis) == ["a/b/app.ts"]
    assert "directory" not in analysis.skipped
    assert analysis.skipped_files == 0


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


@respx.mock
def test_a_repository_with_no_supported_files_is_refused(client: httpx.Client) -> None:
    with pytest.raises(NoSupportedFilesError):
        run(make_source_tar({"README.md": b"#\n", "go.mod": b"module x\n"}), client=client)


@respx.mock
def test_a_repository_of_only_secrets_is_refused(client: httpx.Client) -> None:
    with pytest.raises(NoSupportedFilesError):
        run(make_source_tar(SECRET_FILES), client=client)


@respx.mock
def test_private_repository_is_refused_as_not_found(client: httpx.Client) -> None:
    """A token is for rate limits, not for reach.

    `get_repo_metadata` returns 200 for a private repository the configured
    token can see, so the 403/404 collapse that closes the *existence* oracle
    does not close this. Without the check, an operator who set GITHUB_TOKEN
    turns the service into a proxy that renders any repository that token can
    read. Refused with the same opaque error, so the two stay indistinguishable.
    """
    routes = serve(make_source_tar({"a.ts": b"1"}), payload=repo_payload(private=True))

    with pytest.raises(RepositoryNotFoundError):
        analyze_repository(REPO, settings=Settings(GITHUB_TOKEN=SecretStr(TOKEN)), client=client)

    # Refused on the preflight: no archive byte moves.
    assert not routes["tarball"].called
    assert not routes["download"].called


@respx.mock
def test_hostile_archive_still_rejects_through_the_pipeline(client: httpx.Client) -> None:
    """The reader's guarantees survive being wired to a real download."""
    payload = make_tar([TarMember(name=f"{ROOT}/../../etc/passwd", data=b"root:x:0:0\n")])

    with pytest.raises(ArchiveRejectedError):
        run(payload, client=client)


# --------------------------------------------------------------------------
# The output contract (ADR-016)
# --------------------------------------------------------------------------


@respx.mock
def test_specifiers_are_reported_exactly_as_written(client: httpx.Client) -> None:
    """No resolution happens here, by design — that is the resolver's job."""
    source = (
        b"import a from './a';\n"
        b"import b from '../shared/b';\n"
        b"import c from 'react';\n"
        b"import d from '@scope/pkg/sub';\n"
        b"import e from 'node:fs';\n"
    )
    analysis = run(make_source_tar({"src/index.ts": source}), client=client)

    assert specifiers(analysis, "src/index.ts") == [
        "../shared/b",
        "./a",
        "@scope/pkg/sub",
        "node:fs",
        "react",
    ]


@respx.mock
def test_import_lines_are_zero_indexed(client: httpx.Client) -> None:
    source = b"// header\n\nimport a from './a';\n"
    analysis = run(make_source_tar({"src/index.ts": source}), client=client)

    assert analysis.files[0].imports[0].line == 2


LOC_CASES = [
    (b"", 0),
    (b"one\n", 1),
    (b"one", 1),
    (b"one\ntwo\n", 2),
    (b"one\ntwo", 2),
    (b"\n\n\n", 3),
]


@respx.mock
@pytest.mark.parametrize(("content", "loc"), LOC_CASES, ids=[repr(c) for c, _ in LOC_CASES])
def test_loc_counts_a_final_line_without_a_trailing_newline(
    client: httpx.Client, content: bytes, loc: int
) -> None:
    analysis = run(make_source_tar({"src/a.ts": content}), client=client)

    assert analysis.files[0].loc == loc
    assert analysis.files[0].size_bytes == len(content)


@respx.mock
def test_files_are_returned_in_archive_order(client: httpx.Client) -> None:
    """Sorting belongs to the graph builder (docs/ARCHITECTURE.md)."""
    names = ["src/z.ts", "src/a.ts", "src/m/q.ts", "src/b.ts"]
    payload = make_tar([TarMember(name=f"{ROOT}/{name}", data=b"1") for name in names])

    assert paths(run(payload, client=client)) == names


@respx.mock
def test_the_analysis_carries_no_file_content(client: httpx.Client) -> None:
    """ADR-003 at the seam: the bytes stop here.

    `loc` and `size_bytes` are measured while the content is in hand precisely
    so that nothing downstream needs to hold it. A field that carried bytes
    would make peak memory the size of the repository again.
    """
    analysis = run(make_source_tar(REALISTIC), client=client)

    for source in analysis.files:
        for field in fields(source):
            assert not isinstance(getattr(source, field.name), bytes | bytearray)


@respx.mock
def test_the_only_repository_text_carried_is_a_bounded_description(
    client: httpx.Client,
) -> None:
    """The companion the test above needs once `description` exists (ADR-020).

    A type check cannot make ADR-016's argument on its own: the invariant is
    "nothing on this record scales with the size of a file", and a `str` would
    satisfy `not isinstance(..., bytes)` while holding a whole megabyte source
    file. What actually holds the line is the cap applied at extraction, so that
    is what is asserted — against a header comment far longer than the file it
    would have to describe.
    """
    limit = SETTINGS.MAX_DESCRIPTION_CHARS
    header = b"/** " + b"a" * (limit * 20) + b" */\n"
    analysis = run(
        make_source_tar({"src/a.ts": header + b"import './b';\n", "src/b.ts": b"export {};\n"}),
        client=client,
    )

    described = next(f for f in analysis.files if str(f.path) == "src/a.ts")
    assert described.description == "a" * limit
    assert all(
        len(f.description or "") <= limit for f in analysis.files
    ), "a description is bounded by a constant, not by the size of its file"


@respx.mock
def test_a_file_the_parser_gives_up_on_is_still_a_node(client: httpx.Client) -> None:
    """The documented blind spot at this seam, pinned rather than hidden.

    `extract_imports` reports a skip by yielding nothing, so the pipeline cannot
    tell "no imports" from "not parsed". A binary or oversized file therefore
    stays in the graph with its real bytes and lines and zero imports, which is
    honest — it is a real file — but it means parser-level skips are absent from
    `skipped`. See docs/CURRENT_STATE.md.
    """
    binary = b"\x00\x01\x02\x03" * 64
    analysis = run(
        make_source_tar({"src/blob.ts": binary, "src/real.ts": b"import './a';\n"}),
        client=client,
    )

    blob = next(f for f in analysis.files if str(f.path) == "src/blob.ts")
    assert blob.imports == ()
    assert blob.size_bytes == len(binary)
    assert analysis.skipped_files == 0


# --------------------------------------------------------------------------
# Descriptions (ADR-013, ADR-020) — the first repository text in a response
# --------------------------------------------------------------------------

DESCRIBED = {
    "src/jsdoc.ts": b"/**\n * The user store.\n */\nexport const s = 1;\n",
    "src/block.ts": b"/* Plain block. */\nexport const b = 2;\n",
    "src/line.js": b"// One.\n// Two.\nconst c = 3;\n",
    "src/bare.ts": b"export const d = 4;\n",
}


@respx.mock
def test_each_comment_form_reaches_the_analysis(client: httpx.Client) -> None:
    """The extractor's three forms, through the whole ingestion path.

    `tests/test_descriptions.py` proves the forms against bytes it constructed.
    This proves the pipeline actually calls it, on bytes that came off a
    tarball — the same distinction `test_secret_paths_never_reach_the_parser`
    draws between a rule existing and a rule running.
    """
    analysis = run(make_source_tar(DESCRIBED), client=client)
    described = {str(f.path): f.description for f in analysis.files}

    assert described == {
        "src/jsdoc.ts": "The user store.",
        "src/block.ts": "Plain block.",
        "src/line.js": "One. Two.",
        "src/bare.ts": None,
    }


@respx.mock
def test_a_secret_file_never_produces_a_description(client: httpx.Client) -> None:
    """Structurally, not by a second check (docs/SECURITY.md).

    `.env.ts` is a real TypeScript file by extension and its header comment is a
    perfectly good one. It produces no description because `is_secret_path` runs
    first and it produces no *record at all* — which is the stronger property,
    and the one that keeps holding if the extractor is ever moved.
    """
    analysis = run(
        make_source_tar(
            {
                ".env.ts": b"/** Production credentials. */\nexport const KEY = 'sk-live';\n",
                "src/ok.ts": b"/** Ordinary. */\nexport const x = 1;\n",
            }
        ),
        client=client,
    )

    assert paths(analysis) == ["src/ok.ts"]
    assert analysis.skipped[SKIP_FILTERED] == 1
    assert all("credential" not in (f.description or "").lower() for f in analysis.files)


@respx.mock
def test_a_non_utf8_file_is_described_without_raising(client: httpx.Client) -> None:
    """The parser survives undecodable bytes; the extractor must too."""
    latin1 = "/** Café module. */\n".encode("latin-1") + b"export const x = 1;\n"

    analysis = run(make_source_tar({"src/legacy.ts": latin1}), client=client)

    description = analysis.files[0].description
    assert description is not None
    assert description.startswith("Caf") and description.endswith("module.")


@respx.mock
def test_control_characters_in_a_comment_never_reach_the_analysis(
    client: httpx.Client,
) -> None:
    """Terminal/log injection and the XSS row's sibling, asserted end to end."""
    hostile = b"/**\n * be\x00fore\x1b[2J after\n */\nexport const x = 1;\n"

    analysis = run(make_source_tar({"src/a.ts": hostile}), client=client)

    description = analysis.files[0].description
    assert description is not None
    assert not any(char < " " or char == "\x7f" for char in description)
    assert "\x00" not in description


@respx.mock
def test_a_file_the_parser_gives_up_on_still_gets_its_description(
    client: httpx.Client,
) -> None:
    """The extractor takes no tree, so a parser skip does not cost a description.

    A consequence of ADR-020 worth pinning rather than discovering: this file is
    condemned by the binary sniff and contributes no imports, but its header
    comment is real and the node is real, so the description is too.
    """
    content = b"/** Generated blob. */\n" + b"\x00\x01\x02\x03" * 64

    analysis = run(make_source_tar({"src/blob.ts": content}), client=client)

    assert analysis.files[0].imports == ()
    assert analysis.files[0].description == "Generated blob."


@respx.mock
def test_a_description_survives_the_import_cap(client: httpx.Client) -> None:
    """The partially-read file is kept (ADR-019), and it is kept whole."""
    settings = Settings(MAX_IMPORTS=1)
    content = b"/** Still described. */\nimport './a';\nimport './b';\n"

    analysis = run(make_source_tar({"src/a.ts": content}), client=client, settings=settings)

    assert analysis.imports_truncated is True
    assert analysis.files[0].description == "Still described."


@respx.mock
def test_no_description_reaches_the_logs(
    client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    """Descriptions are repository content, so the rule that covers source code
    and specifiers covers them (docs/SECURITY.md, "Source code in logs")."""
    marker = "zzmarkerdescriptionzz"
    content = f"/** {marker} */\nexport const x = 1;\n".encode()

    with caplog.at_level(logging.DEBUG):
        analysis = run(make_source_tar({"src/a.ts": content}), client=client)

    assert analysis.files[0].description == marker
    assert marker not in caplog.text


# --------------------------------------------------------------------------
# Routes (ADR-021) — the second reader of the tree
# --------------------------------------------------------------------------

SERVER = b"""\
const app = express();
/** List every user. */
app.get('/users', listUsers);
app.post('/users', createUser);
"""


@respx.mock
def test_routes_reach_the_analysis(client: httpx.Client) -> None:
    """The join, for route detection: a tarball in, a service map out."""
    analysis = run(make_source_tar({"src/server.ts": SERVER}), client=client)

    assert [(e.method, e.path, e.file) for e in analysis.service_map] == [
        ("GET", "/users", "src/server.ts"),
        ("POST", "/users", "src/server.ts"),
    ]
    assert analysis.service_map[0].summary == "List every user."
    assert analysis.service_map[1].summary is None
    assert analysis.routes_truncated is False


@respx.mock
def test_the_service_map_spans_files_in_archive_order(client: httpx.Client) -> None:
    """`service_map` flattens per-file routes, so its order is archive order —
    which groups a file's endpoints together, the way a reader wants them."""
    analysis = run(
        make_source_tar(
            {
                "src/a.ts": b"app.get('/a', h);\n",
                "src/b.ts": b"app.get('/b', h);\napp.post('/b', h);\n",
            }
        ),
        client=client,
    )

    assert [e.path for e in analysis.service_map] == ["/a", "/b", "/b"]


@respx.mock
def test_a_file_with_no_routes_carries_none(client: httpx.Client) -> None:
    """The ordinary case. Almost every file in a repository declares no route,
    which is why the field defaults rather than being required."""
    analysis = run(make_source_tar({"src/a.ts": b"export const a = 1;\n"}), client=client)

    assert analysis.files[0].routes == ()
    assert analysis.service_map == ()


@respx.mock
def test_a_secret_file_never_produces_a_route(client: httpx.Client) -> None:
    """`is_secret_path` runs before the parse, so a filtered file has no tree
    and therefore no routes — a property of the ordering, not a second check.

    The companion of `test_a_secret_file_never_produces_a_description`. Both
    now rest on the same fact: nothing filtered reaches `parse_source`.
    """
    analysis = run(
        make_source_tar(
            {
                "node_modules/express/lib/app.js": b"app.get('/vendored', h);\n",
                ".env.ts": b"app.get('/leaked', h);\n",
                "src/app.ts": b"app.get('/real', h);\n",
            }
        ),
        client=client,
    )

    assert [e.path for e in analysis.service_map] == ["/real"]


@respx.mock
def test_a_file_the_parser_gives_up_on_declares_no_routes(client: httpx.Client) -> None:
    """No tree means no routes, and the file is still a node.

    This is the coupling ADR-020 deliberately avoided for descriptions and
    deliberately accepts here: a description is at byte 0 and needs no parse, a
    route can only be located in a tree. So a binary file keeps its description
    and loses its routes, which is the honest outcome for both.
    """
    binary = b"\x00\x01\x02" + b"app.get('/x', h);\n"

    analysis = run(make_source_tar({"src/blob.ts": binary}), client=client)

    assert paths(analysis) == ["src/blob.ts"]
    assert analysis.files[0].routes == ()
    assert analysis.files[0].size_bytes == len(binary)


@respx.mock
def test_the_endpoint_cap_stops_collection_without_truncating_the_analysis(
    client: httpx.Client,
) -> None:
    """`MAX_SERVICE_ENDPOINTS` is not `MAX_IMPORTS` (ADR-021).

    Both cap a running total, but the import cap abandons the download and sets
    `truncated`, because resolution downstream has no clock. This one only stops
    adding to the service map: the graph is unaffected and still complete, so
    reporting the whole analysis as truncated would overstate what was lost.
    Every file is still analyzed and every import still collected.
    """
    settings = Settings(MAX_SERVICE_ENDPOINTS=2)
    content = b"import './x';\napp.get('/a', h);\napp.get('/b', h);\napp.get('/c', h);\n"

    analysis = run(
        make_source_tar({"src/a.ts": content, "src/b.ts": content}),
        client=client,
        settings=settings,
    )

    assert len(analysis.service_map) == 2
    assert analysis.routes_truncated is True
    # The cap fired, and yet nothing else was cut short.
    assert analysis.truncated is False
    assert analysis.imports_truncated is False
    assert paths(analysis) == ["src/a.ts", "src/b.ts"]
    assert analysis.import_count == 2


@respx.mock
def test_the_endpoint_cap_is_counted_across_files_not_per_file(
    client: httpx.Client,
) -> None:
    """One file can declare tens of thousands of routes, so the budget is a
    running total — the same shape as `MAX_IMPORTS` and for the same reason."""
    settings = Settings(MAX_SERVICE_ENDPOINTS=3)
    two = b"app.get('/a', h);\napp.post('/b', h);\n"

    analysis = run(
        make_source_tar({"src/a.ts": two, "src/b.ts": two}), client=client, settings=settings
    )

    assert len(analysis.service_map) == 3
    assert analysis.routes_truncated is True


@respx.mock
def test_a_capped_service_map_still_builds_a_response(client: httpx.Client) -> None:
    """The cap is the wire model's own bound, so hitting it here means
    `AnalyzeResponse` validation can never be what fails a request."""
    settings = Settings(MAX_SERVICE_ENDPOINTS=2)
    content = b"app.get('/a', h);\napp.get('/b', h);\napp.get('/c', h);\n"

    analysis = run(make_source_tar({"src/a.ts": content}), client=client, settings=settings)

    assert len(list(analysis.service_map)) <= settings.MAX_SERVICE_ENDPOINTS


@respx.mock
def test_no_route_path_reaches_the_logs(
    client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    """A route path is repository-authored input, like a specifier and a
    description before it."""
    marker = "zzmarkerroutezz"
    content = f"app.get('/{marker}', h);\n".encode()

    with caplog.at_level(logging.DEBUG):
        analysis = run(make_source_tar({"src/a.ts": content}), client=client)

    assert analysis.service_map[0].path == f"/{marker}"
    assert marker not in caplog.text
