"""Component diagram: a finished graph in, deterministic Mermaid source out.

Like `tests/test_graph_builder.py`, almost every test here runs the **real
resolver and the real graph builder** over a hand-built `RepositoryAnalysis`
and feeds their output straight to `build_component_diagram`. A fixture
therefore describes a repository (`{path: [specifier, ...]}`) rather than
describing a graph, and the properties under test stay properties of the
pipeline the router will actually assemble.

Four groups carry the weight:

*The golden file.* `tests/fixtures/component_diagram_golden.mmd` is the whole
output for one small repository, compared byte for byte. ADR-013 made the
response a pure function of the commit and this is the first place that claim
is cashed as a fixture on disk rather than as a same-input-twice assertion.

*Determinism.* Same input twice, and the same input with its files shuffled,
both produce identical source. The second is the stronger one: it says the
diagram is a function of the graph rather than of the order the graph arrived
in.

*The untrusted half.* Directory names, route paths and route summaries are
repository-authored. They may contain quotes, ``#``, angle brackets, control
characters and bidi overrides, and none of those may reach the output — and
separately, no repository text may appear anywhere outside a quoted label,
which is a structural claim about synthetic node ids rather than about
escaping.

*The bounds.* `MAX_COMPONENT_DIAGRAM_CHARS` is enforced while writing, so a
diagram cut short by a small limit must still be a *valid* diagram: every
subgraph closed, every arrow endpoint declared.
"""

import logging
import os
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

import pytest

from app.analysis.component_diagram import (
    _MAX_CONTAINER_EDGES,
    _MAX_CONTAINERS,
    _MAX_ROUTES,
    build_component_diagram,
)
from app.analysis.graph_builder import build_graph
from app.analysis.pipeline import ImportRef, RepositoryAnalysis, SourceFile
from app.analysis.resolver import resolve_imports
from app.config import Settings
from app.models import AnalyzeResponse, Repository, ServiceEndpoint
from app.models.graph import GraphEdge, GraphNode, Stats

OWNER = "acme"
NAME = "widgets"
FILE_BYTES = 100
FILE_LOC = 7

GOLDEN = Path(__file__).parent / "fixtures" / "component_diagram_golden.mmd"

# The repository the golden file describes: two top-level directories, one file
# in the root, a nested package, four distinct external packages (six import
# statements), and two routes on one file — one commented, one not.
GOLDEN_LAYOUT: Mapping[str, Sequence[str]] = {
    "index.ts": ["./src/app", "react"],
    "src/app.ts": ["./routes/users", "./lib/db", "express", "express"],
    "src/routes/users.ts": ["../lib/db", "zod"],
    "src/lib/db.ts": ["pg"],
    "tests/app.test.ts": ["../src/app", "vitest"],
}
GOLDEN_ROUTES: Mapping[str, Sequence[ServiceEndpoint]] = {
    "src/routes/users.ts": (
        ServiceEndpoint(
            method="GET",
            path="/users/:id",
            file="src/routes/users.ts",
            line=11,
            summary="Fetch one user by id.",
        ),
        ServiceEndpoint(method="POST", path="/users", file="src/routes/users.ts", line=20),
    )
}


# --------------------------------------------------------------------------
# Fixture construction: a repository, not a graph.
# --------------------------------------------------------------------------


def make_analysis(
    layout: Mapping[str, Sequence[str]],
    *,
    name: str = NAME,
    routes: Mapping[str, Sequence[ServiceEndpoint]] | None = None,
    sizes: Mapping[str, int] | None = None,
) -> RepositoryAnalysis:
    routes = routes or {}
    sizes = sizes or {}
    files = tuple(
        SourceFile(
            path=PurePosixPath(path),
            language="TypeScript",
            size_bytes=sizes.get(path, FILE_BYTES),
            loc=FILE_LOC,
            imports=tuple(
                ImportRef(specifier=specifier, line=line)
                for line, specifier in enumerate(specifiers)
            ),
            routes=tuple(routes.get(path, ())),
        )
        for path, specifiers in layout.items()
    )
    return RepositoryAnalysis(
        owner=OWNER,
        name=name,
        ref="main",
        commit_sha="a1b2c3d",
        files=files,
        skipped={},
        truncated=False,
        imports_truncated=False,
    )


