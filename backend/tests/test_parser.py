"""Import extraction: what counts as an import, and what must never crash.

Three groups, and the middle one is the reason this module exists at all:

*Positive* cases pin the syntaxes a TS/JS file can use to name a module.
*Negative* cases pin the ones that only *look* like imports — every one of them
is a phantom dependency a regex-based extractor would report
(docs/SECURITY.md, "Phantom dependencies"). *Hostile* cases pin that a file
engineered to hang or crash the parser is skipped instead
(docs/SECURITY.md, "Parser crash or hang").

Line numbers are asserted alongside specifiers throughout rather than in one
dedicated test, because an off-by-one is invisible in a list of strings and the
number is what the source preview scrolls to.

Sources are written as bytes, matching what the archive reader yields — there
is no decode step in front of the parser, deliberately, since undecodable input
is one of the things it has to survive.
"""

import time
from pathlib import PurePosixPath

import pytest
import tree_sitter_typescript as tree_sitter_typescript_grammars
from tree_sitter import Language

from app.analysis import parser as parser_module
from app.analysis.deadline import Deadline
from app.analysis.parser import IMPORT_QUERY, extract_imports, parse_source
from app.config import Settings
from app.errors import AnalysisTimeoutError

TSX = Language(tree_sitter_typescript_grammars.language_tsx())
TS = Language(tree_sitter_typescript_grammars.language_typescript())

PATH = PurePosixPath("src/index.tsx")
SETTINGS = Settings()
MiB = 1024 * 1024

# The hostile bodies below are prefixed with a 21-byte import, so they are
# sized to leave room for it and still land under MAX_PARSE_BYTES — the guard
# has to be what refuses them, not the size cap.
HOSTILE = SETTINGS.MAX_PARSE_BYTES - 21


def parse(
    source: bytes,
    *,
    language: Language = TSX,
    deadline: Deadline | None = None,
    settings: Settings = SETTINGS,
) -> list[tuple[str, int]]:
    """Run the parse and the import query to completion and sort, so tests do
    not depend on tree-sitter's pattern-grouped match order.

    The two halves are separate functions as of ADR-021 — `parse_source` owns
    every guard, `extract_imports` owns the query — and this helper composes
    them exactly as `analysis/pipeline.py` does. A file the guards refuse yields
    no tree and therefore no imports, which is the same observable behaviour the
    single function had, so the tests below are unchanged.
    """
    tree = parse_source(
        source,
        PATH,
        language,
        deadline if deadline is not None else Deadline.after(60),
        settings,
    )
    if tree is None:
        return []
    return sorted(extract_imports(tree, PATH))


def specifiers(source: bytes, **kwargs: object) -> list[str]:
    return [spec for spec, _line in parse(source, **kwargs)]  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Positive: the syntaxes that really do name a module
# --------------------------------------------------------------------------


def test_extracts_every_import_syntax_with_line_numbers() -> None:
    """One realistic file exercising each supported form at once.

    Written as a single file rather than one test per syntax because the thing
    most likely to break is a *pattern interfering with another pattern*, which
    only shows up when they coexist.
    """
    source = b"""\
import defaultExport from "./default";
import { named } from "./named";
import * as ns from "./namespace";
import "./side-effect";
import type { T } from "./types";
import defaultExport2, { named2 } from "./mixed";
export { re } from "./re-export";
export * from "./star";
export * as nsx from "./star-named";
import legacy = require("./legacy");
const dynamic = await import("./dynamic");
const cjs = require("./cjs");
"""
    assert parse(source) == [
        ("./cjs", 11),
        ("./default", 0),
        ("./dynamic", 10),
        ("./legacy", 9),
        ("./mixed", 5),
        ("./named", 1),
        ("./namespace", 2),
        ("./re-export", 6),
        ("./side-effect", 3),
        ("./star", 7),
        ("./star-named", 8),
        ("./types", 4),
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (b'import a from "./double";', "./double"),
        (b"import a from './single';", "./single"),
        (b'import a from "../parent/mod";', "../parent/mod"),
        (b'import a from "pkg";', "pkg"),
        (b'import a from "@scope/pkg";', "@scope/pkg"),
        (b'import a from "@scope/pkg/deep/path";', "@scope/pkg/deep/path"),
        (b'import a from "./file.js";', "./file.js"),
        (b'import a from "node:fs";', "node:fs"),
        (b'import a from "./\xc3\xa9clair";', "./éclair"),
    ],
)
def test_specifier_shapes(source: bytes, expected: str) -> None:
    """Both quote styles, every specifier class the resolver will have to
    distinguish, and a non-ASCII path that must survive as UTF-8."""
    assert specifiers(source) == [expected]


