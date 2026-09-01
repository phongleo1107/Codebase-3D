"""Graph construction: files and resolved imports in, nodes/edges/stats out.

Almost every test here runs the **real resolver** over a hand-built
`RepositoryAnalysis` and feeds its output straight to `build_graph`. That is not
laziness about stubbing — it is the only arrangement in which "a resolved target
is always a node" stays a property rather than an assumption, and it means a
fixture describes a repository (`{path: [specifier, ...]}`) instead of describing
the resolver's internals. The handful of tests that build `ResolvedImport`
records directly are exactly the ones about inputs the resolver cannot produce:
an import whose source or target is not in the analysis.

Four groups carry the weight:

*The three invariants.* Sorting, deduplication, and self-edge removal are the
only work this module does that the pipeline and the resolver deliberately did
not, so each has tests that fail when the rule is deleted rather than tests that
merely pass while it is present. Verified by mutation, one deletion at a time.

*The identities.* ``stats.dependencies == len(edges)`` is the contract's own
verification (ADR-006), and it comes with three more that have to hold beside
it: ``sum(imports) == sum(importedBy) == len(edges)``,
``root.fileCount == stats.files``, and one node with ``parent is None``.
`test_identities_hold_over_a_large_graph` asserts all of them at once over 300
files so they are checked together rather than one fixture at a time.

*Determinism.* The same analysis produces byte-identical JSON, and so does the
same analysis with its files in a different order. That is the property ADR-013
made possible for the whole response and it starts here.

*The contradictions.* A tarball can contain ``components.ts`` and
``components.ts/x.ts``, or the same path twice. Both are pinned, because both
are decisions rather than accidents.
"""

import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import PurePosixPath

import pytest
from pydantic import ValidationError

from app.analysis.graph_builder import GraphLimits, build_graph
from app.analysis.pipeline import ImportRef, RepositoryAnalysis, SourceFile
from app.analysis.resolver import Resolution, ResolvedImport, resolve_imports
from app.config import Settings
from app.models import AnalyzeResponse, Repository
from app.models.graph import GraphEdge, GraphNode, Stats

OWNER = "acme"
NAME = "widgets"
SHA = "a1b2c3d"

# Every file is the same size unless a fixture says otherwise, so an aggregate
# is checkable by multiplication rather than by restating the fixture.
FILE_BYTES = 100
FILE_LOC = 7


def make_analysis(
    layout: Mapping[str, Sequence[str]],
    *,
    name: str = NAME,
    sizes: Mapping[str, int] | None = None,
    skipped: Mapping[str, int] | None = None,
    truncated: bool = False,
    duplicate: str | None = None,
    descriptions: Mapping[str, str] | None = None,
) -> RepositoryAnalysis:
    """A `RepositoryAnalysis` over ``{path: [specifier, ...]}``, in that order.

    ``duplicate`` appends a second `SourceFile` record for that path, which a
    tarball can legally produce and the archive reader does not collapse.
    """
    sizes = sizes or {}
    files = [
        SourceFile(
            path=PurePosixPath(path),
            language="typescript" if path.endswith((".ts", ".tsx")) else "javascript",
            size_bytes=sizes.get(path, FILE_BYTES),
            loc=FILE_LOC,
            imports=tuple(ImportRef(spec, line) for line, spec in enumerate(specs)),
            description=(descriptions or {}).get(path),
        )
        for path, specs in layout.items()
    ]
    if duplicate is not None:
        original = next(f for f in files if str(f.path) == duplicate)
        files.append(
            SourceFile(
                path=original.path,
                language=original.language,
                size_bytes=original.size_bytes * 2,
                loc=original.loc * 2,
                imports=original.imports,
            )
        )
    return RepositoryAnalysis(
        owner=OWNER,
        name=name,
        ref="main",
        commit_sha=SHA,
        files=tuple(files),
        skipped=dict(skipped or {}),
        truncated=truncated,
        imports_truncated=False,
    )


def build(
    layout: Mapping[str, Sequence[str]],
    *,
    name: str = NAME,
    sizes: Mapping[str, int] | None = None,
    skipped: Mapping[str, int] | None = None,
    truncated: bool = False,
    duplicate: str | None = None,
) -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...], Stats]:
    """Resolve then build — the two stages exactly as the router will run them.

    The keyword arguments restate `make_analysis`' rather than forwarding
    ``**kwargs``, which would need a `type: ignore` to type-check at all.
    """
    analysis = make_analysis(
        layout, name=name, sizes=sizes, skipped=skipped, truncated=truncated, duplicate=duplicate
    )
    return build_graph(analysis, resolve_imports(analysis))


