"""The HTTP boundary: `POST /api/analyze`, `GET /api/health`, and every error body.

This is the last seam in the backend, and like `tests/test_pipeline.py` it tests
the *join* rather than the parts. The application is driven through
`httpx.ASGITransport` — in process, no socket, and no `starlette.testclient`,
whose httpx integration is deprecated in this Starlette and would trip
``filterwarnings = ["error"]``. Below the app everything is real: the real
pipeline, the real resolver, the real graph builder, with only the GitHub
transport swapped by respx.

Three things carry most of the weight here, because they are the three things
that exist nowhere else in the suite.

*The error contract, applied.* `app/errors.py` has been a frozen shape with no
producer since the contract layer. Every rejection below is asserted to have
exactly the three keys, the static message, and no fragment of what the client
sent — including the pydantic case, where the default handler would have echoed
the offending input verbatim (docs/SECURITY.md, "Pydantic validation echoing
user input").

*The request body cap.* `MAX_REQUEST_BODY_BYTES` has been a constant that
nothing read. Both halves are exercised: a declared `Content-Length` over the
cap, and a chunked body that declares nothing and is caught by counting. Both
assert the application never ran, because a cap that fires after the body
reached application code is not the control the threat model describes.

*`MAX_NODES` / `MAX_EDGES`, and the stats that have to survive them.* The point
of these tests is not that the graph got smaller — it is that every number in
the response still describes the graph in the response. `stats.dependencies ==
len(edges)`, `sum(node.imports) == sum(node.importedBy) == stats.dependencies`,
`root.fileCount == stats.files`, `sum(node.externalImports) ==
stats.externalImports`, no edge naming a node that is not there, and no node
naming a parent that is not there. A naive slice of the builder's output passes
none of them (ADR-018, ADR-023).
"""

import logging
import socket
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from app.api.app import create_app
from app.config import Settings
from app.errors import ErrorCode
from app.fetch.github import GITHUB_API_ROOT
from tests.fixtures.tarballs import make_source_tar

OWNER = "acme"
NAME = "widgets"
SHA = "a1b2c3d"  # the SHA carried by tests.fixtures.tarballs.ROOT
REPO_URL = f"{GITHUB_API_ROOT}/repos/{OWNER}/{NAME}"
CODELOAD = f"https://codeload.github.com/{OWNER}/{NAME}/legacy.tar.gz/refs/heads/main"
SUBMITTED_URL = f"https://github.com/{OWNER}/{NAME}"

PUBLIC_IP = "140.82.121.4"

