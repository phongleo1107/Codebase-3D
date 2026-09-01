"""Module resolution: specifier in, file/external/unresolved out.

Every test here builds a `RepositoryAnalysis` by hand. That is not a shortcut
around the pipeline — it is the contract ADR-016 exists to create. The resolver
resolves against the *file list*, so a fixture list and a real download are the
same input to it, and there is nothing to stub.

Three groups carry most of the weight:

*Precedence.* The order candidates are tried in is the only thing that can be
wrong without being obviously wrong: a specifier that resolves to the *wrong*
file produces a graph that looks complete. Each precedence test therefore builds
a repository where two candidates both exist, so it fails if the order flips
rather than merely if a candidate is missing.

*The set-membership property.* `test_no_filesystem_access` runs a full
resolution with the filesystem primitives torn out from under it, and
`test_every_target_is_a_node` asserts the property the graph builder relies on:
a resolved target is always a file in the analysis, so a dangling edge is not
something to filter out but something that cannot be built.

*Exhaustiveness.* One record per import, always, in order — asserted as an
identity over the whole result rather than case by case.
"""

import os
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

import pytest

from app.analysis.pipeline import _BY_EXTENSION, ImportRef, RepositoryAnalysis, SourceFile
from app.analysis.resolver import (
    EXTENSIONS,
    Resolution,
    ResolvedImport,
    resolve_imports,
)

OWNER = "acme"
NAME = "widgets"
SHA = "a1b2c3d"


def make_analysis(layout: Mapping[str, Sequence[str]]) -> RepositoryAnalysis:
    """A `RepositoryAnalysis` over ``{path: [specifier, ...]}``, in that order.

    Sizes and line counts are zero throughout: the resolver reads neither, and
    giving them plausible-looking values would imply it did.
    """
    return RepositoryAnalysis(
        owner=OWNER,
        name=NAME,
        ref="main",
        commit_sha=SHA,
        files=tuple(
            SourceFile(
                path=PurePosixPath(path),
                language="typescript",
                size_bytes=0,
                loc=0,
                imports=tuple(ImportRef(spec, line) for line, spec in enumerate(specs)),
            )
            for path, specs in layout.items()
        ),
        skipped={},
        truncated=False,
        imports_truncated=False,
    )


def resolve_one(
    specifier: str, *, source: str = "src/main.ts", tree: Sequence[str] = ()
) -> ResolvedImport:
    """Resolve one ``specifier`` written in ``source``, in a repo of ``tree``."""
    layout: dict[str, Sequence[str]] = dict.fromkeys(tree, ())
    layout[source] = (specifier,)
    result = resolve_imports(make_analysis(layout))
    assert len(result) == 1
    return result[0]


def target_of(
    specifier: str, *, source: str = "src/main.ts", tree: Sequence[str] = ()
) -> str | None:
    """The resolved target as a string, or None if it did not resolve."""
    answer = resolve_one(specifier, source=source, tree=tree)
    return None if answer.target is None else str(answer.target)


# --------------------------------------------------------------------------
# Precedence — each of these fails if the order flips, not only if a rule is
# missing. Both candidates exist in every fixture.
# --------------------------------------------------------------------------


def test_ts_esm_rewrite_beats_the_literal_js_file() -> None:
    """`./util.js` means `util.ts` when both exist. The brief's headline case.

    A repository that ships TypeScript sources beside their compiled output is
    ordinary. Trying the literal `.js` first resolves the edge to the build
    artifact while the graph claims to be showing source — wrong, and silent.
    """
    assert target_of("./util.js", tree=["src/util.ts", "src/util.js"]) == "src/util.ts"


def test_ts_esm_rewrite_prefers_ts_over_tsx() -> None:
    assert target_of("./util.js", tree=["src/util.ts", "src/util.tsx"]) == "src/util.ts"


def test_ts_esm_rewrite_falls_through_to_tsx() -> None:
    assert target_of("./util.js", tree=["src/util.tsx", "src/util.js"]) == "src/util.tsx"


def test_literal_js_resolves_when_no_typescript_source_exists() -> None:
    """The rewrite is a preference, not a replacement — `.js` still resolves."""
    assert target_of("./util.js", tree=["src/util.js"]) == "src/util.js"