def paths(nodes: Iterable[GraphNode]) -> list[str]:
    return [node.path for node in nodes]


def pairs(edges: Iterable[GraphEdge]) -> list[tuple[str, str]]:
    return [(edge.source, edge.target) for edge in edges]


def node_at(nodes: Iterable[GraphNode], path: str) -> GraphNode:
    return next(node for node in nodes if node.path == path)


# The a/b/c fixture the brief asks for: cross-imports, and a genuine cycle
# between a and b in both directions.
CYCLE: Mapping[str, Sequence[str]] = {
    "src/a.ts": ["./b", "./c"],
    "src/b.ts": ["./a"],
    "src/c.ts": [],
}


# --------------------------------------------------------------------------
# Nodes — one per file, directories inferred from the parent hierarchy.
# --------------------------------------------------------------------------


def test_one_node_per_file_plus_the_inferred_directories() -> None:
    nodes, _, _ = build(CYCLE)
    assert paths(nodes) == [".", "src", "src/a.ts", "src/b.ts", "src/c.ts"]


def test_file_nodes_carry_their_metadata() -> None:
    nodes, _, _ = build(CYCLE, sizes={"src/a.ts": 4096})
    node = node_at(nodes, "src/a.ts")
    assert (node.id, node.name, node.type) == ("src/a.ts", "a.ts", "file")
    assert (node.parent, node.depth) == ("src", 2)
    assert (node.language, node.bytes, node.loc) == ("typescript", 4096, FILE_LOC)


def test_directory_nodes_are_inferred_at_every_level() -> None:
    nodes, _, _ = build({"a/b/c/deep.ts": []})
    assert paths(nodes) == [".", "a", "a/b", "a/b/c", "a/b/c/deep.ts"]
    assert [node.type for node in nodes] == ["directory"] * 4 + ["file"]
    assert [node.depth for node in nodes] == [0, 1, 2, 3, 4]


def test_the_root_is_the_only_parentless_node_and_is_named_for_the_repository() -> None:
    """ADR-006 reserves ``parent is None`` for the root, singular."""
    nodes, _, _ = build({"top.ts": [], "src/a.ts": []}, name="my-repo")
    assert [node.path for node in nodes if node.parent is None] == ["."]
    root = node_at(nodes, ".")
    assert (root.id, root.name, root.type, root.depth) == (".", "my-repo", "directory", 0)


def test_every_parent_names_a_node_that_exists() -> None:
    nodes, _, _ = build({"a/b/c.ts": [], "a/d.ts": [], "e.ts": []})
    ids = {node.id for node in nodes}
    assert all(node.parent in ids for node in nodes if node.parent is not None)


def test_a_file_with_no_imports_is_still_a_node() -> None:
    nodes, edges, stats = build({"src/lonely.ts": []})
    assert paths(nodes) == [".", "src", "src/lonely.ts"]
    assert edges == ()
    assert stats.dependencies == 0
    assert node_at(nodes, "src/lonely.ts").imports == 0


def test_file_and_directory_fields_do_not_overlap() -> None:
    """`type` is the discriminator, and every field on the wrong side is None."""
    nodes, _, _ = build(CYCLE)
    file_node, directory = node_at(nodes, "src/a.ts"), node_at(nodes, "src")
    assert (file_node.fileCount, file_node.totalBytes) == (None, None)
    assert (directory.bytes, directory.loc, directory.language) == (None, None, None)
    assert (directory.imports, directory.importedBy) == (None, None)
    assert (directory.externalImports, directory.unresolvedImports) == (None, None)


def test_directory_aggregates_are_recursive() -> None:
    nodes, _, _ = build({"a/b/one.ts": [], "a/b/two.ts": [], "a/three.ts": []})
    assert (node_at(nodes, "a/b").fileCount, node_at(nodes, "a/b").totalBytes) == (
        2,
        2 * FILE_BYTES,
    )
    assert (node_at(nodes, "a").fileCount, node_at(nodes, "a").totalBytes) == (3, 3 * FILE_BYTES)
    assert (node_at(nodes, ".").fileCount, node_at(nodes, ".").totalBytes) == (3, 3 * FILE_BYTES)


def test_an_empty_analysis_yields_no_nodes_at_all() -> None:
    """Not even a lone root. `NoSupportedFilesError` fires before this in production."""
    nodes, edges, stats = build({})
    assert (nodes, edges) == ((), ())
    assert (stats.files, stats.directories, stats.dependencies) == (0, 0, 0)


# --------------------------------------------------------------------------
# Edges — one per resolved import, and nothing else.
# --------------------------------------------------------------------------


