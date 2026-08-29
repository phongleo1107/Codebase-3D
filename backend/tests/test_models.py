"""Wire-contract models: unknown fields rejected, enums closed, bounds hold."""

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.models import (
    AnalyzeRequest,
    AnalyzeResponse,
    GraphEdge,
    GraphNode,
    Repository,
    SourceRequest,
    SourceResponse,
    Stats,
)

SHA = "0123456789abcdef0123456789abcdef01234567"

VALID_EXAMPLES: dict[type[BaseModel], dict[str, Any]] = {
    GraphNode: {
        "id": "src/index.ts",
        "name": "index.ts",
        "path": "src/index.ts",
        "type": "file",
        "parent": "src",
        "depth": 1,
    },
    GraphEdge: {"source": "src/a.ts", "target": "src/b.ts", "relationship": "imports"},
    Stats: {"files": 2, "directories": 1, "dependencies": 1},
    Repository: {"owner": "octocat", "name": "hello-world", "commitSha": SHA},
    AnalyzeRequest: {"repository_url": "https://github.com/octocat/hello-world"},
    AnalyzeResponse: {
        "repository": {"owner": "octocat", "name": "hello-world", "commitSha": SHA},
        "nodes": [],
        "edges": [],
        "stats": {"files": 0, "directories": 0, "dependencies": 0},
    },
    SourceRequest: {
        "repository_url": "https://github.com/octocat/hello-world",
        "commit_sha": SHA,
        "path": "src/index.ts",
        "token": "f" * 64,
    },
    SourceResponse: {"path": "src/index.ts", "content": "export {}\n"},
}


@pytest.mark.parametrize("model_class", list(VALID_EXAMPLES))
def test_valid_payload_is_accepted(model_class: type[BaseModel]) -> None:
    model_class.model_validate(VALID_EXAMPLES[model_class])


@pytest.mark.parametrize("model_class", list(VALID_EXAMPLES))
def test_unknown_field_is_rejected(model_class: type[BaseModel]) -> None:
    payload = {**VALID_EXAMPLES[model_class], "unexpected": "x"}
    with pytest.raises(ValidationError):
        model_class.model_validate(payload)


def test_full_file_node_round_trips() -> None:
    node = GraphNode.model_validate(
        {
            **VALID_EXAMPLES[GraphNode],
            "language": "typescript",
            "bytes": 1024,
            "loc": 40,
            "imports": 3,
            "importedBy": 5,
            "externalImports": 2,
            "unresolvedImports": 0,
            "sourceToken": "f" * 64,
        }
    )
    assert node.importedBy == 5
    assert GraphNode.model_validate(node.model_dump()) == node


def test_directory_node_carries_aggregates_and_root_parent_is_none() -> None:
    node = GraphNode.model_validate(
        {
            "id": "src",
            "name": "src",
            "path": "src",
            "type": "directory",
            "parent": None,
            "depth": 0,
            "fileCount": 12,
            "totalBytes": 34567,
        }
    )
    assert node.parent is None
    assert node.fileCount == 12


def test_parent_is_required_not_defaulted() -> None:
    """ADR-006: hierarchy travels on `parent`; the analyzer must state it
    explicitly for every node, None being reserved for the root."""
    payload = dict(VALID_EXAMPLES[GraphNode])
    del payload["parent"]
    with pytest.raises(ValidationError):
        GraphNode.model_validate(payload)


@pytest.mark.parametrize("bad_type", ["module", "external", "Directory", "FILE", ""])
def test_node_type_enum_is_closed(bad_type: str) -> None:
    with pytest.raises(ValidationError):
        GraphNode.model_validate({**VALID_EXAMPLES[GraphNode], "type": bad_type})


@pytest.mark.parametrize("bad_relationship", ["contains", "IMPORTS", "requires", ""])
def test_edge_relationship_is_imports_only(bad_relationship: str) -> None:
    """ADR-006: `contains` edges are a contract break, not a variant."""
    with pytest.raises(ValidationError):
        GraphEdge.model_validate(
            {**VALID_EXAMPLES[GraphEdge], "relationship": bad_relationship}
        )


@pytest.mark.parametrize(
    "field",
    [
        "depth",
        "bytes",
        "loc",
        "imports",
        "importedBy",
        "externalImports",
        "unresolvedImports",
        "fileCount",
        "totalBytes",
    ],
)
def test_node_counters_reject_negatives(field: str) -> None:
    with pytest.raises(ValidationError):
        GraphNode.model_validate({**VALID_EXAMPLES[GraphNode], field: -1})


@pytest.mark.parametrize(
    "field",
    [
        "files",
        "directories",
        "dependencies",
        "externalImports",
        "unresolvedImports",
        "skippedFiles",
    ],
)
def test_stats_counters_reject_negatives(field: str) -> None:
    with pytest.raises(ValidationError):
        Stats.model_validate({**VALID_EXAMPLES[Stats], field: -1})


@pytest.mark.parametrize("field", ["id", "name", "path"])
def test_node_identity_fields_reject_empty(field: str) -> None:
    with pytest.raises(ValidationError):
        GraphNode.model_validate({**VALID_EXAMPLES[GraphNode], field: ""})


@pytest.mark.parametrize("field", ["source", "target"])
def test_edge_endpoints_reject_empty(field: str) -> None:
    with pytest.raises(ValidationError):
        GraphEdge.model_validate({**VALID_EXAMPLES[GraphEdge], field: ""})


