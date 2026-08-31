"""The analysis pipeline — the one code path that actually runs a repository.

Every module below this one was built and tested in isolation. This is where
they are joined: a validated :class:`~app.security.url_validation.RepoRef` goes
in, and a content-free description of the repository's TS/JS files and the
module specifiers they name comes out. It is the first code in the project that
downloads an archive rather than building a request for one.

The sequence, and who owns each step::

    RepoRef
      -> fetch.github.get_repo_metadata     preflight: canonical case, default
                                            branch, reported size
      -> fetch.github.get_download_url      the single validated redirect
      -> fetch.github.download_request      the credential-free GET (ADR-009)
      -> Client.send(..., stream=True)      *this module* — the first send
      -> fetch.archive.iter_source_files    wire bytes -> (path, content)
      -> security.secret_filter             drop what must never be a node
      -> analysis.parser.extract_imports    (path, content) -> specifiers
      -> RepositoryAnalysis                 the graph builder's input

Four things about this module are load-bearing rather than incidental.

**One `Deadline` per request, constructed here and nowhere else.** It is frozen
by design (`app/analysis/deadline.py`), and the reason is visible from here: the
same object is handed to `iter_source_files` and to `extract_imports`, so no
stage can extend its own budget by re-deriving one from ``ANALYSIS_TIMEOUT_S``.
The two consumers between them check it once per archive member and twice per
file, which brackets every unit of work in the loop below — this module adds no
third check, because there is no work here that is not already bracketed.

**It stops the next unit of work, never the current one** (ADR-010). There is
no in-parse timeout available in tree-sitter 0.26.0, so a single hostile file
can still hold this thread for a few seconds — measured at ~3.3 s for the worst
of a 21-input sweep — after the budget has already expired. The deadline bounds
the *number* of further files, not the cost of the one in flight. Nothing in
this module should be read as preemption.

**The download is streamed, and it is `iter_raw()`, not `iter_bytes()`.** The
plan of record said ``iter_bytes()``; that is the decoded stream, and httpx
transparently gunzips a response carrying ``Content-Encoding: gzip`` before any
of our meters see a byte. Measured on httpx 0.28.1: a 52-byte gzipped body
served with that header comes back from ``iter_bytes()`` as its 1700 decoded
bytes. Every budget in `fetch/archive.py` is defined on *wire* bytes —
``MAX_DOWNLOAD_BYTES`` is meant to bound bandwidth, and the compression-ratio
guard's denominator is meant to be what was actually transferred — so feeding
it a stream that something else already expanded silently changes what two
controls measure. ``iter_raw()`` is the byte iterator those controls were
written and mutation-tested against. A test pins the difference.

**`MAX_SOURCE_FILES` is the parse cap, and it is not the archive's member
cap.** `fetch/archive.py` refuses an archive of more than ``MAX_ARCHIVE_MEMBERS``
(50 000) entries — a statement about the tarball, made before anything is
filtered. This module stops parsing after ``MAX_SOURCE_FILES`` (3000) *accepted
source files*, counted after the secret filter and the extension test, and
records ``truncated``. They are different limits at different layers and a
repository can hit either without the other.

**`MAX_IMPORTS` is here because the phase after this one has no clock**
(ADR-019). `analysis/resolver.py` runs once this function has returned, after
the whole ``ANALYSIS_TIMEOUT_S`` budget has been spent, and takes no `Deadline`
— so the only thing standing between it and unbounded work is how many imports
leave this loop. Nothing else bounds that number: ``MAX_SOURCE_FILES`` caps
files, not imports *per* file, so before this cap the ceiling was
``MAX_EXTRACTED_BYTES`` — measured at 1 002 000 imports costing 78.7 s to
resolve, more than the entire analysis budget, off an ~11 MiB repository. The
cap cannot be applied later instead: the graph builder computes
``stats.dependencies`` and the per-node counters from what it is given, so
truncating downstream would make those numbers lie.

Skips are counted here because nothing below can count them. `parser.py` logs a
reason and returns nothing; `archive.py` keeps a tally it used to only log. The
counts this module publishes are the files that produced **no graph node**:
members the archive reader dropped, paths the secret filter refused, and
extensions with no grammar. A file the *parser* gave up on — oversized, binary,
pathologically malformed — is still a node, correctly reporting its bytes and
lines and zero imports, so it is not a skip. That is a real blind spot at this
seam and it is recorded in docs/CURRENT_STATE.md rather than papered over.

Resolution is explicitly **not** done here. Specifiers come out exactly as
written; turning ``"./util"`` into ``src/util.ts`` is `analysis/resolver.py`'s
job and it does not exist yet.
"""