def test_an_edge_per_resolved_import_including_both_directions_of_a_cycle() -> None:
    _, edges, stats = build(CYCLE)
    assert pairs(edges) == [
        ("src/a.ts", "src/b.ts"),
        ("src/a.ts", "src/c.ts"),
        ("src/b.ts", "src/a.ts"),
    ]
    assert {edge.relationship for edge in edges} == {"imports"}
    assert stats.dependencies == len(edges)


def test_external_and_unresolved_imports_produce_no_edges() -> None:
    _, edges, _ = build({"src/a.ts": ["react", "./missing", "node:fs"]})
    assert edges == ()


def test_duplicate_imports_of_the_same_module_are_one_edge() -> None:
    """Two spellings and a repeat, one dependency. The edge means "a depends on b"."""
    nodes, edges, stats = build({"src/a.ts": ["./b", "./b.ts", "./b"], "src/b.ts": []})
    assert pairs(edges) == [("src/a.ts", "src/b.ts")]
    assert stats.dependencies == 1
    assert node_at(nodes, "src/a.ts").imports == 1
    assert node_at(nodes, "src/b.ts").importedBy == 1


def test_a_self_import_resolves_but_never_becomes_an_edge() -> None:
    """`export * from './index'` inside `index.ts` is true, and is not a dependency."""
    analysis = make_analysis({"src/index.ts": ["./index"]})
    resolved = resolve_imports(analysis)
    assert [record.resolution for record in resolved] == [Resolution.RESOLVED]
    assert resolved[0].target == PurePosixPath("src/index.ts")

    nodes, edges, stats = build_graph(analysis, resolved)
    assert edges == ()
    assert stats.dependencies == 0
    assert (node_at(nodes, "src/index.ts").imports, node_at(nodes, "src/index.ts").importedBy) == (
        0,
        0,
    )


def test_a_self_import_does_not_suppress_a_real_edge_from_the_same_file() -> None:
    """Dropping the self-edge must drop one pair, not the file's whole row."""
    _, edges, _ = build({"src/index.ts": ["./index", "./b"], "src/b.ts": []})
    assert pairs(edges) == [("src/index.ts", "src/b.ts")]


def test_every_edge_endpoint_is_a_file_node() -> None:
    """The resolver's set-membership property, observed from the graph's side."""
    nodes, edges, _ = build(CYCLE)
    files = {node.id for node in nodes if node.type == "file"}
    assert all(edge.source in files and edge.target in files for edge in edges)


# --------------------------------------------------------------------------
# Counts — per node and in the stats.
# --------------------------------------------------------------------------


def test_external_and_unresolved_are_counted_per_file() -> None:
    nodes, _, stats = build(
        {
            "src/a.ts": ["react", "@scope/pkg", "node:fs", "./nope", "/abs/path"],
            "src/b.ts": ["react"],
        }
    )
    a, b = node_at(nodes, "src/a.ts"), node_at(nodes, "src/b.ts")
    assert (a.externalImports, a.unresolvedImports) == (3, 2)
    assert (b.externalImports, b.unresolvedImports) == (1, 0)
    assert (stats.externalImports, stats.unresolvedImports) == (4, 2)


def test_external_imports_count_statements_not_distinct_packages() -> None:
    """Deduplication is specified for edges and only for edges."""
    nodes, _, stats = build({"src/a.ts": ["react", "react", "react"]})
    assert node_at(nodes, "src/a.ts").externalImports == 3
    assert stats.externalImports == 3


def test_imports_and_imported_by_are_counted_off_the_finished_edge_set() -> None:
    nodes, edges, _ = build(CYCLE)
    a, b, c = (node_at(nodes, f"src/{n}.ts") for n in "abc")
    assert (a.imports, a.importedBy) == (2, 1)
    assert (b.imports, b.importedBy) == (1, 1)
    assert (c.imports, c.importedBy) == (0, 1)
    assert sum(n.imports or 0 for n in nodes) == sum(n.importedBy or 0 for n in nodes) == len(edges)


def test_stats_files_and_directories_count_the_emitted_nodes() -> None:
    nodes, _, stats = build(CYCLE)
    assert stats.files == sum(1 for node in nodes if node.type == "file") == 3
    assert stats.directories == sum(1 for node in nodes if node.type == "directory") == 2


def test_skipped_and_truncated_are_carried_from_the_analysis_unchanged() -> None:
    """The builder truncates nothing itself, so it may not invent either field."""
    _, _, stats = build(
        CYCLE, skipped={"secret_or_excluded": 2, "unsupported_extension": 9}, truncated=True
    )
    assert (stats.skippedFiles, stats.truncated) == (11, True)