def test_jsx_rewrites_to_tsx_before_the_literal() -> None:
    assert target_of("./view.jsx", tree=["src/view.tsx", "src/view.jsx"]) == "src/view.tsx"


def test_mjs_does_not_rewrite_to_ts() -> None:
    """`./util.mjs` means `util.mts` and nothing else — so here: unresolved.

    Pins the deliberate absence of an `.mjs` -> `.ts` rewrite, which survived
    adding `.mjs` -> `.mts`. The extension in the specifier names the module kind
    the compiler will emit, and only an `.mts` source emits an `.mjs`; resolving
    to a plain `.ts` would draw the edge to a file TypeScript itself would not
    have chosen.
    """
    answer = resolve_one("./util.mjs", tree=["src/util.ts"])
    assert answer.resolution is Resolution.UNRESOLVED


def test_cjs_does_not_rewrite_to_ts() -> None:
    """The same rule in the CommonJS direction: `./util.cjs` is not `util.ts`."""
    answer = resolve_one("./util.cjs", tree=["src/util.ts"])
    assert answer.resolution is Resolution.UNRESOLVED


def test_mjs_rewrites_to_mts_before_the_literal() -> None:
    """`./util.mjs` is `util.mts` when both it and the emitted `.mjs` exist.

    The headline `.js` -> `.ts` case, in the module-kind-bearing spelling. Both
    candidates exist in the fixture, so flipping the order fails it rather than
    merely finding nothing.
    """
    assert target_of("./util.mjs", tree=["src/util.mts", "src/util.mjs"]) == "src/util.mts"


def test_cjs_rewrites_to_cts_before_the_literal() -> None:
    assert target_of("./util.cjs", tree=["src/util.cts", "src/util.cjs"]) == "src/util.cts"


def test_literal_mjs_resolves_when_no_mts_source_exists() -> None:
    """The rewrite is a preference, not a replacement — plain `.mjs` still wins."""
    assert target_of("./util.mjs", tree=["src/util.mjs"]) == "src/util.mjs"


def test_literal_cjs_resolves_when_no_cts_source_exists() -> None:
    assert target_of("./util.cjs", tree=["src/util.cjs"]) == "src/util.cjs"


def test_mts_and_cts_resolve_literally() -> None:
    """A specifier that already names a TS ESM source needs no rewrite at all."""
    assert target_of("./util.mts", tree=["src/util.mts"]) == "src/util.mts"
    assert target_of("./util.cts", tree=["src/util.cts"]) == "src/util.cts"


def test_extensionless_specifier_reaches_an_mts_file() -> None:
    """`./util` -> `util.mts` via the extension-append step, not the rewrite.

    The step that made `.mts` unreachable before it was analyzed: the candidate
    was generated, missed the set, and there was no way for it to be a node.
    """
    assert target_of("./util", tree=["src/util.mts"]) == "src/util.mts"
    assert target_of("./util", tree=["src/util.cts"]) == "src/util.cts"


def test_mts_directory_index_resolves() -> None:
    assert target_of("./util", tree=["src/util/index.mts"]) == "src/util/index.mts"


def test_ts_beats_mts_when_a_bare_specifier_could_mean_either() -> None:
    """Extension-append order, at the pair that `.mts` joining the list created."""
    assert target_of("./util", tree=["src/util.mts", "src/util.ts"]) == "src/util.ts"


def test_extension_order_is_ts_first() -> None:
    """`./util` with every extension present resolves to the first in order."""
    tree = [f"src/util{extension}" for extension in EXTENSIONS]
    assert target_of("./util", tree=tree) == "src/util.ts"


@pytest.mark.parametrize("extension", EXTENSIONS)
def test_every_supported_extension_is_a_candidate(extension: str) -> None:
    assert target_of("./util", tree=[f"src/util{extension}"]) == f"src/util{extension}"


def test_a_file_beats_a_directory_index() -> None:
    """`./util` is `util.ts`, not `util/index.ts`, when both exist.

    Every file candidate is tried before any directory candidate — including
    the *last* file extension against the *first* index one, which is the pair
    that actually distinguishes the two orderings.
    """
    assert target_of("./util", tree=["src/util/index.ts", "src/util.cjs"]) == "src/util.cjs"


