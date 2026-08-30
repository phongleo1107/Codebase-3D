"""Streaming tarball reader — the only place archive members are inspected.

Consumes the byte iterator of a codeload download (``response.iter_raw()`` —
the wire bytes, see `app/analysis/pipeline.py` on why not ``iter_bytes()``) and
yields ``(path, content)`` for the regular files inside it. **Nothing is
written to disk** (ADR-003): there is no temporary directory, no extraction
step, and therefore no write syscall for a traversal or symlink escape to
exploit. Peak memory is one member plus the fixed stream buffers, not the size
of the repository.

Facts about the archive as a whole — the commit SHA carried by the root
directory, and the per-reason skip counts — travel on an optional
:class:`ArchiveInfo` out-parameter rather than in the yielded tuple (ADR-011).

Member paths are nevertheless validated in full, because they do not stay
inside this module: they become graph node IDs, they are echoed back to the
frontend, and they are the subject of the ``/api/source`` token. A path that is
merely *harmless to us* is not good enough.

Layering, and why it is three pieces rather than ``tarfile.open(mode="r|gz")``::

    Iterator[bytes]            the download, chunk by chunk
      -> _CountingRawStream    counts compressed bytes; caps MAX_DOWNLOAD_BYTES
      -> gzip.GzipFile         decompression
      -> _DecompressedStream   counts decompressed bytes; caps MAX_EXTRACTED_BYTES
                               and enforces the compression ratio
      -> tarfile (mode="r|")   sequential, non-seeking member iteration

``r|gz`` would do the gzip step internally and leave nothing to meter between
the two, which matters more than it sounds: in a non-seeking stream ``tarfile``
must *read past* the body of every member, including ones this module skips for
being oversized. A bomb whose payload is one 1 GiB member is therefore fully
decompressed by ``tarfile`` even though no file is ever yielded from it.
Metering the decompressed side rather than summing the sizes of accepted
members is what makes that bomb die at ~8 MiB.

Two failure modes, both opaque to the caller (docs/SECURITY.md):

``RepositoryTooLargeError``  A byte budget was exhausted — the compressed
                             download cap or the cumulative decompressed cap.
``ArchiveRejectedError``     The archive is structurally unacceptable: a
                             malformed tar or gzip stream, a bomb-grade
                             compression ratio, too many members, or a member
                             path this module refuses to name.

The distinction is for the operator's logs and for tests; both are static,
detail-free messages, and neither says which check tripped.

Rejecting the *archive* and skipping a *member* are deliberately different
outcomes. A path that could not have been produced by an honest ``git archive``
— absolute, traversing, multi-rooted, malformed — is evidence the whole
tarball is hostile, so the run stops. A path that is merely beyond a resource
budget — too deep, too long, too big — is something a large legitimate
repository can contain, so that one file is dropped and the analysis continues.
"""

import gzip
import logging
import re
import tarfile
import zlib
from collections import Counter
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import IO, Final, NoReturn, cast

from app.analysis.deadline import Deadline
from app.config import Settings, get_settings
from app.errors import ArchiveRejectedError, RepositoryTooLargeError

logger = logging.getLogger(__name__)

# The single root directory GitHub's legacy tarball wraps everything in:
# "<owner>-<repo>-<sha>" or "<repo>-<sha>". The trailing group is what makes
# this a useful check rather than a formality — the commit SHA captured from it
# is authoritative and pins every later /api/source fetch
# (docs/ARCHITECTURE.md, ingestion step 5).
#
# On a root whose *name* also contains a hyphen-delimited hex run —
# "acme-deadbee-a1b2c3d" — the group binds the trailing run, which is the
# correct end of the string to read a SHA from. That is not a consequence of
# the leading `+` being greedy, as it first appears: under `fullmatch` the
# group has to consume the entire remainder, and `-` is not in `[0-9a-f]`, so
# exactly one split can match and greedy and lazy quantifiers agree on every
# input. Verified by mutation — making the `+` lazy changes no result. The
# parametrized test exists because a reader will nonetheless expect ambiguity
# here.
ROOT_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]+-([0-9a-f]{7,40})")

# "" catches "a//b" and a leading "/"; "." and ".." catch traversal in both
# directions of normalization. Checked per component, so no amount of nesting
# or repetition sneaks one through.
_UNSAFE_COMPONENTS: Final = frozenset({"", ".", ".."})

# "C:", "Z:" — a Windows drive-relative path. Not traversal on this platform,
# but it is not a path an honest archive of a git tree contains either.
_DRIVE_PREFIX: Final = re.compile(r"[A-Za-z]:")