# --------------------------------------------------------------------------
# Ordering — by path components, which is not the same as by path string.
# --------------------------------------------------------------------------


def test_nodes_and_edges_are_sorted() -> None:
    """Fixture order is deliberately reverse-alphabetical, so an unsorted build shows."""
    nodes, edges, _ = build(
        {"src/z.ts": ["./y", "./x"], "src/y.ts": ["./x"], "src/x.ts": []},
    )
    assert paths(nodes) == [".", "src", "src/x.ts", "src/y.ts", "src/z.ts"]
    assert pairs(edges) == [
        ("src/y.ts", "src/x.ts"),
        ("src/z.ts", "src/x.ts"),
        ("src/z.ts", "src/y.ts"),
    ]


def test_ordering_is_by_components_not_by_string() -> None:
    """`src-b` sorts before `src/a.ts` as a string, because "-" < "/".

    Sorting strings would split a directory away from its own children. This is
    the case that fails if someone "simplifies" the key to ``str``.
    """
    nodes, _, _ = build({"src/a.ts": [], "src-b/x.ts": []})
    assert paths(nodes) == [".", "src", "src/a.ts", "src-b", "src-b/x.ts"]
    assert paths(nodes) != sorted(paths(nodes))


def test_a_parent_always_precedes_its_children_including_the_root() -> None:
    """`!` sorts before `.` as a string, so only component order puts the root first."""
    nodes, _, _ = build({"!top.ts": [], "src/a.ts": []})
    assert paths(nodes)[0] == "."
    seen: set[str] = set()
    for node in nodes:
        assert node.parent is None or node.parent in seen
        seen.add(node.id)


# --------------------------------------------------------------------------
# Determinism — the property the whole response inherits (ADR-013).
# --------------------------------------------------------------------------


def as_json(result: tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...], Stats]) -> str:
    nodes, edges, stats = result
    return json.dumps(
        {
            "nodes": [node.model_dump() for node in nodes],
            "edges": [edge.model_dump() for edge in edges],
            "stats": stats.model_dump(),
        }
    )


def test_the_same_analysis_produces_byte_identical_json() -> None:
    assert as_json(build(CYCLE)) == as_json(build(CYCLE))


def test_file_order_in_the_analysis_does_not_change_the_graph() -> None:
    """Archive order is an accident of the tarball; the graph must not inherit it."""
    layout = {f"src/f{i}.ts": [f"./f{(i + 1) % 12}", "react"] for i in range(12)}
    entries = list(layout.items())
    # Two deterministic permutations rather than a shuffle: a seeded RNG would
    # be one more thing to trust, and odds-then-evens separates every file from
    # both of its neighbours, which is what an archive order can do.
    reversed_order = dict(reversed(entries))
    interleaved = dict(entries[1::2] + entries[0::2])
    assert as_json(build(reversed_order)) == as_json(build(layout))
    assert as_json(build(interleaved)) == as_json(build(layout))


# --------------------------------------------------------------------------
# Contradictions a hostile archive can produce. Resolved, never raised.
# --------------------------------------------------------------------------


def test_a_path_that_is_both_a_file_and_a_directory_stays_a_file() -> None:
    """A tarball may carry `components.ts` beside `components.ts/x.ts`.

    Observed beats inferred: one node, typed file, ids still unique, and the
    child's parent still names something that exists.
    """
    nodes, _, stats = build({"components.ts": [], "components.ts/x.ts": []})
    assert paths(nodes) == [".", "components.ts", "components.ts/x.ts"]
    assert len({node.id for node in nodes}) == len(nodes)
    assert node_at(nodes, "components.ts").type == "file"
    assert node_at(nodes, "components.ts/x.ts").parent == "components.ts"
    assert (stats.files, stats.directories) == (2, 1)


def test_a_repeated_path_becomes_one_node_from_the_first_record() -> None:
    nodes, _, stats = build({"src/a.ts": ["react"], "src/b.ts": []}, duplicate="src/a.ts")
    assert paths(nodes) == [".", "src", "src/a.ts", "src/b.ts"]
    a = node_at(nodes, "src/a.ts")
    assert (a.bytes, a.loc) == (FILE_BYTES, FILE_LOC)
    assert stats.files == 2
    # Counts are keyed by path, so both records' imports land on the one node.
    assert a.externalImports == stats.externalImports == 2


# --------------------------------------------------------------------------
# Preconditions. The resolver cannot produce either of these; a future caller
# pairing mismatched inputs could, and a dangling edge must not be the result.
# --------------------------------------------------------------------------