ANALYZE = "/api/analyze"
HEALTH = "/api/health"


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def resolves_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every name resolves to one globally routable address.

    Autouse for `tests/test_pipeline.py`'s reason: `assert_public_ip` runs on the
    redirect, and without this the suite-wide network block fires instead, so
    every failure would look like an SSRF rejection.
    """

    def fake_getaddrinfo(*args: object, **kwargs: object) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture
def app() -> FastAPI:
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Drives the real ASGI application in process. Opens no socket."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


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


def serve(tarball: bytes) -> None:
    """Mock the whole three-request conversation: preflight, redirect, download."""
    respx.get(REPO_URL).mock(return_value=httpx.Response(200, json=repo_payload()))
    respx.get(f"{REPO_URL}/tarball/main").mock(
        return_value=httpx.Response(302, headers={"Location": CODELOAD})
    )
    respx.get(CODELOAD).mock(return_value=httpx.Response(200, content=tarball))


def use_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    """Tighten a limit for one test, the way `tests/test_models.py` does."""
    monkeypatch.setattr(
        "app.api.routes.get_settings", lambda: Settings(**overrides), raising=True
    )


def error_of(response: httpx.Response) -> dict[str, str]:
    """The error body, asserted to have exactly the three contracted keys."""
    body = response.json()
    assert set(body) == {"error"}
    detail = body["error"]
    assert set(detail) == {"code", "message", "requestId"}
    assert detail["requestId"]
    return dict(detail)


REALISTIC = {
    "package.json": b'{"name":"widgets"}',
    ".env": b"SECRET=hunter2\n",
    "src/index.ts": (
        b"/** The entry point. */\n"
        b"import { a } from './a';\n"
        b"import express from 'express';\n"
        b"const app = express();\n"
        b"// Health probe.\n"
        b"app.get('/healthz', (req, res) => res.send('ok'));\n"
    ),
    "src/a.ts": b"export const a = 1;\n",
}


# A chain: aN imports a(N+1), and every file also imports one external package.
CHAIN_LENGTH = 10


def chain_repository() -> bytes:
    files: dict[str, bytes] = {}
    for index in range(CHAIN_LENGTH):
        body = b"import 'react';\n"
        if index + 1 < CHAIN_LENGTH:
            body += f"import './a{index + 1}';\n".encode()
        files[f"src/a{index}.ts"] = body
    return make_source_tar(files)


def node_map(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["id"]: node for node in body["nodes"]}


def assert_internally_consistent(body: dict[str, Any]) -> None:
    """Every number in the response describes the graph in the response.

    This is the whole point of ADR-023: capping is allowed to make the graph
    smaller and is not allowed to make any of these false. Each assertion fails
    under a naive slice of `build_graph`'s output.
    """
    nodes = body["nodes"]
    edges = body["edges"]
    stats = body["stats"]
    by_id = node_map(body)

    assert len(by_id) == len(nodes), "node ids are unique"
    assert stats["dependencies"] == len(edges)
    assert stats["files"] == sum(1 for n in nodes if n["type"] == "file")
    assert stats["directories"] == sum(1 for n in nodes if n["type"] == "directory")

    files = [n for n in nodes if n["type"] == "file"]
    assert sum(n["imports"] for n in files) == stats["dependencies"]
    assert sum(n["importedBy"] for n in files) == stats["dependencies"]
    assert sum(n["externalImports"] for n in files) == stats["externalImports"]
    assert sum(n["unresolvedImports"] for n in files) == stats["unresolvedImports"]

    for edge in edges:
        assert edge["source"] in by_id, "an edge names a node that is not here"
        assert edge["target"] in by_id, "an edge names a node that is not here"
        assert edge["source"] != edge["target"]

    seen: set[str] = set()
    for node in nodes:
        if node["parent"] is None:
            assert node["id"] == "."
        else:
            assert node["parent"] in by_id, "a node names a parent that is not here"
            assert node["parent"] in seen, "a parent must precede its children"
        seen.add(node["id"])

    root = by_id.get(".")
    if root is not None:
        assert root["fileCount"] == stats["files"]

    outbound: dict[str, int] = {}
    inbound: dict[str, int] = {}
    for edge in edges:
        outbound[edge["source"]] = outbound.get(edge["source"], 0) + 1
        inbound[edge["target"]] = inbound.get(edge["target"], 0) + 1
    for node in files:
        assert node["imports"] == outbound.get(node["id"], 0)
        assert node["importedBy"] == inbound.get(node["id"], 0)


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


async def test_health_is_liveness_and_nothing_else(client: httpx.AsyncClient) -> None:
    response = await client.get(HEALTH)

    assert response.status_code == 200
    # Deliberately exact: a health check is reachable by anyone, and every
    # extra field is a fact about the deployment given away for free.
    assert response.json() == {"status": "ok"}


@respx.mock
async def test_analyzes_a_repository_over_http(client: httpx.AsyncClient) -> None:
    """A repository URL in, a whole `AnalyzeResponse` out. The first time.

    Nothing in this project had ever produced a response body before this route
    existed — `build_graph` had no caller at all.
    """
    serve(make_source_tar(REALISTIC))

    response = await client.post(ANALYZE, json={"repository_url": SUBMITTED_URL})

    assert response.status_code == 200
    body = response.json()

    assert body["repository"] == {"owner": OWNER, "name": NAME, "commitSha": SHA}
    assert [node["id"] for node in body["nodes"]] == [
        ".",
        "src",
        "src/a.ts",
        "src/index.ts",
    ]
    assert body["edges"] == [
        {"source": "src/index.ts", "target": "src/a.ts", "relationship": "imports"}
    ]
    assert body["stats"] == {
        "files": 2,
        "directories": 2,
        "dependencies": 1,
        "externalImports": 1,
        "unresolvedImports": 0,
        # `package.json` (unsupported extension) and `.env` (secret filter).
        # The archive's own root directory entry is not a file and so is not
        # in this number — `_NON_FILE_SKIPS` exists to keep it out.
        "skippedFiles": 2,
        "truncated": False,
    }
    assert_internally_consistent(body)

    # The header comment, quoted rather than generated (ADR-013).
    assert node_map(body)["src/index.ts"]["description"] == "The entry point."
    # And the route, with the comment above the handler as its summary.
    assert body["serviceMap"] == [
        {
            "method": "GET",
            "path": "/healthz",
            "file": "src/index.ts",
            "line": 5,
            "summary": "Health probe.",
        }
    ]


@respx.mock
async def test_the_secret_file_reaches_no_part_of_the_response(
    client: httpx.AsyncClient,
) -> None:
    """`is_secret_path` as an end-to-end property of a *response*, not of a call.

    docs/SECURITY.md has wanted this assertion since the filter was written; it
    could not be made while there was no response to make it about.
    """
    serve(make_source_tar(REALISTIC))

    response = await client.post(ANALYZE, json={"repository_url": SUBMITTED_URL})

    assert response.status_code == 200
    assert ".env" not in response.text
    assert "hunter2" not in response.text


@respx.mock
async def test_the_component_diagram_is_absent_until_its_generator_exists(
    client: httpx.AsyncClient,
) -> None:
    """Absent, and a valid response regardless — the field defaults (ADR-013).

    Flip this test when `app/analysis/component_diagram.py` lands.
    """
    serve(make_source_tar(REALISTIC))

    response = await client.post(ANALYZE, json={"repository_url": SUBMITTED_URL})

    assert response.json()["componentDiagram"] is None


@respx.mock
async def test_every_response_carries_a_request_id_header(
    client: httpx.AsyncClient,
) -> None:
    serve(make_source_tar(REALISTIC))

    ok = await client.get(HEALTH)
    analyzed = await client.post(ANALYZE, json={"repository_url": SUBMITTED_URL})
    refused = await client.post(ANALYZE, json={"repository_url": "nope"})

    ids = {r.headers["x-request-id"] for r in (ok, analyzed, refused)}
    assert len(ids) == 3, "an id is per request, not per process"
    assert error_of(refused)["requestId"] == refused.headers["x-request-id"]


@respx.mock
async def test_no_repository_content_reaches_the_logs(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The response path was the open half of SECURITY.md's log-hygiene row."""
    serve(make_source_tar(REALISTIC))

    with caplog.at_level(logging.DEBUG):
        response = await client.post(ANALYZE, json={"repository_url": SUBMITTED_URL})
    assert response.status_code == 200

    above_debug = [r.getMessage() for r in caplog.records if r.levelno > logging.DEBUG]
    assert above_debug, "an empty capture would make the assertions below vacuous"
    for message in above_debug:
        assert SUBMITTED_URL not in message
        assert "src/index.ts" not in message
        assert "hunter2" not in message