def render(
    layout: Mapping[str, Sequence[str]],
    *,
    name: str = NAME,
    routes: Mapping[str, Sequence[ServiceEndpoint]] | None = None,
    settings: Settings | None = None,
) -> str:
    """The diagram for a repository. Asserts one was produced."""
    source = render_optional(layout, name=name, routes=routes, settings=settings)
    assert source is not None
    return source


def render_optional(
    layout: Mapping[str, Sequence[str]],
    *,
    name: str = NAME,
    routes: Mapping[str, Sequence[ServiceEndpoint]] | None = None,
    settings: Settings | None = None,
) -> str | None:
    analysis = make_analysis(layout, name=name, routes=routes)
    nodes, edges, stats = build_graph(analysis, resolve_imports(analysis))
    return build_component_diagram(
        nodes, edges, stats, analysis.service_map, settings=settings
    )


def labels(source: str) -> list[str]:
    """Every quoted label in the diagram, in order."""
    return [part for index, part in enumerate(source.split('"')) if index % 2 == 1]


def outside_labels(source: str) -> str:
    """Everything that is *not* inside a quoted label — the syntax half."""
    return "".join(part for index, part in enumerate(source.split('"')) if index % 2 == 0)


def declared_ids(source: str) -> set[str]:
    """Node ids that the diagram declares, i.e. that carry a label."""
    found: set[str] = set()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("%%", "flowchart", "subgraph", "end")) or "[" not in stripped:
            continue
        found.add(stripped.split("[", 1)[0])
    return found


def referenced_ids(source: str) -> set[str]:
    """Node ids that an arrow names on either end."""
    found: set[str] = set()
    for line in source.splitlines():
        stripped = line.strip()
        # A declaration carries a label, and a label may legally contain
        # anything -- including "-->". Arrows never carry one.
        if "-->" not in stripped or "[" in stripped:
            continue
        head, tail = stripped.split("-->", 1)
        found.add(head.strip())
        found.add(tail.rsplit("|", 1)[-1].strip())
    return found


def endpoint(path: str, *, file: str, line: int = 0, summary: str | None = None) -> ServiceEndpoint:
    return ServiceEndpoint(method="GET", path=path, file=file, line=line, summary=summary)


# --------------------------------------------------------------------------
# The golden file.
# --------------------------------------------------------------------------


def test_the_whole_diagram_matches_the_golden_file() -> None:
    """One repository, one file on disk, byte for byte.

    This is the assertion ADR-013 exists to make possible: with no LLM in the
    path the diagram is a pure function of the commit, so a checked-in fixture
    is a test rather than a record of whatever ran last. Every other test in
    this file checks one rule; this one checks that the rules compose into
    exactly the source a reviewer can read.

    The fixture ends in a newline because files do. The diagram does not emit
    a trailing separator, so the newline is added here rather than stripped
    there — stripping would make the comparison blind to a stray blank line.
    """
    source = render(GOLDEN_LAYOUT, routes=GOLDEN_ROUTES)
    assert source + "\n" == GOLDEN.read_text(encoding="utf-8")


def test_the_golden_diagram_is_structurally_closed() -> None:
    """Read as syntax rather than as text: blocks balance, arrows land."""
    source = render(GOLDEN_LAYOUT, routes=GOLDEN_ROUTES)
    lines = [line.strip() for line in source.splitlines()]
    assert lines.count("end") == sum(1 for line in lines if line.startswith("subgraph"))
    assert referenced_ids(source) <= declared_ids(source)


# --------------------------------------------------------------------------
# Determinism.
# --------------------------------------------------------------------------


def test_the_same_graph_produces_identical_source() -> None:
    assert render(GOLDEN_LAYOUT, routes=GOLDEN_ROUTES) == render(
        GOLDEN_LAYOUT, routes=GOLDEN_ROUTES
    )


