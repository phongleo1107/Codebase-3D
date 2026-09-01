"""Route detection: what is a route, what only looks like one, and what a
summary is allowed to be.

The middle group is the reason this file is long. A member call whose property
is an HTTP verb is extremely common in code that has nothing to do with HTTP —
`map.get`, `cache.delete`, `headers.set` — so the negative cases here are the
route-detection form of `tests/test_parser.py`'s phantom dependencies, and they
matter more: a wrong edge is one line among thousands in a graph, while a wrong
endpoint is one row in a service map of six, where a reader has no way to tell
it from a real one.

Sources are written as bytes, matching what the archive reader yields and what
`parse_source` takes.
"""

from itertools import islice
from pathlib import PurePosixPath

import pytest
import tree_sitter_typescript as tree_sitter_typescript_grammars
from tree_sitter import Language

from app.analysis import routes as routes_module
from app.analysis.deadline import Deadline
from app.analysis.parser import parse_source
from app.analysis.routes import detect_routes
from app.config import Settings
from app.errors import AnalysisTimeoutError
from app.models.api import ServiceEndpoint

TSX = Language(tree_sitter_typescript_grammars.language_tsx())
TS = Language(tree_sitter_typescript_grammars.language_typescript())

SETTINGS = Settings()
SERVER = "src/server.ts"


def endpoints(
    source: bytes,
    *,
    path: str = SERVER,
    language: Language = TSX,
    settings: Settings = SETTINGS,
    deadline: Deadline | None = None,
) -> list[ServiceEndpoint]:
    """Parse and detect, exactly as `analysis/pipeline.py` composes them."""
    member = PurePosixPath(path)
    budget = deadline if deadline is not None else Deadline.after(60)
    tree = parse_source(source, member, language, budget, settings)
    assert tree is not None, "the fixture should parse; the guards are parser.py's tests"
    return list(detect_routes(tree, member, budget, settings))


def summary(source: bytes, **kwargs: object) -> str | None:
    found = endpoints(source, **kwargs)  # type: ignore[arg-type]
    assert len(found) == 1
    return found[0].summary


def routed(source: bytes, **kwargs: object) -> list[tuple[str, str]]:
    """Just `(method, path)`, for tests that do not care about line or summary."""
    return [(e.method, e.path) for e in endpoints(source, **kwargs)]  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Positive: method calls that really are route registrations
# --------------------------------------------------------------------------


def test_detects_every_verb_with_line_numbers() -> None:
    """One realistic router exercising each supported verb at once.

    Written as a single file rather than one test per verb because the failure
    most worth catching is a pattern interfering with another pattern, which
    per-verb tests cannot see. Line numbers are asserted alongside, because an
    off-by-one is invisible in a list of paths and the number is what the
    inspector scrolls to.
    """
    source = (
        b"const app = express();\n"
        b"app.get('/users', listUsers);\n"
        b"app.post('/users', createUser);\n"
        b"app.put('/users/:id', replaceUser);\n"
        b"app.patch('/users/:id', updateUser);\n"
        b"app.delete('/users/:id', removeUser);\n"
        b"app.head('/health', ping);\n"
        b"app.options('/users', preflight);\n"
        b"app.all('/legacy', anything);\n"
    )

    assert [(e.method, e.path, e.line) for e in endpoints(source)] == [
        ("GET", "/users", 1),
        ("POST", "/users", 2),
        ("PUT", "/users/:id", 3),
        ("PATCH", "/users/:id", 4),
        ("DELETE", "/users/:id", 5),
        ("HEAD", "/health", 6),
        ("OPTIONS", "/users", 7),
        ("ALL", "/legacy", 8),
    ]


def test_the_receiver_can_be_anything() -> None:
    """`app`, `router`, `server.app` — the object is not part of the rule.

    Deliberately not filtered on a name allowlist: every framework spells its
    router differently and half of them let the user pick the variable, so a
    name test would be a guess that silently drops real routes.
    """
    source = (
        b"router.get('/a', h);\n"
        b"server.app.post('/b', h);\n"
        b"this.instance.put('/c', h);\n"
        b"fastify.delete('/d', h);\n"
    )

    assert routed(source) == [("GET", "/a"), ("POST", "/b"), ("PUT", "/c"), ("DELETE", "/d")]