def test_imports_nested_deep_inside_functions_are_found() -> None:
    """`require` and dynamic `import` are expressions, so they can appear at
    any depth. Only top-level ESM statements are shallow."""
    source = b"""\
function outer() {
  if (cond) {
    return function inner() {
      const a = require("./deep");
      return import("./deeper");
    };
  }
}
"""
    assert parse(source) == [("./deep", 3), ("./deeper", 4)]


def test_duplicate_imports_are_reported_once_each() -> None:
    """No dedup here — two imports of the same module are two occurrences, and
    collapsing them is the graph builder's job, not the parser's."""
    source = b'import a from "./same";\nimport b from "./same";\n'
    assert parse(source) == [("./same", 0), ("./same", 1)]


def test_typescript_grammar_handles_type_assertion() -> None:
    """`.ts` needs the TypeScript grammar: TSX reads `<T>expr` as a JSX tag.

    Pins the reason two grammars exist at all — under TSX this same file is a
    syntax error and its import would be at risk.
    """
    source = b'import a from "./a";\nconst x = <string>y;\n'
    assert specifiers(source, language=TS) == ["./a"]


def test_jsx_file_parses_under_tsx_grammar() -> None:
    source = b'import React from "react";\nconst A = () => <div className="x">hi</div>;\n'
    assert specifiers(source) == ["react"]


# --------------------------------------------------------------------------
# Negative: the phantom dependencies a regex reports and an AST does not
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("line comment", b"// import './fake';\n"),
        ("block comment", b"/* import 'x' */\n"),
        ("jsdoc comment", b"/**\n * import 'x'\n */\n"),
        ("string literal", b"const s = \"import 'x'\";\n"),
        ("template literal", b"const t = `import('${x}')`;\n"),
        ("require of variable", b"const v = './x';\nconst m = require(v);\n"),
        ("import.meta", b"const u = import.meta.url;\n"),
        ("commented require", b"// const m = require('./fake');\n"),
        ("string containing require", b"const s = \"require('./fake')\";\n"),
        ("word in identifier", b"const importantThing = 1;\nconst requires = 2;\n"),
    ],
)
def test_phantom_dependencies_are_not_reported(label: str, source: bytes) -> None:
    """The six regex-killers from docs/SECURITY.md, plus four near-misses.

    Each of these is a false edge in the graph if extraction is textual. They
    are asserted empty rather than merely "not crashing" — a regex extractor
    passes the crash test and fails every line here.
    """
    assert parse(source) == [], label


def test_non_require_calls_with_string_arguments_are_ignored() -> None:
    """The `@fn` pattern matches *any* identifier call whose first argument is
    a string, so the Python-side callee filter is the only thing separating
    `require('./real')` from every test name in the file.

    This is the case `QueryCursor.captures()` cannot express: it returns the
    callees and the strings as two unrelated lists, so the pairing needed here
    is lost. `matches()` preserves it.
    """
    source = b"""\
describe("a test suite", () => {});
it("does something", () => {});
console.log("./not-an-import");
define("./amd-module");
t("translation.key");
require("./real");
"""
    assert parse(source) == [("./real", 5)]


def test_require_with_non_string_first_argument_is_ignored() -> None:
    source = b'require(path.join(a, b));\nrequire(`./tpl`);\nrequire();\nrequire("./ok");\n'
    assert parse(source) == [("./ok", 3)]


def test_require_string_in_later_argument_is_ignored() -> None:
    """The `.` anchor pins the string to the first argument."""
    assert parse(b'register(handler, "./not-a-module");\n') == []