def test_an_import_from_a_file_outside_the_analysis_is_rejected() -> None:
    analysis = make_analysis({"src/a.ts": []})
    stranger = ResolvedImport(
        source=PurePosixPath("other/b.ts"),
        specifier="react",
        line=0,
        resolution=Resolution.EXTERNAL,
        target=None,
    )
    with pytest.raises(ValueError, match="source that is not in the analysis"):
        build_graph(analysis, (stranger,))


def test_an_edge_to_a_target_outside_the_analysis_is_rejected() -> None:
    analysis = make_analysis({"src/a.ts": []})
    ghost = ResolvedImport(
        source=PurePosixPath("src/a.ts"),
        specifier="./ghost",
        line=0,
        resolution=Resolution.RESOLVED,
        target=PurePosixPath("src/ghost.ts"),
    )
    with pytest.raises(ValueError, match="target that is not in the analysis"):
        build_graph(analysis, (ghost,))


def test_the_rejection_messages_quote_no_path() -> None:
    """Fixed literals, like every other message in the project — SECURITY.md."""
    analysis = make_analysis({"src/a.ts": []})
    stranger = ResolvedImport(
        source=PurePosixPath("secret/tokens.ts"),
        specifier="./x",
        line=0,
        resolution=Resolution.UNRESOLVED,
        target=None,
    )
    with pytest.raises(ValueError) as caught:
        build_graph(analysis, (stranger,))
    assert "secret" not in str(caught.value)


# --------------------------------------------------------------------------
# Scale, depth, and the identities checked together.
# --------------------------------------------------------------------------


def test_deep_nesting() -> None:
    deep = "/".join(f"d{i}" for i in range(30)) + "/leaf.ts"
    nodes, _, stats = build({deep: []})
    assert stats.directories == 31  # the root plus d0..d29
    assert node_at(nodes, deep).depth == 31
    assert node_at(nodes, ".").fileCount == 1


def test_identities_hold_over_a_large_graph() -> None:
    """Every relationship the contract depends on, asserted at once over 300 files.

    A ring of 300 modules, each importing its successor, its successor's
    successor twice under two spellings (one duplicate edge), itself (one
    self-edge), one package, and one specifier that matches nothing. The ring
    closes with modulo, so every module has exactly the same six imports and no
    tail case has to be reasoned about separately.
    """
    size = 300
    layout = {
        f"src/m{i:03d}/index.ts": [
            f"../m{(i + 1) % size:03d}",
            f"../m{(i + 2) % size:03d}",
            f"../m{(i + 2) % size:03d}/index",
            "./index",
            "lodash",
            "./nowhere",
        ]
        for i in range(size)
    }
    nodes, edges, stats = build(layout)

    files = [node for node in nodes if node.type == "file"]
    assert stats.files == len(files) == size
    assert stats.directories == sum(1 for node in nodes if node.type == "directory") == size + 2
    assert stats.dependencies == len(edges)
    # Two distinct successors each; the third specifier is a duplicate spelling
    # of the second, and the fourth is the self-import.
    assert len(edges) == 2 * size
    assert sum(node.imports or 0 for node in files) == len(edges)
    assert sum(node.importedBy or 0 for node in files) == len(edges)
    assert stats.externalImports == sum(node.externalImports or 0 for node in files) == size
    assert stats.unresolvedImports == sum(node.unresolvedImports or 0 for node in files) == size
    assert node_at(nodes, ".").fileCount == stats.files
    assert node_at(nodes, ".").totalBytes == size * FILE_BYTES
    assert len({node.id for node in nodes}) == len(nodes)
    assert paths(nodes) == sorted(paths(nodes), key=lambda p: PurePosixPath(p).parts)


# --------------------------------------------------------------------------
# MAX_NODES / MAX_EDGES (ADR-023). The cap is opt-in, and the reason it lives
# here rather than in the router is that everything a cap can falsify is
# computed here. These tests are about the second half: what stays true.
# `tests/test_api_routes.py` exercises the same caps over a whole response.
# --------------------------------------------------------------------------


def capped(
    layout: Mapping[str, Sequence[str]],
    *,
    max_nodes: int = 10_000,
    max_edges: int = 10_000,
) -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...], Stats]:
    """Build with one cap tightened and the other lifted out of the way."""
    analysis = make_analysis(layout)
    return build_graph(
        analysis,
        resolve_imports(analysis),
        limits=GraphLimits(max_nodes=max_nodes, max_edges=max_edges),
    )


# Ten files in one directory, each importing the next. Twelve nodes, nine
# edges, and a sorted order (".", "src", "src/a0.ts" … "src/a9.ts") in which
# every cut position is easy to state.
CHAIN: Mapping[str, Sequence[str]] = {
    f"src/a{i}.ts": ([f"./a{i + 1}"] if i < 9 else []) + ["react"] for i in range(10)
}


