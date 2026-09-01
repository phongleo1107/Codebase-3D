"""The MVP's two HTTP endpoints: `POST /api/analyze` and `GET /api/health`.

*(Not to be confused with `app/analysis/routes.py`, which detects routes **in an
analyzed repository**. This module serves our own.)*

`/api/analyze` is the last seam in the backend and it holds almost no logic of
its own: validate the URL, run the analysis, resolve, build the graph, hand back
the wire model. Everything it calls was written and tested in isolation; what is
decided *here* is the three things nobody below could decide.

**The analysis runs on a worker thread.** `analyze_repository` is blocking from
end to end — a streaming download, then a tree-sitter parse per file — and
`resolve_imports` and `build_graph` are CPU-bound on top of it, measured at up
to ~8 s for a capped import count (ADR-019). Running that on the event loop
would stall `/api/health` and, later, the rate limiter for the whole 60 s
budget. So the whole blocking span, model construction included, goes to
`asyncio.to_thread` in one piece.

**There is deliberately no `asyncio.wait_for` around it** (ADR-023). The plan of
record called for one; docs/SECURITY.md already explains why it is not the real
mechanism — it cannot kill a thread, so the timeout would return a 504 while the
work continued, and a client that retried on that 504 would multiply live
threads rather than shed them. The bound that exists is the one that works
cooperatively: `Deadline` (60 s) between archive members and around every parse,
plus httpx's own connect/read timeouts, plus `MAX_IMPORTS` on the clockless
phase after it. Shedding load is `MAX_CONCURRENT_ANALYSES`' job.

**Load shedding now has code behind it** (ADR-008): a per-IP sliding window
(`RATE_LIMIT_ANALYZE`, `RATE_LIMIT_ANALYZE_HOURLY`) rejects with a 429 and
`Retry-After` before the URL is even parsed, and a global, non-blocking
`ConcurrencyGate` (`MAX_CONCURRENT_ANALYSES`) rejects with a 503 immediately
before the worker thread would be spawned — never after. Both live in
`app/api/rate_limit.py` and are held on `app.state`, one pair per app
instance, so they cannot leak hits across a test's fresh `create_app()`.

**`MAX_NODES` / `MAX_EDGES` are enforced by asking for a smaller graph, not by
slicing a large one** (ADR-023). `build_graph` takes `GraphLimits` and derives
every counter from what survives; see its module docstring for why truncating
its output here instead would falsify `stats.dependencies`, the per-node
`imports`/`importedBy` counters, and four more numbers besides.

Nothing in this module logs a URL, an owner, a repository name, or a path.
docs/SECURITY.md keeps repository text out of records above `DEBUG`, and the
counts an operator actually needs — how large the graph was, whether a cap fired
— are not repository text.
"""

import asyncio
import logging
import math

from fastapi import APIRouter, Request

from app.analysis.component_diagram import build_component_diagram
from app.analysis.graph_builder import GraphLimits, build_graph
from app.analysis.pipeline import analyze_repository
from app.analysis.resolver import resolve_imports
from app.api.middleware import request_id_of
from app.api.rate_limit import ConcurrencyGate, SlidingWindowLimiter
from app.config import Settings, get_settings
from app.errors import InternalError, RateLimitedError, ServerBusyError
from app.models.api import AnalyzeRequest, AnalyzeResponse, Repository
from app.security.url_validation import RepoRef, parse_github_url

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/health")
async def health() -> dict[str, str]:
    """Liveness. Deliberately says nothing about the process it is running in.

    No version, no uptime, no dependency status — a health check is reachable by
    anyone who can reach the service, and every extra field is a fact about the
    deployment that an attacker did not have to work for.
    """
    return {"status": "ok"}