# --------------------------------------------------------------------------
# The error contract
# --------------------------------------------------------------------------


async def test_a_bad_repository_url_is_refused_without_being_echoed(
    client: httpx.AsyncClient,
) -> None:
    hostile = "https://evil.example/github.com/acme/widgets"

    response = await client.post(ANALYZE, json={"repository_url": hostile})

    assert response.status_code == 400
    detail = error_of(response)
    assert detail["code"] == ErrorCode.INVALID_REPOSITORY_URL.value
    assert "evil.example" not in response.text


async def test_validation_failure_never_echoes_the_offending_input(
    client: httpx.AsyncClient,
) -> None:
    """The one security control in `app/api/app.py`.

    FastAPI's default `RequestValidationError` handler returns pydantic's
    `detail`, which embeds `input` — the client's own value — verbatim. This
    asserts the override, not the status code: with the handler removed the
    marker below comes straight back.
    """
    marker = "MARKER-" + "z" * 40

    response = await client.post(ANALYZE, json={"repository_url": ["x"], marker: 1})

    assert response.status_code == 422
    assert error_of(response)["code"] == ErrorCode.INVALID_REQUEST.value
    assert marker not in response.text
    assert "loc" not in response.text
    assert "repository_url" not in response.text


async def test_a_missing_body_is_a_contract_shaped_422(client: httpx.AsyncClient) -> None:
    response = await client.post(ANALYZE)

    assert response.status_code == 422
    assert error_of(response)["code"] == ErrorCode.INVALID_REQUEST.value


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [("GET", "/api/nope", 404), ("GET", ANALYZE, 405)],
)
async def test_starlettes_own_errors_keep_the_contract_shape(
    client: httpx.AsyncClient, method: str, path: str, status: int
) -> None:
    """An unknown path and a wrong method are the two most reachable responses
    there are, and FastAPI answers both with `{"detail": …}` by default."""
    response = await client.request(method, path)

    assert response.status_code == status
    assert error_of(response)["code"] == ErrorCode.INVALID_REQUEST.value
    assert "detail" not in response.json()