def test_file_order_does_not_change_the_source() -> None:
    """The diagram is a function of the graph, not of archive order.

    `RepositoryAnalysis.files` arrives in archive order and
    `RepositoryAnalysis.service_map` inherits it. Both are deterministic for a
    given commit, so preserving them would also pass a same-input-twice test —
    and would break the moment a repository was re-tarred. Sorting is what
    makes this assertion possible.
    """
    reversed_layout = dict(reversed(list(GOLDEN_LAYOUT.items())))
    assert render(reversed_layout, routes=GOLDEN_ROUTES) == render(
        GOLDEN_LAYOUT, routes=GOLDEN_ROUTES
    )


def test_routes_are_ordered_by_path_components_not_by_path_string() -> None:
    """The `src-b` case, again (ADR-018).

    ``-`` is 0x2d and ``/`` is 0x2f, so sorting the file strings puts
    ``src/a.ts`` after ``src-b.ts``. Endpoints are grouped by the file that
    declares them, so the same key the graph builder uses is the one that keeps
    a file's routes together.
    """
    layout: dict[str, list[str]] = {"src-b.ts": [], "src/a.ts": []}
    routes = {
        "src-b.ts": (endpoint("/late", file="src-b.ts"),),
        "src/a.ts": (endpoint("/early", file="src/a.ts"),),
    }
    source = render(layout, routes=routes)
    assert 'r0["GET /early"]' in source
    assert 'r1["GET /late"]' in source


# --------------------------------------------------------------------------
# Containers: top-level directories.
# --------------------------------------------------------------------------


def test_only_top_level_directories_become_containers() -> None:
    """``src/lib`` is a directory node in the graph and not a container here."""
    source = render(GOLDEN_LAYOUT, routes=GOLDEN_ROUTES)
    container_labels = [label for label in labels(source) if " file" in label]
    assert container_labels == ["(root) · 1 file", "src · 3 files", "tests · 1 file"]


def test_the_root_container_is_rendered_first() -> None:
    """Ordered by path *components*, as `graph_builder` orders nodes (ADR-018).

    ``PurePosixPath(".").parts`` is the empty tuple, so the root sorts first for
    free. Sorting the key *strings* instead is right for every container name
    anyone would type and wrong for a directory beginning with ``-``, which is
    0x2d against ``.``'s 0x2e — the same off-by-one-codepoint mistake ADR-018
    documents for ``src-b``, arriving from the other side.
    """
    source = render({"index.ts": [], "-lib/a.ts": []})
    assert source.index('"(root) · 1 file"') < source.index('"-lib · 1 file"')


def test_files_in_the_repository_root_get_their_own_container() -> None:
    """Labelled ``(root)`` rather than ``.``.

    ``.`` is what the graph calls it and is accurate; it is also unreadable as
    a box label, and the repository's own name is already on the enclosing
    subgraph. The substitute is our text, so it is not a sanitizing question.
    """
    source = render({"index.ts": [], "src/a.ts": []})
    assert '"(root) · 1 file"' in source
    assert '["."' not in source


def test_a_container_exists_even_when_the_directory_node_does_not() -> None:
    """ADR-018's file/directory collision, seen from downstream.

    A tarball may carry ``components.ts`` *and* ``components.ts/x.ts``. The
    graph builder keeps the file node and infers no directory (observed beats
    inferred), so a diagram that read containers off directory *nodes* would
    silently drop ``x.ts``'s container. Containers are read off file paths for
    exactly this reason.
    """
    source = render({"components.ts": [], "components.ts/x.ts": []})
    assert '"components.ts · 1 file"' in source
    assert '"(root) · 1 file"' in source


def test_the_subgraph_is_named_for_the_repository() -> None:
    source = render(GOLDEN_LAYOUT, name="my-repo")
    assert 'subgraph repo["my-repo"]' in source


def test_a_graph_with_no_root_node_still_draws() -> None:
    """A direct caller may hand over nodes `build_graph` would never omit.

    The repository name lives on the root node, and the diagram falls back to a
    generic label rather than raising: this module is downstream of every
    validation the analysis already did, and a missing label is not worth
    failing a response over.
    """
    nodes = (
        GraphNode(
            id="src/a.ts",
            name="a.ts",
            path="src/a.ts",
            type="file",
            parent="src",
            depth=2,
            externalImports=0,
        ),
    )
    source = build_component_diagram(nodes, (), Stats(files=1, directories=0, dependencies=0))
    assert source is not None
    assert 'subgraph repo["repository"]' in source