def test_the_cap_is_off_unless_limits_are_passed() -> None:
    """Every other caller in the project builds the whole graph, and must keep
    doing so — a default cap would silently truncate a direct call."""
    nodes, edges, stats = build(CHAIN)
    assert (len(nodes), len(edges)) == (12, 9)
    assert stats.truncated is False


def test_graph_limits_reads_settings_rather_than_restating_them() -> None:
    limits = GraphLimits.from_settings(Settings(MAX_NODES=7, MAX_EDGES=9))
    assert (limits.max_nodes, limits.max_edges) == (7, 9)


def test_a_node_cap_keeps_a_prefix_that_is_closed_under_parent() -> None:
    """The property the whole design rests on.

    A path's parent is a prefix of it in `parts` order, so a prefix of the
    sorted node list can never leave a survivor naming a parent that is gone.
    Nothing else about "drop the tail" would be safe.
    """
    nodes, _, stats = capped(CHAIN, max_nodes=6)

    assert paths(nodes) == [".", "src", "src/a0.ts", "src/a1.ts", "src/a2.ts", "src/a3.ts"]
    assert stats.truncated is True
    ids = {node.id for node in nodes}
    assert all(node.parent is None or node.parent in ids for node in nodes)


def test_a_node_cap_drops_the_edges_that_pointed_into_it() -> None:
    """`src/a3.ts` imports `src/a4.ts`, which is gone. It must not become an
    edge into nothing — the one outcome ADR-018 refuses everywhere else too."""
    nodes, edges, stats = capped(CHAIN, max_nodes=6)

    ids = {node.id for node in nodes}
    assert pairs(edges) == [
        ("src/a0.ts", "src/a1.ts"),
        ("src/a1.ts", "src/a2.ts"),
        ("src/a2.ts", "src/a3.ts"),
    ]
    assert all(edge.source in ids and edge.target in ids for edge in edges)
    assert stats.dependencies == len(edges) == 3


def test_a_node_cap_re_derives_every_count_from_what_survived() -> None:
    """The assertion a naive slice of the returned tuples fails.

    Each of these is a number the builder computes internally, so truncating
    afterwards would leave all six describing a graph the caller cannot see.
    """
    nodes, edges, stats = capped(CHAIN, max_nodes=6)

    files = [node for node in nodes if node.type == "file"]
    assert stats.files == len(files) == 4
    assert stats.directories == 2
    assert stats.dependencies == len(edges)
    assert sum(node.imports or 0 for node in files) == len(edges)
    assert sum(node.importedBy or 0 for node in files) == len(edges)
    # Statement counts over the *emitted* files: four kept files import `react`
    # once each. Ten would be a total no node in the response adds up to.
    assert stats.externalImports == sum(node.externalImports or 0 for node in files) == 4
    assert node_at(nodes, ".").fileCount == stats.files
    assert node_at(nodes, ".").totalBytes == 4 * FILE_BYTES
    # The last surviving file lost the node it imported, so its counter moved
    # with the edge rather than staying at the raw import count.
    assert node_at(nodes, "src/a3.ts").imports == 0


def test_an_edge_cap_leaves_the_nodes_alone_and_moves_the_counters() -> None:
    nodes, edges, stats = capped(CHAIN, max_edges=2)

    assert len(nodes) == 12, "the node cap did not fire"
    assert pairs(edges) == [("src/a0.ts", "src/a1.ts"), ("src/a1.ts", "src/a2.ts")]
    assert stats.truncated is True
    assert stats.dependencies == 2
    assert node_at(nodes, "src/a2.ts").importedBy == 1
    assert node_at(nodes, "src/a3.ts").importedBy == 0
    # Nodes were not capped, so no file lost its external import.
    assert stats.externalImports == 10


def test_a_graph_exactly_at_both_caps_is_not_truncated() -> None:
    """The at-the-limit case, so ``>`` cannot quietly become ``>=``."""
    _, _, stats = capped(CHAIN, max_nodes=12, max_edges=9)
    assert stats.truncated is False

    _, _, tighter = capped(CHAIN, max_nodes=11, max_edges=9)
    assert tighter.truncated is True


def test_a_cap_does_not_clear_a_truncation_the_analysis_already_reported() -> None:
    analysis = make_analysis(CHAIN, truncated=True)
    _, _, stats = build_graph(
        analysis, resolve_imports(analysis), limits=GraphLimits(max_nodes=99, max_edges=99)
    )
    assert stats.truncated is True