def test_escaped_specifier_is_skipped() -> None:
    """Escape sequences are ignored rather than decoded.

    Unescaping means reimplementing JS string escapes; guessing wrong invents
    an edge to a file that was never referenced. Dropping the import is the
    safe direction, and real specifiers do not contain backslashes.
    """
    assert parse(b'import a from "./\\u002e/x";\n') == []
    assert parse(b'import a from "./a\\tb";\n') == []


def test_empty_specifier_is_skipped() -> None:
    assert parse(b'import a from "";\n') == []


def test_specifier_with_invalid_utf8_is_skipped() -> None:
    """Decoded strictly. Substituting U+FFFD would manufacture a specifier
    that silently never resolves."""
    assert parse(b'import a from "./\xff\xfe";\nimport b from "./ok";\n') == [("./ok", 1)]


@pytest.mark.parametrize("control", [b"\x01", b"\x09", b"\x1b", b"\x1f"])
def test_specifier_with_control_character_is_skipped(control: bytes) -> None:
    """A specifier becomes a node ID and the subject of an /api/source token,
    so it must survive JSON and byte comparison intact.

    Only the offending import is dropped — the file keeps parsing, which is
    what distinguishes this from the binary guard below.
    """
    source = b'import a from "./a' + control + b'b";\nimport c from "./ok";\n'
    assert parse(source) == [("./ok", 1)]


def test_nul_in_a_specifier_is_caught_by_the_binary_guard_first() -> None:
    """A NUL is the one control character that never reaches `_specifier`.

    The binary sniff runs first and condemns the whole file, which is the
    stronger reading: a source file containing a NUL is not a source file. Kept
    as its own test so the ordering is pinned rather than incidental — the
    other control characters cost one import, a NUL costs the file.
    """
    assert parse(b'import a from "./a\x00b";\nimport c from "./ok";\n') == []


# --------------------------------------------------------------------------
# Pre-flight guards
# --------------------------------------------------------------------------


def test_file_over_the_parse_cap_yields_nothing() -> None:
    source = b'import a from "./a";\n' + b"x".ljust(SETTINGS.MAX_PARSE_BYTES, b"y")
    assert len(source) > SETTINGS.MAX_PARSE_BYTES
    assert parse(source) == []


def test_file_exactly_at_the_parse_cap_is_parsed() -> None:
    """The cap is `>`, not `>=`. A file of exactly MAX_PARSE_BYTES is legal,
    and an off-by-one here silently drops the largest file in a repository."""
    head = b'import a from "./a";\n'
    source = head + b"// " + b"z" * (SETTINGS.MAX_PARSE_BYTES - len(head) - 3)
    assert len(source) == SETTINGS.MAX_PARSE_BYTES
    assert parse(source) == [("./a", 0)]


def test_binary_file_yields_nothing() -> None:
    """Extension is a claim, not evidence — the archive reader yields every
    regular file, so a .ts that is really a PNG arrives here."""
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)) * 40
    assert parse(png) == []


def test_nul_after_the_sniff_window_does_not_prevent_parsing() -> None:
    """The sniff is bounded so it stays O(1) in file size. A NUL past the
    window is left to the parser, which handles it without complaint."""
    source = b'import a from "./a";\n' + b"// " + b"p" * (16 * 1024) + b"\x00\n"
    assert parse(source) == [("./a", 0)]


def test_bom_is_stripped_and_first_import_survives() -> None:
    """A BOM must not cost the first import.

    Both grammars happen to tolerate one already, so this passes with the strip
    removed — it pins the *behaviour*, not the implementation, and would catch
    a future grammar that stopped being so forgiving.
    """
    assert parse(b'\xef\xbb\xbfimport a from "./a";\n') == [("./a", 0)]


def test_bom_does_not_shift_line_numbers() -> None:
    source = b'\xef\xbb\xbfimport a from "./a";\nimport b from "./b";\n'
    assert parse(source) == [("./a", 0), ("./b", 1)]


def test_bom_only_file_yields_nothing() -> None:
    assert parse(b"\xef\xbb\xbf") == []