def test_a_graph_with_no_files_produces_no_diagram() -> None:
    """``None``, not an empty flowchart. `componentDiagram` is optional."""
    assert build_component_diagram((), (), Stats(files=0, directories=0, dependencies=0)) is None


# --------------------------------------------------------------------------
# Arrows: imports collapsed onto container pairs.
# --------------------------------------------------------------------------


def test_edges_between_containers_carry_the_collapsed_count() -> None:
    layout = {
        "src/a.ts": ["../lib/one", "../lib/two"],
        "lib/one.ts": [],
        "lib/two.ts": [],
    }
    source = render(layout)
    assert "-->|2|" in source


def test_edges_inside_one_container_are_dropped() -> None:
    """Every container imports itself; saying so on a component diagram is noise."""
    source = render({"src/a.ts": ["./b"], "src/b.ts": []})
    assert "-->" not in source


def test_an_arrow_is_drawn_between_two_different_containers() -> None:
    source = render({"src/a.ts": ["../lib/b"], "lib/b.ts": []})
    assert "c1 -->|1| c0" in source


# --------------------------------------------------------------------------
# External packages: one system, a count, and no names (ADR-005).
# --------------------------------------------------------------------------


def test_external_packages_become_one_external_system_with_a_total() -> None:
    source = render(GOLDEN_LAYOUT)
    assert 'ext["External packages · 6 imports"]' in source


def test_no_external_system_when_nothing_is_external() -> None:
    source = render({"src/a.ts": ["./b"], "src/b.ts": []})
    assert "ext[" not in source
    assert "External packages" not in source


def test_each_container_carries_its_own_external_import_count() -> None:
    source = render({"src/a.ts": ["react", "react", "zod"], "tests/a.test.ts": ["vitest"]})
    assert "c0 -->|3| ext" in source
    assert "c1 -->|1| ext" in source


def test_a_container_with_no_external_imports_gets_no_arrow() -> None:
    """Not ``-->|0|``. An arrow labelled zero is a dependency that is not there.

    Needs two containers to be a real test: with every container importing
    something external, drawing the arrow unconditionally is indistinguishable
    from drawing it when the count is positive.
    """
    source = render({"src/a.ts": ["react"], "lib/b.ts": []})
    assert "-->|0|" not in source
    assert source.count("ext") == 2  # the declaration, and one arrow


def test_no_package_is_ever_named() -> None:
    """ADR-005 keeps names out of the graph, so they cannot be here.

    Worth pinning rather than leaving implicit: the only input that ever held a
    package name is `resolver.ResolvedImport.specifier`, and this module is
    deliberately not given it. If a future change starts naming packages, that
    is a new class of repository-authored text in a response body and needs an
    ADR — this test is what makes that change loud.
    """
    source = render({"src/a.ts": ["react-dom", "@scope/secret-pkg", "node:fs"]})
    assert "react-dom" not in source
    assert "@scope" not in source
    assert "node:fs" not in source


def test_the_external_total_is_the_stats_total_not_the_drawn_total() -> None:
    """The box states the repository's number, and the arrows state the drawn one.

    With more top-level directories than `_MAX_CONTAINERS`, some containers are
    not drawn and their external arrows are not either. The total on the box
    still counts them, because it is a fact about the repository rather than
    about the picture.
    """
    layout = {f"d{index:03d}/a.ts": ["react"] for index in range(_MAX_CONTAINERS + 5)}
    source = render(layout)
    assert f'ext["External packages · {_MAX_CONTAINERS + 5} imports"]' in source
    assert source.count("-->|1| ext") == _MAX_CONTAINERS


# --------------------------------------------------------------------------
# The API surface.
# --------------------------------------------------------------------------