import logging
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

import httpx
import tree_sitter_typescript as tree_sitter_typescript_grammars
from tree_sitter import Language

from app.analysis.deadline import Deadline
from app.analysis.parser import extract_imports
from app.config import Settings, get_settings
from app.errors import NoSupportedFilesError, RepositoryNotFoundError, UpstreamUnavailableError
from app.fetch.archive import ArchiveInfo, Limits, iter_source_files
from app.fetch.github import (
    create_client,
    download_request,
    get_download_url,
    get_repo_metadata,
)
from app.security.secret_filter import is_secret_path
from app.security.url_validation import RepoRef

logger = logging.getLogger(__name__)

# Built once. `parser._compiled_query` caches the compiled query per Language,
# so constructing a fresh Language per file would also rebuild the query — 8.8
# ms a time, which at MAX_SOURCE_FILES is ~26 s of a 60 s budget.
_TSX_GRAMMAR: Final = Language(tree_sitter_typescript_grammars.language_tsx())
_TS_GRAMMAR: Final = Language(tree_sitter_typescript_grammars.language_typescript())

# The `language` field on a GraphNode — what the frontend colours by. It is not
# the same question as which grammar parses the file: `.tsx` is TypeScript but
# needs the TSX grammar, and `.jsx` is JavaScript and needs it too.
TYPESCRIPT: Final = "typescript"
JAVASCRIPT: Final = "javascript"

# Extension -> (grammar, reported language).
#
# The TSX grammar is a superset that handles plain JS and JSX, so it covers
# everything except `.ts`. `.ts` must use the TypeScript grammar because TSX
# reads the type assertion `<T>expr` as an opening JSX tag — and this is not a
# cosmetic difference: measured on tree-sitter-typescript 0.23.2, a `.ts` file
# containing `const x = <Foo>bar;` between two imports yields both imports under
# the TypeScript grammar and only the first under TSX, because the phantom JSX
# element swallows the rest of the file into an ERROR node. The mistake costs a
# real dependency edge, silently. It is symmetric: a genuine `<div>` element
# loses the same way under the TypeScript grammar. Both directions are pinned by
# test.
#
# `.mts` and `.cts` are deliberately absent — docs/ARCHITECTURE.md fixes the v1
# set at these six, and quietly widening it here would put the code and the
# document out of step. Adding them is this dict plus a doc edit.
_BY_EXTENSION: Final[dict[str, tuple[Language, str]]] = {
    ".ts": (_TS_GRAMMAR, TYPESCRIPT),
    ".tsx": (_TSX_GRAMMAR, TYPESCRIPT),
    ".js": (_TSX_GRAMMAR, JAVASCRIPT),
    ".jsx": (_TSX_GRAMMAR, JAVASCRIPT),
    ".mjs": (_TSX_GRAMMAR, JAVASCRIPT),
    ".cjs": (_TSX_GRAMMAR, JAVASCRIPT),
}

# Archive-level skip reasons that are not *files* and so must not inflate a
# file count. `_skip_kind` labels every directory entry "directory", and a
# tarball has one per directory.
_NON_FILE_SKIPS: Final = frozenset({"directory"})

# Skip reasons this module owns. Fixed literals, like every other reason string
# in the project — a count is keyed by one of these, never by a path.
SKIP_FILTERED: Final = "secret_or_excluded"
SKIP_UNSUPPORTED: Final = "unsupported_extension"


@dataclass(frozen=True, slots=True)
class ImportRef:
    """One module specifier, exactly as the source wrote it. Never resolved."""

    specifier: str
    line: int


