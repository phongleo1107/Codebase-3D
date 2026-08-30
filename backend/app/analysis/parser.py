"""Import extraction — the only place repository source code is parsed.

`extract_imports` turns one file's bytes into ``(specifier, line)`` pairs. It
does **not** resolve them: ``"./util"`` comes out exactly as written, and
turning that into a file in the archive is the resolver's job. Specifiers are
reported, never followed, and no repository code is ever executed
(docs/SECURITY.md, "Repository code executed").

Parsing is deterministic and AST-based rather than textual, which is what makes
the negative cases in the test suite work: ``// import './x'``,
``/* import 'x' */``, ``"import 'x'"``, ``` `import('${x}')` ```,
``require(v)``, and ``import.meta`` are all invisible to a query over the tree,
and a regex reports every one of them as a phantom dependency
(docs/SECURITY.md, "Phantom dependencies").

**Nothing here is fatal.** A file that is too large, binary, undecodable, or
malformed is skipped with a fixed-literal reason and the analysis continues; an
oversized or hostile file must not be able to end a run. The single exception
is `AnalysisTimeoutError`, which is a statement about the *run* rather than
about the file and is deliberately allowed to propagate.

Three findings from the tree-sitter 0.26.0 spike shape this module. Each was
measured, and each is a bug or a trap rather than a preference:

**1. `progress_callback` is unusable, so it is not used.** The obvious way to
bound a pathological parse is the deadline-driven progress callback that
`Parser.parse` advertises. It does not work in 0.26.0 (the current release):
passing it alongside a `bytes` source makes tree-sitter emit
``UserWarning: The progress_callback is ignored when parsing a bytestring``
and never call it, and passing it alongside the chunked-reader source form
**segfaults the interpreter** as soon as the callback actually fires — the C
stack lands in `PyObject_IsTrue` inside `_binding`. A segfault takes the whole
worker with it and no ``except`` can catch it, which is strictly worse than the
hang it was meant to prevent. `Parser.timeout_micros`, the older mechanism, was
removed. There is therefore no in-parse timeout available at all, and the parse
cost has to be bounded structurally instead — by `MAX_PARSE_BYTES` on the way
in and by `_is_pathological` on the way out.

**2. The query engine is quadratic in the width of an ERROR node.** This is the
real hazard, and it is not the parser: a 1 MiB file of ``"("`` *parses* in
0.23 s but then takes roughly **eleven minutes** to query, because tree-sitter
recovers it into a single ERROR node with a million flat children and scanning
those siblings is O(n²) — measured at 0.09 s / 0.95 s / 3.97 s for 10k / 40k /
80k children — fourfold per doubling. Every pattern in `IMPORT_QUERY` costs
the same, so it is the traversal and not the pattern. Depth is *not* the
trigger: a legitimately 20 000-deep nesting queries in 4 ms. `_is_pathological`
rejects the wide-ERROR shape before the query ever runs, which brings the worst
case measured across a sweep of hostile 1 MiB inputs down to ~3.3 s, and that
remainder is parse time, not query time.

**3. `QueryCursor.captures()` cannot express the `require` filter.** It returns
``{"fn": [...], "src": [...]}`` — two independently ordered lists with the
match association thrown away, so there is no way to tell which callee owned
which string. Using it would mean either dropping every ``require()`` or
treating ``describe('not an import')`` as a dependency. `matches()` keeps the
grouping, so it is what this module uses; the `require` test is then an
ordinary Python comparison rather than an `#eq?` predicate, exactly as
docs/ARCHITECTURE.md specifies.
"""

import logging
from collections.abc import Iterator
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Final

from tree_sitter import Language, Node, Parser, Query, QueryCursor

from app.analysis.deadline import Deadline
from app.config import Settings, get_settings
from app.errors import AnalysisTimeoutError

logger = logging.getLogger(__name__)

# Every shape that names a module in TS/JS. `import_require_clause` is the
# TypeScript `import x = require("y")` form; the two `call_expression` patterns
# are dynamic `import()` and, with the callee captured for filtering in Python,
# `require()`. The `.` anchor pins the string to the *first* argument, so
# `foo(bar, './x')` is not mistaken for a module reference.
IMPORT_QUERY: Final = """
(import_statement source: (string) @src)
(export_statement source: (string) @src)
(import_require_clause source: (string) @src)
(call_expression function: (import) arguments: (arguments . (string) @src))
(call_expression function: (identifier) @fn arguments: (arguments . (string) @src))
"""

# Stripped for the benefit of everything downstream, not for the parser's.
# Both grammars tolerate a leading BOM today — it produces no ERROR node and
# the first import still reports line 0, verified for one BOM, two, and one
# mid-file — so deleting this line breaks no test, and it is annotated as a
# known mutation survivor rather than presented as load-bearing. It stays
# because it costs one call and because it makes `source` here the same bytes
# any later byte-offset consumer (a source preview, a highlighter) would slice.
_BOM: Final = b"\xef\xbb\xbf"

