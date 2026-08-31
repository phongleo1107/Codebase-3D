"""Graph construction — a file list plus resolved imports become the graph.

`analysis/pipeline.py` says which files exist. `analysis/resolver.py` says what
each import refers to. Neither draws anything, and both say so out loud: the
pipeline returns files in archive order, the resolver returns one record per
import in that same order, and each docstring hands ordering, deduplication,
self-edge removal and the response counters to this module (ADR-016, and
`resolve_imports`). This is where the analysis becomes what the wire contract
describes, and it is the resolver's first caller.

Pure and total in the same sense the resolver is: no I/O, no clock, no
`Deadline`, and — like the resolver, and for the same reason — **no logger**.
The only text this module handles is repository-relative paths, which
docs/SECURITY.md keeps out of log lines above DEBUG, and the counts worth
logging are already logged by the pipeline. A module with no logger cannot leak
one.

## What the graph is

*Nodes.* One per file in `RepositoryAnalysis.files`, plus one per directory that
appears as an ancestor of some file. Directories are **inferred**, never
observed — the archive reader yields no directory entries — so a directory node
exists exactly when something is inside it. External packages are not nodes
(ADR-005); they are counts on the importing file.

*Edges.* One per `Resolution.RESOLVED` import, source file to target file,
`relationship="imports"` and nothing else. Hierarchy travels on `parent`
(ADR-006), which is what makes ``stats.dependencies == len(edges)`` an exact
identity rather than an approximation.

*Node identity is the path.* ``id``, ``path``, and the ``source``/``target`` of
every edge are all the same repository-relative string. There is no second
identifier space to keep in sync, edges are readable in a response body without
a lookup table, and the frontend's node map is keyed by the thing it already
has. The cost is that ids are as long as paths; a payload-size problem would be
solved by capping nodes, not by renaming them.

## Three invariants, and where each is enforced

1. **Order.** Nodes and edges are both sorted by path *components* — the
   `PurePosixPath.parts` tuple, not the path string. Determinism alone would be
   satisfied by either, but component order is the one that puts a directory
   immediately before its contents: sorting strings places ``src/a.ts`` after
   ``src-b`` (``-`` is 0x2d, ``/`` is 0x2f), which splits a directory's children
   away from it. Component order also puts the root first, since its `parts` is
   the empty tuple, so **a parent always precedes its children** and the
   frontend can build the tree in one forward pass. Do not "simplify" this to
   ``sorted(key=str)``; a test pins the ``src-b`` case.

2. **Deduplication.** Edges are a set of ``(source, target)`` pairs. A file
   importing two symbols from the same module, or importing it twice, is one
   dependency — the edge means "A depends on B", and there is no multiplicity in
   the contract to carry a second one.

3. **No self-edges.** ``export * from './index'`` inside ``index.ts`` resolves,
   correctly, to itself. The resolver reports it because it is a true statement
   about the import; the graph drops it because a self-loop is not a dependency
   and renders as an artifact.

The first two are also what makes ``node.imports`` and ``node.importedBy``
meaningful: both are counted off the finished edge set, so
``sum(imports) == sum(importedBy) == len(edges) == stats.dependencies``.
Counting `imports` from the raw import list instead would make the two fields
count different things while sitting side by side in the inspector.

## Counts

``externalImports`` and ``unresolvedImports`` are **statement counts, not
distinct-package counts**: two imports from ``react`` count two. Deduplication
is specified for edges and only for edges, and the resolver deliberately does
not extract a package name from a specifier (that is the component diagram's job
later), so there is nothing here to deduplicate by that would not be a guess.

Directory ``fileCount`` and ``totalBytes`` are **recursive** — every file at or
below the directory, not just its immediate children. That is what a containment
layout sizes a shell by, and it makes ``root.fileCount == stats.files`` a
checkable identity.

## Preconditions, and the two that are checked

Paths in `analysis.files` are repository-relative, with no leading slash and no
``..``: `fetch/archive.py` rejects any member that is not, and every path here
came through it. That is a structural guarantee, not something re-derived, so it
is not re-checked.

Two things *are* checked, because they are the ones a caller could break by
pairing a `RepositoryAnalysis` with resolved imports from a different one:
every record's ``source``, and every resolved ``target``, must be a file in the
analysis. Both raise `ValueError` with a fixed literal message and no path in
it — a programming error, like `path_safety.safe_relative_path`'s, not an
`AppError` and not an HTTP boundary. The resolver makes both unreachable by
resolving against the same set it is handed; the check exists so that a future
caller which does not cannot silently produce an edge to a node that is not
there.

## Two contradictions in untrusted input, resolved rather than raised

*A path that is both a file and a directory.* A tarball may legally contain a
regular file ``components.ts`` **and** a directory ``components.ts/`` holding
``x.ts``. Both survive the archive reader, both are supported extensions, and
the two would want the same node id. **Observed beats inferred**: the file node
is kept and no directory node is created, so the id stays unique, no edge
dangles, no count lies, and ``x.ts``'s ``parent`` still names a node that
exists. It renders as a file with children, which is odd and honest. Raising
instead would let one strange archive fail an entire analysis, which is the
wrong trade for input we did not write.

*The same path twice in `analysis.files`.* Also legal in a tarball. It is one
node: metadata comes from the first record, because node ids are unique by
definition and "first wins" is the only rule that does not depend on how far
the reader got. Import counts are keyed by path, so they aggregate across both
records — the two halves are consistent with each other in the only way they
can be.

## Not done here

**No `MAX_NODES` / `MAX_EDGES` cap.** The builder is uncapped by design of the
task brief: caps and pagination belong to the router. Worth knowing before
writing that router — the cap cannot simply truncate the returned tuples,
because ``stats.dependencies == len(edges)`` and the per-node ``imports`` /
``importedBy`` counters are computed here and would immediately become false.
Whoever applies the cap has to re-derive the stats, or ask this module to build
a smaller graph. Tracked in docs/CURRENT_STATE.md.

**No descriptions.** `GraphNode.description` stays `None` until
`analysis/descriptions.py` exists and puts the file's leading header comment on
`SourceFile` (ADR-013, ADR-016) — it cannot be recovered here, because the bytes
it is quoted from only exist inside the pipeline loop. **No `sourceToken`**
either: ADR-007's mechanism is deferred as one unit and the field stays `None`.

**No layout and no positions.** Graph analysis stays independent of the
visualization layer; placement is the frontend's, in a worker.
"""