@dataclass(frozen=True, slots=True)
class SourceFile:
    """One analyzed file. Carries no content — see ADR-003 and ADR-016.

    ``loc`` and ``size_bytes`` are computed here because this is the last place
    the bytes exist. Holding the content instead so that a later stage could
    measure it would make peak memory the size of the repository, which is the
    property the streaming reader exists to avoid.
    """

    path: PurePosixPath
    language: str
    size_bytes: int
    loc: int
    imports: tuple[ImportRef, ...]


@dataclass(frozen=True, slots=True)
class RepositoryAnalysis:
    """Everything the graph builder needs, and nothing it does not (ADR-016).

    ``files`` is in archive order. Sorting, dedup, and the
    ``stats.dependencies == len(edges)`` invariant belong to the graph builder
    (docs/ARCHITECTURE.md, "Graph model"), so they are deliberately not done
    here.

    ``commit_sha`` comes from the archive root, which is authoritative — the
    redirect target pins a commit only when the ref was already a SHA. It is
    ``None`` only if the archive yielded nothing, which cannot happen on a
    successfully returned analysis.

    ``skipped`` maps a fixed-literal reason to a count of files that produced no
    node. ``truncated`` says a cap stopped the run early; it is reported, never
    silent. ``imports_truncated`` says *which* cap — ``MAX_IMPORTS`` rather than
    ``MAX_SOURCE_FILES`` — because the two have different consequences for a
    consumer: the file cap drops whole files off the end of archive order, while
    the import cap can also leave the last file present with only part of its
    import list (ADR-019).

    An import-cap stop is deliberately **not** a key in ``skipped``. That map is
    files that produced no node, it is what ``skipped_files`` sums, and the
    file that hit the cap *is* a node — folding it in would repeat the mistake
    ``_NON_FILE_SKIPS`` exists to prevent, and would make
    ``len(files) + skipped_files`` stop describing what the archive produced.
    """

    owner: str
    name: str
    ref: str
    commit_sha: str | None
    files: tuple[SourceFile, ...]
    skipped: Mapping[str, int]
    truncated: bool
    imports_truncated: bool

    @property
    def skipped_files(self) -> int:
        return sum(self.skipped.values())

    @property
    def import_count(self) -> int:
        """Imports across every analyzed file — what `MAX_IMPORTS` bounds.

        Derived rather than stored, so it cannot drift from ``files``: it is
        also, by construction, exactly the length of the sequence
        `analysis/resolver.resolve_imports` will return.
        """
        return sum(len(f.imports) for f in self.files)


@contextmanager
def _client_scope(client: httpx.Client | None) -> Iterator[httpx.Client]:
    """Use the caller's client, or own one for the duration of the analysis.

    One client across all three requests, so the TLS connection to
    ``api.github.com`` is established once. Reuse is safe because
    ``create_client`` never sets a credential on the client itself (ADR-009).
    """
    if client is not None:
        yield client
        return
    with create_client() as owned:
        yield owned


def _token(settings: Settings) -> str | None:
    """The GitHub token, unwrapped at the last possible moment.

    Held as a ``SecretStr`` everywhere else so it cannot reach a log or a
    ``repr`` by accident. It goes only to ``api.github.com``; the download
    request is built by `download_request`, which never receives it.
    """
    return settings.GITHUB_TOKEN.get_secret_value() if settings.GITHUB_TOKEN else None