async def test_the_schema_is_served_but_the_cdn_backed_docs_pages_are_not(
    client: httpx.AsyncClient,
) -> None:
    """Swagger UI and ReDoc load their JavaScript from a public CDN, which would
    make a third-party script the only remote resource this backend serves."""
    assert (await client.get("/openapi.json")).status_code == 200
    assert (await client.get("/docs")).status_code == 404
    assert (await client.get("/redoc")).status_code == 404


@respx.mock
async def test_an_upstream_failure_surfaces_as_its_typed_error(
    client: httpx.AsyncClient,
) -> None:
    respx.get(REPO_URL).mock(return_value=httpx.Response(404))

    response = await client.post(ANALYZE, json={"repository_url": SUBMITTED_URL})

    assert response.status_code == 404
    detail = error_of(response)
    assert detail["code"] == ErrorCode.REPOSITORY_NOT_FOUND.value
    assert detail["message"] == "The repository could not be found or is not publicly accessible."


@respx.mock
async def test_a_repository_with_nothing_to_draw_is_refused(
    client: httpx.AsyncClient,
) -> None:
    serve(make_source_tar({"README.md": b"# hi\n"}))

    response = await client.post(ANALYZE, json={"repository_url": SUBMITTED_URL})

    assert response.status_code == 422
    assert error_of(response)["code"] == ErrorCode.NO_SUPPORTED_FILES.value


@pytest.fixture
async def lenient_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client that lets the 500 *response* be inspected.

    Starlette's `ServerErrorMiddleware` sends the response and then re-raises so
    the server logs the traceback, and `ASGITransport` re-raises it at the
    caller by default — which is right for every other test here and hides the
    one body this test is about.
    """
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_an_unforeseen_failure_returns_a_bare_500(
    lenient_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No traceback, no module path, no exception text — only a request id."""

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("a filesystem path /srv/secret and a traceback would go here")

    monkeypatch.setattr("app.api.routes.analyze_repository", explode)

    response = await lenient_client.post(ANALYZE, json={"repository_url": SUBMITTED_URL})

    assert response.status_code == 500
    detail = error_of(response)
    assert detail["code"] == ErrorCode.INTERNAL_ERROR.value
    assert detail["message"] == "An internal error occurred."
    assert "/srv/secret" not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