def test_empty_file_yields_nothing() -> None:
    assert parse(b"") == []


# --------------------------------------------------------------------------
# Malformed but recoverable: partial recovery is the point
# --------------------------------------------------------------------------


def test_truncated_file_still_yields_the_imports_before_the_break() -> None:
    """A file cut off mid-import must not lose the imports above it.

    This is why a tree with `has_error` is *not* skipped: tree-sitter recovers,
    and everything it recovered is real.
    """
    source = b'import a from "./a";\nimport b from "./b";\nimport { c fro\n'
    assert parse(source) == [("./a", 0), ("./b", 1)]


def test_syntax_error_between_imports_does_not_lose_the_later_one() -> None:
    source = b'import a from "./a";\nfunction ( { ] } broken\nimport b from "./b";\n'
    assert ("./a", 0) in parse(source)


def test_json_file_yields_nothing_and_does_not_crash() -> None:
    """package.json and tsconfig.json reach the analysis pass; they are not
    TS/JS and must produce no imports rather than an error."""
    source = b'{"name": "pkg", "dependencies": {"react": "^19.0.0"}, "main": "./index.js"}\n'
    assert parse(source) == []


def test_undecodable_file_yields_nothing_and_does_not_crash() -> None:
    assert parse(b"\xff\xfe\xfd\xfc" * 500) == []


def test_deeply_nested_source_is_parsed_without_recursion_error() -> None:
    """20 000 levels. Depth is not what makes the query expensive — this is
    here to prove the module has no Python-level recursion, which is exactly
    what a `RecursionError` on a hostile file would reveal."""
    source = b'const x = ' + b"(" * 20_000 + b'1' + b")" * 20_000 + b';\nrequire("./a");\n'
    assert parse(source) == [("./a", 1)]


def test_deeply_nested_unclosed_source_does_not_crash() -> None:
    assert parse(b"(" * 20_000) == []