def analyze_repository(
    repo: RepoRef,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> RepositoryAnalysis:
    """Download, extract, and parse a repository. The whole ingestion path.

    ``repo`` must already have passed
    :func:`~app.security.url_validation.parse_github_url`; this function does
    not re-validate the URL and is not a second guard for it.

    Raises only :class:`~app.errors.AppError` subclasses, all of which carry a
    static message: :class:`~app.errors.RepositoryNotFoundError` (missing,
    private, or inaccessible), :class:`~app.errors.RepositoryTooLargeError`,
    :class:`~app.errors.ArchiveRejectedError`,
    :class:`~app.errors.NoSupportedFilesError`,
    :class:`~app.errors.AnalysisTimeoutError`, and
    :class:`~app.errors.UpstreamUnavailableError`.
    """
    settings = settings if settings is not None else get_settings()

    # Exactly one, for the whole request. Frozen, so nothing downstream can
    # award itself more time; see the module docstring.
    deadline = Deadline.from_settings(settings)
    token = _token(settings)

    with _client_scope(client) as active:
        metadata = get_repo_metadata(repo.owner, repo.name, token, client=active)

        # A configured token is for rate limits, not for reach. Without this,
        # an operator who set GITHUB_TOKEN turns the service into a proxy that
        # will happily render any private repository that token can see — and
        # `get_repo_metadata` returns 200 for one, so the 403/404 collapse that
        # closes the *existence* oracle does not close this. Refused as
        # not-found, reusing that same opaque error so the two stay
        # indistinguishable.
        if metadata.private:
            logger.info("repository refused: not public")
            raise RepositoryNotFoundError()

        # The URL may name a ref; if it does not, GitHub's default branch is
        # the one to ask for. Canonical owner/name from the preflight, not the
        # user's spelling.
        ref = repo.ref if repo.ref is not None else metadata.default_branch
        # The second element is a SHA only when the redirect target happened to
        # pin one, which it does not for a branch ref. Discarded rather than
        # used as a fallback: the archive root is authoritative and always
        # present, so preferring the hint would mean two sources of truth for
        # the commit every /api/source fetch is pinned to.
        download_url, _redirect_sha_hint = get_download_url(
            metadata.owner, metadata.name, ref, token, client=active
        )

        # `stream=True` is a control, not a tuning knob: without it httpx reads
        # the entire body into memory before returning, so MAX_DOWNLOAD_BYTES
        # would be enforced against bytes that had already been buffered and
        # ADR-003's bounded-memory claim would be false.
        response = active.send(download_request(active, download_url), stream=True)
        with closing(response):
            if response.status_code != 200:
                # Includes a redirect: the client does not follow one, and a
                # second hop is not part of the validated path.
                logger.warning("download failed: GET -> %s", response.status_code)
                raise UpstreamUnavailableError()

            info = ArchiveInfo()
            members = iter_source_files(
                # iter_raw, not iter_bytes — the wire bytes, see the module
                # docstring. This is the stream every budget in archive.py was
                # written against.
                response.iter_raw(),
                Limits.from_settings(settings),
                deadline,
                info,
            )
            # A known mutation survivor, and redundant *given two things that
            # are true today*: `closing(response)` above already releases the
            # stream this generator reads, and CPython's refcounting finalizes
            # the abandoned generator as soon as `members` goes out of scope.
            # Deleting it breaks no test. It is kept because both of those are
            # implementation details rather than guarantees — on a
            # non-refcounting runtime the tarfile and the decompressor would
            # survive until a collection — and because `iter_source_files`
            # documents that a caller stopping early should close it, so the
            # only caller in the project ought to be the reference for that.
            with closing(members):
                files, skipped, truncated, imports_truncated = _analyze(
                    members, deadline, settings
                )

    for reason, count in info.skipped.items():
        if reason not in _NON_FILE_SKIPS:
            skipped[reason] += count

    if not files:
        # Nothing to draw. Distinct from an empty repository only in the log.
        raise NoSupportedFilesError()

    logger.info(
        "analysis complete: %d files, %d imports, %d skipped (%s), truncated=%s imports=%s",
        len(files),
        sum(len(f.imports) for f in files),
        sum(skipped.values()),
        dict(sorted(skipped.items())),
        truncated,
        imports_truncated,
    )
    return RepositoryAnalysis(
        owner=metadata.owner,
        name=metadata.name,
        ref=ref,
        commit_sha=info.commit_sha,
        files=tuple(files),
        skipped=dict(skipped),
        truncated=truncated,
        imports_truncated=imports_truncated,
    )


def _analyze(
    members: Iterator[tuple[PurePosixPath, bytes]],
    deadline: Deadline,
    settings: Settings,
) -> tuple[list[SourceFile], Counter[str], bool, bool]:
    """Filter, classify, and parse each member. Returns files, skips, two flags.

    The loop body is deliberately the only place a file is judged: the archive
    reader decides what is a *readable member*, and everything about whether it
    is a *source file we will draw* is decided here.

    Two caps stop it: ``MAX_SOURCE_FILES`` on the file count and ``MAX_IMPORTS``
    on the running import total (ADR-019). Both set ``truncated``; the second
    additionally sets ``imports_truncated``, so a consumer can tell a short file
    list from a short import list.
    """
    files: list[SourceFile] = []
    skipped: Counter[str] = Counter()
    truncated = False
    imports_truncated = False
    import_count = 0

    for path, content in members:
        # First, before anything looks at the extension or spends a parse on
        # it. This is one of the two call sites docs/SECURITY.md requires
        # (the other is /api/source, which does not exist yet); the module also
        # excludes node_modules, dist, and build, which is what keeps vendored
        # code out of the graph.
        if is_secret_path(path):
            skipped[SKIP_FILTERED] += 1
            continue

        # .lower(), not .casefold(). Everywhere else in this project the
        # widening fold is the safe one, because it widens a *rejection*. Here
        # it would widen an *acceptance*: U+017F LATIN SMALL LETTER LONG S
        # casefolds to "s" and lowercases to itself, so under casefold a file
        # whose extension is ".t" + U+017F would be handed to the TypeScript
        # grammar. Narrow is the conservative direction for a rule that decides
        # whether untrusted bytes reach the parser at all. (The character is
        # named rather than written, per the RUF001 convention the archive
        # tests already use for homoglyphs.)
        grammar = _BY_EXTENSION.get(path.suffix.lower())
        if grammar is None:
            skipped[SKIP_UNSUPPORTED] += 1
            continue

        # Checked *after* the filters, so the cap counts files we would draw
        # rather than files the archive happened to contain: 3000 PNGs must not
        # exhaust the budget for source. Breaking rather than continuing
        # abandons the rest of the download, which is the point of the reader
        # being a generator.
        if len(files) >= settings.MAX_SOURCE_FILES:
            truncated = True
            logger.info("source file cap reached: %d", settings.MAX_SOURCE_FILES)
            break

        language, label = grammar
        # Specifiers are not resolved, here or anywhere in this module.
        #
        # Counted one at a time rather than per file, because a single 1 MiB
        # file can hold tens of thousands of them — `import"./a"` is twelve
        # bytes — so a per-file check would overshoot the cap by however many
        # the last file happened to contain. The stop can therefore land
        # mid-file, and the partial file is kept: its bytes, lines, and the
        # imports already collected are all true, and dropping it would delete a
        # node that other files legitimately import. `imports_truncated` is what
        # says the list is short.
        imports: list[ImportRef] = []
        for specifier, line in extract_imports(content, path, language, deadline, settings):
            if import_count >= settings.MAX_IMPORTS:
                truncated = imports_truncated = True
                break
            imports.append(ImportRef(specifier, line))
            import_count += 1

        files.append(
            SourceFile(
                path=path,
                language=label,
                size_bytes=len(content),
                loc=_line_count(content),
                imports=tuple(imports),
            )
        )

        # Same shape as the file cap, and for the same reason: breaking
        # abandons the generator and therefore the rest of the download. What
        # this one buys is the phase *after* this function — `resolve_imports`
        # runs with no Deadline at all, so its cost is whatever number leaves
        # here, and nothing downstream can cap it without falsifying the graph
        # stats built from it (ADR-019).
        if imports_truncated:
            logger.info("import cap reached: %d", settings.MAX_IMPORTS)
            break

    return files, skipped, truncated, imports_truncated


def _line_count(content: bytes) -> int:
    """Lines in a file, counting a final line with no trailing newline.

    Bytes, not text: the content is never decoded, so this cannot raise on the
    undecodable files the parser is built to survive. An empty file is 0 lines.
    """
    if not content:
        return 0
    return content.count(b"\n") + (0 if content.endswith(b"\n") else 1)