def test_directory_index_resolves() -> None:
    assert target_of("./util", tree=["src/util/index.ts"]) == "src/util/index.ts"


def test_directory_index_extension_order() -> None:
    tree = [f"src/util/index{extension}" for extension in EXTENSIONS]
    assert target_of("./util", tree=tree) == "src/util/index.ts"


# --------------------------------------------------------------------------
# Relative path arithmetic
# --------------------------------------------------------------------------


def test_parent_traversal() -> None:
    assert target_of("../api/index", source="src/ui/app.ts", tree=["src/api/index.ts"]) == (
        "src/api/index.ts"
    )


def test_repeated_parent_traversal() -> None:
    assert target_of("../../top", source="src/a/b.ts", tree=["top.ts"]) == "top.ts"


def test_same_directory() -> None:
    assert target_of("./sibling", source="a/b.ts", tree=["a/sibling.ts"]) == "a/sibling.ts"


def test_file_at_repository_root() -> None:
    assert target_of("./util", source="main.ts", tree=["util.ts"]) == "util.ts"


def test_interior_dot_segments_are_no_ops() -> None:
    assert target_of("./././util", tree=["src/util.ts"]) == "src/util.ts"


def test_interior_parent_segment_is_folded() -> None:
    assert target_of("./a/../util", tree=["src/util.ts"]) == "src/util.ts"


def test_repeated_slashes_are_collapsed() -> None:
    assert target_of(".//util", tree=["src/util.ts"]) == "src/util.ts"


def test_a_dot_segment_is_not_something_a_later_parent_segment_can_consume() -> None:
    """`./a/./../util` is `src/util`, not `src/a/util`.

    `PurePosixPath` collapses a `.` component on its own, so dropping the
    no-op-segment skip *looks* harmless — until a later `..` pops the `.`
    instead of popping `a`, and the import silently resolves one directory too
    deep. Found by mutation; this is the case that distinguishes them.
    """
    assert target_of("./a/./../util", tree=["src/util.ts"]) == "src/util.ts"


def test_an_empty_segment_is_not_something_a_later_parent_segment_can_consume() -> None:
    """The same trap, spelled with a doubled slash instead of a dot."""
    assert target_of("./a//../util", tree=["src/util.ts"]) == "src/util.ts"


def test_climbing_above_the_repository_root_is_unresolved() -> None:
    """The traversal that would matter if anything here touched a filesystem.

    Nothing does, so this is unresolved by arithmetic rather than by refusal —
    but it is pinned, because a `..` that clamped at the root instead of
    failing would silently turn `../../../../etc/passwd` into `etc/passwd` and
    resolve it against a repository that happened to contain that path.
    """
    answer = resolve_one("../../../../etc/passwd", source="src/main.ts")
    assert answer.resolution is Resolution.UNRESOLVED
    assert answer.target is None


def test_traversal_is_not_clamped_to_the_root() -> None:
    """`../../etc/passwd` from a depth-1 file must not become `etc/passwd`."""
    answer = resolve_one("../../etc/passwd.ts", source="src/main.ts", tree=["etc/passwd.ts"])
    assert answer.resolution is Resolution.UNRESOLVED


# --------------------------------------------------------------------------
# Directory-only forms
# --------------------------------------------------------------------------


def test_trailing_slash_is_directory_only() -> None:
    """`./util/` names a directory, so `util.ts` is not a candidate for it."""
    assert target_of("./util/", tree=["src/util.ts", "src/util/index.ts"]) == "src/util/index.ts"


def test_trailing_slash_does_not_fall_back_to_a_file() -> None:
    answer = resolve_one("./util/", tree=["src/util.ts"])
    assert answer.resolution is Resolution.UNRESOLVED


def test_bare_dot_is_the_current_directory_index() -> None:
    assert target_of(".", source="src/main.ts", tree=["src/index.ts"]) == "src/index.ts"


def test_bare_double_dot_is_the_parent_directory_index() -> None:
    assert target_of("..", source="src/a/main.ts", tree=["src/index.ts"]) == "src/index.ts"


def test_dot_at_the_repository_root() -> None:
    assert target_of(".", source="main.ts", tree=["index.ts"]) == "index.ts"