def test_a_route_becomes_a_node_pointing_at_its_container() -> None:
    layout: dict[str, list[str]] = {"src/routes.ts": []}
    routes = {"src/routes.ts": (endpoint("/users", file="src/routes.ts"),)}
    source = render(layout, routes=routes)
    assert 'r0["GET /users"]' in source
    assert "r0 --> c0" in source


def test_a_route_summary_is_appended_to_its_label() -> None:
    layout: dict[str, list[str]] = {"src/routes.ts": []}
    routes = {
        "src/routes.ts": (endpoint("/users", file="src/routes.ts", summary="List every user."),)
    }
    assert 'r0["GET /users - List every user."]' in render(layout, routes=routes)


def test_a_summary_that_sanitizes_away_leaves_the_label_alone() -> None:
    """No dangling separator, and no ``(unnamed)`` glued to a real path.

    A comment of nothing but bidi overrides is legal and normalizes to an empty
    string here. The route is still a route; only the gloss is gone.
    """
    layout: dict[str, list[str]] = {"src/routes.ts": []}
    routes = {"src/routes.ts": (endpoint("/users", file="src/routes.ts", summary="‮#&"),)}
    assert 'r0["GET /users"]' in render(layout, routes=routes)


def test_no_api_subgraph_when_no_routes_were_detected() -> None:
    """An empty labelled box is a worse statement than no box."""
    source = render(GOLDEN_LAYOUT)
    assert "API surface" not in source
    assert "subgraph api" not in source


def test_routes_are_grouped_by_file_then_line() -> None:
    layout: dict[str, list[str]] = {"src/b.ts": [], "src/a.ts": []}
    routes = {
        "src/b.ts": (
            endpoint("/b-second", file="src/b.ts", line=9),
            endpoint("/b-first", file="src/b.ts", line=2),
        ),
        "src/a.ts": (endpoint("/a", file="src/a.ts", line=5),),
    }
    source = render(layout, routes=routes)
    assert [label for label in labels(source) if label.startswith("GET ")] == [
        "GET /a",
        "GET /b-first",
        "GET /b-second",
    ]


# --------------------------------------------------------------------------
# Untrusted repository text. CLAUDE.md: the repository is untrusted data,
# including its comments.
# --------------------------------------------------------------------------


HOSTILE = '" ]) #quot; <script>alert(1) %% {a} `b` |c| ;d& \\e'


def test_no_label_metacharacter_survives_into_the_source() -> None:
    """Quotes, entity codes, comment markers and HTML, from three directions.

    A directory name, a route path and a route summary are all repository-
    authored, and all three land inside a double-quoted Mermaid label. The
    characters that could close that label, open a Mermaid entity or comment,
    or be read as HTML by a renderer with ``htmlLabels`` on are removed at the
    label, not escaped — see the module docstring on why removal beats
    substitution.
    """
    layout: dict[str, list[str]] = {f"{HOSTILE}/a.ts": []}
    routes = {f"{HOSTILE}/a.ts": (endpoint("/x", file=f"{HOSTILE}/a.ts", summary=HOSTILE),)}
    source = render(layout, routes=routes)

    for character in '"#&<>%\\`{}|;':
        assert character not in "".join(labels(source))
    assert "script" in "".join(labels(source))  # the *text* survives; the markup does not


def test_control_characters_and_bidi_overrides_are_dropped() -> None:
    """``str.isprintable()`` covers Trojan Source, as it does in `descriptions.py`.

    U+202E RIGHT-TO-LEFT OVERRIDE reorders how the rest of a line displays
    without changing what it is — a display-spoofing primitive aimed at exactly
    the kind of surface a diagram label is.
    """
    layout: dict[str, list[str]] = {"a‮b\x00c\x1bd/x.ts": []}
    source = render(layout)
    assert '"abcd · 1 file"' in source


def test_a_newline_in_repository_text_cannot_add_a_line() -> None:
    """The one metacharacter that would break the format structurally.

    A path component may contain a newline; a Mermaid label may not. Whitespace
    is collapsed rather than dropped so that two words do not run together.
    """
    baseline = render({"ab/x.ts": []})
    hostile = render({"a\nb/x.ts": []})
    assert len(hostile.splitlines()) == len(baseline.splitlines())
    assert '"a b · 1 file"' in hostile


