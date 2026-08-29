"""Graph wire contract: PRD §4 plus the metadata fields agreed in
docs/ARCHITECTURE.md "Graph model".

Two ADRs shape this contract:

- ADR-005: external packages are never nodes; they surface only as
  ``externalImports`` / ``unresolvedImports`` counts.
- ADR-006: directory hierarchy travels on ``parent``, never as an edge, so
  ``relationship`` stays the single literal ``"imports"`` and
  ``stats.dependencies == len(edges)`` holds exactly.

Field names are the wire names — the frontend zod schema mirrors this file
verbatim, so nothing here may be renamed casually.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NodeType = Literal["directory", "file"]


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    type: NodeType
    # Hierarchy lives here, never on edges (ADR-006). None marks the root.
    parent: str | None
    depth: int = Field(ge=0)
    language: str | None = None

    # File metadata (None on directory nodes).
    bytes: int | None = Field(default=None, ge=0)
    loc: int | None = Field(default=None, ge=0)
    imports: int | None = Field(default=None, ge=0)
    importedBy: int | None = Field(default=None, ge=0)
    externalImports: int | None = Field(default=None, ge=0)
    unresolvedImports: int | None = Field(default=None, ge=0)
    # HMAC token authorizing /api/source for exactly this file (ADR-007).
    sourceToken: str | None = None

    # Directory aggregates (None on file nodes).
    fileCount: int | None = Field(default=None, ge=0)
    totalBytes: int | None = Field(default=None, ge=0)


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    relationship: Literal["imports"]


class Stats(BaseModel):
    """PRD §9 counters plus the honesty fields ARCHITECTURE.md requires:
    skips and truncation are reported, never silent."""

    model_config = ConfigDict(extra="forbid")

    files: int = Field(ge=0)
    directories: int = Field(ge=0)
    # Invariant (ADR-006): dependencies == len(edges).
    dependencies: int = Field(ge=0)
    externalImports: int = Field(default=0, ge=0)
    unresolvedImports: int = Field(default=0, ge=0)
    skippedFiles: int = Field(default=0, ge=0)
    truncated: bool = False