def test_trailing_parent_segment_is_directory_only() -> None:
    """`./a/..` resolves the directory it lands in, not a file beside it."""
    assert target_of("./a/..", source="src/main.ts", tree=["src/index.ts"]) == "src/index.ts"


# --------------------------------------------------------------------------
# Unresolved
# --------------------------------------------------------------------------


def test_relative_import_matching_nothing_is_unresolved() -> None:
    answer = resolve_one("./nope", tree=["src/util.ts"])
    assert answer.resolution is Resolution.UNRESOLVED
    assert answer.target is None


def test_relative_import_of_an_unanalyzed_file_is_unresolved() -> None:
    """A `./styles.css` sitting beside the importer is not a node, so not a target.

    The target set is the *parsed* files (ADR-016), not everything the archive
    held — which is what makes the edge-implies-node property hold. The archive
    yielded `src/styles.css`; the pipeline counted it as an unsupported
    extension and it never entered the list, so nothing here can reach it.
    """
    answer = resolve_one("./styles.css", tree=["src/util.ts"])
    assert answer.resolution is Resolution.UNRESOLVED


def test_an_extension_bearing_specifier_still_tries_the_append_rule() -> None:
    """`./styles.css` does resolve to `styles.css.ts` — and that is correct.

    TypeScript resolves it the same way, and it is how a CSS-modules type
    declaration is imported. Pinned because it looks like the bug the test
    above is about and is the opposite: the target *is* in the file list.
    """
    assert target_of("./styles.css", tree=["src/styles.css.ts"]) == "src/styles.css.ts"


def test_secret_filtered_neighbour_cannot_be_resolved() -> None:
    """`./.env` cannot resolve, because a filtered file is not in the list.

    This is the ADR-016 argument stated as a test: the pipeline removed the
    file before the list was built, so no ordering mistake here can put it
    back. There is no second secret check in this module and there should not
    be one.
    """
    answer = resolve_one("./.env", tree=["src/util.ts"])
    assert answer.resolution is Resolution.UNRESOLVED


def test_an_empty_specifier_is_unresolved() -> None:
    """Unreachable through the parser, and answered anyway.

    `parser._specifier` refuses an empty string body, so no real analysis can
    carry one. But this module is documented as total over `str`, and a
    resolver that raises `IndexError` on an input it merely did not expect is
    worse than one that counts it as unresolved.
    """
    assert resolve_one("").resolution is Resolution.UNRESOLVED


@pytest.mark.parametrize(
    "specifier",
    [
        "/etc/passwd",
        "/src/util",
        "#internal/thing",
        ".hidden",
        "..hidden",
        ".",  # with nothing to resolve to
    ],
)
def test_not_package_shaped_specifiers_are_unresolved_not_external(specifier: str) -> None:
    """These must not inflate the external-dependency count.

    An absolute path, a `package.json` subpath import, and a malformed relative
    form are all things we cannot resolve. Counting them as external would
    report a dependency on a package that does not exist.
    """
    answer = resolve_one(specifier, tree=["src/util.ts"])
    assert answer.resolution is Resolution.UNRESOLVED


# --------------------------------------------------------------------------
# External
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "specifier",
    [
        "react",
        "node:fs",
        "@scope/package",
        "lodash/fp",
        "@scope/package/deep/path",
        "https://esm.sh/react",
        "util",  # a builtin name that is also a plausible local file name
    ],
)
def test_bare_specifiers_are_external(specifier: str) -> None:
    answer = resolve_one(specifier, tree=["src/util.ts"])
    assert answer.resolution is Resolution.EXTERNAL
    assert answer.target is None


def test_a_bare_specifier_never_matches_a_file_of_the_same_name() -> None:
    """`import 'util'` is the Node builtin, never `src/util.ts`.

    Node only resolves a bare specifier against `node_modules` and the builtin
    list — never against the importing file's directory. Resolving it locally
    would invent an edge from a coincidence of naming, which is exactly the
    phantom dependency the AST parser exists to avoid producing.
    """
    answer = resolve_one("util", source="src/main.ts", tree=["src/util.ts", "util.ts"])
    assert answer.resolution is Resolution.EXTERNAL


def test_the_specifier_is_not_normalized() -> None:
    """External specifiers travel exactly as written — no package extraction."""
    answer = resolve_one("@scope/pkg/sub/path.js")
    assert answer.specifier == "@scope/pkg/sub/path.js"