@pytest.mark.parametrize("field", ["owner", "name"])
def test_repository_identity_fields_reject_empty(field: str) -> None:
    with pytest.raises(ValidationError):
        Repository.model_validate({**VALID_EXAMPLES[Repository], field: ""})


@pytest.mark.parametrize("field", ["repository_url", "path", "token"])
def test_source_request_rejects_empty_strings(field: str) -> None:
    with pytest.raises(ValidationError):
        SourceRequest.model_validate({**VALID_EXAMPLES[SourceRequest], field: ""})


def test_source_request_bounds_url_path_and_token() -> None:
    settings = get_settings()
    over_url = "https://github.com/o/" + "r" * settings.MAX_URL_LENGTH
    over_path = "a/" * settings.MAX_PATH_LENGTH
    over_token = "f" * 513
    for field, value in (
        ("repository_url", over_url),
        ("path", over_path),
        ("token", over_token),
    ):
        with pytest.raises(ValidationError):
            SourceRequest.model_validate({**VALID_EXAMPLES[SourceRequest], field: value})


def test_source_response_path_is_bounded() -> None:
    with pytest.raises(ValidationError):
        SourceResponse.model_validate(
            {**VALID_EXAMPLES[SourceResponse], "path": "a" * (get_settings().MAX_PATH_LENGTH + 1)}
        )


def test_analyze_request_enforces_the_url_limit_exactly() -> None:
    limit = get_settings().MAX_URL_LENGTH
    base = "https://github.com/o/"
    AnalyzeRequest.model_validate({"repository_url": base + "r" * (limit - len(base))})
    with pytest.raises(ValidationError):
        AnalyzeRequest.model_validate({"repository_url": base + "r" * (limit - len(base) + 1)})
    with pytest.raises(ValidationError):
        AnalyzeRequest.model_validate({"repository_url": ""})


def test_url_bound_tracks_configured_setting_not_a_hardcoded_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config.py owns every limit. A model that hardcoded 300 would accept
    this URL even after an operator tightened MAX_URL_LENGTH."""
    tightened = Settings(MAX_URL_LENGTH=80)
    monkeypatch.setattr("app.models.api.get_settings", lambda: tightened)
    url = "https://github.com/o/" + "r" * 200
    assert len(url) <= Settings.model_fields["MAX_URL_LENGTH"].default
    with pytest.raises(ValidationError):
        AnalyzeRequest.model_validate({"repository_url": url})


def test_path_bound_tracks_configured_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    tightened = Settings(MAX_PATH_LENGTH=16)
    monkeypatch.setattr("app.models.api.get_settings", lambda: tightened)
    with pytest.raises(ValidationError):
        SourceRequest.model_validate({**VALID_EXAMPLES[SourceRequest], "path": "a" * 32})


@pytest.mark.parametrize(
    "bad_sha",
    [
        "",  # empty
        "abc123",  # 6 chars — below the 7-char abbreviated minimum
        "0123456789ABCDEF0123456789ABCDEF01234567",  # uppercase
        "z" * 40,  # not hex
        "0" * 41,  # too long
        "../main",  # traversal, not a ref
    ],
)
def test_source_request_rejects_non_sha_refs(bad_sha: str) -> None:
    with pytest.raises(ValidationError):
        SourceRequest.model_validate({**VALID_EXAMPLES[SourceRequest], "commit_sha": bad_sha})


def test_source_request_accepts_abbreviated_sha() -> None:
    SourceRequest.model_validate({**VALID_EXAMPLES[SourceRequest], "commit_sha": "abc1234"})


def test_prd_section_9_response_shape_validates() -> None:
    """The PRD §9 example, plus `commitSha` which ADR-007 requires so the
    frontend can pin /api/source fetches to the analyzed snapshot."""
    response = AnalyzeResponse.model_validate(VALID_EXAMPLES[AnalyzeResponse])
    assert response.repository.owner == "octocat"
    assert response.stats.dependencies == 0
    assert response.nodes == []
    assert response.edges == []


def test_serialization_is_deterministic_and_key_order_is_schema_order() -> None:
    """ARCHITECTURE.md requires byte-identical JSON for a given commit, which
    is what makes golden-file tests possible. Input key order must not leak
    into output key order, and a round-trip must be a fixed point."""
    populated: dict[str, Any] = {
        **VALID_EXAMPLES[AnalyzeResponse],
        "nodes": [VALID_EXAMPLES[GraphNode], {**VALID_EXAMPLES[GraphNode], "id": "src/b.ts"}],
        "edges": [VALID_EXAMPLES[GraphEdge]],
        "stats": {"files": 2, "directories": 1, "dependencies": 1},
    }
    shuffled: dict[str, Any] = {
        "stats": populated["stats"],
        "edges": populated["edges"],
        "nodes": [dict(reversed(list(n.items()))) for n in populated["nodes"]],
        "repository": dict(reversed(list(populated["repository"].items()))),
    }
    canonical = AnalyzeResponse.model_validate(populated).model_dump_json()
    assert AnalyzeResponse.model_validate(shuffled).model_dump_json() == canonical
    assert AnalyzeResponse.model_validate_json(canonical).model_dump_json() == canonical
    assert canonical.index('"repository"') < canonical.index('"nodes"') < canonical.index('"edges"')