def test_repository_text_never_appears_outside_a_quoted_label() -> None:
    """The structural claim, and the reason there is no injection site.

    Node identifiers are synthetic — ``c0``, ``r0``, ``ext`` — so no repository
    string is ever concatenated into an identifier, an arrow or a subgraph
    name. Escaping is the second line of defence; this is the first, and it is
    the one that does not depend on the denylist above being complete.
    """
    layout = {"marker-directory/a.ts": ["marker-package"]}
    routes = {
        "marker-directory/a.ts": (
            endpoint("/marker-route", file="marker-directory/a.ts", summary="marker summary"),
        )
    }
    syntax = outside_labels(render(layout, routes=routes, name="marker-repo"))
    assert "marker" not in syntax


def test_a_name_that_sanitizes_to_nothing_becomes_a_placeholder() -> None:
    """An empty label renders as a box with no text, which reads as a bug."""
    source = render({"‮​/a.ts": []})
    assert '"(unnamed) · 1 file"' in source


def test_a_long_directory_name_is_capped() -> None:
    source = render({("z" * 300) + "/a.ts": []})
    assert f'"{"z" * 48} · 1 file"' in source
    assert "z" * 49 not in source


def test_a_long_route_path_and_summary_are_capped() -> None:
    """`MAX_ENDPOINT_SUMMARY_CHARS` is 300, which is a fine tooltip and a
    terrible box label, so the diagram caps again and more tightly."""
    layout: dict[str, list[str]] = {"src/a.ts": []}
    routes = {
        "src/a.ts": (
            endpoint("/" + "p" * 300, file="src/a.ts", summary="s" * 290),
        )
    }
    rendered = labels(render(layout, routes=routes))
    label = next(item for item in rendered if item.startswith("GET"))
    # 80 and 60 characters of *output*, so the leading "/" is one of the path's.
    assert "p" * 79 in label
    assert "p" * 80 not in label
    assert "s" * 60 in label
    assert "s" * 61 not in label


# --------------------------------------------------------------------------
# Bounds. The character cap is the contract's; the item caps are legibility's.
# --------------------------------------------------------------------------


def test_containers_are_capped_and_the_largest_are_kept() -> None:
    """A repository past the cap shows its biggest directories, not its first.

    Selection is by file count and rendering is by path, so this is two
    different orders on purpose: alphabetical selection would hide whatever
    happened to be named ``z``.
    """
    layout: dict[str, list[str]] = {
        f"d{index:03d}/a.ts": [] for index in range(_MAX_CONTAINERS + 3)
    }
    layout["zzz/a.ts"] = []
    layout["zzz/b.ts"] = []
    source = render(layout)
    assert len([label for label in labels(source) if " file" in label]) == _MAX_CONTAINERS
    assert '"zzz · 2 files"' in source


def test_routes_are_capped() -> None:
    layout: dict[str, list[str]] = {"src/a.ts": []}
    routes = {
        "src/a.ts": tuple(
            endpoint(f"/r{index:03d}", file="src/a.ts", line=index)
            for index in range(_MAX_ROUTES + 20)
        )
    }
    source = render(layout, routes=routes)
    assert len([label for label in labels(source) if label.startswith("GET ")]) == _MAX_ROUTES


def test_container_edges_are_capped_and_the_heaviest_are_kept() -> None:
    """Enough distinct container pairs to exceed the cap, with one clear winner."""
    layout: dict[str, list[str]] = {}
    for index in range(14):
        layout[f"d{index:02d}/a.ts"] = [
            f"../d{other:02d}/a" for other in range(14) if other != index
        ]
    layout["hot/a.ts"] = ["../hot-target/one", "../hot-target/two"]
    layout["hot-target/one.ts"] = []
    layout["hot-target/two.ts"] = []
    source = render(layout)
    arrows = [line for line in source.splitlines() if "-->" in line and "ext" not in line]
    assert len(arrows) == _MAX_CONTAINER_EDGES
    assert any("-->|2|" in arrow for arrow in arrows)