# How much tarfile pulls from the decompressed stream per call, and so the
# granularity at which the caps below are consulted. A multiple of the 512-byte
# tar block, and small enough that "cumulative total" means the same thing to
# the guard as it does to a reader.
#
# It is *not* what bounds the compressed read-ahead in front of the ratio
# guard — that is gzip's own buffering, which reads the raw stream in
# `io.DEFAULT_BUFFER_SIZE` chunks. The consequence is that the ratio's
# denominator runs slightly ahead of the bytes actually decompressed, which
# makes the guard marginally conservative rather than trigger-happy.
_TAR_BLOCK_SIZE: Final = 16 * 1024


@dataclass(frozen=True, slots=True)
class Limits:
    """The archive-relevant subset of :class:`~app.config.Settings`.

    No defaults, deliberately. Values live in ``Settings`` and nowhere else
    (CLAUDE.md); this is a view onto them, so restating a number here would be
    a second source of truth. Taking them as a parameter rather than reading
    ``get_settings()`` inline is what lets a test exercise one control at a
    time without reaching into the process-wide settings cache.
    """

    max_download_bytes: int
    max_extracted_bytes: int
    max_compression_ratio: int
    ratio_floor_bytes: int
    max_archive_members: int
    max_member_bytes: int
    max_path_depth: int
    max_path_length: int

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Limits:
        s = settings if settings is not None else get_settings()
        return cls(
            max_download_bytes=s.MAX_DOWNLOAD_BYTES,
            max_extracted_bytes=s.MAX_EXTRACTED_BYTES,
            max_compression_ratio=s.MAX_COMPRESSION_RATIO,
            ratio_floor_bytes=s.RATIO_FLOOR_BYTES,
            max_archive_members=s.MAX_ARCHIVE_MEMBERS,
            max_member_bytes=s.MAX_MEMBER_BYTES,
            max_path_depth=s.MAX_PATH_DEPTH,
            max_path_length=s.MAX_PATH_LENGTH,
        )


@dataclass(slots=True)
class ArchiveInfo:
    """Facts about the archive *as a whole*, filled in while it is read.

    The second return channel for :func:`iter_source_files`. It exists because
    the commit SHA is one fact about the tarball, not a fact about each file:
    putting it in the yielded tuple would repeat a constant on every member and
    invite a caller to trust the *last* copy rather than the root-equality check
    that already guarantees they agree.

    Mutable, and deliberately not a return value. ``commit_sha`` is set as soon
    as the first regular member establishes the root, so a caller that stops
    early — at ``MAX_SOURCE_FILES``, say — still has it. A generator's
    ``return`` value would be delivered only on exhaustion, which is exactly the
    case that does not happen.

    ``skipped`` counts members this module dropped, keyed by a fixed literal
    from :func:`_skip_kind` or by the budget that dropped them. Never a path.
    """

    commit_sha: str | None = None
    skipped: Counter[str] = field(default_factory=Counter)


def _reject(reason: str) -> NoReturn:
    """Refuse the archive. ``reason`` is a fixed literal — never a member path."""
    logger.info("archive rejected: %s", reason)
    raise ArchiveRejectedError() from None


def _too_large(reason: str) -> NoReturn:
    logger.info("archive refused as oversized: %s", reason)
    raise RepositoryTooLargeError() from None


class _CountingRawStream:
    """File-like adapter over the download iterator, capping compressed bytes.

    ``count`` is the number of bytes *handed on* to the decompressor. Bytes
    pulled from the iterator but still buffered are excluded, which keeps it a
    tight denominator for the ratio guard; the cap itself is enforced against
    buffered bytes too, so a single enormous chunk cannot be accumulated in
    memory before anyone objects.
    """

    def __init__(self, chunks: Iterator[bytes], limit: int) -> None:
        self._chunks = chunks
        self._limit = limit
        self._buffer = b""
        self._exhausted = False
        self.count = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return False

    def close(self) -> None:
        # The iterator belongs to the caller's HTTP response; closing that is
        # the caller's business, not this adapter's.
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            while not self._exhausted:
                self._fill()
            data, self._buffer = self._buffer, b""
        else:
            while len(self._buffer) < size and not self._exhausted:
                self._fill()
            data, self._buffer = self._buffer[:size], self._buffer[size:]
        self.count += len(data)
        return data

    def _fill(self) -> None:
        try:
            chunk = next(self._chunks)
        except StopIteration:
            self._exhausted = True
            return
        self._buffer += chunk
        # Eager: as soon as the bytes have been received they count against the
        # budget, whether or not the decompressor has asked for them yet. This
        # is the control that bounds bandwidth and this object's own memory.
        if self.count + len(self._buffer) > self._limit:
            _too_large("compressed download cap")