def test_the_file_is_the_repository_path() -> None:
    """`ServiceEndpoint.file` is the graph node ID, so it must be the path the
    graph builder used — not a basename and not an absolute path."""
    found = endpoints(b"app.get('/x', h);\n", path="packages/api/src/routes.ts")

    assert found[0].file == "packages/api/src/routes.ts"


def test_a_double_quoted_path_is_the_same_as_a_single_quoted_one() -> None:
    assert routed(b'app.get("/x", h);\n') == [("GET", "/x")]


def test_extra_arguments_are_fine() -> None:
    """Middleware between the path and the handler is the common Express shape."""
    assert routed(b"app.get('/x', auth, rateLimit, h);\n") == [("GET", "/x")]


def test_detects_routes_under_the_typescript_grammar_too() -> None:
    """`.ts` uses a different grammar from `.tsx` (see pipeline `_BY_EXTENSION`),
    so the query has to hold under both."""
    assert routed(b"app.get('/x', h);\n", language=TS) == [("GET", "/x")]


def test_a_route_inside_a_function_is_still_a_route() -> None:
    source = b"export function mount(app) {\n  app.get('/x', h);\n}\n"

    assert routed(source) == [("GET", "/x")]


# --------------------------------------------------------------------------
# Negative: member calls that only look like route registrations
# --------------------------------------------------------------------------


def test_a_map_lookup_is_not_a_route() -> None:
    """The single most common false positive available.

    `map.get('key')` satisfies the verb test and nothing else. Both further
    conditions independently reject it — the path does not begin with `/` and
    there is no handler argument — which is deliberate belt-and-braces on the
    check that matters most.
    """
    source = b"const v = map.get('key');\nconst w = cache.delete('user:1');\n"

    assert routed(source) == []


def test_a_path_that_is_not_a_url_path_is_not_a_route() -> None:
    """The leading-slash rule, isolated: two arguments, an HTTP verb, and still
    not a route, because `'key'` is not a URL."""
    assert routed(b"store.get('key', fallback);\n") == []


def test_a_call_with_no_handler_is_not_a_route() -> None:
    """The arity rule, isolated. `app.get('/x')` with one argument is Express's
    own settings *getter*, not a registration — and a route with no handler is
    not a route in any framework."""
    assert routed(b"const port = app.get('/x');\n") == []


def test_express_settings_access_is_not_a_route() -> None:
    assert routed(b"const proxy = app.get('trust proxy');\n") == []


def test_a_verb_that_is_not_the_property_is_not_a_route() -> None:
    """`get` has to be the thing being called on something, not an argument or a
    bare function."""
    source = b"get('/x', h);\nfetch('/x', opts);\nemitter.on('get', h);\n"

    assert routed(source) == []


def test_the_path_must_be_the_first_argument() -> None:
    """The `.` anchor in the query. Without it, `emitter.on('evt', '/x')` and
    every other call that happens to carry a slash-leading string in a later
    position becomes an endpoint."""
    assert routed(b"tracker.get(handler, '/x');\n") == []


def test_a_non_literal_path_is_not_a_route() -> None:
    """A path built at runtime cannot be reported, because we do not run code.

    Absent rather than guessed: reporting `${prefix}/users` verbatim would put a
    URL in the service map that no request can ever match.
    """
    source = b"app.get(PATH, h);\napp.post(`${prefix}/users`, h);\napp.put('/a' + b, h);\n"

    assert routed(source) == []


def test_a_route_in_a_comment_or_a_string_is_not_a_route() -> None:
    """The whole reason this is a query over a tree rather than a regex."""
    source = (
        b"// app.get('/commented', h);\n"
        b"/* app.post('/blocked', h); */\n"
        b"const doc = \"app.put('/quoted', h)\";\n"
    )

    assert routed(source) == []


def test_a_chained_route_call_is_not_detected() -> None:
    """A known and deliberate gap, pinned so it stays deliberate.

    `router.route('/x').get(h)` splits the path and the verb across two calls:
    `route` is not an HTTP verb, and the `.get(h)` that follows has no string
    first argument. Detecting it means correlating two nodes, and the module
    docstring explains why absent beats invented. If this test ever fails,
    someone has added chain support and should delete it.
    """
    assert routed(b"router.route('/x').get(h).post(h2);\n") == []


