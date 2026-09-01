"""Component diagram — the finished graph, drawn as Mermaid source.

`AnalyzeResponse.componentDiagram` is a **deterministic sketch generated from
the graph**, never a model's description of it (ADR-013). This module produces
it: given the nodes, edges and stats `analysis/graph_builder.py` already built,
plus the service map `analysis/routes.py` already detected, it returns Mermaid
`flowchart` source — or ``None`` when there is nothing to draw.

Three things become three kinds of box, exactly as ADR-013 specifies:

* **Top-level directories become containers.** One per first path component,
  plus one for the repository root when files sit directly in it.
* **External packages become an external system.** Singular, and see below.
* **Detected routes become the API surface**, each pointing at the container
  that declares it.

Everything else is an arrow: a file-to-file import edge is aggregated up to the
pair of containers that hold its endpoints, and the count of collapsed edges
rides on the arrow's label.

## Why the external system has no name

ADR-005 keeps external packages out of the graph entirely — a bare specifier
such as ``react`` is an ``externalImports`` *count* on the importing file node
and nothing else. So by the time the graph is finished the package names are
gone: only `resolver.ResolvedImport.specifier` ever held one, and this module
is deliberately downstream of that. The honest drawing of "this container
depends on 21 external imports" is therefore one shared **External packages**
box with a total, not twelve named boxes this module would have to invent from
data it was not given. Naming them would mean taking the resolver's specifiers
as a second input and putting a new class of repository-authored text into a
response body; that is a decision for an ADR, not for a diagram generator.

## Purity, and why it is golden-file testable

Pure and total in the same sense `analysis/graph_builder.py` is: no I/O, no
clock, no `Deadline`, and **no logger** — the only text handled is paths, route
paths and comment text, all of which docs/SECURITY.md keeps out of log records.
Nothing raises. The same graph produces byte-identical source, which is what
makes the golden fixture in `tests/test_component_diagram.py` a test rather
than a snapshot of whatever ran last: every selection is by an explicit sort
key and every cap is a constant.

## Untrusted text never becomes syntax

Directory names, route paths and route summaries are all repository-authored
(CLAUDE.md: the analyzed repository is untrusted data, *including its
comments*). They reach this module's output in exactly one position — inside a
double-quoted Mermaid label — and the structural reason they cannot escape it
is that **node identifiers are synthetic**: ``c0``, ``r0``, ``ext``. No
repository string is ever concatenated into an identifier, an arrow, or a
subgraph name, so there is no injection site to defend, which is CLAUDE.md's
"eliminate the class rather than defend against it" applied to a text format.

Inside a label, `_label` then does what `analysis/descriptions.py` does to a
comment, for the same reasons: non-printable characters are dropped
(``str.isprintable()`` is False for C0/C1 controls, surrogates, unassigned code
points, and the ``Cf`` format characters that include the Trojan Source
overrides), whitespace is collapsed to single spaces, and the result is capped.
On top of that a small set of characters that mean something to Mermaid or to
an HTML label — ``"``, ``#``, ``&``, ``<``, ``>``, ``%``, ``\\``, `` ` ``,
``{``, ``}``, ``|``, ``;`` — is removed. Removed rather than substituted: a
label is a quotation, and a replacement character is one the repository did not
write. This is the backend half of the rule; the frontend hands the source to
the Mermaid renderer and never builds HTML from it, which is the second,
independent half (docs/ARCHITECTURE.md, "Component diagram and service map").

## Two bounds, and why there are two

`MAX_COMPONENT_DIAGRAM_CHARS` is the contract's bound and it is enforced
**while writing**, not by truncating afterwards — truncated Mermaid is not
Mermaid. `_Writer` refuses a line that would not fit, sections are emitted in
the order boxes-then-arrows, and an arrow whose endpoints were not emitted is
never written, so a diagram cut short by a small limit is still a valid,
smaller diagram.

The item caps (`_MAX_CONTAINERS`, `_MAX_ROUTES`, `_MAX_CONTAINER_EDGES`) are
about legibility rather than safety, and they bite long before the character
budget does: a repository with 300 top-level directories has no readable
component diagram at any size. They are chosen so the worst case lands around
half of the default 20 000 characters, which is what keeps the character bound
a safety net rather than the thing that shapes ordinary output.

## No caller yet

`app/api/` is still empty, so nothing calls this — the router is its first
caller, exactly as `graph_builder.build_graph` was the resolver's. Grep for the
call, not the module.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath
from typing import Final

from app.config import Settings, get_settings
from app.models.api import ServiceEndpoint
from app.models.graph import GraphEdge, GraphNode, Stats

# The container that holds files sitting directly in the repository root.
# `PurePosixPath(".")` is the same value `graph_builder.ROOT` uses and for the
# same reason: it is where `.parents` terminates, so it is not invented here.
_ROOT_KEY: Final = "."
# Our text, not the repository's: `.` is an accurate label and an unreadable
# one, and the repository's own name is already on the enclosing subgraph.
_ROOT_LABEL: Final = "(root)"

# Legibility caps. See the module docstring on why these are not the security
# bound -- `MAX_COMPONENT_DIAGRAM_CHARS` is, and it is enforced by `_Writer`.
_MAX_CONTAINERS: Final = 24
_MAX_ROUTES: Final = 30
_MAX_CONTAINER_EDGES: Final = 80

# Per-label caps, in characters of *output*. A path component can be 200 bytes
# and a summary is already bounded at MAX_ENDPOINT_SUMMARY_CHARS (300), which
# is a fine tooltip and a terrible box label.
_NAME_LABEL_CHARS: Final = 48
_PATH_LABEL_CHARS: Final = 80
_METHOD_LABEL_CHARS: Final = 16
_SUMMARY_LABEL_CHARS: Final = 60

# Characters removed from every label. `"` closes the label, `#` opens a
# Mermaid entity code, `%` starts a comment, and `&<>` are HTML in a renderer
# with htmlLabels on. The rest are removed as a matter of not putting format
# metacharacters into a format we did not write the parser for.
_UNSAFE_LABEL_CHARS: Final = frozenset('"#&<>%\\`{}|;')

# What a label becomes when sanitizing leaves nothing at all -- a directory
# named entirely of control characters is legal in a tarball. An empty Mermaid
# label is a box with no text, which reads as a rendering bug rather than as a
# fact about the repository.
_UNNAMED: Final = "(unnamed)"

# Used only when the graph has no root node, which `build_graph` always emits.
# A direct caller can omit it; the diagram should still be drawable.
_UNNAMED_REPO: Final = "repository"

_END: Final = "  end"
_DECLARATION: Final = "flowchart LR"


def build_component_diagram(
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
    stats: Stats,
    service_map: Sequence[ServiceEndpoint] = (),
    *,
    settings: Settings | None = None,
) -> str | None:
    """Mermaid source for the finished graph, or ``None`` if there is none.

    ``nodes``, ``edges`` and ``stats`` must be one call's output from
    :func:`~app.analysis.graph_builder.build_graph`, and ``service_map`` the
    matching `RepositoryAnalysis.service_map`. Nothing is re-derived from them
    and nothing is validated: the graph builder already guaranteed that every
    edge names nodes that exist, and this module only ever aggregates.

    ``None`` comes back for a graph with no file nodes. That is not a failure —
    it is the same "absent is ordinary" encoding `GraphNode.description` and
    `ServiceEndpoint.summary` use — and it is why
    `AnalyzeResponse.componentDiagram` is optional.

    The result is at most ``MAX_COMPONENT_DIAGRAM_CHARS`` characters long, and
    is a syntactically complete diagram at any length: the bound is applied
    while writing, so a limit small enough to bite drops whole boxes and whole
    arrows rather than cutting a line in half.
    """
    settings = settings if settings is not None else get_settings()

    containers = _containers(nodes)
    # Redundant by construction, and kept: with no containers `_subgraph` is
    # handed no items, draws nothing, and the `drawn_containers` guard below
    # returns `None` anyway. Mutation testing confirms deleting this changes no
    # output. It stays because it states the rule where a reader looks for it —
    # a graph with no file nodes has no component diagram — instead of leaving
    # it to be inferred from an empty set three statements later.
    if not containers:
        return None

    writer = _Writer(settings.MAX_COMPONENT_DIAGRAM_CHARS)
    # The two comments are optional and the declaration is not, so the
    # declaration's cost is reserved before they are offered. A limit small
    # enough to lose it loses the whole diagram: source without a `flowchart`
    # line is a comment, not a smaller diagram, and `None` says that honestly.
    writer.reserve(len(_DECLARATION) + 1)
    writer.write("%% Component diagram, generated from the dependency graph (ADR-013).")
    writer.write("%% Containers are top-level directories; arrows are import counts.")
    writer.release(len(_DECLARATION) + 1)
    # Also redundant by construction, for a reason worth writing down rather
    # than rediscovering: `_DECLARATION` is 12 characters and the shortest
    # possible subgraph header is 20, so a limit that cannot hold the
    # declaration cannot hold a container block either, and the
    # `drawn_containers` guard returns `None` on its own. It stays because the
    # arithmetic that makes it redundant is not local to this line.
    if not writer.write(_DECLARATION):
        return None

    # Containers before routes, and the order is a priority rather than a
    # layout: a budget too small for everything should spend itself on the
    # thing ADR-013 names first. The picture is unaffected — in an `LR`
    # flowchart it is the arrows that place a box, not the declaration order.
    drawn_containers = _subgraph(
        writer,
        f'  subgraph repo["{_repository_name(nodes)}"]',
        [
            (container.node_id, f'    {container.node_id}["{container.label}"]')
            for container in containers
        ],
    )
    if not drawn_containers:
        return None

    routes = _routes(service_map)
    drawn_routes = _subgraph(
        writer,
        '  subgraph api["API surface"]',
        [(route.node_id, f'    {route.node_id}["{route.label}"]') for route in routes],
    )

    external_total = stats.externalImports
    drawn_external = external_total > 0 and writer.write(
        f'  ext["External packages · {_plural(external_total, "import")}"]'
    )

    # An arrow whose endpoints were not both emitted is never written. Mermaid
    # would happily invent a box named `r17` for a dangling id, which is a
    # rendering artifact of our truncation rather than a fact about the code.
    ids = {
        container.key: container.node_id
        for container in containers
        if container.node_id in drawn_containers
    }
    for route in routes:
        target = ids.get(route.container)
        if route.node_id in drawn_routes and target is not None:
            writer.write(f"  {route.node_id} --> {target}")

    for (source_key, target_key), count in _container_edges(edges):
        source, target = ids.get(source_key), ids.get(target_key)
        if source is not None and target is not None:
            writer.write(f"  {source} -->|{count}| {target}")

    if drawn_external:
        for container in containers:
            if container.key in ids and container.external > 0:
                writer.write(f"  {container.node_id} -->|{container.external}| ext")

    return writer.text() or None


class _Container:
    """One top-level directory, with the file nodes that live under it."""

    __slots__ = ("external", "files", "key", "node_id")

    def __init__(self, key: str) -> None:
        self.key = key
        self.files = 0
        self.external = 0
        # Assigned once the render order is fixed; see `_containers`.
        self.node_id = ""

    @property
    def label(self) -> str:
        name = _ROOT_LABEL if self.key == _ROOT_KEY else _label(self.key, _NAME_LABEL_CHARS)
        return f"{name} · {_plural(self.files, 'file')}"


class _Route:
    """One detected endpoint, and the container of the file declaring it."""

    __slots__ = ("container", "label", "node_id")

    def __init__(self, node_id: str, label: str, container: str) -> None:
        self.node_id = node_id
        self.label = label
        self.container = container


def _containers(nodes: Iterable[GraphNode]) -> list[_Container]:
    """Top-level directories, most files first, rendered in path order.

    Built from **file** node paths rather than from directory nodes, so the
    file/directory collision ADR-018 resolves in the file's favour still
    produces a container: ``components.ts/x.ts`` has no ``components.ts``
    directory node to read, and its first path component is a container all the
    same.

    Selection is by file count so that a repository past `_MAX_CONTAINERS`
    shows its largest directories rather than its alphabetically first ones;
    rendering is then by path components, matching `graph_builder`'s ordering
    so the root container comes first (its ``parts`` is the empty tuple).
    """
    found: dict[str, _Container] = {}
    for node in nodes:
        if node.type != "file":
            continue
        key = _container_of(node.path)
        container = found.get(key)
        if container is None:
            container = found[key] = _Container(key)
        container.files += 1
        container.external += node.externalImports or 0

    ranked = sorted(found.values(), key=lambda c: (-c.files, c.key))[:_MAX_CONTAINERS]
    selected = sorted(ranked, key=lambda c: PurePosixPath(c.key).parts)
    for index, container in enumerate(selected):
        container.node_id = f"c{index}"
    return selected


def _container_of(path: str) -> str:
    """The top-level directory a repository-relative path belongs to.

    A file with a single path component sits in the repository root and belongs
    to the root container. Paths here came through `fetch/archive.py`, so they
    are relative with no ``..`` — this does not re-check that (`graph_builder`
    does not either, and for the same reason).
    """
    parts = PurePosixPath(path).parts
    return parts[0] if len(parts) > 1 else _ROOT_KEY


def _repository_name(nodes: Iterable[GraphNode]) -> str:
    """The label for the repository subgraph — the root node's name.

    `build_graph` names the root for the repository precisely because ``"."``'s
    basename is empty (ADR-018), so this is a lookup rather than a derivation.
    """
    for node in nodes:
        if node.parent is None:
            return _label(node.name, _NAME_LABEL_CHARS)
    return _UNNAMED_REPO


def _routes(service_map: Sequence[ServiceEndpoint]) -> list[_Route]:
    """The endpoints to draw, in file order then line order.

    Sorted rather than taken as given. `RepositoryAnalysis.service_map` is
    already deterministic for a commit — it is archive order — but sorting here
    makes the diagram a function of the *set* of endpoints rather than of the
    order they arrived in, which is what lets a test shuffle the input and
    assert byte-identical output.
    """
    ordered = sorted(
        service_map,
        key=lambda e: (PurePosixPath(e.file).parts, e.line, e.method, e.path),
    )[:_MAX_ROUTES]
    return [
        _Route(f"r{index}", _route_label(endpoint), _container_of(endpoint.file))
        for index, endpoint in enumerate(ordered)
    ]


def _route_label(endpoint: ServiceEndpoint) -> str:
    """``GET /users/:id`` — plus the handler's own comment, when there is one.

    One line, joined with a hyphen rather than a ``<br/>``: the diagram source
    is handed to a renderer that may or may not have HTML labels enabled, and
    emitting markup we would then have to keep out of the untrusted half is a
    distinction not worth maintaining for a line break.
    """
    method = _label(endpoint.method, _METHOD_LABEL_CHARS)
    path = _label(endpoint.path, _PATH_LABEL_CHARS)
    head = f"{method} {path}"
    if endpoint.summary is None:
        return head
    summary = _label(endpoint.summary, _SUMMARY_LABEL_CHARS)
    return f"{head} - {summary}" if summary != _UNNAMED else head


def _container_edges(edges: Iterable[GraphEdge]) -> list[tuple[tuple[str, str], int]]:
    """File-to-file imports collapsed onto container pairs, biggest first.

    Edges inside one container are dropped: a container that imports itself is
    every container, and a self-loop on a component diagram says nothing that
    the file count does not. Selection is by weight so a truncated diagram
    keeps the strongest couplings; rendering is by the pair itself so the order
    does not depend on how ties happened to fall.
    """
    counts: Counter[tuple[str, str]] = Counter()
    for edge in edges:
        pair = (_container_of(edge.source), _container_of(edge.target))
        if pair[0] != pair[1]:
            counts[pair] += 1

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:_MAX_CONTAINER_EDGES]
    return sorted(ranked, key=lambda item: item[0])


class _Writer:
    """A line buffer that refuses the line that would break the character cap.

    The cap is `MAX_COMPONENT_DIAGRAM_CHARS`, and applying it here rather than
    to the finished string is the same call `analysis/descriptions.py` makes
    about `MAX_DESCRIPTION_CHARS`: truncating output that has structure
    produces output that no longer has it. Each line is charged its length plus
    one for the separator, which over-counts the final line by one — a
    conservative direction, and it is what makes ``len(text()) <= limit`` hold
    rather than nearly hold.
    """

    __slots__ = ("_limit", "_lines", "_reserved", "_used")

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._lines: list[str] = []
        self._used = 0
        self._reserved = 0

    def write(self, line: str) -> bool:
        cost = len(line) + 1
        if self._used + cost + self._reserved > self._limit:
            return False
        self._lines.append(line)
        self._used += cost
        return True

    def reserve(self, chars: int) -> None:
        self._reserved += chars

    def release(self, chars: int) -> None:
        self._reserved -= chars

    def mark(self) -> int:
        """A position to `rewind` to. See `_subgraph` for the one use."""
        return len(self._lines)

    def rewind(self, mark: int) -> None:
        for line in self._lines[mark:]:
            self._used -= len(line) + 1
        del self._lines[mark:]

    def text(self) -> str:
        return "\n".join(self._lines)


def _subgraph(writer: _Writer, header: str, items: Sequence[tuple[str, str]]) -> frozenset[str]:
    """Write a subgraph block, and report which item ids actually made it.

    Nothing is written for a block with no contents — an empty ``subgraph`` is
    legal Mermaid and renders as an empty labelled box, which is a worse
    statement than saying nothing. "No contents" covers both cases: no items
    were offered, and the character budget accepted none of the ones that were.
    The second is why this rewinds rather than checking ``items`` up front.

    The closing ``end`` is reserved *before* the header is written, so a block
    is never opened that cannot be closed.
    """
    if not items:
        return frozenset()

    start = writer.mark()
    writer.reserve(len(_END) + 1)
    opened = writer.write(header)
    drawn: set[str] = set()
    if opened:
        for node_id, line in items:
            if not writer.write(line):
                break
            drawn.add(node_id)
    writer.release(len(_END) + 1)
    if not drawn:
        writer.rewind(start)
        return frozenset()
    writer.write(_END)
    return frozenset(drawn)


def _label(text: str, limit: int) -> str:
    """Repository text, safe to sit inside a double-quoted Mermaid label.

    Non-printables out, Mermaid and HTML metacharacters out, whitespace
    collapsed, capped at ``limit`` *output* characters — counted while cleaning
    for ADR-020's reason, since cleaning removes characters and no raw length
    reliably yields ``limit`` clean ones. See the module docstring on why
    removal beats substitution.
    """
    out: list[str] = []
    pending_space = False

    for char in text:
        if char.isspace():
            pending_space = bool(out)
            continue
        if not char.isprintable() or char in _UNSAFE_LABEL_CHARS:
            continue
        if pending_space:
            out.append(" ")
            pending_space = False
            if len(out) >= limit:
                break
        out.append(char)
        if len(out) >= limit:
            break

    return "".join(out).rstrip() or _UNNAMED


def _plural(count: int, noun: str) -> str:
    """``1 file`` / ``2 files``. The diagram is read by people."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