class _DecompressedStream:
    """Meters the decompressed side: cumulative cap plus the ratio guard.

    Both checks run on every read rather than once per member. That is
    stronger, and it is the only placement that works: the decompression that
    kills a bomb happens while ``tarfile`` reads *past* an oversized member, at
    which point there is no "next member" to check at.
    """

    def __init__(self, source: _CountingRawStream, limits: Limits) -> None:
        self._source = source
        self._limits = limits
        self._gz = gzip.GzipFile(fileobj=cast(IO[bytes], source), mode="rb")
        self.count = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return False

    def close(self) -> None:
        self._gz.close()

    def read(self, size: int = -1) -> bytes:
        data: bytes = self._gz.read(size)
        self.count += len(data)
        if self.count > self._limits.max_extracted_bytes:
            _too_large("extracted size cap")
        self._check_ratio()
        return data

    def _check_ratio(self) -> None:
        """Refuse a bomb-grade expansion factor, once past the floor.

        The floor exists so a small archive whose few kilobytes happen to
        compress spectacularly is not mistaken for an attack; past it, the
        expansion factor is the only thing that separates a repository from a
        gigabyte of zeros. Written as a multiplication so a zero denominator is
        not a special case.
        """
        if self.count <= self._limits.ratio_floor_bytes:
            return
        if self.count > self._limits.max_compression_ratio * self._source.count:
            _reject("compression ratio")


def _check_member_name(name: str) -> list[str]:
    """Validate a member path and return its components, or reject the archive.

    Returns the ``/``-separated components including the root directory.
    """
    # tarfile decodes names with errors="surrogateescape", so undecodable header
    # bytes survive as lone surrogates rather than raising. Those strings crash
    # anything that later encodes them (a JSON response, a log record) and are
    # not a path any real repository contains. The round-trip is the check.
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        _reject("member name is not valid UTF-8")
    # Reachable only through a pax "path" header: the ustar field is NUL
    # terminated, so tarfile truncates there. Rejected explicitly because a
    # NUL truncates differently in C than in Python, which is the whole point
    # of embedding one.
    if "\x00" in name:
        _reject("member name contains NUL")

    # Absolute paths, in all three spellings: "/etc/passwd", "C:\\Windows",
    # and the UNC "\\\\server\\share" (which starts with a backslash, so the
    # first test covers it).
    #
    # Redundant by design, and confirmed so by mutation testing: deleting this
    # whole check leaves the suite green, because a leading separator becomes
    # an empty component below and a drive letter becomes a root that fails
    # ROOT_PATTERN. It is kept because "the path is absolute" is a statement
    # worth making where a reader looks for it, and because the checks it
    # currently leans on both exist for other reasons and could be relaxed.
    if name.startswith(("/", "\\")) or _DRIVE_PREFIX.match(name):
        _reject("absolute member path")

    # Backslash is a separator on the platform an attacker is aiming at, so it
    # is normalized *into* one rather than treated as an ordinary character —
    # otherwise "src\\..\\..\\x" is a single innocuous-looking component that
    # passes the check below and is yielded verbatim. Not redundant: mutation
    # testing survived its removal until a case with backslashes *below* the
    # root directory was added, since everything shallower fails ROOT_PATTERN.
    components = name.replace("\\", "/").split("/")
    if any(component in _UNSAFE_COMPONENTS for component in components):
        _reject("unsafe member path component")
    return components