def test_an_unknown_verb_is_not_a_route() -> None:
    assert routed(b"app.subscribe('/x', h);\napp.use('/api', router);\n") == []


def test_a_verb_is_matched_case_insensitively_on_the_property() -> None:
    """`app.GET` is not idiomatic but is legal, and reporting it is harmless —
    the verb set is lowercased before the test, so this is one behaviour rather
    than an accident of which case the fixture used."""
    assert routed(b"app.GET('/x', h);\n") == [("GET", "/x")]


# --------------------------------------------------------------------------
# Summaries: the comment above the handler
# --------------------------------------------------------------------------


def test_a_jsdoc_block_above_the_route_becomes_the_summary() -> None:
    source = b"/** List every user. */\napp.get('/users', h);\n"

    assert summary(source) == "List every user."


def test_a_multi_line_jsdoc_is_collapsed() -> None:
    source = b"/**\n * List every user.\n * Paginated.\n */\napp.get('/users', h);\n"

    assert summary(source) == "List every user. Paginated."


def test_a_line_comment_above_the_route_becomes_the_summary() -> None:
    source = b"// List every user.\napp.get('/users', h);\n"

    assert summary(source) == "List every user."


def test_a_run_of_line_comments_is_joined() -> None:
    source = b"// List every user.\n// Paginated.\napp.get('/users', h);\n"

    assert summary(source) == "List every user. Paginated."


def test_a_blank_line_inside_a_comment_run_ends_it() -> None:
    """The run is unbroken or it is not a run.

    Only the two lines touching the handler are its summary; the line above the
    gap documents something else. `descriptions._line_run` makes the same call
    on the same shape.
    """
    source = b"// Unrelated.\n\n// List every user.\n// Paginated.\napp.get('/users', h);\n"

    assert summary(source) == "List every user. Paginated."


def test_a_block_comment_above_a_line_comment_is_not_glued_on() -> None:
    """Mixing the two forms would send the pair down `normalize_comment`'s block
    branch and leave the `//` markers in the output.

    The assertion is as much that no marker survives as that the block text is
    absent — a naive join produces `Section. // List users.` here.
    """
    source = b"/** Section. */\n// List users.\napp.get('/users', h);\n"

    result = summary(source)

    assert result == "List users."
    assert "/" not in (result or "")


def test_a_comment_run_longer_than_the_bound_is_cut_not_refused() -> None:
    """The walk is bounded because the number of lines above a handler is
    attacker-controlled. Exceeding it still yields a summary — from the lines
    nearest the route, which are the ones about it — rather than nothing."""
    source = b"// pad\n" * 400 + b"// Nearest.\napp.get('/x', h);\n"

    result = summary(source)

    assert result is not None
    assert result.endswith("Nearest.")


def test_no_comment_means_no_summary() -> None:
    """The ordinary case, and the reason the field is optional."""
    assert summary(b"app.get('/users', h);\n") is None


def test_a_comment_separated_by_a_blank_line_is_not_the_summary() -> None:
    """"Directly above" is literal.

    A blank line means the comment documents whatever came before it, and
    attaching it anyway would put unrelated prose under a route in the service
    map. Same call `descriptions._line_run` makes about an unbroken run.
    """
    source = b"/** About the section below. */\n\napp.get('/users', h);\n"

    assert summary(source) is None


def test_a_route_does_not_inherit_the_comment_above_its_enclosing_function() -> None:
    """Only the enclosing *statement* is examined, never an ancestor of it.

    A climb-until-you-find-a-comment search would give every route in this file
    the function's JSDoc, which is text about the function and not about the
    route.
    """
    source = b"/** Mount the API. */\nexport function mount(app) {\n  app.get('/x', h);\n}\n"

    assert summary(source) is None


def test_a_comment_above_a_route_inside_a_block_is_found() -> None:
    """The other half of the previous test: adjacency inside a function body
    still counts, because the comment really is a sibling of the statement."""
    source = b"export function mount(app) {\n  /** List users. */\n  app.get('/x', h);\n}\n"

    assert summary(source) == "List users."