def test_the_source_never_exceeds_the_configured_character_cap() -> None:
    settings = Settings(MAX_COMPONENT_DIAGRAM_CHARS=400)
    layout = {f"d{index:03d}/a.ts": ["react"] for index in range(_MAX_CONTAINERS)}
    source = render(layout, settings=settings)
    assert len(source) <= 400


def test_a_truncated_diagram_is_still_a_valid_diagram() -> None:
    """The bound is applied while writing, because truncated Mermaid is not Mermaid.

    Two things have to survive a limit small enough to bite: every ``subgraph``
    is still closed by an ``end``, and every arrow still names ids the diagram
    declared. A dangling id is not a syntax error in Mermaid — it silently
    invents a box labelled ``r17``, which is an artifact of our truncation
    presented as a fact about the repository.
    """
    layout = {f"d{index:03d}/a.ts": ["react"] for index in range(_MAX_CONTAINERS)}
    routes = {
        "d000/a.ts": tuple(
            endpoint(f"/r{index:02d}", file="d000/a.ts", line=index) for index in range(_MAX_ROUTES)
        )
    }
    for limit in range(20, 2400, 37):
        source = render_optional(
            layout, routes=routes, settings=Settings(MAX_COMPONENT_DIAGRAM_CHARS=limit)
        )
        if source is None:
            continue
        assert len(source) <= limit
        lines = [line.strip() for line in source.splitlines()]
        assert lines.count("end") == sum(1 for line in lines if line.startswith("subgraph"))
        assert referenced_ids(source) <= declared_ids(source)


def test_an_empty_subgraph_is_never_emitted() -> None:
    """Not even when the budget, rather than the input, is what emptied it.

    Checking ``items`` before opening the block catches only half of it: a
    limit that accepts the ``subgraph`` line and then nothing else leaves a box
    with a label and no contents, which reads as a repository with an API
    surface of zero routes rather than as our truncation. The writer rewinds.
    """
    layout: dict[str, list[str]] = {f"d{index:03d}/a.ts": [] for index in range(_MAX_CONTAINERS)}
    routes = {
        "d000/a.ts": tuple(
            endpoint("/" + "r" * 70, file="d000/a.ts", line=index) for index in range(_MAX_ROUTES)
        )
    }
    for limit in range(20, 2400, 13):
        source = render_optional(
            layout, routes=routes, settings=Settings(MAX_COMPONENT_DIAGRAM_CHARS=limit)
        )
        if source is None:
            continue
        lines = [line.strip() for line in source.splitlines()]
        assert not any(
            lines[index].startswith("subgraph") and lines[index + 1] == "end"
            for index in range(len(lines) - 1)
        )
        # And the block is not merely empty-looking: abandoning the header
        # without rewinding it leaves an unclosed `subgraph` instead, which is
        # a parse error rather than an empty box.
        assert lines.count("end") == sum(1 for line in lines if line.startswith("subgraph"))


def test_containers_outrank_routes_when_the_budget_is_short() -> None:
    """ADR-013 names top-level directories first, and so does the writer.

    A diagram of routes with no containers is not a smaller component diagram;
    it is a different one. The declaration order is therefore a priority, not a
    layout — in an `LR` flowchart it is the arrows that place a box.
    """
    layout: dict[str, list[str]] = {"src/a.ts": [], "lib/b.ts": []}
    routes = {
        "src/a.ts": tuple(
            endpoint("/" + "r" * 70, file="src/a.ts", line=index) for index in range(_MAX_ROUTES)
        )
    }
    source = render(layout, routes=routes, settings=Settings(MAX_COMPONENT_DIAGRAM_CHARS=260))
    assert '"lib · 1 file"' in source
    assert "API surface" not in source


def test_a_limit_too_small_for_one_container_produces_no_diagram() -> None:
    """``None``, for the reason an empty subgraph is not emitted either."""
    settings = Settings(MAX_COMPONENT_DIAGRAM_CHARS=40)
    assert render_optional({"src/a.ts": []}, settings=settings) is None