# --------------------------------------------------------------------------
# Shape, order, and exhaustiveness of the result
# --------------------------------------------------------------------------


def test_one_record_per_import() -> None:
    analysis = make_analysis(
        {
            "a.ts": ("./b", "react", "./missing"),
            "b.ts": ("./a",),
            "c.ts": (),
        }
    )
    result = resolve_imports(analysis)
    assert len(result) == sum(len(f.imports) for f in analysis.files) == 4


def test_result_is_in_file_order_then_import_order() -> None:
    analysis = make_analysis({"z.ts": ("react", "lodash"), "a.ts": ("axios",)})
    result = resolve_imports(analysis)
    assert [(str(r.source), r.specifier) for r in result] == [
        ("z.ts", "react"),
        ("z.ts", "lodash"),
        ("a.ts", "axios"),
    ]


def test_line_numbers_are_carried_through() -> None:
    analysis = make_analysis({"a.ts": ("react", "./b", "lodash")})
    result = resolve_imports(analysis)
    assert [r.line for r in result] == [0, 1, 2]


def test_a_repository_with_no_imports_resolves_to_nothing() -> None:
    assert resolve_imports(make_analysis({"a.ts": (), "b.ts": ()})) == ()


def test_an_empty_analysis_resolves_to_nothing() -> None:
    assert resolve_imports(make_analysis({})) == ()


def test_every_target_is_a_node() -> None:
    """The property the graph builder depends on: no edge without a node.

    Asserted over a mixed repository rather than a single import, because it is
    a claim about the module and not about one path.
    """
    analysis = make_analysis(
        {
            "src/main.ts": ("./util", "./util.js", "../out", "react", "./nope", "."),
            "src/util.ts": ("./main",),
            "src/index.ts": ("./util/", "node:fs"),
            "src/util/index.ts": ("../main",),
        }
    )
    nodes = {f.path for f in analysis.files}
    for answer in resolve_imports(analysis):
        assert (answer.target is None) or (answer.target in nodes)


def test_resolution_and_target_agree_on_every_record() -> None:
    analysis = make_analysis({"a.ts": ("./b", "react", "./nope"), "b.ts": ()})
    for answer in resolve_imports(analysis):
        assert (answer.target is not None) is (answer.resolution is Resolution.RESOLVED)


def test_a_record_cannot_claim_a_target_without_resolving() -> None:
    with pytest.raises(ValueError):
        ResolvedImport(
            PurePosixPath("a.ts"), "react", 0, Resolution.EXTERNAL, PurePosixPath("b.ts")
        )


def test_a_record_cannot_resolve_to_nothing() -> None:
    with pytest.raises(ValueError):
        ResolvedImport(PurePosixPath("a.ts"), "./b", 0, Resolution.RESOLVED, None)


def test_records_are_frozen() -> None:
    answer = resolve_one("react")
    with pytest.raises(AttributeError):
        answer.specifier = "lodash"  # type: ignore[misc]


# --------------------------------------------------------------------------
# Cycles and self-reference
# --------------------------------------------------------------------------


def test_a_two_file_cycle_resolves_both_ways() -> None:
    """A -> B -> A. Both edges present, and resolution terminates.

    There is no traversal here to loop — resolution is per-import and
    stateless — which is precisely why this test asserts the edges rather than
    a timeout.
    """
    result = resolve_imports(make_analysis({"a.ts": ("./b",), "b.ts": ("./a",)}))
    assert [(str(r.source), str(r.target)) for r in result] == [
        ("a.ts", "b.ts"),
        ("b.ts", "a.ts"),
    ]


def test_a_three_file_cycle_resolves() -> None:
    layout = {"a.ts": ("./b",), "b.ts": ("./c",), "c.ts": ("./a",)}
    result = resolve_imports(make_analysis(layout))
    assert all(r.resolution is Resolution.RESOLVED for r in result)
    assert len(result) == 3


def test_a_self_import_resolves_to_itself() -> None:
    """Not dropped here. Self-edge removal is the graph builder's job.

    Deciding it here would put half of the determinism contract in this module
    and half in the next one, which is the split ADR-016 declines to make.
    """
    answer = resolve_one("./a", source="a.ts")
    assert answer.resolution is Resolution.RESOLVED
    assert answer.target == PurePosixPath("a.ts")