def test_a_directory_whose_children_all_fell_past_the_cut_survives_empty() -> None:
    """A documented artifact, pinned so it stays deliberate.

    A directory sorts before its contents, so a cut between them keeps the
    directory with a `fileCount` of 0. Honest — the directory really is in the
    repository — and it keeps `root.fileCount == stats.files` true.
    """
    nodes, _, stats = capped(CHAIN, max_nodes=2)

    assert paths(nodes) == [".", "src"]
    assert stats.files == 0
    assert node_at(nodes, "src").fileCount == 0
    assert node_at(nodes, ".").fileCount == stats.files


def test_a_cap_does_not_hide_a_mispaired_caller() -> None:
    """The preconditions are checked against the whole analysis, before the cut.

    Otherwise a `ValueError` would depend on whether the bad record's file
    happened to fall inside the cap — a programming error that reports itself
    only sometimes is worse than one that does not report at all.
    """
    analysis = make_analysis(CHAIN)
    bogus = ResolvedImport(
        source=PurePosixPath("src/a9.ts"),
        specifier="./elsewhere",
        line=0,
        resolution=Resolution.RESOLVED,
        target=PurePosixPath("src/not-in-the-analysis.ts"),
    )
    with pytest.raises(ValueError, match="not in the analysis"):
        build_graph(analysis, (bogus,), limits=GraphLimits(max_nodes=2, max_edges=2))


def test_a_capped_graph_still_composes_into_an_analyze_response() -> None:
    nodes, edges, stats = capped(CHAIN, max_nodes=6, max_edges=2)
    response = AnalyzeResponse(
        repository=Repository(owner=OWNER, name=NAME, commitSha="a" * 40),
        nodes=list(nodes),
        edges=list(edges),
        stats=stats,
    )
    assert response.stats.truncated is True
    assert len(response.nodes) == 6


# --------------------------------------------------------------------------
# Structural properties the security model leans on. Both are stated the same
# way the resolver's are: not "the module does not do X" as a claim, but a run
# that fails if it ever starts to.
# --------------------------------------------------------------------------


def test_no_filesystem_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """A whole graph is built with the filesystem primitives torn out.

    ADR-003 says nothing is written to disk, and docs/SECURITY.md notes that the
    graph builder is one of the two places that could still reintroduce a write.
    `PurePosixPath` cannot perform I/O by construction — but that is a fact about
    a type this module could stop using, and this fails if it does.
    """

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("the graph builder touched the filesystem")

    for name in ("stat", "lstat", "listdir", "scandir", "open", "access", "readlink", "mkdir"):
        monkeypatch.setattr(os, name, forbidden)

    nodes, edges, _ = build(CYCLE)
    assert (len(nodes), len(edges)) == (5, 3)


def test_nothing_is_logged_at_any_level(caplog: pytest.LogCaptureFixture) -> None:
    """The module has no logger, so a path cannot leak through one.

    docs/SECURITY.md keeps repository paths out of records above `DEBUG` and
    import specifiers out of records entirely. Paths are the only repository
    text this module handles, and the counts worth logging are already logged by
    the pipeline — so the whole class is removed rather than defended, as in
    `analysis/resolver.py`.
    """
    with caplog.at_level(logging.DEBUG):
        build({"src/marker-path.ts": ["./other", "marker-specifier", "./missing"]})
    assert caplog.records == []


# --------------------------------------------------------------------------
# Non-goals, pinned so that "it is not there" stays deliberate.
# --------------------------------------------------------------------------


def test_no_descriptions_and_no_source_tokens_are_produced() -> None:
    """Both belong to modules that do not exist (ADR-013 / ADR-007's scope note)."""
    nodes, _, _ = build(CYCLE)
    assert all(node.description is None and node.sourceToken is None for node in nodes)


def test_the_output_composes_into_an_analyze_response() -> None:
    """The wire contract accepts what this module produces — end to end, once.

    Everything else here checks the builder against its own reasoning. This
    checks it against `app/models/api.py`, which is a separately-written file
    that nothing had ever fed real analysis output to: `AnalyzeResponse` was
    unexercised by any producer, so its field *semantics* were only as good as
    its docstrings. Tuples must survive into `list[GraphNode]` fields, every
    node must satisfy `min_length=1` on `id`/`name`/`path` (which is why the
    root is `"."` and not `""`), and `extra="forbid"` must not trip.
    """
    nodes, edges, stats = build(CYCLE)
    response = AnalyzeResponse(
        repository=Repository(owner=OWNER, name=NAME, commitSha="a" * 40),
        # `list(...)` is not decoration. The analysis modules speak in tuples
        # throughout (`RepositoryAnalysis.files`, `resolve_imports`, and this
        # builder), while the wire models declare `list[GraphNode]` /
        # `list[GraphEdge]`. Pydantic coerces a tuple happily at runtime, so
        # this reads as optional — but mypy strict rejects it, so the router
        # will have to write the same conversion at the same seam.
        nodes=list(nodes),
        edges=list(edges),
        stats=stats,
    )
    assert len(response.nodes) == len(nodes)
    assert response.stats.dependencies == len(response.edges)
    # The deterministic-JSON claim, made against the real serializer.
    assert response.model_dump_json() == response.model_copy(deep=True).model_dump_json()
    # The ADR-013 / ADR-007 fields nothing populates yet. `description` is no
    # longer one of them — it is absent here because this fixture's files carry
    # no header comment, which is the ordinary case rather than a gap.
    assert response.componentDiagram is None
    assert response.serviceMap == []
    assert all(node.description is None for node in response.nodes)