def test_the_worst_legible_case_fits_comfortably_under_the_default_cap() -> None:
    """The item caps are chosen so the character cap is a net, not the shape.

    If this ever fails, the caps and the limit have drifted apart and the
    ordinary output of a large repository is being decided by truncation.
    """
    layout: dict[str, list[str]] = {}
    for index in range(_MAX_CONTAINERS):
        name = f"{'d' * 60}{index:03d}"
        layout[f"{name}/a.ts"] = ["react"] + [
            f"../{'d' * 60}{other:03d}/a" for other in range(_MAX_CONTAINERS) if other != index
        ]
    routes = {
        f"{'d' * 60}000/a.ts": tuple(
            endpoint("/" + "p" * 200, file=f"{'d' * 60}000/a.ts", line=index, summary="s" * 290)
            for index in range(_MAX_ROUTES)
        )
    }
    source = render(layout, routes=routes)
    assert len(source) < Settings().MAX_COMPONENT_DIAGRAM_CHARS // 2


# --------------------------------------------------------------------------
# Purity, pinned structurally rather than claimed.
# --------------------------------------------------------------------------


def test_no_filesystem_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """`graph_builder`'s argument, re-used. ADR-003 says nothing touches disk."""

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("the component diagram touched the filesystem")

    analysis = make_analysis(GOLDEN_LAYOUT, routes=GOLDEN_ROUTES)
    nodes, edges, stats = build_graph(analysis, resolve_imports(analysis))
    for name in ("stat", "lstat", "listdir", "scandir", "open", "access", "readlink", "mkdir"):
        monkeypatch.setattr(os, name, forbidden)

    assert build_component_diagram(nodes, edges, stats, analysis.service_map) is not None


def test_nothing_is_logged_at_any_level(caplog: pytest.LogCaptureFixture) -> None:
    """The module has no logger, so a path or a comment cannot leak through one."""
    with caplog.at_level(logging.DEBUG):
        render(
            {"marker-path/a.ts": ["marker-specifier"]},
            routes={
                "marker-path/a.ts": (
                    endpoint("/marker", file="marker-path/a.ts", summary="marker summary"),
                )
            },
        )
    assert caplog.records == []


def test_nothing_raises_on_a_graph_this_module_did_not_build() -> None:
    """Total, like the resolver. A caller pairing mismatched inputs gets a
    diagram, not an exception: every lookup here is an aggregation, and the
    graph builder already guaranteed the invariants worth checking."""
    nodes = (
        GraphNode(id=".", name=NAME, path=".", type="directory", parent=None, depth=0),
        GraphNode(
            id="src/a.ts", name="a.ts", path="src/a.ts", type="file", parent="src", depth=2
        ),
    )
    edges = (GraphEdge(source="src/a.ts", target="nowhere/b.ts", relationship="imports"),)
    stats = Stats(files=1, directories=1, dependencies=1)
    routes = (endpoint("/x", file="nowhere/b.ts"),)
    source = build_component_diagram(nodes, edges, stats, routes)
    assert source is not None
    assert referenced_ids(source) <= declared_ids(source)


# --------------------------------------------------------------------------
# The wire contract.
# --------------------------------------------------------------------------


def test_the_diagram_composes_into_an_analyze_response() -> None:
    """`MAX_COMPONENT_DIAGRAM_CHARS` gets a producer, so its model-side check
    finally has something to check. Enforced twice, as `MAX_DESCRIPTION_CHARS`
    is: once while writing, which is the one that matters, and once at the
    model boundary, which catches a future producer that forgets."""
    analysis = make_analysis(GOLDEN_LAYOUT, routes=GOLDEN_ROUTES)
    nodes, edges, stats = build_graph(analysis, resolve_imports(analysis))
    response = AnalyzeResponse(
        repository=Repository(owner=OWNER, name=NAME, commitSha="a" * 40),
        nodes=list(nodes),
        edges=list(edges),
        stats=stats,
        serviceMap=list(analysis.service_map),
        componentDiagram=build_component_diagram(nodes, edges, stats, analysis.service_map),
    )
    assert response.componentDiagram is not None
    assert response.model_dump_json() == response.model_copy(deep=True).model_dump_json()