# --------------------------------------------------------------------------
# The set-membership guarantee
# --------------------------------------------------------------------------


def test_no_filesystem_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolution completes with the filesystem primitives torn out.

    The suite-wide network block in conftest.py makes "this module opens no
    socket" structural rather than trusted; this is the same argument for the
    filesystem. `PurePosixPath` cannot perform I/O by construction, but that is
    a fact about a type the module could stop using — this fails if it does.
    """

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("the resolver touched the filesystem")

    for name in ("stat", "lstat", "listdir", "scandir", "open", "access", "readlink"):
        monkeypatch.setattr(os, name, forbidden)

    analysis = make_analysis(
        {
            "src/main.ts": ("./util", "./util.js", "react", "../escape", "./nope"),
            "src/util.ts": (),
        }
    )
    result = resolve_imports(analysis)
    assert len(result) == 5


def test_a_real_file_on_disk_is_not_a_target() -> None:
    """Existing on the analyzing machine does not make a path resolvable.

    `app/config.py` exists in this checkout. A repository importing
    `./app/config` from its root must still be unresolved, because the target
    set is the archive's parsed files and nothing else.
    """
    answer = resolve_one("./app/config", source="main.ts")
    assert answer.resolution is Resolution.UNRESOLVED


# --------------------------------------------------------------------------
# Byte-exact comparison — inherited from the archive reader's guarantee
# --------------------------------------------------------------------------


def test_paths_are_compared_case_sensitively() -> None:
    """A known, accepted gap: `./Util` does not reach `Util.TS`.

    Candidate extensions are lowercase literals and nothing is folded, so a
    file committed with an uppercase extension is a node that this specifier
    cannot name. Folding would be the wrong fix — `fetch/archive.py` guarantees
    byte-exact member names, and a case fold could collapse two genuinely
    distinct files onto one node. Recorded in docs/CURRENT_STATE.md.
    """
    answer = resolve_one("./Util", source="main.ts", tree=["Util.TS"])
    assert answer.resolution is Resolution.UNRESOLVED


def test_lowercase_specifier_does_not_reach_a_differently_cased_file() -> None:
    answer = resolve_one("./util", source="main.ts", tree=["Util.ts"])
    assert answer.resolution is Resolution.UNRESOLVED


def test_nfc_and_nfd_spellings_are_distinct() -> None:
    """The archive reader keeps both spellings; so must resolution.

    U+00E9 and U+0065 U+0301 render identically. Normalizing either way here
    would resolve an import to a file the source did not name — the same reason
    `fetch/archive.py` refuses to normalize member names.
    """
    nfc, nfd = "café", "café"
    assert resolve_one(f"./{nfc}", source="main.ts", tree=[f"{nfd}.ts"]).target is None
    assert target_of(f"./{nfc}", source="main.ts", tree=[f"{nfc}.ts"]) == f"{nfc}.ts"


# --------------------------------------------------------------------------
# The seam to the pipeline
# --------------------------------------------------------------------------


def test_candidate_extensions_match_the_pipeline() -> None:
    """The two lists must name the same extensions, or nodes become unlinkable.

    `_BY_EXTENSION` decides what is parsed; `EXTENSIONS` decides what can be
    resolved to. An extension in the first and not the second produces file
    nodes that no import can ever reach; the reverse produces candidates that
    can never match. The *order* is deliberately this module's own decision and
    is not asserted against the map.
    """
    assert set(EXTENSIONS) == set(_BY_EXTENSION)


def test_extensions_are_unique() -> None:
    assert len(EXTENSIONS) == len(set(EXTENSIONS))


def test_resolver_accepts_the_pipeline_contract_unchanged() -> None:
    """No field of `SourceFile` is read that ADR-016 does not promise.

    Sizes and line counts are zero in every fixture in this file, and every
    assertion still holds — so the resolver reads `path` and `imports` and
    nothing else.
    """
    analysis = make_analysis({"a.ts": ("./b",), "b.ts": ()})
    assert all(f.size_bytes == 0 and f.loc == 0 for f in analysis.files)
    assert resolve_imports(analysis)[0].target == PurePosixPath("b.ts")