from collections import Counter
from pathlib import PurePosixPath
from typing import Final

from app.analysis.pipeline import RepositoryAnalysis, SourceFile
from app.analysis.resolver import Resolution, ResolvedImport
from app.models.graph import GraphEdge, GraphNode, Stats

# `PurePosixPath("src/a.ts").parent.parent` is this, and `PurePosixPath(".")` is
# what `.parents` terminates at, so the repository root is the natural top of
# every path in the analysis rather than a value invented here. Its `parts` is
# the empty tuple, which is why it sorts first, and its `str()` is "." rather
# than "", which is why it satisfies the contract's `min_length=1` on `id`,
# `name` and `path` without a special case.
ROOT: Final = PurePosixPath(".")


def build_graph(
    analysis: RepositoryAnalysis,
    resolved: tuple[ResolvedImport, ...],
) -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...], Stats]:
    """Build the sorted, deduplicated node and edge lists, and the stats.

    ``resolved`` must be the output of
    :func:`~app.analysis.resolver.resolve_imports` over ``analysis``. Pairing it
    with a different analysis raises `ValueError`; see the module docstring.

    An analysis with no files yields no nodes, no edges, and zeroed counters —
    not a lone root node. `analyze_repository` raises `NoSupportedFilesError`
    before it can happen in production, so this is the shape of a direct call
    rather than a state the API can return.
    """
    files = _first_wins(analysis.files)
    known = frozenset(files)

    edges = _edges(resolved, known)
    outbound = Counter(source for source, _ in edges)
    inbound = Counter(target for _, target in edges)

    external = _count(resolved, Resolution.EXTERNAL)
    unresolved = _count(resolved, Resolution.UNRESOLVED)

    nodes = _nodes(analysis, files, outbound, inbound, external, unresolved)
    stats = Stats(
        files=len(files),
        # Counted off the nodes actually emitted, so the file/directory
        # collision above is reported the way it was resolved rather than the
        # way it was inferred.
        directories=sum(1 for node in nodes if node.type == "directory"),
        dependencies=len(edges),
        externalImports=sum(external.values()),
        unresolvedImports=sum(unresolved.values()),
        skippedFiles=analysis.skipped_files,
        truncated=analysis.truncated,
    )
    wire_edges = tuple(
        GraphEdge(source=str(source), target=str(target), relationship="imports")
        for source, target in edges
    )
    return nodes, wire_edges, stats


