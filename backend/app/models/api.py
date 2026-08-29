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


RepositoryUrl = Annotated[str, Field(min_length=1), AfterValidator(_within_url_limit)]
MemberPath = Annotated[str, Field(min_length=1), AfterValidator(_within_path_limit)]
CommitSha = Annotated[str, Field(pattern=_COMMIT_SHA_PATTERN)]


class Repository(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1)
    name: str = Field(min_length=1)
    # Pins /api/source fetches to the analyzed snapshot (ADR-007).
    commitSha: CommitSha


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_url: RepositoryUrl


class AnalyzeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: Repository
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    stats: Stats


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
