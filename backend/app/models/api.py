"""Request/response schemas for the two POST endpoints (PRD §9,
docs/ARCHITECTURE.md "API Boundaries").

Requests use snake_case keys (PRD §9: ``repository_url``); response objects
use camelCase like the graph models.

Size bounds are read from :mod:`app.config` at validation time rather than
hardcoded, so an operator who tightens a limit actually gets it enforced here.
Format validation of the repository URL is deliberately NOT done in this
module — ``security/url_validation.py`` owns the grammar.
"""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.config import get_settings
from app.models.graph import GraphEdge, GraphNode, Stats

# Full or abbreviated lowercase-hex commit SHA, as harvested from the tar root.
_COMMIT_SHA_PATTERN = r"^[0-9a-f]{7,40}$"


def _within_url_limit(value: str) -> str:
    if len(value) > get_settings().MAX_URL_LENGTH:
        raise ValueError("value is longer than the configured maximum URL length")
    return value


def _within_path_limit(value: str) -> str:
    if len(value) > get_settings().MAX_PATH_LENGTH:
        raise ValueError("value is longer than the configured maximum path length")
    return value


def _within_summary_limit(value: str) -> str:
    if len(value) > get_settings().MAX_ENDPOINT_SUMMARY_CHARS:
        raise ValueError("value is longer than the configured maximum summary length")
    return value


def _within_diagram_limit(value: str) -> str:
    if len(value) > get_settings().MAX_COMPONENT_DIAGRAM_CHARS:
        raise ValueError("value is longer than the configured maximum diagram length")
    return value


RepositoryUrl = Annotated[str, Field(min_length=1), AfterValidator(_within_url_limit)]
MemberPath = Annotated[str, Field(min_length=1), AfterValidator(_within_path_limit)]
CommitSha = Annotated[str, Field(pattern=_COMMIT_SHA_PATTERN)]
EndpointSummary = Annotated[str, Field(min_length=1), AfterValidator(_within_summary_limit)]
ComponentDiagramSource = Annotated[str, Field(min_length=1), AfterValidator(_within_diagram_limit)]
# Deliberately a character class rather than a Literal of the known verbs. The
# detector is ours and may learn a router defining a method we did not
# enumerate; refusing to *describe* a route we successfully found would be a
# worse failure than reporting an unusual verb.
HttpMethod = Annotated[str, Field(pattern=r"^[A-Z]{1,16}$")]


class Repository(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1)
    name: str = Field(min_length=1)
    # Pins /api/source fetches to the analyzed snapshot (ADR-007).
    commitSha: CommitSha


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_url: RepositoryUrl


class ServiceEndpoint(BaseModel):
    """One HTTP route in the service map (ADR-012, amended by ADR-013).

    ``method``, ``path``, ``file`` and ``line`` are structural facts produced
    by the deterministic route-detection query. ``summary`` is quoted from the
    repository -- the comment immediately above the route handler -- and is
    optional because most handlers have no such comment. Absent is the
    ordinary case, not a degraded one.
    """

    model_config = ConfigDict(extra="forbid")

    method: HttpMethod
    # The route pattern as written in the source, e.g. "/api/users/:id".
    # Bounded like a member path — a different kind of string, same magnitude.
    path: MemberPath
    # The graph node ID of the file that defines the route.
    file: MemberPath
    # 0-indexed, matching parser.extract_imports; the frontend adds one.
    line: int = Field(ge=0)
    summary: EndpointSummary | None = None


def _within_endpoint_limit(value: list[ServiceEndpoint]) -> list[ServiceEndpoint]:
    if len(value) > get_settings().MAX_SERVICE_ENDPOINTS:
        raise ValueError("more endpoints than the configured maximum")
    return value


class AnalyzeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: Repository
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    stats: Stats
    # Derived from the finished graph and never feeding back into it, so the
    # graph stays a pure function of the commit (ADR-012, amended by ADR-013:
    # both are deterministic -- the service map from a route-detection query,
    # the diagram generated from the graph itself). Both default to absent
    # because a repository may define no routes at all, which is not a
    # failure. `componentDiagram` was named `c4` under ADR-012; it is a
    # structural component sketch, not a C4 model, and the name now says so.
    serviceMap: Annotated[list[ServiceEndpoint], AfterValidator(_within_endpoint_limit)] = []
    componentDiagram: ComponentDiagramSource | None = None


class SourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_url: RepositoryUrl
    commit_sha: CommitSha
    path: MemberPath
    # Hex-encoded HMAC issued by the analyzer (ADR-007). Not a documented
    # limit, so the bound is local: generous, but not unbounded.
    token: str = Field(min_length=1, max_length=512)


class SourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: MemberPath
    content: str
    language: str | None = None
    # True when the file was cut at MAX_PREVIEW_BYTES.
    truncated: bool = False