def test_a_statement_above_a_route_is_never_quoted_as_its_summary() -> None:
    """The sibling before a route is usually *code*, and code must not reach a
    response body as a summary.

    Two independent things stop it — the `comment` node-type test here, and
    `normalize_comment` refusing input that is not comment syntax (ADR-020).
    The assertion is written against the source text rather than against `None`
    so it fails loudly if a future change ever lets one line of code through.
    """
    source = b"const secret = process.env.API_KEY;\napp.get('/x', h);\n"

    result = summary(source)

    assert result is None
    assert "API_KEY" not in (result or "")


def test_a_route_nested_deeper_than_the_climb_bound_keeps_its_route() -> None:
    """The statement climb is bounded because tree depth is attacker-controlled.

    Past the bound a route is still reported — losing an endpoint to deep
    nesting would be worse than losing its summary — and only the summary is
    given up. A bound nothing observes is indistinguishable from a bound nobody
    wrote, so this test exists to observe it.

    The depth that matters is *expression* nesting between the call and its
    statement, not block nesting: wrapping the route in 40 braces leaves the
    call two levels below its `expression_statement` and climbs nothing. Nested
    parentheses are what actually lengthens the walk.
    """
    depth = 40
    source = b"/** Deep. */\n" + b"(" * depth + b"app.get('/x', h)" + b")" * depth + b";\n"

    found = endpoints(source)

    assert [(e.method, e.path) for e in found] == [("GET", "/x")]
    assert found[0].summary is None


def test_a_summary_is_bounded_by_the_endpoint_limit_not_the_description_limit() -> None:
    """The two share a normalizer and not a cap (ADR-020, ADR-021).

    `MAX_ENDPOINT_SUMMARY_CHARS` is 300 and `MAX_DESCRIPTION_CHARS` is 500, so a
    400-character comment discriminates: passing the wrong limit through gives
    400 characters here.
    """
    long_comment = b"/** " + (b"a" * 400) + b" */\napp.get('/x', h);\n"

    result = summary(long_comment)

    assert result is not None
    assert len(result) == SETTINGS.MAX_ENDPOINT_SUMMARY_CHARS


def test_a_summary_drops_control_characters() -> None:
    """The Trojan Source strip, inherited from `normalize_comment`.

    U+202E reorders how the rest of a line displays, which is a spoofing
    primitive aimed at exactly this sink — a short label rendered beside a URL.
    """
    source = "/** Safe‮ evil */\napp.get('/x', h);\n".encode()

    result = summary(source)

    assert result == "Safe evil"
    assert "‮" not in result


def test_an_empty_comment_yields_no_summary() -> None:
    """`min_length=1` on the model, and "the author wrote `/** */`" is the same
    fact as "the author wrote nothing"."""
    assert summary(b"/** */\napp.get('/x', h);\n") is None


# --------------------------------------------------------------------------
# The Next.js App Router file convention
# --------------------------------------------------------------------------


def test_detects_exported_handlers_in_a_next_route_file() -> None:
    source = b"export async function GET(req) {}\nexport async function POST(req) {}\n"

    assert routed(source, path="app/api/users/route.ts") == [
        ("GET", "/api/users"),
        ("POST", "/api/users"),
    ]


def test_detects_a_const_arrow_handler() -> None:
    """Both export forms are idiomatic in a route file and neither is rarer."""
    source = b"export const GET = async (req) => {};\n"

    assert routed(source, path="app/api/ping/route.ts") == [("GET", "/api/ping")]


def test_a_next_route_file_under_src_is_found() -> None:
    source = b"export function GET() {}\n"

    assert routed(source, path="src/app/api/users/route.ts") == [("GET", "/api/users")]


def test_dynamic_segments_are_quoted_as_written() -> None:
    """`[id]` is not rewritten to `:id`.

    A service map quotes a repository, it does not translate between
    frameworks, and `[id]` is what a reader finds when they open the file.
    """
    source = b"export function GET() {}\n"

    assert routed(source, path="app/api/users/[id]/route.ts") == [("GET", "/api/users/[id]")]


def test_route_groups_are_dropped_from_the_path() -> None:
    """Next.js excludes a parenthesised directory from the URL, so keeping it
    would produce a path that genuinely does not resolve."""
    source = b"export function GET() {}\n"

    assert routed(source, path="app/(marketing)/api/leads/route.ts") == [("GET", "/api/leads")]