# How much of the file is sniffed for a NUL. A NUL anywhere is decisive, but
# scanning only the head keeps the check O(1) in file size, and the formats
# this is aimed at — images, fonts, .wasm, sourcemap-adjacent binaries — all
# carry one in their first few bytes.
_BINARY_SNIFF_BYTES: Final = 8 * 1024

_QUOTES: Final = (b"'", b'"')
_REQUIRE: Final = b"require"


@lru_cache(maxsize=4)
def _compiled_query(language: Language) -> Query:
    """`IMPORT_QUERY` compiled once per grammar.

    Compilation measures ~8.8 ms. At `MAX_SOURCE_FILES` that is ~26 s of the
    60 s analysis budget spent rebuilding a constant, so it is cached rather
    than rebuilt per file. Keyed on the `Language`, which is hashable and
    compares equal across instances wrapping the same grammar. The cache holds
    the compiled query only — `QueryCursor` carries the mutable execution
    state and is created per call.
    """
    return Query(language, IMPORT_QUERY)


def extract_imports(
    source: bytes,
    path: PurePosixPath,
    language: Language,
    deadline: Deadline,
    settings: Settings | None = None,
) -> Iterator[tuple[str, int]]:
    """Yield ``(specifier, line)`` for every module reference in ``source``.

    ``line`` is 0-indexed, as tree-sitter reports it; the frontend adds one.
    Specifiers are raw and unresolved. Order follows tree-sitter's match order,
    which is grouped by query pattern rather than by position — deterministic,
    but not document order.

    ``path`` is used only for logging. ``language`` selects the grammar and is
    the caller's choice: the TSX grammar is a superset covering ``.tsx .js
    .jsx .mjs .cjs``, while ``.ts`` needs the TypeScript grammar, whose
    ``<T>expr`` type assertion TSX would read as JSX.

    Never raises for a bad file — a file it cannot handle simply yields
    nothing. `AnalysisTimeoutError` is the deliberate exception: the budget
    belongs to the run, not to this file, so it propagates.

    This is a generator, so the guards below run on first iteration rather
    than at call time.
    """
    try:
        yield from _collect(source, path, language, deadline, settings or get_settings())
    except AnalysisTimeoutError:
        # The run is over. Not this file's failure to report, and the one thing
        # here that must not be swallowed.
        raise
    except (RecursionError, MemoryError) as exc:
        # Both are subclasses of Exception and would be caught below anyway.
        # They are named because they say something different: the host ran out
        # of a resource, which is worth a louder level than a malformed file.
        logger.warning("parse abandoned, resource exhausted: %s", type(exc).__name__)
    except Exception as exc:
        # The catch-all is the point of the module (docs/SECURITY.md, "Parser
        # crash or hang"). Only the exception *type* is logged: a tree-sitter
        # or codec message can quote the source bytes that caused it, and
        # repository content must never reach a log record.
        _skip(path, f"unexpected {type(exc).__name__}")


def _collect(
    source: bytes,
    path: PurePosixPath,
    language: Language,
    deadline: Deadline,
    settings: Settings,
) -> list[tuple[str, int]]:
    """The real work. Returns a list so that a failure part-way yields nothing."""
    if len(source) > settings.MAX_PARSE_BYTES:
        _skip(path, "file exceeds the parse size cap")
        return []

    # Binary files reach here because the archive reader yields every regular
    # file and extension is a claim, not evidence. Parsing one wastes the
    # budget and can only produce noise.
    if b"\x00" in source[:_BINARY_SNIFF_BYTES]:
        _skip(path, "binary file")
        return []

    source = source.removeprefix(_BOM)

    # Before committing to a parse, not after: the caller loops over thousands
    # of files and this is where an exhausted budget should stop it.
    deadline.check()

    # No progress_callback — see the module docstring. It is ignored for a
    # bytes source (with a UserWarning that `filterwarnings = ["error"]` turns
    # into a test failure) and segfaults for a callback source.
    tree = Parser(language).parse(source)

    root = tree.root_node
    # NOT `root.has_error`. A recoverable syntax error is normal and its
    # imports are still harvested — partial recovery is the whole reason for
    # using tree-sitter here. Only the shape that makes the query quadratic is
    # refused.
    if _is_pathological(root, settings):
        _skip(path, "parse tree is pathologically malformed")
        return []

    # Parsing is bounded but not free — ~3.1 s for the worst 1 MiB input
    # measured. Re-checking here keeps that off the front of the query.
    deadline.check()

    imports: list[tuple[str, int]] = []
    for _pattern, captures in QueryCursor(_compiled_query(language)).matches(root):
        sources = captures.get("src")
        if not sources:
            # Defensive, and a known mutation survivor: every pattern in
            # IMPORT_QUERY binds `@src`, so a match cannot exist without one.
            # `require(variable)` does not reach here — it produces no match at
            # all, because the pattern requires a `(string)` first argument.
            continue
        callees = captures.get("fn")
        # Present only for the bare-identifier call pattern, where the callee
        # decides whether this is a module reference at all. Filtered in Python
        # rather than with `#eq?` (docs/ARCHITECTURE.md); `matches()` is what
        # makes the callee/string pairing available to filter on.
        if callees is not None and (len(callees) != 1 or callees[0].text != _REQUIRE):
            continue
        for node in sources:
            specifier = _specifier(node)
            if specifier is not None:
                imports.append((specifier, node.start_point[0]))
    return imports


