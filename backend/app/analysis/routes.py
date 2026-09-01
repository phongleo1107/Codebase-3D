"""Route detection — the deterministic service map (ADR-013, ADR-021).

`detect_routes` turns one already-parsed file into `ServiceEndpoint` records.
Like every other analysis module it is a pure function of the commit: there is
no LLM anywhere in this project, so a route is found by matching syntax or it is
not found at all. Nothing here is guessed, and nothing is executed — a route is
*read*, never registered, so a repository that builds its routing table at
runtime simply has no service map.

Two detectors, because TS/JS has two genuinely different conventions and
neither subsumes the other:

**Method calls** — ``app.get('/users/:id', handler)``. One tree-sitter query
over member-expression calls, with the property name filtered against a verb
set in Python. This covers Express, Koa's router, Fastify's shorthand, Hono,
and every library that copied the shape, which is most of them.

**The Next.js App Router file convention** — ``app/api/users/route.ts``
exporting ``export async function GET(request)``. Here the *path* is not
written anywhere in the file; it is the directory the file sits in. So this
detector is a filename test plus a query for exported functions named after an
HTTP verb, and the path is derived from the repository path.

## Why the verb set is not the whole filter

``map.get('key')`` is a member call whose property is an HTTP verb, and a
detector that stopped at the verb would report it as a route. This is the
route-detection form of the phantom dependency `parser.py` exists to avoid, and
it is worse than a phantom import: a wrong *edge* is one line in a graph of
thousands, while a wrong *endpoint* is one row in a service map of maybe six,
where a reader has no way to tell it from a real one.

Two further conditions close it, and both are structural rather than heuristic:

1. **The first argument is a string literal beginning with ``/``.** A route
   pattern is a URL path; ``map.get('key')`` and ``cache.delete('user:1')`` are
   not. The ``.`` anchor in the query pins the string to the *first* argument,
   so ``emitter.on('x', '/not-a-route')`` cannot match either.
2. **There is at least one further argument.** A route is a path *and* a
   handler. This is what separates ``app.get('/x', h)`` from Express's own
   one-argument settings getter ``app.get('trust proxy')``, and it also rules
   out ``map.delete('/tmp/x')`` and ``router.route('/x')``, neither of which
   declares a method at that call.

The residue is deliberate. ``router.route('/x').get(h)`` is **not** detected —
the path and the verb are on two different calls — and neither is Fastify's
``fastify.route({method: 'GET', url: '/x'})`` object form. Both are real routes
we miss. Missing a route leaves a service map short; inventing one puts a URL
in front of a user that does not exist, and between those two the choice is the
same one ADR-013 made for descriptions: **absent, but never wrong.**

## What is shared, and what is not

Two primitives come from elsewhere, and in both cases this module supplies its
own *locator* and borrows only the *normalizer* — the split ADR-020 set up:

- `parser.string_literal_text` unquotes the path, refusing anything with an
  escape, a control character, or undecodable bytes. A route path is compared
  and displayed as an exact string, exactly like a module specifier, so it
  wants the same strict answer rather than the description extractor's
  replace-and-continue.
- `descriptions.normalize_comment` turns the comment above the handler into the
  summary. ADR-020 predicted this caller precisely: locating that comment
  requires the tree, because at a handler deep in a file the byte-0 argument
  that lets the description extractor skip parsing does not hold. So the
  locating is here, and it is the only part that is.

The summary is bounded by ``MAX_ENDPOINT_SUMMARY_CHARS`` rather than
``MAX_DESCRIPTION_CHARS``: the same normalizer, a different cap, passed in
rather than applied afterwards so that ADR-020's "count output characters while
cleaning" stays exact instead of becoming a truncation of an already-truncated
string.
"""

import logging
from collections.abc import Iterator
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Final

from pydantic import ValidationError
from tree_sitter import Language, Node, Query, QueryCursor, Tree

from app.analysis.deadline import Deadline
from app.analysis.descriptions import normalize_comment
from app.analysis.parser import string_literal_text
from app.config import Settings, get_settings
from app.models.api import ServiceEndpoint

logger = logging.getLogger(__name__)

# `app.get(...)`, `router.post(...)`, `server.app.delete(...)`. The property
# name and the first argument are captured separately so the verb test and the
# path test can both happen in Python; `@call` carries the whole node so the
# argument count and the enclosing statement are reachable. The `.` anchor pins
# the string to the *first* argument — see the module docstring.
METHOD_ROUTE_QUERY: Final = """
(call_expression
  function: (member_expression property: (property_identifier) @method)
  arguments: (arguments . (string) @path)) @call
"""