def test_a_nested_app_directory_resolves_from_the_outermost_one() -> None:
    """`app/app/route.ts` is legal and ambiguous, so the rule is fixed: the
    *first* `app` segment roots the URL. Searching from the other end gives
    `/` here instead of `/app`, and a rule that depends on which end you
    searched from is not a rule."""
    source = b"export function GET() {}\n"

    assert routed(source, path="app/app/route.ts") == [("GET", "/app")]


def test_the_route_filename_is_not_part_of_the_url() -> None:
    """`route.ts` is the convention marker, not a path segment. Leaving it on
    makes every Next.js endpoint end in `/route.ts`."""
    source = b"export function GET() {}\n"

    assert routed(source, path="app/api/users/route.ts")[0][1] == "/api/users"


def test_a_route_file_at_the_app_root_serves_the_root_path() -> None:
    source = b"export function GET() {}\n"

    assert routed(source, path="app/route.ts") == [("GET", "/")]


def test_a_lowercase_export_is_not_a_next_handler() -> None:
    """Next.js recognises the uppercase spellings and only those, so a lowercase
    `get` export is an ordinary function."""
    source = b"export function get() {}\nexport const post = () => {};\n"

    assert routed(source, path="app/api/users/route.ts") == []


def test_a_non_route_file_under_app_declares_nothing() -> None:
    """The filename is the convention. `page.tsx` exporting a component named
    `GET` would otherwise become an endpoint."""
    source = b"export function GET() {}\n"

    assert routed(source, path="app/api/users/page.tsx") == []


def test_a_route_file_outside_an_app_directory_declares_nothing() -> None:
    """`lib/route.ts` is a file called route, not a Next.js route handler."""
    source = b"export function GET() {}\n"

    assert routed(source, path="lib/route.ts") == []


def test_a_non_exported_handler_is_not_a_route() -> None:
    """Next.js only routes exports; a local helper named GET is not a handler."""
    source = b"function GET() {}\nconst POST = () => {};\n"

    assert routed(source, path="app/api/users/route.ts") == []


def test_a_next_handler_takes_the_comment_above_its_export() -> None:
    source = b"/** List every user. */\nexport async function GET(req) {}\n"

    assert summary(source, path="app/api/users/route.ts") == "List every user."


def test_both_detectors_can_fire_on_one_file() -> None:
    """A route file that also calls a router is unusual but legal, and the two
    detectors must not shadow each other."""
    source = b"export function GET() {}\napp.post('/other', h);\n"

    assert set(routed(source, path="app/api/x/route.ts")) == {
        ("GET", "/api/x"),
        ("POST", "/other"),
    }


# --------------------------------------------------------------------------
# Bounds and hostile input
# --------------------------------------------------------------------------


def test_an_overlong_route_path_is_dropped_without_taking_the_file_with_it() -> None:
    """One bad record costs one record, not the file's whole service map.

    The second assertion is the one that matters and the first is nearly free:
    an implementation with *no* length check also returns nothing for the bad
    route, because the `ValidationError` from the model is caught by
    `detect_routes`' catch-all — which then abandons every other route in the
    file. Mutation testing found exactly that, so the good route beside it is
    what makes this test discriminate.
    """
    huge = b"/" + b"a" * (SETTINGS.MAX_PATH_LENGTH + 10)
    source = b"app.get('" + huge + b"', h);\napp.post('/fine', h);\n"

    assert routed(source) == [("POST", "/fine")]


def test_a_route_that_the_model_refuses_costs_only_itself() -> None:
    """The backstop behind the length check, on a different field.

    `HttpMethod` is `^[A-Z]{1,16}$`, so a method that survives the verb test but
    not the model must still drop alone. There is no way to write such a route
    in TS/JS today — the verb set is lowercase and gets uppercased — which is
    why this drives the model directly rather than through a source fixture.
    """
    from app.analysis.routes import _endpoint

    tree = parse_source(b"app.get('/x', h);\n", PurePosixPath(SERVER), TSX, Deadline.after(60))
    assert tree is not None

    assert (
        _endpoint(
            method="not a method",
            route_path="/x",
            file=PurePosixPath(SERVER),
            node=tree.root_node,
            settings=SETTINGS,
        )
        is None
    )