def _first_wins(files: tuple[SourceFile, ...]) -> dict[PurePosixPath, SourceFile]:
    """Path -> record, keeping the first record for a repeated path.

    ``dict`` insertion order is archive order, which every later pass relies on
    only for its *stability*: the output is sorted regardless.
    """
    records: dict[PurePosixPath, SourceFile] = {}
    for source_file in files:
        records.setdefault(source_file.path, source_file)
    return records


def _edges(
    resolved: tuple[ResolvedImport, ...],
    known: frozenset[PurePosixPath],
) -> tuple[tuple[PurePosixPath, PurePosixPath], ...]:
    """Resolved imports -> sorted, deduplicated, self-edge-free path pairs."""
    pairs: set[tuple[PurePosixPath, PurePosixPath]] = set()
    for record in resolved:
        if record.source not in known:
            raise ValueError("a resolved import names a source that is not in the analysis")

        target = record.target
        # ``target is None`` is redundant by construction —
        # `ResolvedImport.__post_init__` enforces that a target is set exactly
        # when the resolution is RESOLVED, so the first test implies the second.
        # It is kept because it is also what narrows `target` from
        # ``PurePosixPath | None``, and re-deriving that with a cast would state
        # the same fact less honestly.
        if record.resolution is not Resolution.RESOLVED or target is None:
            continue

        if target not in known:
            raise ValueError("a resolved import names a target that is not in the analysis")

        # A file may genuinely import itself (`export * from './index'` inside
        # `index.ts`). True, reported by the resolver, and not a dependency.
        if target == record.source:
            continue

        # A set, so importing the same module twice is one edge.
        pairs.add((record.source, target))

    return tuple(sorted(pairs, key=lambda pair: (pair[0].parts, pair[1].parts)))


def _count(
    resolved: tuple[ResolvedImport, ...],
    resolution: Resolution,
) -> Counter[PurePosixPath]:
    """Imports of one kind, per importing file. Statements, not distinct names."""
    return Counter(record.source for record in resolved if record.resolution is resolution)


def _nodes(
    analysis: RepositoryAnalysis,
    files: dict[PurePosixPath, SourceFile],
    outbound: Counter[PurePosixPath],
    inbound: Counter[PurePosixPath],
    external: Counter[PurePosixPath],
    unresolved: Counter[PurePosixPath],
) -> tuple[GraphNode, ...]:
    """Every file node and every inferred directory node, in component order."""
    file_count: Counter[PurePosixPath] = Counter()
    total_bytes: Counter[PurePosixPath] = Counter()
    ancestors: set[PurePosixPath] = set()
    for path, record in files.items():
        for ancestor in path.parents:
            ancestors.add(ancestor)
            # Recursive by construction: `.parents` is the whole chain up to the
            # root, so every file is counted into each of its ancestors and
            # `root.fileCount == stats.files`.
            file_count[ancestor] += 1
            total_bytes[ancestor] += record.size_bytes

    # Observed beats inferred: a path that is both a real file and some other
    # file's ancestor stays a file node. See the module docstring.
    directories = ancestors - set(files)

    nodes = [
        GraphNode(
            id=str(path),
            name=path.name,
            path=str(path),
            type="file",
            parent=str(path.parent),
            depth=len(path.parts),
            language=record.language,
            bytes=record.size_bytes,
            loc=record.loc,
            # Off the finished edge set, so these agree with `len(edges)`.
            imports=outbound[path],
            importedBy=inbound[path],
            externalImports=external[path],
            unresolvedImports=unresolved[path],
            # `description` and `sourceToken` stay at their defaults: neither
            # has a producer yet, and neither can be derived from a path.
        )
        for path, record in files.items()
    ]
    nodes.extend(
        GraphNode(
            id=str(path),
            # The root's basename is "", which the contract forbids, and its
            # useful label is the repository it is the root of. Every other
            # directory is named by its last component.
            name=analysis.name if path == ROOT else path.name,
            path=str(path),
            type="directory",
            # The one node with no parent (ADR-006).
            parent=None if path == ROOT else str(path.parent),
            depth=len(path.parts),
            fileCount=file_count[path],
            totalBytes=total_bytes[path],
        )
        for path in directories
    )
    return tuple(sorted(nodes, key=lambda node: PurePosixPath(node.path).parts))