# --------------------------------------------------------------------------
# The request body cap — MAX_REQUEST_BODY_BYTES
# --------------------------------------------------------------------------


@pytest.fixture
def never_analyzed(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    """Records any call into the analysis, so a cap can be shown to precede it."""
    calls: list[object] = []

    def spy(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("the application ran on a body that was over the cap")

    monkeypatch.setattr("app.api.routes.analyze_repository", spy)
    yield calls
    assert calls == []


async def test_a_declared_content_length_over_the_cap_is_refused(
    client: httpx.AsyncClient, never_analyzed: list[object]
) -> None:
    cap = Settings().MAX_REQUEST_BODY_BYTES
    oversized = b'{"repository_url": "' + b"a" * (cap + 1) + b'"}'

    response = await client.post(
        ANALYZE, content=oversized, headers={"content-type": "application/json"}
    )

    assert response.status_code == 413
    detail = error_of(response)
    assert detail["code"] == ErrorCode.PAYLOAD_TOO_LARGE.value
    assert detail["message"] == "The request body is too large."


async def test_a_declared_length_is_refused_before_the_body_is_read(
    client: httpx.AsyncClient, never_analyzed: list[object]
) -> None:
    """What the header check buys over the byte counter, which is the *only*
    reason to have both.

    Deleting the declared-length check leaves the previous test green — the
    counter catches the same request one cap's worth of bytes later. The
    difference is whether an oversized body is read at all, so that is what is
    asserted: not one chunk is pulled.
    """
    cap = Settings().MAX_REQUEST_BODY_BYTES
    pulled: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        for chunk in range(4):
            pulled.append(chunk)
            yield b"a" * cap

    response = await client.post(
        ANALYZE,
        content=body(),
        headers={
            "content-type": "application/json",
            "content-length": str(cap * 4),
        },
    )

    assert response.status_code == 413
    assert pulled == [], "an oversized body was read despite declaring its length"


async def test_a_chunked_body_over_the_cap_is_refused_by_counting(
    client: httpx.AsyncClient, never_analyzed: list[object]
) -> None:
    """The half a `Content-Length` check cannot do.

    httpx sends an iterator body with `Transfer-Encoding: chunked` and no
    length, so this request declares nothing at all — exactly the shape
    docs/SECURITY.md names when it asks for byte counting as well.
    """
    cap = Settings().MAX_REQUEST_BODY_BYTES

    async def body() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"a" * cap

    response = await client.post(
        ANALYZE, content=body(), headers={"content-type": "application/json"}
    )

    assert response.status_code == 413
    assert error_of(response)["code"] == ErrorCode.PAYLOAD_TOO_LARGE.value


@respx.mock
async def test_a_body_at_the_cap_is_accepted(client: httpx.AsyncClient) -> None:
    """The at-the-limit case, so the cap is a boundary and not an approximation."""
    serve(make_source_tar(REALISTIC))
    cap = Settings().MAX_REQUEST_BODY_BYTES
    padding = " " * (cap - len(f'{{"repository_url": "{SUBMITTED_URL}"}}'))
    payload = f'{{"repository_url": "{SUBMITTED_URL}"{padding}}}'.encode()
    assert len(payload) == cap

    response = await client.post(
        ANALYZE, content=payload, headers={"content-type": "application/json"}
    )

    assert response.status_code == 200


async def test_a_get_is_not_drained_for_a_body_it_does_not_have(
    client: httpx.AsyncClient,
) -> None:
    """`/api/health` must not wait on a body message it will never be sent."""
    response = await client.get(HEALTH)

    assert response.status_code == 200


# --------------------------------------------------------------------------
# MAX_NODES / MAX_EDGES — the caps, and the stats that outlive them
# --------------------------------------------------------------------------


@respx.mock
async def test_the_chain_repository_is_uncapped_by_default(
    client: httpx.AsyncClient,
) -> None:
    """The control. Without it the two tests below could pass on an empty graph."""
    serve(chain_repository())

    body = (await client.post(ANALYZE, json={"repository_url": SUBMITTED_URL})).json()

    # root + src + ten files; nine links in the chain.
    assert len(body["nodes"]) == CHAIN_LENGTH + 2
    assert len(body["edges"]) == CHAIN_LENGTH - 1
    assert body["stats"]["truncated"] is False
    assert body["stats"]["externalImports"] == CHAIN_LENGTH
    assert_internally_consistent(body)


@respx.mock
async def test_max_nodes_caps_the_graph_and_the_stats_still_hold(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six nodes: the root, `src`, and the first four files of the chain.

    Every number is re-derived from what survived, so the response describes
    itself. The three edges are the links between kept files — `src/a3.ts`'s
    import of `src/a4.ts` goes with the node it pointed at, rather than becoming
    an edge into nothing.
    """
    use_settings(monkeypatch, MAX_NODES=6)
    serve(chain_repository())

    body = (await client.post(ANALYZE, json={"repository_url": SUBMITTED_URL})).json()

    assert [node["id"] for node in body["nodes"]] == [
        ".",
        "src",
        "src/a0.ts",
        "src/a1.ts",
        "src/a2.ts",
        "src/a3.ts",
    ]
    assert body["stats"]["truncated"] is True
    assert body["stats"]["files"] == 4
    assert body["stats"]["directories"] == 2
    assert body["stats"]["dependencies"] == 3
    # Statement counts over the *emitted* files, not over the repository: four
    # kept files import `react` once each. A total of ten would be a number no
    # set of nodes in this response adds up to.
    assert body["stats"]["externalImports"] == 4
    assert node_map(body)["."]["fileCount"] == 4
    assert_internally_consistent(body)


@respx.mock
async def test_max_edges_caps_the_edges_and_the_per_node_counters_follow(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nodes left alone, edges cut to two — the counters must move with them.

    `src/a2.ts` keeps an `importedBy` of 1 and `src/a3.ts` drops to 0, because
    both are counted off the finished edge set rather than off the import list.
    """
    use_settings(monkeypatch, MAX_EDGES=2)
    serve(chain_repository())

    body = (await client.post(ANALYZE, json={"repository_url": SUBMITTED_URL})).json()

    assert len(body["nodes"]) == CHAIN_LENGTH + 2, "the node cap did not fire"
    assert body["edges"] == [
        {"source": "src/a0.ts", "target": "src/a1.ts", "relationship": "imports"},
        {"source": "src/a1.ts", "target": "src/a2.ts", "relationship": "imports"},
    ]
    assert body["stats"]["truncated"] is True
    assert body["stats"]["dependencies"] == 2
    nodes = node_map(body)
    assert nodes["src/a2.ts"]["importedBy"] == 1
    assert nodes["src/a3.ts"]["importedBy"] == 0
    assert nodes["src/a3.ts"]["imports"] == 0
    # Nodes were not capped, so every file still counts its external import.
    assert body["stats"]["externalImports"] == CHAIN_LENGTH
    assert_internally_consistent(body)


@respx.mock
async def test_the_service_map_survives_a_node_cap(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deliberate asymmetry, recorded in ADR-023.

    The service map is a separate view keyed by path, not part of the graph.
    Shortening the reported API surface because an unrelated size cap fired
    would make a deterministic list quietly wrong, so it is left whole even
    when the file that declares the route is no longer a node.
    """
    use_settings(monkeypatch, MAX_NODES=2)
    serve(make_source_tar(REALISTIC))

    body = (await client.post(ANALYZE, json={"repository_url": SUBMITTED_URL})).json()

    assert [node["id"] for node in body["nodes"]] == [".", "src"]
    assert body["stats"]["truncated"] is True
    assert [endpoint["path"] for endpoint in body["serviceMap"]] == ["/healthz"]
    assert body["serviceMap"][0]["file"] not in node_map(body)
    assert_internally_consistent(body)