# `export async function GET(req)` and `export const GET = async (req) => {}`.
# Both forms are idiomatic in a Next.js route file and neither is rarer than
# the other, so both are matched; the name is filtered in Python against the
# uppercase verb set.
NEXT_HANDLER_QUERY: Final = """
(export_statement declaration: (function_declaration name: (identifier) @name)) @export
(export_statement
  declaration: (lexical_declaration (variable_declarator name: (identifier) @name))) @export
"""

# Matched against a lowercased property name. `all` is Express's any-method
# registration and is reported as "ALL", which is not an HTTP method but is
# what the source says — `models/api.HttpMethod` is a character class rather
# than an enum for exactly this reason.
_CALL_METHODS: Final = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "all"}
)

# Matched against an exported name, case-sensitively. Next.js recognises these
# and only these, spelled exactly like this; a lowercase `get` export is an
# ordinary function and not a route handler.
_NEXT_METHODS: Final = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

# The App Router's magic filename, and the directory that roots it.
_NEXT_ROUTE_STEM: Final = "route"
_NEXT_APP_DIR: Final = "app"

# Node types whose children are statements. Used to find the statement a
# handler call belongs to, which is the node a comment can sit above.
_STATEMENT_CONTAINERS: Final = frozenset({"program", "statement_block", "class_body"})

# How far up the tree the search for an enclosing statement will walk. Real
# code needs two or three levels; the bound exists because tree depth is
# attacker-controlled and `parser._is_pathological` deliberately does not
# refuse deep nesting (20 000 levels query in 4 ms, so depth is not the
# quadratic trigger and is not worth refusing a file over).
_MAX_STATEMENT_CLIMB: Final = 32

_COMMENT: Final = "comment"
_LINE_COMMENT: Final = b"//"

# How many consecutive `//` lines above a handler are gathered into one summary.
# See `_comment_above` — the run is reassembled from one node per line, and the
# line count is attacker-controlled.
_MAX_COMMENT_RUN: Final = 64


@lru_cache(maxsize=8)
def _compiled(language: Language, query: str) -> Query:
    """One compiled query per (grammar, pattern), for `parser._compiled_query`'s
    reason: compilation is ~8.8 ms and this runs once per file."""
    return Query(language, query)


def detect_routes(
    tree: Tree,
    path: PurePosixPath,
    deadline: Deadline,
    settings: Settings | None = None,
) -> Iterator[ServiceEndpoint]:
    """Yield every HTTP route declared in ``tree``.

    ``tree`` must come from `parser.parse_source`, which is what guarantees the
    queries below are not the quadratic case (`parser.py`, finding 2).
    ``path`` is the repository-relative path of the file — it becomes
    ``ServiceEndpoint.file`` and, for a Next.js route file, the route path
    itself, so unlike in `parser.py` it is *not* only used for logging.

    A generator, so the caller can stop at ``MAX_SERVICE_ENDPOINTS`` without
    this module having to know about that cap — the same arrangement
    `analysis/pipeline.py` already uses for ``MAX_IMPORTS`` (ADR-019). Order is
    deterministic but is tree-sitter's match order rather than document order,
    exactly as for imports.

    Never raises for a bad file: a query that fails yields nothing. The single
    exception is `AnalysisTimeoutError`, which is a statement about the run.
    """
    settings = settings if settings is not None else get_settings()
    # A third traversal of this tree, after the parse and the import query.
    # Bounded but not free, so the budget gets a say before it starts.
    #
    # Deliberately *outside* the try below. Everywhere else in the project a
    # catch-all is paired with an `except AnalysisTimeoutError: raise` so the
    # one exception that must propagate is not swallowed; here the check simply
    # does not happen inside the guarded region, so there is nothing for the
    # catch-all to swallow and no re-raise to forget. Nothing `_routes` calls
    # touches the deadline. Structural rather than defended, which is the same
    # trade ADR-009 makes about credentials.
    deadline.check()
    try:
        yield from _routes(tree, path, settings)
    except (RecursionError, MemoryError) as exc:
        logger.warning("route detection abandoned, resource exhausted: %s", type(exc).__name__)
    except Exception as exc:
        # Only the type, never the message: a tree-sitter or codec error can
        # quote the source bytes that caused it (docs/SECURITY.md).
        logger.info("route detection skipped: unexpected %s", type(exc).__name__)
        logger.debug("route detection skipped: unexpected %s (path=%s)", type(exc).__name__, path)