def _is_pathological(root: Node, settings: Settings) -> bool:
    """True if querying this tree would be quadratic.

    The trigger is an ERROR node with an enormous number of direct children,
    which is what tree-sitter produces from a file of unbalanced punctuation.
    Real source is nowhere near: a file truncated mid-import has a widest ERROR
    node of 4 children, and a valid file has none at all.

    Two things keep this cheap. `has_error` is O(1) and false for almost every
    real file, so the common case never walks anything; and the walk descends
    only into subtrees that contain an error and stops at the first ERROR wide
    enough to decide, so the hostile 1 MiB case is settled after visiting one
    or two nodes.

    The visit budget is the backstop for the shape the width cap alone would
    miss — a large file of many *small* errors, where no single ERROR node is
    wide but the walk itself would become the expensive part.
    """
    # Pure optimization, and a known mutation survivor: a tree with no error
    # anywhere contains no ERROR node, so the walk below would return False on
    # its own. It earns its place on the common path — almost every real file
    # takes this branch and walks nothing.
    if not root.has_error:
        return False

    visits = 0
    stack = [root]
    while stack:
        node = stack.pop()
        visits += 1
        if visits > settings.MAX_PARSE_TREE_VISITS:
            return True
        if node.type == "ERROR" and node.child_count > settings.MAX_ERROR_NODE_CHILDREN:
            return True
        # A subtree with no error inside it cannot hold an ERROR node, so it is
        # not worth descending into. This is not only a speed-up: without it,
        # half a megabyte of ordinary code containing one syntax error visits
        # ~280 000 nodes instead of ~40 000, trips the budget above, and is
        # wrongly skipped along with every import in it.
        if node.has_error:
            stack.extend(node.children)
    return False


def _specifier(node: Node) -> str | None:
    """The text inside a string literal node, or None if it is not usable.

    Returning None is always "skip this one import", never an error: an
    unusable specifier could not have been resolved anyway.
    """
    # The next three checks are redundant against the grammar as it stands, and
    # mutation testing confirms it: deleting any of them leaves the suite green.
    # A `(string)` node is only ever built for a *terminated* string, so it
    # always carries two matching quotes and is always at least two bytes —
    # verified against unterminated, lone-quote, and mismatched-quote sources,
    # none of which produce a `(string)` node at all. They are kept because
    # what they guard is the difference between a specifier and the entire
    # remainder of the file, and because `node.text` is typed as optional and a
    # grammar is not a contract this module controls.
    raw = node.text
    if raw is None or len(raw) < 2:
        return None

    quote = raw[:1]
    if quote not in _QUOTES or raw[-1:] != quote:
        return None
    body = raw[1:-1]

    if not body:
        return None
    # Escape sequences are vanishingly rare in a real module specifier, and
    # decoding them correctly means reimplementing JS string escapes. Ignoring
    # such an import is safe; guessing at it is not — `'.\\u002e/x'` would have
    # to be unescaped to mean anything, and getting that wrong invents an edge
    # to a file the source never referenced.
    if b"\\" in body:
        return None
    # A specifier becomes a graph node ID and the subject of an /api/source
    # token, so it must survive JSON and comparison intact. A NUL truncates
    # differently in C than in Python — the same reason the archive reader
    # refuses one in a member name — and no control character belongs in a
    # module path.
    if any(byte < 0x20 for byte in body):
        return None

    try:
        # Strict, not "replace". A specifier that is not valid UTF-8 cannot
        # match a path from the archive, and substituting U+FFFD would
        # manufacture one that silently never resolves.
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _skip(path: PurePosixPath, reason: str) -> None:
    """Record a skipped file. ``reason`` is a fixed literal, never file content."""
    # The path is repository data, so it stays at DEBUG; the reason can only be
    # one of the literals in this module and is safe at any level.
    logger.info("parse skipped: %s", reason)
    logger.debug("parse skipped: %s (path=%s)", reason, path)