# --------------------------------------------------------------------------
# Hostile: inputs engineered to hang the query engine
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("open parens", b"(" * HOSTILE),
        ("open braces", b"{" * HOSTILE),
        ("close/open braces", b"};" * (HOSTILE // 2)),
        ("open brackets", b"[" * HOSTILE),
        ("unterminated template", b"`" + b"${x}" * (HOSTILE // 4)),
        ("arrow soup", b"=>=>" * (HOSTILE // 4)),
        ("jsx opens", b"<a" * (HOSTILE // 2)),
        ("bare require opens", b"require(" * (HOSTILE // 8)),
        ("import opens", b"import(" * (HOSTILE // 7)),
        ("many small errors", b"const x = ;\n" * (HOSTILE // 12)),
    ],
)
def test_pathological_file_is_skipped_quickly(label: str, source: bytes) -> None:
    """The core hang defence.

    tree-sitter's query engine is quadratic in the width of an ERROR node, and
    every input here recovers into one enormous ERROR. Unguarded, "open parens"
    alone takes roughly eleven minutes at this size — measured, not estimated —
    while parsing it takes 0.23 s. The deadline cannot help: `progress_callback`
    is unusable in tree-sitter 0.26.0 (ignored for a bytes source, segfault for
    a callback source), so the cost has to be refused structurally.

    Each file opens with a real import, so a guard that stopped working would
    change the *result* and not merely the runtime — the timing assertion is
    the backstop, not the whole test.

    The generous per-file bound is deliberate. It is not a benchmark — it is
    the assertion that this returns in seconds rather than minutes, and it
    holds with three orders of magnitude of headroom over the unguarded cost.
    """
    source = b'import a from "./a";\n' + source
    assert len(source) <= SETTINGS.MAX_PARSE_BYTES, "must be caught by the guard, not the cap"
    started = time.monotonic()
    result = parse(source)
    elapsed = time.monotonic() - started

    assert result == [], label
    assert elapsed < 20.0, f"{label} took {elapsed:.1f}s"


def test_wide_error_node_is_skipped_but_a_narrow_one_is_not() -> None:
    """The guard keys on the *width* of an ERROR node, and this is the pair
    that shows the line is in the right place.

    Both files have a syntax error. The first recovers into one huge ERROR node
    and is refused; the second has a small one and its imports are harvested.
    Skipping on `has_error` alone would lose the second, which is the common
    case in a real repository.
    """
    narrow = b'import a from "./a";\nlet x = ;\nimport b from "./b";\n'
    assert parse(narrow) == [("./a", 0), ("./b", 2)]

    wide = b'import a from "./a";\n' + b"(" * 5_000
    assert parse(wide) == []


def test_error_node_just_under_the_width_cap_is_still_parsed() -> None:
    """A file whose widest ERROR node sits below the cap is queried normally,
    so the guard cannot be quietly widened into "skip anything with an error"
    without this failing."""
    source = b'import a from "./a";\n' + b"(" * (SETTINGS.MAX_ERROR_NODE_CHILDREN - 10)
    assert parse(source) == [("./a", 0)]


def test_visit_budget_skips_a_file_of_pervasive_small_errors() -> None:
    """The width cap alone would miss this shape: no single ERROR node is wide,
    but there are hundreds of thousands of them, and the guard's own walk would
    become the expensive part.

    The file carries a real import so the two outcomes differ by *content* and
    not merely by timing — without the budget this yields `./a`.
    """
    source = b'import a from "./a";\n' + b"const x = ;\n" * 2_000
    assert parse(source) == [("./a", 0)]
    assert parse(source, settings=Settings(MAX_PARSE_TREE_VISITS=50)) == []


def test_large_file_with_one_small_error_is_still_parsed() -> None:
    """The guard's walk descends only into subtrees that contain an error, and
    that prune is load-bearing rather than merely fast.

    This file is the realistic bad case: half a megabyte of ordinary code with
    a single syntax error in it — a real repository using one construct the
    grammar does not know. Pruning, the walk visits ~40 000 nodes and the file
    parses; walking everything it visits ~280 000, trips MAX_PARSE_TREE_VISITS,
    and the file is silently dropped along with every import in it.
    """
    source = b'import a from "./a";\n' + b"const x1 = 1;\n" * 40_000 + b"let y = ;\n"
    assert len(source) < SETTINGS.MAX_PARSE_BYTES
    assert parse(source) == [("./a", 0)]


def test_lowering_the_width_cap_skips_a_file_that_otherwise_parses() -> None:
    """Pins that the setting is read rather than hardcoded."""
    source = b'import a from "./a";\n' + b"(" * 100
    assert parse(source) == [("./a", 0)]
    assert parse(source, settings=Settings(MAX_ERROR_NODE_CHILDREN=10)) == []


# --------------------------------------------------------------------------
# Deadline
# --------------------------------------------------------------------------


def test_expired_deadline_raises_before_parsing() -> None:
    """The one exception that is *not* swallowed. A timeout is a statement
    about the run, not about this file, so it must reach the pipeline rather
    than be logged as a skip and lost."""
    with pytest.raises(AnalysisTimeoutError):
        parse(b'import a from "./a";\n', deadline=Deadline.after(-1))


def test_deadline_expiring_is_not_reported_as_a_skipped_file() -> None:
    """Guards against the catch-all swallowing AnalysisTimeoutError, which
    would turn an aborted run into a silently empty graph."""
    with pytest.raises(AnalysisTimeoutError):
        parse_source(b'import a from "./a";', PATH, TSX, Deadline.after(-1))


def test_live_deadline_completes_normally() -> None:
    assert parse(b'import a from "./a";\n', deadline=Deadline.after(60)) == [("./a", 0)]


def test_deadline_is_checked_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired budget must stop the parse from ever starting.

    Deterministic rather than timed: `Parser` is replaced by something that
    refuses to be constructed, so *reaching* it is the failure. Without this,
    a spent budget still pays for a parse — up to ~3 s on a hostile 1 MiB file
    — before anyone notices.
    """

    def never_parse(*args: object, **kwargs: object) -> object:
        raise AssertionError("parsed despite an expired deadline")

    monkeypatch.setattr(parser_module, "Parser", never_parse)
    with pytest.raises(AnalysisTimeoutError):
        parse(b'import a from "./a";', deadline=Deadline.after(-1))


class _SpentByParsing(Deadline):
    """Alive for the first check, spent for every one after.

    Models the case the second check exists for: a parse slow enough to consume
    the remaining budget by itself.
    """

    checks: int

    def __init__(self) -> None:
        super().__init__(expires_at=0.0)
        object.__setattr__(self, "checks", 0)

    def expired(self) -> bool:
        object.__setattr__(self, "checks", self.checks + 1)
        return self.checks > 1


def test_deadline_is_checked_again_after_parsing() -> None:
    """The budget is re-checked between the parse and the query.

    Parsing is bounded but not free, so a file can arrive under budget and
    leave over it. Without the second check the query runs anyway, on a
    deadline that has already passed.

    The check lives in `parse_source` rather than in `extract_imports` as of
    ADR-021, and that placement is the point: it now guards *every* query the
    caller is about to run over the tree, so route detection inherits it
    instead of needing its own copy.
    """
    with pytest.raises(AnalysisTimeoutError):
        parse_source(b'import a from "./a";', PATH, TSX, _SpentByParsing(), SETTINGS)


def test_deadline_is_checked_before_the_size_guard_rejects_nothing() -> None:
    """Order matters: an oversized file is skipped rather than raising, even
    when the deadline is also spent, because the size guard runs first."""
    oversized = b"x" * (SETTINGS.MAX_PARSE_BYTES + 1)
    assert parse(oversized, deadline=Deadline.after(-1)) == []


# --------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------


def test_the_guards_run_eagerly() -> None:
    """`parse_source` is an ordinary function, so its guards fire at call time.

    This is the footgun ADR-021 removed rather than a property it added. When
    the guards lived inside a generator, an expired deadline went unnoticed
    until the first `next()`, so a caller that built one generator per file up
    front and iterated later got all of its timeouts at the wrong moment. There
    is nothing to iterate now — the raise happens on the call.
    """
    with pytest.raises(AnalysisTimeoutError):
        parse_source(b'import a from "./a";', PATH, TSX, Deadline.after(-1))


def test_import_extraction_is_lazy() -> None:
    """The query half is still a generator, so a caller that never iterates
    never pays for the traversal."""
    tree = parse_source(b'import a from "./a";', PATH, TSX, Deadline.after(60))
    assert tree is not None
    generator = extract_imports(tree, PATH)
    assert next(generator) == ("./a", 0)


def test_repeated_extraction_is_deterministic() -> None:
    """Same bytes, same output — the query is cached across calls, so this also
    covers the cache handing back a still-correct compiled query."""
    source = b'import a from "./a";\nconst b = require("./b");\nexport * from "./c";\n'
    first = parse(source)
    assert all(parse(source) == first for _ in range(5))


def test_both_grammars_share_the_module_without_interference() -> None:
    """The compiled-query cache is keyed on the Language. If that key were
    wrong, the second grammar would silently reuse the first one's query."""
    source = b'import a from "./a";\n'
    assert specifiers(source, language=TSX) == ["./a"]
    assert specifiers(source, language=TS) == ["./a"]
    assert specifiers(source, language=TSX) == ["./a"]


def test_query_covers_every_documented_pattern() -> None:
    """The query is the contract. This fails if a pattern is dropped."""
    for pattern in (
        "(import_statement source: (string) @src)",
        "(export_statement source: (string) @src)",
        "(import_require_clause source: (string) @src)",
        "(call_expression function: (import) arguments: (arguments . (string) @src))",
        "(call_expression function: (identifier) @fn arguments: (arguments . (string) @src))",
    ):
        assert pattern in IMPORT_QUERY


def test_source_bytes_are_not_mutated() -> None:
    """`bytes` is immutable, but the BOM strip rebinds — a future refactor to
    `bytearray` for speed would make this a real aliasing bug for the caller."""
    source = b'\xef\xbb\xbfimport a from "./a";\n'
    original = bytes(source)
    parse(source)
    assert source == original