def _routes(
    tree: Tree, path: PurePosixPath, settings: Settings
) -> Iterator[ServiceEndpoint]:
    """Both detectors, lazily.

    **Lazy on purpose, and it is what makes the caller's cap a real bound.**
    Building a list here and yielding from it afterwards reads the same from the
    outside but is not: `analysis/pipeline.py` stops at
    ``MAX_SERVICE_ENDPOINTS``, and with an eager list that stop happens *after*
    every endpoint in the file has already been built. Measured on the densest
    legal input — a full ``MAX_PARSE_BYTES`` of ``app.get('/a',h);`` — that is
    **61 680 records built to keep 200, at 0.94 s for the file**. Yielding drops
    the same case to the traversal cost alone, ~0.07 s, because the per-match
    work is 93% of it.

    The cost of laziness is that a failure part-way cannot retract what it
    already yielded, so `detect_routes`' catch-all turns a mid-file error into a
    *partial* service map rather than an empty one. That is the better failure
    and it is the same call ADR-019 made about the file the import cap cut in
    half: the endpoints already found are true, and dropping them to hide a
    short list would delete real information to make an error look tidier.
    """
    yield from _method_call_routes(tree, path, settings)
    if _is_next_route_file(path):
        yield from _next_routes(tree, path, settings)


def _method_call_routes(
    tree: Tree, path: PurePosixPath, settings: Settings
) -> Iterator[ServiceEndpoint]:
    """`app.get('/x', handler)` and everything shaped like it."""
    cursor = QueryCursor(_compiled(tree.language, METHOD_ROUTE_QUERY))
    for _pattern, captures in cursor.matches(tree.root_node):
        method_nodes = captures.get("method")
        path_nodes = captures.get("path")
        call_nodes = captures.get("call")
        if not method_nodes or not path_nodes or not call_nodes:
            # Defensive: the single pattern binds all three, so a match cannot
            # exist without them.
            continue

        raw_method = method_nodes[0].text
        if raw_method is None or raw_method.decode("ascii", "replace").lower() not in _CALL_METHODS:
            continue

        # A route pattern is a URL path. This is the check that keeps
        # `map.get('key')` out of the service map — see the module docstring.
        route_path = string_literal_text(path_nodes[0])
        if route_path is None or not route_path.startswith("/"):
            continue

        # A path with no handler after it is not a route registration. Rules
        # out Express's own one-argument `app.get('setting')` getter.
        if not _has_handler_argument(call_nodes[0]):
            continue

        endpoint = _endpoint(
            method=raw_method.decode("ascii", "replace").upper(),
            route_path=route_path,
            file=path,
            node=call_nodes[0],
            settings=settings,
        )
        if endpoint is not None:
            yield endpoint


def _next_routes(
    tree: Tree, path: PurePosixPath, settings: Settings
) -> Iterator[ServiceEndpoint]:
    """`export async function GET()` in an `app/**/route.ts` file.

    The route path comes from the directory, not from the source, so it is
    computed once and shared by every handler the file exports.
    """
    route_path = _next_route_path(path)
    if route_path is None:
        return

    cursor = QueryCursor(_compiled(tree.language, NEXT_HANDLER_QUERY))
    for _pattern, captures in cursor.matches(tree.root_node):
        name_nodes = captures.get("name")
        export_nodes = captures.get("export")
        if not name_nodes or not export_nodes:
            continue

        raw_name = name_nodes[0].text
        # Case-sensitive, and a known *equivalent* mutation: adding `.upper()`
        # here changes no observable behaviour, because any name it would newly
        # admit is by definition not already uppercase, and such a name reaches
        # `_endpoint` as the `method` and is refused there by `HttpMethod`'s
        # `^[A-Z]{1,16}$`. The two agree, so the route drops either way. This is
        # still the check that carries the meaning — Next.js routes the
        # uppercase spellings and only those, and a lowercase `get` export is an
        # ordinary function — and the model is the second line, not the first.
        if raw_name is None or raw_name.decode("ascii", "replace") not in _NEXT_METHODS:
            continue

        endpoint = _endpoint(
            method=raw_name.decode("ascii", "replace"),
            route_path=route_path,
            file=path,
            node=export_nodes[0],
            settings=settings,
        )
        if endpoint is not None:
            yield endpoint


