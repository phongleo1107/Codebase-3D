"""Module resolution — a written specifier becomes a file, a count, or nothing.

`analysis/pipeline.py` stops at the specifier. ``"./util"`` comes out of the
parser exactly as the author typed it, and this module decides what it *refers
to*. Every import gets exactly one of three answers:

* **resolved** — a file that is already in the analysis, so already a node;
* **external** — a bare package specifier, which under ADR-005 is a count on
  the importing node and never a node of its own;
* **unresolved** — a relative specifier that matched nothing.

Nothing here raises. An import that cannot be resolved is an ordinary outcome
of analyzing a repository we did not write, not an error condition, so it is
reported rather than thrown. The graph builder turns these records into edges
and counts; it does not re-derive anything from the specifier string.

**Resolution is set membership, and that is the whole security argument.**
The target set is the paths in `RepositoryAnalysis.files` (ADR-016) — the
content-free record of the files that were actually parsed, after the secret
filter and the extension test. There is no `Path.exists`, no `os.stat`, no
`os.path.join`, and no traversal check, because there is nothing to traverse:
this module never learns whether a path exists on the machine it runs on, only
whether it exists in the archive that was analyzed. Two properties follow, and
they are properties rather than checks:

* **A resolved target is always a node.** The set resolved against and the set
  drawn are the same set, so the graph builder cannot emit an edge with no node
  on the far end. Dangling edges are unrepresentable, not filtered out.
* **A traversing specifier is inert.** ``"../../../../etc/passwd"`` is folded
  against a repo-relative path in memory, misses the set, and is counted as
  unresolved. It reads nothing because nothing here reads.

That is also why `security/path_safety.py` is still not called. Its job is to
keep a resolved path inside a base *directory on disk*, and under ADR-003 there
is no such directory. Wire it in the day something takes a resolved path to a
filesystem — which is not this day, and under ADR-003 is not any day.

**There is no logger in this module, on purpose.** docs/SECURITY.md forbids
logging import specifiers, and a specifier is the one thing this module handles
that a path-shaped log line would otherwise be tempted to include. A module
with no logger cannot leak one.

Paths are compared **byte for byte** — no case folding and no Unicode
normalization, matching the guarantee `fetch/archive.py` makes about the names
it yields. Folding either way here would undo it: NFKC turns U+FF0F into ``/``,
and a case fold could collapse two genuinely distinct files onto one node. The
cost is a real, small gap — a file committed as ``Util.TS`` is a node, but
``./Util`` does not reach it, because the candidate extensions below are
lowercase literals. Accepted; see docs/CURRENT_STATE.md.

## Precedence

For a relative specifier joined against the importing file's directory, in
order, first hit wins:

1. **TS ESM rewrite** — ``.js`` → ``.ts``, ``.tsx``; ``.jsx`` → ``.tsx``;
   ``.mjs`` → ``.mts``; ``.cjs`` → ``.cts``.
2. **The path literally**, if it already carries an extension we analyze.
3. **The path plus each extension**, in the order given by `EXTENSIONS`.
4. **The path as a directory**, plus ``index`` and each extension.

Step 1 is first, and the order is the point. ``import './util.js'`` in a
TypeScript project means ``util.ts`` — the extension in the specifier names the
*emitted* file, not the source. A repository that ships both ``util.ts`` and a
compiled ``util.js`` is ordinary, and trying the literal first would silently
draw the edge to the build output while the graph claims to show the source. A
test builds exactly that fixture and asserts the edge lands on ``util.ts``.

## Scope, and the config seam

MVP scope is relative imports plus bare-specifier-as-external, and nothing
else. **No `tsconfig.json` ``paths``, no ``baseUrl``, no workspace packages**
(TODO.md, "Deferred"). An import that needs one of those is counted as
external, if it is package-shaped, or unresolved otherwise — never guessed at.

How configuration will *enter* this module is decided but not built: **ADR-017,
option 1**. The pipeline recognizes `tsconfig.json` / `jsconfig.json` /
`package.json` while it is already streaming them, parses each one there, and
carries the *narrowed, already-parsed* result — a base directory, an alias
table, a workspace glob list — on `RepositoryAnalysis`. That structure is as
content-free as `loc` and `imports` are, so ADR-016 holds and the test
asserting no field of `SourceFile` is `bytes` keeps passing.

The two rejected options are recorded in ADR-017 with their reasons; the short
version is that carrying raw config *bytes* would break exactly the invariant
ADR-016 pins, and a second harvest pass would have to re-enter an archive that
ADR-003 guarantees was never kept. The seam that follows from the decision is
sketched beside `resolve_imports` below, and `resolve_imports` already takes
the whole `RepositoryAnalysis` rather than just its file list so that adding
the field is additive rather than a signature change at every call site.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final

from app.analysis.pipeline import ImportRef, RepositoryAnalysis

# The extensions a candidate may carry, in the order they are tried. This is
# the *precedence* decision and it belongs here; which extensions are analyzed
# at all is `pipeline._BY_EXTENSION`'s decision and it belongs there. A test
# asserts the two sets are equal, so widening one without the other is a red
# bar rather than a silent class of unresolvable import: an extension the
# pipeline parses but this tuple omits produces a node nothing can link to.
#
# TypeScript extensions come first, and `.cjs` stays last — a test distinguishes
# the two orderings by pitting the last file candidate against the first
# directory one.
EXTENSIONS: Final[tuple[str, ...]] = (
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
)

# TS ESM: the specifier names the file the compiler will *emit*, so the source
# it refers to has a TypeScript extension. Tried before the literal path.
# Values are ordered; `.ts` before `.tsx` matches tsc.
#
# `.mjs` and `.cjs` rewrite to exactly one extension each, and narrowly: tsc
# maps a module-kind-bearing specifier only onto the source that emits *that*
# module kind, so `./x.mjs` means `x.mts` and never `x.ts`. Both were absent
# until 2026-09-01 because `.mts`/`.cts` were not analyzed, so the targets they
# name could not be nodes and the rewrite could only ever miss.
_TS_ESM_REWRITES: Final[Mapping[str, tuple[str, ...]]] = {
    ".js": (".ts", ".tsx"),
    ".jsx": (".tsx",),
    ".mjs": (".mts",),
    ".cjs": (".cts",),
}

# A specifier is relative if it starts one of these ways, and no other way.
_RELATIVE_PREFIXES: Final = ("./", "../")
_RELATIVE_EXACT: Final = frozenset({".", ".."})

# First characters that mean "not a package name", so a specifier we could not
# resolve is counted as unresolved rather than inflating the external count:
#
#   "."  a relative form we do not recognize (`.foo`, `.` inside a longer
#        segment). npm package names cannot begin with a dot, so this is a
#        malformed relative import, not a dependency.
#   "/"  filesystem-absolute, or a bundler alias. The first must never be
#        followed (ADR-003) and the second is config work (ADR-017).
#   "#"  a package-internal subpath import, resolved through `package.json`
#        `"imports"` — config work, deferred with the rest of it.
#
# Backslash-prefixed Windows spellings need no entry: `string_literal_text`
# refuses any specifier containing a backslash outright, so none reaches here.
_NOT_PACKAGE_SHAPED: Final = frozenset({".", "/", "#"})


class Resolution(StrEnum):
    """What one import turned out to be. Exhaustive — every import gets one."""

    RESOLVED = "resolved"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class ResolvedImport:
    """One import, answered. One of these per `ImportRef`, never fewer.

    ``source`` and ``target`` are both paths from `RepositoryAnalysis.files`,
    so the graph builder can use them as node identities directly without
    re-deriving or re-checking anything.

    ``specifier`` and ``line`` are carried through unchanged from the
    `ImportRef` — the specifier exactly as written, never normalized. The
    resolver reads it; it does not rewrite it, and nothing downstream should
    have to re-parse it.

    ``target`` is set **exactly when** ``resolution`` is `Resolution.RESOLVED`.
    That is enforced at construction rather than documented and hoped for,
    because the alternative is a graph builder that has to defend against a
    record claiming both an external classification and an edge target.
    """

    source: PurePosixPath
    specifier: str
    line: int
    resolution: Resolution
    target: PurePosixPath | None

    def __post_init__(self) -> None:
        if (self.target is not None) != (self.resolution is Resolution.RESOLVED):
            raise ValueError("target is set exactly when the import resolved")


def resolve_imports(analysis: RepositoryAnalysis) -> tuple[ResolvedImport, ...]:
    """Answer every import in ``analysis``, in file order then import order.

    Returns one `ResolvedImport` per `ImportRef`, so
    ``len(result) == sum(len(f.imports) for f in analysis.files)`` — a checkable
    identity, in the same spirit as the pipeline's
    ``len(files) + skipped_files``. Nothing is dropped, deduplicated, sorted, or
    counted here: deduplication, self-edge removal, ordering, and the
    ``stats.dependencies == len(edges)`` invariant all belong to the graph
    builder (docs/ARCHITECTURE.md, "Graph model"), and splitting a determinism
    guarantee across two modules is exactly what ADR-016 declines to do.

    A flat sequence is the return shape rather than a mapping keyed by source
    file. Both are one pass apart, but only the flat form makes the
    one-record-per-import identity above a single assertion, and only the flat
    form keeps `line` attached to the record that owns it. Grouping by source —
    which the graph builder wants for per-node external/unresolved counts
    (ADR-005) — is one `Counter` over ``.source``.

    Pure and total: no I/O, no filesystem, no clock, and no exception.

    **It takes no `Deadline`, and what makes that safe is a cap upstream, not
    anything in this function** (ADR-019). An earlier version of this docstring
    justified the omission by saying the function does no unbounded work — "at
    most fifteen set lookups per import, over a file list already capped at
    ``MAX_SOURCE_FILES``". The per-import half is true. The other half was not:
    ``MAX_SOURCE_FILES`` caps *files*, nothing capped imports *per file*, and so
    the total was bounded only by ``MAX_EXTRACTED_BYTES`` (256 MiB) — in a phase
    that runs after `analyze_repository` has already spent its whole 60 s budget.

    Measured 2026-08-31: 3000 files by 334 unresolvable relative imports is
    1 002 000 imports and **76-79 s** here (~77 µs/import). An unresolved
    relative specifier is the worst case by a wide margin — it exhausts every
    candidate before failing, ~65x the cost of a bare package specifier — and it
    is also the cheapest string for an attacker to write.

    That measurement predates the 2026-09-01 `.mts`/`.cts` widening and is now
    an **underestimate**, by inspection rather than by a re-run: `_candidates`
    yields rewrites + 1 literal + ``EXTENSIONS`` + ``EXTENSIONS`` index forms,
    so the worst case (an unresolvable ``./x.js``) went from 15 candidates to
    19, and a bare ``./x`` from 13 to 17. Treat the figures below as a floor
    roughly a quarter under the truth until someone re-measures.

    `analysis/pipeline.py` now stops parsing at ``MAX_IMPORTS`` (100 000), so
    the input to this function is bounded by construction: the same fixture
    measured **7.7 s** under the shipped defaults, on the same pre-widening
    basis. The number to keep in view is ``MAX_IMPORTS`` x the per-import cost
    above; anyone raising that limit — or adding an extension, which lengthens
    every failing lookup — is spending time in *this* function, with no clock
    running to stop it.
    Threading a `Deadline` through here was the alternative and was rejected —
    see ADR-019 for why a partial resolution is a worse output than a bounded
    one.

    The config seam (ADR-017, option 1) attaches here. When `tsconfig` ``paths``
    and workspace resolution land, the pipeline will carry an already-parsed
    config structure on `RepositoryAnalysis`, this function will read it
    alongside ``.files``, and `_resolve_one` will gain two more strategies
    between the relative attempt and the external fallback — in the order
    docs/ARCHITECTURE.md fixes: relative, then ``paths``, then ``baseUrl``, then
    workspace packages, then external. The signature does not change; that is
    why this takes a `RepositoryAnalysis` today rather than the bare file list
    it currently uses.
    """
    targets = frozenset(source_file.path for source_file in analysis.files)
    return tuple(
        _resolve_one(source_file.path, ref, targets)
        for source_file in analysis.files
        for ref in source_file.imports
    )


def _resolve_one(
    source: PurePosixPath,
    ref: ImportRef,
    targets: frozenset[PurePosixPath],
) -> ResolvedImport:
    """Classify and, if it is relative, resolve one specifier."""
    specifier = ref.specifier

    if _is_relative(specifier):
        target = _resolve_relative(source, specifier, targets)
        if target is not None:
            return ResolvedImport(source, specifier, ref.line, Resolution.RESOLVED, target)
        return ResolvedImport(source, specifier, ref.line, Resolution.UNRESOLVED, None)

    # Empty is unreachable — `parser.string_literal_text` refuses an empty body —
    # but `specifier[0]` below is not total without it, and a resolver that
    # crashes on an unexpected input is worse than one that counts it.
    if not specifier or specifier[0] in _NOT_PACKAGE_SHAPED:
        return ResolvedImport(source, specifier, ref.line, Resolution.UNRESOLVED, None)

    # Everything left is package-shaped: `react`, `@scope/pkg`, `node:fs`, a
    # deep import like `lodash/fp`, or a URL import. None of them can name a
    # file in this repository, and under ADR-005 none of them becomes a node.
    # The package *name* is deliberately not extracted here — the specifier
    # travels exactly as written, and whoever needs to group by package (the
    # component diagram's external systems) derives it from that.
    return ResolvedImport(source, specifier, ref.line, Resolution.EXTERNAL, None)


def _is_relative(specifier: str) -> bool:
    """``./x``, ``../x``, ``.`` and ``..`` are relative. Nothing else is."""
    return specifier in _RELATIVE_EXACT or specifier.startswith(_RELATIVE_PREFIXES)


def _resolve_relative(
    source: PurePosixPath,
    specifier: str,
    targets: frozenset[PurePosixPath],
) -> PurePosixPath | None:
    """The first candidate that is a file in the analysis, or None."""
    parts = _join(source, specifier)
    if parts is None:
        return None
    for candidate in _candidates(parts, _is_directory_form(specifier)):
        if candidate in targets:
            return candidate
    return None


def _is_directory_form(specifier: str) -> bool:
    """True when the specifier can only mean a directory.

    ``./util/``, ``.``, ``..`` and ``./a/..`` name a directory and nothing else,
    so the file candidates are skipped and only ``index.*`` is tried. Without
    this, ``./util/`` would happily resolve to ``util.ts``, which is a file the
    specifier explicitly did not ask for.
    """
    return specifier.rsplit("/", 1)[-1] in ("", ".", "..")


def _join(source: PurePosixPath, specifier: str) -> list[str] | None:
    """Fold a relative specifier against the importing file's directory.

    Returns the target's path components, or None if the specifier climbs above
    the repository root. An empty list is the root itself, which is a legal
    result (``./`` from a file at the top level) and only ever a directory.

    This is deliberately hand-rolled rather than `os.path.normpath` or
    `Path.resolve`: both are defined against a real filesystem, `resolve` reads
    one, and normpath's `..` semantics are subtly platform-flavoured. What is
    wanted is pure string arithmetic over repo-relative components — and
    climbing past the root must be a *distinguishable* answer rather than being
    silently clamped, because ``../../../../etc/passwd`` and ``etc/passwd``
    are not the same statement even though neither one can match anything.
    """
    parts = list(source.parent.parts)
    for segment in specifier.split("/"):
        if segment in ("", "."):
            # A repeated or trailing slash, and the no-op `.` segment.
            continue
        if segment == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(segment)
    return parts


def _candidates(parts: Sequence[str], directory_only: bool) -> Iterator[PurePosixPath]:
    """The paths a joined specifier could name, in precedence order.

    Lazy on purpose: the common case hits on the first or second candidate, and
    a specifier that resolves does not pay for the ten that follow.
    """
    # `parts` being empty is a known mutation survivor and is redundant *by
    # construction*: `_join` only returns an empty list when every segment it
    # saw was ``""``, ``"."`` or ``".."``, and `_is_directory_form` is true for
    # exactly those endings — so an empty `parts` always arrives with
    # `directory_only` set and the guard below cannot be the thing that stops
    # it. Kept because the two functions would have to be read together to know
    # that, and `parts[-1]` on the next line is an IndexError if it ever stops
    # holding.
    if parts and not directory_only:
        stem, name = parts[:-1], parts[-1]

        # 1. TS ESM: `./util.js` means `util.ts`, before it means `util.js`.
        suffix = PurePosixPath(name).suffix
        base = name[: len(name) - len(suffix)]
        for replacement in _TS_ESM_REWRITES.get(suffix, ()):
            yield PurePosixPath(*stem, base + replacement)

        # 2. The path exactly as written. Only ever a hit when the specifier
        #    already carries one of `EXTENSIONS`, since nothing else is a node.
        yield PurePosixPath(*parts)

        # 3. `./util` -> `util.ts`, `util.tsx`, ...
        for extension in EXTENSIONS:
            yield PurePosixPath(*stem, name + extension)

    # 4. `./util` -> `util/index.ts`, ... Every file candidate is tried before
    #    any directory one, so a repository holding both `util.ts` and
    #    `util/index.ts` resolves `./util` to the file, as Node and tsc do.
    for extension in EXTENSIONS:
        yield PurePosixPath(*parts, "index" + extension)