@router.post("/api/analyze")
async def analyze(payload: AnalyzeRequest, request: Request) -> AnalyzeResponse:
    """Analyze a public GitHub repository into a dependency graph.

    Raises only `AppError` subclasses, all of which carry a static message:
    `InvalidRepositoryUrlError` from the URL grammar, whatever
    `analyze_repository` raises for the fetch and the parse, `RateLimitedError`
    / `ServerBusyError` from the two load-shedding checks below, and
    `InternalError` for the one impossible state at the bottom.
    """
    settings = get_settings()
    request_id = request_id_of(request.scope)

    # Per-IP sliding window, checked before anything else runs (ADR-008). Keyed
    # on `request.client`, which is the immediate peer — this service has no
    # reverse proxy in front of it yet, so there is no `X-Forwarded-For` to
    # trust or spoof.
    client_ip = request.client.host if request.client else "unknown"
    limiter: SlidingWindowLimiter = request.app.state.analyze_rate_limiter
    retry_after = limiter.retry_after(
        client_ip, (settings.RATE_LIMIT_ANALYZE, settings.RATE_LIMIT_ANALYZE_HOURLY)
    )
    if retry_after is not None:
        raise RateLimitedError(retry_after_s=math.ceil(retry_after))

    # Before the thread, and before anything opens a socket: this is the grammar
    # check, not a second one. `analyze_repository` documents that it does not
    # re-validate, so the boundary is here.
    repo = parse_github_url(payload.repository_url)

    # The global concurrency gate. Checked immediately before the worker
    # thread would be spawned, not earlier, so a request that fails URL
    # validation never occupies a slot in the first place.
    gate: ConcurrencyGate = request.app.state.analyze_concurrency
    if not gate.try_acquire(settings.MAX_CONCURRENT_ANALYSES):
        raise ServerBusyError()
    try:
        response = await asyncio.to_thread(_analyze_blocking, repo, settings)
    finally:
        gate.release()

    logger.info(
        "analyze complete: %d nodes, %d edges, %d endpoints, truncated=%s (caps %d/%d)",
        len(response.nodes),
        len(response.edges),
        len(response.serviceMap),
        response.stats.truncated,
        settings.MAX_NODES,
        settings.MAX_EDGES,
        extra={"request_id": request_id},
    )
    return response


def _analyze_blocking(repo: RepoRef, settings: Settings) -> AnalyzeResponse:
    """The whole blocking span, in one call, so it costs one thread hop.

    Runs off the event loop. Every step raises `AppError` and nothing else.
    """
    analysis = analyze_repository(repo, settings=settings)
    resolved = resolve_imports(analysis)
    nodes, edges, stats = build_graph(
        analysis, resolved, limits=GraphLimits.from_settings(settings)
    )

    if analysis.commit_sha is None:
        # Unreachable in practice: the SHA is set as soon as the archive root is
        # validated, and `analyze_repository` raises `NoSupportedFilesError`
        # when no file was yielded — so a returned analysis always has one. It
        # is `str | None` on the dataclass because a streaming reader cannot
        # know its root before reading a member (ADR-015). Refused rather than
        # faked: a `Repository` with an invented commit is a wrong pin.
        logger.error("analysis returned no commit SHA")
        raise InternalError()

    return AnalyzeResponse(
        repository=Repository(
            owner=analysis.owner, name=analysis.name, commitSha=analysis.commit_sha
        ),
        # The analysis modules speak in tuples and the wire models declare
        # `list`. Pydantic coerces either way at runtime; mypy strict does not,
        # so the conversion is written out at the one boundary that needs it.
        nodes=list(nodes),
        edges=list(edges),
        stats=stats,
        # Deliberately *not* filtered to surviving nodes when the graph is
        # capped — it is a separate view keyed by path, not part of the graph.
        # See `app/analysis/graph_builder.py`.
        serviceMap=list(analysis.service_map),
        # Built from the *capped* `nodes`/`edges` above, never from `analysis`:
        # the diagram must describe the graph in this response, or it draws
        # containers for files the client cannot see. `None` for a graph with
        # no file nodes is ordinary, not a failure (ADR-024).
        componentDiagram=build_component_diagram(
            nodes, edges, stats, analysis.service_map, settings=settings
        ),
    )