def _endpoint(
    *,
    method: str,
    route_path: str,
    file: PurePosixPath,
    node: Node,
    settings: Settings,
) -> ServiceEndpoint | None:
    """Assemble one record, or ``None`` if it cannot be a valid one.

    The length test is here rather than left to pydantic because a route path
    longer than ``MAX_PATH_LENGTH`` is not a route anyone can use, and dropping
    it is the same call `string_literal_text` makes about an unusable specifier.

    The `ValidationError` catch behind it is a **backstop, and it exists because
    of where this function sits**. Anything raised here escapes into
    `detect_routes`' catch-all, which abandons the whole file — so without this,
    one malformed record would silently delete every *other* route in the same
    file, and the result would be indistinguishable from a file that declares
    none. Found by mutation testing, twice: both an over-long path and a
    lowercase method were "caught" by the catch-all in a way that made the real
    checks look untested. One bad record must cost one record.
    """
    # A known mutation survivor once the backstop below exists, and deliberately
    # kept: with this deleted, `MemberPath` refuses the same value one line
    # later and the record drops identically. It stays because the bound is
    # supposed to be a decision this module makes about what it will emit, not
    # an emergent property of a pydantic annotation someone could relax without
    # ever reading this file. Same call `parser.string_literal_text` documents
    # for its own redundant quote checks.
    if len(route_path) > settings.MAX_PATH_LENGTH:
        return None
    try:
        return ServiceEndpoint(
            method=method,
            path=route_path,
            file=str(file),
            line=node.start_point[0],
            summary=_summary(node, settings),
        )
    except ValidationError:
        # No logging: every field here is repository-authored, and pydantic's
        # message embeds the value that failed (docs/SECURITY.md).
        return None


def _has_handler_argument(call: Node) -> bool:
    """True if the call has a second argument after the path.

    Counted over *named* children, so a comment or a trailing comma between
    the arguments does not count as one.
    """
    arguments = call.child_by_field_name("arguments")
    return arguments is not None and arguments.named_child_count >= 2


def _summary(node: Node, settings: Settings) -> str | None:
    """The comment directly above the statement ``node`` belongs to.

    "Directly above" is literal: the comment must end on the line immediately
    before the statement starts. A blank line between them means the comment
    documents something else, and attaching it anyway would put unrelated text
    under a route in the service map. This is `descriptions._line_run`'s
    unbroken-run rule applied to a different kind of adjacency, and for the
    same reason — a comment near a thing is not a comment about it.

    Only the *enclosing statement* is examined, never an ancestor of it. A
    route registered inside a function must not inherit the JSDoc written above
    that function, which is what a climb-until-you-find-a-comment search would
    give it.
    """
    statement = _enclosing_statement(node)
    if statement is None:
        return None
    # The cap is the endpoint one, not the description one, and it is passed in
    # so that it is applied while cleaning rather than to an already-cut string
    # (ADR-020).
    return normalize_comment(
        _comment_above(statement), settings, limit=settings.MAX_ENDPOINT_SUMMARY_CHARS
    )


def _comment_above(statement: Node) -> bytes | None:
    """The comment directly above ``statement``, as one blob of comment text.

    **A run of ``//`` lines is several nodes, not one.** tree-sitter ends a line
    comment at the newline, so ``// One.`` / ``// Two.`` above a handler are two
    sibling `comment` nodes and reading only `prev_named_sibling` would quote
    the second line and silently drop the first. `descriptions.py` gets this for
    free — its scanner walks lines and `_line_run` gathers them — so this is the
    one place where locating from the tree is *harder* than locating from bytes,
    rather than merely different. Found by test, not by reading the grammar.

    A block comment is already whole, so a run is only collected for the ``//``
    form, and a run is never *mixed* with a block: `normalize_comment` dispatches
    on the first two characters of what it is handed, so gluing ``/* a */`` to
    ``// b`` would send the pair down the block branch and leave ``// b``'s
    markers in the output. Taking only the first form encountered is the same
    rule `descriptions._leading_comment` follows, for the same reason.
    """
    first = statement.prev_named_sibling
    # The `_COMMENT` half of this test is defence in depth rather than the thing
    # that makes it safe, and mutation testing says so: with it removed,
    # `normalize_comment` still refuses the statement text it would then be
    # handed, because it dispatches on comment syntax and returns `None` for
    # anything else. That refusal is exactly the guarantee ADR-020 wrote into
    # the normalizer for this caller — "if route detection ever hands it the
    # wrong node, the failure should be an absent summary and not a line of
    # source code in a response body" — and this is the first evidence it holds.
    # The test stays because relying on a downstream refusal to keep source code
    # out of a response is a worse contract than not sending it.
    if first is None or first.type != _COMMENT:
        return None
    if statement.start_point[0] - first.end_point[0] != 1:
        return None

    text = first.text
    if text is None:
        return None
    if not text.startswith(_LINE_COMMENT):
        return text

    run = [text]
    node = first
    # Bounded because the number of comment lines above a handler is
    # attacker-controlled and this walk happens before the normalizer's
    # character cap can stop anything. Far more lines than a real doc comment
    # has, and far more than could survive `MAX_ENDPOINT_SUMMARY_CHARS`.
    for _ in range(_MAX_COMMENT_RUN):
        previous = node.prev_named_sibling
        if previous is None or previous.type != _COMMENT:
            break
        previous_text = previous.text
        if previous_text is None or not previous_text.startswith(_LINE_COMMENT):
            break
        if node.start_point[0] - previous.end_point[0] != 1:
            break
        run.append(previous_text)
        node = previous
    return b"\n".join(reversed(run))