def test_nothing_is_capped_here() -> None:
    """MAX_NODES / MAX_EDGES are the router's, and the router does not exist yet.

    Pinned so the gap is visible: this asserts current behaviour, not a design
    anyone should be happy with. See docs/CURRENT_STATE.md.
    """
    layout = {f"src/f{i:04d}.ts": [f"./f{j:04d}" for j in range(40)] for i in range(200)}
    nodes, edges, stats = build(layout)
    assert len(nodes) == 202
    # Every file imports the same 40 modules, so only those 40 import themselves.
    assert len(edges) == 200 * 40 - 40
    assert stats.dependencies == len(edges)
    assert stats.truncated is False


# --------------------------------------------------------------------------
# Descriptions (ADR-013, ADR-020) — carried, never derived
# --------------------------------------------------------------------------


def test_a_file_nodes_description_comes_from_its_source_record() -> None:
    analysis = make_analysis(
        {"src/a.ts": ["./b"], "src/b.ts": []},
        descriptions={"src/a.ts": "The entry point."},
    )

    nodes, _, _ = build_graph(analysis, resolve_imports(analysis))

    assert node_at(nodes, "src/a.ts").description == "The entry point."
    assert node_at(nodes, "src/b.ts").description is None


def test_directory_nodes_and_the_root_never_carry_a_description() -> None:
    """A directory has no header comment. Deriving one from a child's, or from a
    `README`, is explicitly out of MVP scope (ADR-013)."""
    analysis = make_analysis(
        {"src/a.ts": [], "src/deep/b.ts": []},
        descriptions={"src/a.ts": "A.", "src/deep/b.ts": "B."},
    )

    nodes, _, _ = build_graph(analysis, resolve_imports(analysis))

    assert [n.description for n in nodes if n.type == "directory"] == [None, None, None]
    assert node_at(nodes, ".").description is None


def test_a_repeated_path_takes_the_first_records_description() -> None:
    """Same rule as every other field on a duplicated path: first wins."""
    analysis = make_analysis({"src/a.ts": []}, descriptions={"src/a.ts": "First."})
    first = analysis.files[0]
    doubled = RepositoryAnalysis(
        owner=analysis.owner,
        name=analysis.name,
        ref=analysis.ref,
        commit_sha=analysis.commit_sha,
        files=(*analysis.files, replace(first, description="Second.")),
        skipped=analysis.skipped,
        truncated=analysis.truncated,
        imports_truncated=analysis.imports_truncated,
    )

    nodes, _, _ = build_graph(doubled, resolve_imports(doubled))

    assert node_at(nodes, "src/a.ts").description == "First."


def test_the_model_rejects_a_description_past_the_cap() -> None:
    """The second, independent application of the bound.

    `analysis/descriptions.py` truncates at extraction and this is not a
    substitute for that — it is the boundary check that makes a future producer
    which forgets fail loudly instead of shipping an unbounded string. A
    constant is not a control, so the failure is asserted rather than assumed.
    """
    over = "a" * (Settings().MAX_DESCRIPTION_CHARS + 1)
    analysis = make_analysis({"src/a.ts": []}, descriptions={"src/a.ts": over})

    with pytest.raises(ValidationError):
        build_graph(analysis, resolve_imports(analysis))


def test_a_description_does_not_disturb_deterministic_json() -> None:
    analysis = make_analysis(
        {"src/a.ts": ["./b"], "src/b.ts": []},
        descriptions={"src/a.ts": "Quoted from the repository."},
    )

    first = build_graph(analysis, resolve_imports(analysis))
    second = build_graph(analysis, resolve_imports(analysis))

    assert json.dumps([n.model_dump() for n in first[0]]) == json.dumps(
        [n.model_dump() for n in second[0]]
    )