def test_a_path_with_an_escape_is_dropped() -> None:
    """Inherited from `string_literal_text`: decoding JS escapes correctly means
    reimplementing them, and guessing invents a URL that does not exist.

    The source below carries a literal backslash-u escape, which JS would
    unescape to `/café` and which we refuse rather than half-decode.
    """
    assert routed(b"app.get('/caf\\u00e9', h);\n") == []


def test_a_path_with_a_control_character_is_dropped() -> None:
    """A NUL truncates differently in C than in Python, and no control character
    belongs in a URL that reaches a response body."""
    assert routed(b"app.get('/a\x01b', h);\n") == []


def test_a_unicode_path_survives() -> None:
    """Non-ASCII is not the same as unusable — a valid UTF-8 path is a valid
    route, and only escapes and control characters are refused."""
    assert routed("app.get('/café', h);\n".encode()) == [("GET", "/café")]


def test_an_expired_deadline_stops_detection() -> None:
    """The one exception that is not swallowed: a timeout is a statement about
    the run, not about this file."""
    tree = parse_source(b"app.get('/x', h);\n", PurePosixPath(SERVER), TSX, Deadline.after(60))
    assert tree is not None

    with pytest.raises(AnalysisTimeoutError):
        list(detect_routes(tree, PurePosixPath(SERVER), Deadline.after(-1), SETTINGS))


def test_a_failing_query_yields_nothing_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catch-all, which is the module's contract: route detection must never
    be able to end a run."""

    def explode(*args: object) -> object:
        raise RuntimeError("boom")

    member = PurePosixPath(SERVER)
    tree = parse_source(b"app.get('/x', h);\n", member, TSX, Deadline.after(60))
    assert tree is not None

    monkeypatch.setattr(routes_module, "QueryCursor", explode)

    assert list(detect_routes(tree, member, Deadline.after(60), SETTINGS)) == []


def test_a_syntactically_broken_file_still_yields_its_routes() -> None:
    """Partial recovery is the whole reason for using tree-sitter. A file that
    does not compile is still a file whose routes a reader wants to see."""
    source = b"app.get('/x', h);\nfunction broken( {\n"

    assert routed(source) == [("GET", "/x")]


def test_detection_stops_when_the_caller_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Laziness is a real bound, so it gets a real test.

    `detect_routes` yields rather than returning a list, which is what makes
    `MAX_SERVICE_ENDPOINTS` in the pipeline bound *work* and not just output.
    An eager implementation reads identically from the outside and quietly
    builds every endpoint in the file before the caller's cap can stop it — on
    the densest legal input that is 61 680 records built to keep 200.

    Pinned by counting how many times the model is actually constructed, since
    the difference is invisible in the returned value.
    """
    built = 0

    def counting(**kwargs: object) -> ServiceEndpoint:
        nonlocal built
        built += 1
        return ServiceEndpoint(**kwargs)  # type: ignore[arg-type]

    source = b"app.get('/a', h);\n" * 500
    member = PurePosixPath(SERVER)
    tree = parse_source(source, member, TSX, Deadline.after(60), SETTINGS)
    assert tree is not None

    monkeypatch.setattr(routes_module, "ServiceEndpoint", counting)
    taken = list(islice(detect_routes(tree, member, Deadline.after(60), SETTINGS), 10))

    assert len(taken) == 10
    # An eager implementation builds all 500 before the caller sees the first.
    assert built == 10


def test_detection_is_deterministic() -> None:
    """CLAUDE.md requires the same commit to produce byte-identical JSON, and
    this module's output is part of it."""
    source = b"app.get('/a', h);\napp.post('/b', h);\napp.put('/c', h);\n"

    assert [routed(source) for _ in range(3)].count(routed(source)) == 3


# --------------------------------------------------------------------------
# Log hygiene
# --------------------------------------------------------------------------


def test_no_route_path_is_ever_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A route path is repository-authored input, and docs/SECURITY.md forbids
    repository content in a log record. The module logs only exception *types*,
    and only when detection fails."""
    source = b"app.get('/secret-internal-admin', h);\n"

    with caplog.at_level(0):
        assert routed(source) == [("GET", "/secret-internal-admin")]

    assert "secret-internal-admin" not in caplog.text