def _enclosing_statement(node: Node) -> Node | None:
    """The ancestor of ``node`` that is a direct child of a statement container.

    That is the node a leading comment would be a sibling of. Returns ``None``
    if the walk runs off the top of the tree or past `_MAX_STATEMENT_CLIMB`.
    """
    current = node
    for _ in range(_MAX_STATEMENT_CLIMB):
        parent = current.parent
        if parent is None:
            return None
        if parent.type in _STATEMENT_CONTAINERS:
            return current
        current = parent
    return None


def _is_next_route_file(path: PurePosixPath) -> bool:
    """True for `**/app/**/route.{ts,tsx,js,jsx,...}`.

    The extension is not tested: the pipeline only hands this module files it
    already chose a grammar for, so anything reaching here has a supported one.

    Two known mutation survivors live in this one line, both redundant *by
    construction* rather than untested:

    - The ``app`` half is re-checked by `_next_route_path`, which returns
      ``None`` without an ``app`` segment, so deleting it here changes no
      behaviour. Kept because this predicate is what the reader of `_routes`
      sees, and "is this a Next.js route file" is a question about a filename
      *and* a location. The ``stem`` half is not redundant and is caught.
    - ``parts[:-1]`` could be ``parts`` here with no effect, because the stem
      test already forces the filename to be ``route.*``, which is never
      ``app``. It is written ``[:-1]`` anyway so that it says the same thing as
      the identical slice in `_next_route_path`, where dropping the filename
      *is* load-bearing and is caught.
    """
    return path.stem == _NEXT_ROUTE_STEM and _NEXT_APP_DIR in path.parts[:-1]


def _next_route_path(path: PurePosixPath) -> str | None:
    """The URL a Next.js App Router file serves, derived from its directory.

    ``src/app/api/users/[id]/route.ts`` becomes ``/api/users/[id]``. The
    dynamic-segment syntax is left exactly as the repository wrote it rather
    than rewritten to Express's ``:id`` — a service map quotes a repository, it
    does not translate between frameworks, and ``[id]`` is what a reader will
    find when they open the file.

    Route *groups* — a directory in parentheses — are dropped, because Next.js
    excludes them from the URL and keeping them would produce a path that
    genuinely does not resolve. Parallel routes (``@slot``) and private folders
    (``_internal``) are deliberately **not** special-cased: they are rare, each
    would be another framework rule encoded here, and the failure mode is a
    path that is wrong rather than one that is missing.
    """
    # `[:-1]` drops the filename: `route.ts` is the convention marker, not a
    # path segment, and leaving it on would make every Next.js endpoint end in
    # `/route.ts`.
    parts = path.parts[:-1]
    if _NEXT_APP_DIR not in parts:
        return None
    # The first `app` segment, so that a repository with a nested `app/app`
    # produces a stable answer rather than one that depends on which end we
    # searched from.
    after = parts[parts.index(_NEXT_APP_DIR) + 1 :]
    segments = [s for s in after if not (s.startswith("(") and s.endswith(")"))]
    # No `if segments else "/"`: joining an empty list gives "", so this already
    # yields "/" for a route file at the `app` root. The conditional that used
    # to be here was dead, which mutation testing found by not noticing its
    # removal.
    return "/" + "/".join(segments)