def iter_source_files(
    raw: Iterator[bytes],
    limits: Limits,
    deadline: Deadline,
    info: ArchiveInfo | None = None,
    # Generator, not Iterator, in the annotation as well as in fact: a caller
    # that stops early is expected to `close()` this so the tarfile and the
    # decompressor are released deterministically rather than at collection.
) -> Generator[tuple[PurePosixPath, bytes]]:
    """Yield ``(path, content)`` for every acceptable regular file in a tarball.

    ``raw`` is the compressed download, chunk by chunk — the *wire* bytes, so
    the byte budgets below mean what they say. ``path`` is relative to the
    archive's root directory, which is stripped — callers see ``src/index.ts``,
    not ``owner-repo-a1b2c3d/src/index.ts``.

    ``info`` is an optional out-parameter: pass an :class:`ArchiveInfo` to
    receive the commit SHA and the skip counts, which are facts about the
    archive rather than about any one member. It is filled in as the archive is
    read, so it is usable even if iteration stops early.

    Members are yielded in archive order and the stream is read exactly once,
    so this is a true generator: abandoning it part-way abandons the download.

    Raises :class:`~app.errors.RepositoryTooLargeError`,
    :class:`~app.errors.ArchiveRejectedError`, or
    :class:`~app.errors.AnalysisTimeoutError`.
    """
    compressed = _CountingRawStream(raw, limits.max_download_bytes)
    decompressed = _DecompressedStream(compressed, limits)

    if info is None:
        info = ArchiveInfo()
    root: str | None = None
    member_count = 0
    yielded = 0
    skipped = info.skipped

    try:
        # mode="r|" — sequential, non-seeking. The gzip layer is ours (see the
        # module docstring), so tarfile sees a plain tar stream.
        with tarfile.open(
            # Duck-typed on purpose: tarfile and gzip need only `read`, and the
            # metering wrappers deliberately do not inherit from IOBase so that
            # no unmetered inherited method can bypass them.
            fileobj=cast(IO[bytes], decompressed),
            mode="r|",
            bufsize=_TAR_BLOCK_SIZE,
        ) as tar:
            for member in tar:
                deadline.check()

                member_count += 1
                if member_count > limits.max_archive_members:
                    _reject("member count cap")

                # Only regular files are ever read. Symlinks, hardlinks,
                # devices, and FIFOs are skipped and never resolved, so an
                # archive cannot use one to reach outside itself or to make
                # this module open something on the host (docs/SECURITY.md,
                # "Symlink / hardlink escape"). Directories are skipped here
                # too — the tree is rebuilt from the file paths, not from the
                # archive's own directory entries.
                if not member.isfile():
                    skipped[_skip_kind(member)] += 1
                    continue

                components = _check_member_name(member.name)
                if len(components) < 2:
                    # A regular file sitting beside the root rather than inside
                    # it. git archive does not produce that.
                    _reject("member outside the archive root")

                root_match = ROOT_PATTERN.fullmatch(components[0])
                if root_match is None:
                    _reject("archive root name")
                if root is None:
                    root = components[0]
                    # The authoritative commit SHA (docs/ARCHITECTURE.md,
                    # ingestion step 5). Recorded from the *first* accepted
                    # member; every later member is required to carry the same
                    # root by the branch below, so there is nothing to revise.
                    info.commit_sha = root_match.group(1)
                elif components[0] != root:
                    _reject("multiple archive roots")

                relative = "/".join(components[1:])
                if len(components) - 1 > limits.max_path_depth:
                    skipped["path_depth"] += 1
                    continue
                if len(relative) > limits.max_path_length:
                    skipped["path_length"] += 1
                    continue
                # Unreachable through tarfile as it stands: a GNU base-256
                # size field encoding a negative number makes tarfile compute
                # a negative offset and raise ReadError before the member is
                # ever handed over. Kept because the consequence of it becoming
                # reachable is that the cap above compares the wrong way round
                # and an unbounded read follows.
                if member.size < 0:
                    _reject("negative member size")
                if member.size > limits.max_member_bytes:
                    skipped["member_size"] += 1
                    continue

                handle = tar.extractfile(member)
                if handle is None:
                    # isfile() was true, so this should not happen; skipping is
                    # the safe reading of a tarfile that disagrees with itself.
                    skipped["unreadable"] += 1
                    continue
                # Bounded by the member-size check above, which is why this
                # unbounded-looking read is not one.
                content = handle.read()

                yielded += 1
                logger.debug("archive member accepted: %s", relative)
                yield PurePosixPath(relative), content
    except tarfile.TarError:
        # Truncated, corrupt, or deliberately malformed tar structure. The
        # exception message can quote header bytes, so it is not re-raised.
        _reject("malformed tar stream")
    except (gzip.BadGzipFile, zlib.error, EOFError):
        # Not a gzip stream, or one that ends mid-block.
        _reject("malformed gzip stream")

    logger.info(
        "archive read: %d members, %d files yielded, %d skipped (%s), %d bytes decompressed",
        member_count,
        yielded,
        sum(skipped.values()),
        dict(sorted(skipped.items())),
        decompressed.count,
    )


def _skip_kind(member: tarfile.TarInfo) -> str:
    """A fixed label for why a member is not a regular file. Never a path."""
    if member.isdir():
        return "directory"
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    if member.ischr() or member.isblk():
        return "device"
    if member.isfifo():
        return "fifo"
    return "other"
