"""Streaming archive reader: member validation and resource limits.

Every archive here is built in process (`tests/fixtures/tarballs.py`), so each
attack is readable in the diff rather than hidden in a committed binary. No
test touches the network or the filesystem — the reader has no code path that
opens either.

The two outcomes are deliberately distinguished throughout:
`ArchiveRejectedError` means the archive is structurally hostile and the run
stops; `RepositoryTooLargeError` means a byte budget ran out. A test that
merely asserted `AppError` would not notice the two swapping places.
"""

import gzip
import io
import tarfile
import unicodedata
from collections.abc import Iterator
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Literal

import pytest

from app.analysis.deadline import Deadline
from app.config import Settings
from app.errors import (
    AnalysisTimeoutError,
    AppError,
    ArchiveRejectedError,
    ErrorCode,
    RepositoryTooLargeError,
)
from app.fetch.archive import ROOT_PATTERN, ArchiveInfo, Limits, iter_source_files
from tests.fixtures.tarballs import (
    ROOT,
    TarMember,
    chunked,
    make_bomb,
    make_hardlink_member,
    make_many_members,
    make_member_with_name,
    make_oversized_header,
    make_pax_name,
    make_source_tar,
    make_symlink_member,
    make_tar,
    noise,
)

LIMITS = Limits.from_settings(Settings())
FRESH = Deadline.after(60)

# For tests about *one* control, the others are lifted out of the way rather
# than left to fire first and mask the thing under test.
NO_RATIO_GUARD = replace(LIMITS, max_compression_ratio=1_000_000)


def read(payload: bytes, limits: Limits = LIMITS, deadline: Deadline | None = None) -> list[str]:
    """Run the reader to completion and return the yielded paths as strings."""
    files = iter_source_files(chunked(payload), limits, deadline or Deadline.after(60))
    return [str(path) for path, _ in files]


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_yields_regular_files_relative_to_the_root() -> None:
    payload = make_source_tar(
        {
            "package.json": b"{}",
            "src/index.ts": b"import './a';\n",
            "src/nested/a.ts": b"export const a = 1;\n",
        }
    )
    results = list(iter_source_files(chunked(payload), LIMITS, FRESH))

    # The root directory is stripped: node IDs are repository-relative.
    assert [str(path) for path, _ in results] == [
        "package.json",
        "src/index.ts",
        "src/nested/a.ts",
    ]
    assert results[1][1] == b"import './a';\n"
    assert all(isinstance(path, PurePosixPath) for path, _ in results)


def test_preserves_archive_order() -> None:
    names = ["z.ts", "a.ts", "m/q.ts", "b.ts"]
    payload = make_tar([TarMember(name=f"{ROOT}/{name}") for name in names])
    assert read(payload) == names


def test_empty_archive_yields_nothing() -> None:
    assert read(make_tar([])) == []


def test_directory_entries_are_not_yielded() -> None:
    payload = make_tar(
        [
            TarMember(name=ROOT, type=tarfile.DIRTYPE, mode=0o755),
            TarMember(name=f"{ROOT}/src", type=tarfile.DIRTYPE, mode=0o755),
            TarMember(name=f"{ROOT}/src/a.ts", data=b"1"),
        ]
    )
    assert read(payload) == ["src/a.ts"]


def test_reads_lazily_rather_than_buffering_the_archive() -> None:
    """Abandoning the generator abandons the download.

    This is the property ADR-003 rests on: memory is one member, not one
    repository, and a caller that stops early stops the transfer.
    """
    payload = make_source_tar({f"src/f{i}.ts": noise(4096, seed=i) for i in range(200)})
    pulled = 0

    def counting_chunks() -> Iterator[bytes]:
        nonlocal pulled
        for chunk in chunked(payload, 4096):
            pulled += len(chunk)
            yield chunk

    files = iter_source_files(counting_chunks(), LIMITS, FRESH)
    next(files)
    del files

    assert 0 < pulled < len(payload)


# --------------------------------------------------------------------------
# Non-regular members: skipped, never followed
# --------------------------------------------------------------------------


def test_symlink_is_skipped_and_not_followed() -> None:
    payload = make_symlink_member(f"{ROOT}/link.ts", "/etc/passwd")
    # The symlink vanishes; the real file beside it survives. Nothing reads
    # /etc/passwd, which is guaranteed structurally: extractfile is never
    # called for a non-regular member.
    assert read(payload) == ["real.ts"]


def test_symlink_to_a_relative_escape_is_skipped() -> None:
    payload = make_symlink_member(f"{ROOT}/link.ts", "../../../../etc/shadow")
    assert read(payload) == ["real.ts"]


def test_hardlink_is_skipped() -> None:
    payload = make_hardlink_member(f"{ROOT}/link.ts", f"{ROOT}/real.ts")
    assert read(payload) == ["real.ts"]


@pytest.mark.parametrize(
    "member_type",
    [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE],
    ids=["chr", "blk", "fifo"],
)
def test_device_and_fifo_members_are_skipped(member_type: bytes) -> None:
    payload = make_tar(
        [
            TarMember(name=f"{ROOT}/dev-entry", type=member_type),
            TarMember(name=f"{ROOT}/real.ts", data=b"1"),
        ]
    )
    assert read(payload) == ["real.ts"]


# --------------------------------------------------------------------------
# Path traversal and absolute paths
# --------------------------------------------------------------------------

TRAVERSING_NAMES: list[str] = [
    "../../etc/passwd",
    f"{ROOT}/../../etc/passwd",
    f"{ROOT}/src/../../../etc/passwd",
    "..\\..\\x",
    f"{ROOT}\\..\\..\\x",
    # A backslash-only spelling must not survive as one innocuous component.
    f"{ROOT}\\..\\evil.ts",
    # Backslashes *below* the root, where the root-name pattern cannot help.
    # Without the \ -> / normalization these are a single component that is not
    # "..", so they pass the component check and are yielded verbatim. Mutation
    # testing found this: deleting the normalization left every other traversal
    # case failing on the root pattern instead, and the suite stayed green.
    f"{ROOT}/src\\..\\..\\evil.ts",
    f"{ROOT}/a/b\\..\\..\\..\\etc\\passwd",
    f"{ROOT}/src/..\\..\\evil.ts",
    f"{ROOT}/src\\../../evil.ts",
    # Single-dot components and empty components, both spellings.
    f"{ROOT}/./evil.ts",
    f"{ROOT}//evil.ts",
    f"{ROOT}/src/./../../evil.ts",
    "..",
    ".",
    # A name that is *only* separators.
    "/",
    "//",
]


@pytest.mark.parametrize("name", TRAVERSING_NAMES, ids=TRAVERSING_NAMES)
def test_traversing_member_name_rejects_the_archive(name: str) -> None:
    with pytest.raises(ArchiveRejectedError):
        read(make_member_with_name(name, b"root:x:0:0\n"))


ABSOLUTE_NAMES: list[str] = [
    "/etc/passwd",
    "/",
    "C:\\Windows\\System32\\config\\SAM",
    "c:/windows/win.ini",
    "\\\\server\\share\\payload.ts",
    "\\etc\\passwd",
    # Absolute even with a legitimate-looking tail.
    f"/{ROOT}/src/index.ts",
]


@pytest.mark.parametrize("name", ABSOLUTE_NAMES, ids=ABSOLUTE_NAMES)
def test_absolute_member_name_rejects_the_archive(name: str) -> None:
    with pytest.raises(ArchiveRejectedError):
        read(make_member_with_name(name))


def test_backslash_below_the_root_is_treated_as_a_separator() -> None:
    """A backslash is a separator, not an ordinary character.

    Splitting on it is what makes the traversal cases above reachable by the
    component check. The visible consequence is that a member genuinely named
    ``src\\evil.ts`` — legal on Linux — is reported as ``src/evil.ts``. That is
    the conservative direction: nothing is written, and a path that is
    ambiguous between two platforms is resolved toward the stricter reading.
    """
    assert read(make_member_with_name(f"{ROOT}/src\\evil.ts")) == ["src/evil.ts"]


def test_traversal_rejects_before_any_later_file_is_yielded() -> None:
    """The archive is refused, not partially trusted.

    A hostile member early in the stream must stop the run rather than be
    skipped, because an archive containing one is not a repository.
    """
    payload = make_tar(
        [
            TarMember(name=f"{ROOT}/../../etc/passwd", data=b"x"),
            TarMember(name=f"{ROOT}/ok.ts", data=b"x"),
        ]
    )
    with pytest.raises(ArchiveRejectedError):
        read(payload)


# --------------------------------------------------------------------------
# Archive root
# --------------------------------------------------------------------------


def test_root_pattern_matches_a_github_tarball_root() -> None:
    assert ROOT_PATTERN.fullmatch(ROOT)
    assert ROOT_PATTERN.fullmatch("react-a1b2c3d")
    assert ROOT_PATTERN.fullmatch("facebook-react-" + "0" * 40)


BAD_ROOTS: list[str] = [
    # No SHA suffix at all.
    "src",
    "widgets",
    # Too short / too long / not hex to be a commit.
    "widgets-a1b2c3",
    "widgets-" + "a" * 41,
    "widgets-g1b2c3d",
    "widgets-A1B2C3D",
    # Characters outside the accepted set.
    "wid gets-a1b2c3d",
    "widgets$-a1b2c3d",
    "wid/gets-a1b2c3d",
]


@pytest.mark.parametrize("root", BAD_ROOTS, ids=BAD_ROOTS)
def test_unrecognised_archive_root_is_rejected(root: str) -> None:
    with pytest.raises(ArchiveRejectedError):
        read(make_member_with_name(f"{root}/src/index.ts"))


# --------------------------------------------------------------------------
# ArchiveInfo — the second return channel (ADR-015)
# --------------------------------------------------------------------------


def test_info_receives_the_commit_sha() -> None:
    """The SHA the whole /api/source contract is pinned to.

    Harvested from the root directory rather than from the redirect URL, which
    names a commit only when the ref was already a SHA.
    """
    info = ArchiveInfo()
    list(iter_source_files(chunked(make_source_tar({"a.ts": b"1"})), LIMITS, FRESH, info))

    assert info.commit_sha == "a1b2c3d"


# The root is "<name>-<sha>", and a repository name may itself end in a
# hyphenated hex run. The trailing run is the commit; the earlier one is part
# of the name.
SHA_ROOTS: list[tuple[str, str]] = [
    ("acme-widgets-a1b2c3d", "a1b2c3d"),
    ("react-a1b2c3d", "a1b2c3d"),
    ("facebook-react-" + "0" * 40, "0" * 40),
    # A hex run inside the *name*: the capture must bind the last one.
    ("acme-deadbee-a1b2c3d", "a1b2c3d"),
    ("repo-abc-defabcd", "defabcd"),
]


@pytest.mark.parametrize(("root", "sha"), SHA_ROOTS, ids=[r for r, _ in SHA_ROOTS])
def test_commit_sha_is_the_trailing_hex_run(root: str, sha: str) -> None:
    info = ArchiveInfo()
    list(
        iter_source_files(
            chunked(make_source_tar({"a.ts": b"1"}, root=root)), LIMITS, FRESH, info
        )
    )

    assert info.commit_sha == sha


def test_commit_sha_is_available_before_the_archive_is_exhausted() -> None:
    """The property that makes an out-parameter the right channel.

    The pipeline stops at `MAX_SOURCE_FILES` without draining the generator, so
    a channel that only delivers on exhaustion — a generator `return` value —
    would hand back nothing in exactly the case that needs it.
    """
    info = ArchiveInfo()
    members = iter_source_files(
        chunked(make_source_tar({"a.ts": b"1", "b.ts": b"2"})), LIMITS, FRESH, info
    )

    next(members)
    assert info.commit_sha == "a1b2c3d"
    members.close()


def test_info_receives_skip_counts_keyed_by_reason() -> None:
    """Counts the reader used to compute and then only log."""
    payload = make_tar(
        [
            TarMember(name=ROOT, type=tarfile.DIRTYPE, mode=0o755),
            TarMember(name=f"{ROOT}/real.ts", data=b"1"),
            TarMember(name=f"{ROOT}/link.ts", type=tarfile.SYMTYPE, linkname="/etc/passwd"),
            TarMember(name=f"{ROOT}/big.ts", data=b"x" * 4096),
        ]
    )
    info = ArchiveInfo()
    limits = replace(LIMITS, max_member_bytes=1024)

    paths = [str(path) for path, _ in iter_source_files(chunked(payload), limits, FRESH, info)]

    assert paths == ["real.ts"]
    assert dict(info.skipped) == {"directory": 1, "symlink": 1, "member_size": 1}


def test_info_is_optional() -> None:
    """Every existing caller passes three arguments; none of them break."""
    assert read(make_source_tar({"a.ts": b"1"})) == ["a.ts"]


def test_multiple_roots_are_rejected() -> None:
    payload = make_tar(
        [
            TarMember(name=f"{ROOT}/a.ts", data=b"1"),
            TarMember(name="other-repo-9f8e7d6/b.ts", data=b"2"),
        ]
    )
    with pytest.raises(ArchiveRejectedError):
        read(payload)


def test_second_root_is_rejected_even_when_it_is_well_formed() -> None:
    """A valid-looking second root is still a second root.

    The SHA harvested from the root name pins every later /api/source fetch, so
    an archive that offers two of them has no single authoritative answer.
    """
    payload = make_tar(
        [
            TarMember(name=f"{ROOT}/a.ts"),
            TarMember(name=f"{ROOT}/b.ts"),
            TarMember(name=f"acme-widgets-{'f' * 40}/c.ts"),
        ]
    )
    with pytest.raises(ArchiveRejectedError):
        read(payload)


def test_file_beside_the_root_is_rejected() -> None:
    # A regular file at the top level of the archive, with no root directory to
    # strip. git archive does not produce that shape.
    with pytest.raises(ArchiveRejectedError):
        read(make_member_with_name("widgets-a1b2c3d"))


# --------------------------------------------------------------------------
# Malformed member names
# --------------------------------------------------------------------------


def test_nul_in_a_pax_path_rejects_the_archive() -> None:
    # Only reachable through a pax extended header: the ustar name field is
    # NUL-terminated, so tarfile truncates there instead.
    payload = make_pax_name(f"{ROOT}/ev\x00il.ts")
    with pytest.raises(ArchiveRejectedError):
        read(payload)


def test_nul_fixture_really_produces_a_nul_name() -> None:
    """Guards the fixture, not the reader.

    If a future tarfile sanitized pax paths, the test above would pass for the
    wrong reason — the archive would simply be clean.
    """
    payload = make_pax_name(f"{ROOT}/ev\x00il.ts")
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(payload)), mode="r|") as tar:
        names = [member.name for member in tar]
    assert names == [f"{ROOT}/ev\x00il.ts"]


SURROGATE_NAMES: list[str] = [
    f"{ROOT}/\udcff.ts",
    f"{ROOT}/src/\udc80\udcbe.ts",
    f"{ROOT}/caf\udce9.ts",
]


@pytest.mark.parametrize("name", SURROGATE_NAMES, ids=["ff", "80be", "e9"])
def test_undecodable_member_name_rejects_the_archive(name: str) -> None:
    # tarfile decodes header names with errors="surrogateescape", so raw
    # non-UTF-8 bytes arrive as lone surrogates rather than raising. Such a
    # string cannot be JSON-encoded or logged, and no real repository has one.
    with pytest.raises(ArchiveRejectedError):
        read(make_member_with_name(name))


def test_surrogate_fixture_really_writes_raw_bytes() -> None:
    payload = make_member_with_name(f"{ROOT}/\udcff.ts")
    assert b"\xff.ts" in gzip.decompress(payload)


def test_valid_non_ascii_member_name_is_accepted() -> None:
    # The rule is "decodable", not "ASCII". A repository may legitimately
    # contain UTF-8 filenames and rejecting them would be a bug, not a control.
    assert read(make_member_with_name(f"{ROOT}/café/日本語.ts")) == ["café/日本語.ts"]


# --------------------------------------------------------------------------
# Unicode normalization: the reader must not perform any
# --------------------------------------------------------------------------
#
# A tar member name is bytes. `tarfile` decodes it but does not normalize it,
# and neither does this module — the component check runs against the exact
# code points from the header. That is the safe direction, and these tests pin
# it, because normalizing would *create* the attack rather than defend against
# one: under NFKC, U+FF0E FULLWIDTH FULL STOP folds to "." and U+FF0F FULLWIDTH
# SOLIDUS folds to "/", so a name that is one inert component before
# normalization becomes traversal after it.
#
# The exposure is therefore downstream, not here: anything that later
# normalizes a yielded path — a filesystem with a normalizing layer, a
# comparison that calls unicodedata.normalize, a database collation — undoes
# the guarantee these tests assert.


def test_fullwidth_lookalikes_are_not_folded_into_traversal() -> None:
    """U+FF0E and U+FF0F stay ordinary characters, not "." and "/".

    Both survive as a single component, so they are neither traversal nor a
    separator. If this ever starts raising, something upstream began
    normalizing and the traversal check is being handed different text than
    the archive contained.
    """
    # Written as escapes, not literals: the characters are visually identical
    # to "." and "/", which is the entire premise of the attack and makes them
    # unreadable in a diff. U+FF0E FULLWIDTH FULL STOP, U+FF0F FULLWIDTH SOLIDUS.
    name = "\uff0e\uff0e\uff0fetc\uff0fpasswd.ts"
    assert unicodedata.normalize("NFKC", name) == "../etc/passwd.ts"
    assert read(make_member_with_name(f"{ROOT}/{name}")) == [name]


def test_fullwidth_solidus_does_not_split_a_component() -> None:
    # The component count is the observable proof: one component, not three.
    # A normalizing implementation would report depth 3 here.
    name = "src\uff0fnested\uff0ffile.ts"
    assert PurePosixPath(read(make_member_with_name(f"{ROOT}/{name}"))[0]).parts == (name,)


@pytest.mark.parametrize("form", ["NFC", "NFD"])
def test_decomposed_and_composed_names_are_preserved_verbatim(
    form: Literal["NFC", "NFD"],
) -> None:
    """Both spellings of the same grapheme round-trip unchanged and distinctly.

    A path yielded here becomes a graph node ID and the subject of an
    /api/source token. Silently folding one spelling into the other would make
    the ID disagree with the archive it came from, so the token would not
    match the file the user clicked.
    """
    name = unicodedata.normalize(form, "café/résumé.ts")
    yielded = read(make_member_with_name(f"{ROOT}/{name}"))
    assert yielded == [name]
    # Not merely equal after normalization — equal as code points.
    assert yielded[0].encode("utf-8") == name.encode("utf-8")


def test_the_two_spellings_are_different_paths() -> None:
    # Guards the assertion above from being vacuous: if these two were equal,
    # "preserved verbatim" would hold trivially for any implementation.
    assert unicodedata.normalize("NFC", "café") != unicodedata.normalize("NFD", "café")


def test_normalization_does_not_rescue_real_traversal() -> None:
    # The mirror case. Actual ASCII ".." is still rejected when it sits beside
    # characters that a normalizing reader might have been distracted by.
    with pytest.raises(ArchiveRejectedError):
        read(make_member_with_name(f"{ROOT}/café/../../etc/passwd"))


# --------------------------------------------------------------------------
# Per-file resource caps: skip the file, keep the archive
# --------------------------------------------------------------------------


def test_oversized_member_is_skipped_not_fatal() -> None:
    limits = replace(LIMITS, max_member_bytes=1024)
    payload = make_tar(
        [
            TarMember(name=f"{ROOT}/small.ts", data=b"x" * 512),
            TarMember(name=f"{ROOT}/huge.ts", data=b"x" * 4096),
            TarMember(name=f"{ROOT}/after.ts", data=b"y" * 16),
        ]
    )
    # The skipped member's data is still streamed past to reach the next
    # header, which is why "skip" must not mean "do not decompress".
    assert read(payload, limits) == ["small.ts", "after.ts"]


def test_member_at_exactly_the_size_cap_is_kept() -> None:
    limits = replace(LIMITS, max_member_bytes=1024)
    payload = make_member_with_name(f"{ROOT}/edge.ts", b"x" * 1024)
    assert read(payload, limits) == ["edge.ts"]


def test_too_deep_member_is_skipped() -> None:
    limits = replace(LIMITS, max_path_depth=4)
    deep = "/".join(f"d{i}" for i in range(10)) + "/leaf.ts"
    payload = make_tar(
        [
            TarMember(name=f"{ROOT}/{deep}"),
            TarMember(name=f"{ROOT}/a/b/c/ok.ts"),
        ]
    )
    assert read(payload, limits) == ["a/b/c/ok.ts"]


def test_member_at_exactly_the_depth_cap_is_kept() -> None:
    limits = replace(LIMITS, max_path_depth=4)
    payload = make_member_with_name(f"{ROOT}/a/b/c/leaf.ts")
    assert read(payload, limits) == ["a/b/c/leaf.ts"]


def test_too_long_member_path_is_skipped() -> None:
    limits = replace(LIMITS, max_path_length=64)
    payload = make_tar(
        [
            TarMember(name=f"{ROOT}/{'n' * 200}.ts"),
            TarMember(name=f"{ROOT}/short.ts"),
        ]
    )
    assert read(payload, limits) == ["short.ts"]


def test_path_length_is_measured_after_the_root_is_stripped() -> None:
    # The root is not part of what the frontend ever sees, so counting it would
    # make the cap depend on the length of the repository name.
    limits = replace(LIMITS, max_path_length=len("short.ts"))
    assert read(make_member_with_name(f"{ROOT}/short.ts"), limits) == ["short.ts"]


def test_default_limits_come_from_settings() -> None:
    settings = Settings()
    limits = Limits.from_settings(settings)
    assert limits.max_download_bytes == settings.MAX_DOWNLOAD_BYTES
    assert limits.max_extracted_bytes == settings.MAX_EXTRACTED_BYTES
    assert limits.max_compression_ratio == settings.MAX_COMPRESSION_RATIO
    assert limits.ratio_floor_bytes == settings.RATIO_FLOOR_BYTES
    assert limits.max_archive_members == settings.MAX_ARCHIVE_MEMBERS
    assert limits.max_member_bytes == settings.MAX_MEMBER_BYTES
    assert limits.max_path_depth == settings.MAX_PATH_DEPTH
    assert limits.max_path_length == settings.MAX_PATH_LENGTH


def test_limits_from_settings_tracks_an_overridden_setting() -> None:
    # The point of reading Settings rather than restating the numbers: tighten
    # a limit in the environment and the reader actually enforces the new one.
    limits = Limits.from_settings(Settings(MAX_MEMBER_BYTES=7))
    assert limits.max_member_bytes == 7


# --------------------------------------------------------------------------
# Archive-wide resource caps
# --------------------------------------------------------------------------


def test_member_count_cap_is_enforced() -> None:
    limits = replace(NO_RATIO_GUARD, max_archive_members=10)
    with pytest.raises(ArchiveRejectedError):
        read(make_many_members(11), limits)


def test_member_count_at_exactly_the_cap_is_accepted() -> None:
    limits = replace(NO_RATIO_GUARD, max_archive_members=10)
    assert len(read(make_many_members(10), limits)) == 10


def test_fifty_thousand_members_are_accepted_at_the_default_cap() -> None:
    # 50 000 is the real MAX_ARCHIVE_MEMBERS. The ratio guard is lifted so this
    # tests the count cap alone: an archive of 50 000 *empty* files is mostly
    # zero padding and sits within a factor of two of the 100:1 ratio, which
    # would make this test's outcome a hostage to the zlib version.
    limits = replace(NO_RATIO_GUARD, max_archive_members=50_000)
    assert len(read(make_many_members(50_000), limits)) == 50_000


def test_one_member_past_the_default_cap_is_rejected() -> None:
    limits = replace(NO_RATIO_GUARD, max_archive_members=50_000)
    with pytest.raises(ArchiveRejectedError):
        read(make_many_members(50_001), limits)


def test_compressed_download_cap_is_enforced() -> None:
    payload = make_source_tar({f"src/f{i}.ts": noise(4096, seed=i) for i in range(64)})
    limits = replace(LIMITS, max_download_bytes=1024)
    assert len(payload) > 1024
    with pytest.raises(RepositoryTooLargeError):
        read(payload, limits)


def test_download_cap_trips_before_the_whole_stream_is_pulled() -> None:
    payload = make_source_tar({f"src/f{i}.ts": noise(4096, seed=i) for i in range(400)})
    pulled = 0

    def counting_chunks() -> Iterator[bytes]:
        nonlocal pulled
        for chunk in chunked(payload, 4096):
            pulled += len(chunk)
            yield chunk

    limits = replace(LIMITS, max_download_bytes=16 * 1024)
    with pytest.raises(RepositoryTooLargeError):
        list(iter_source_files(counting_chunks(), limits, FRESH))
    assert pulled <= 16 * 1024 + 4096


def test_extracted_size_cap_is_enforced() -> None:
    payload = make_source_tar({f"src/f{i}.ts": b"x" * 4096 for i in range(64)})
    limits = replace(NO_RATIO_GUARD, max_extracted_bytes=8192)
    with pytest.raises(RepositoryTooLargeError):
        read(payload, limits)


def test_gigabyte_bomb_trips_the_ratio_guard_near_the_floor() -> None:
    """A 1 GiB payload of zeros dies at ~8 MiB, not at 1 GiB.

    The bomb's single member is far past MAX_MEMBER_BYTES, so no file is ever
    yielded from it — and yet tarfile must decompress all of it to reach the
    next header. Summing the sizes of *accepted* members would therefore see
    zero bytes while a gigabyte went through the decompressor. Metering the
    decompressed stream is what catches it.
    """
    payload = make_bomb(1024 * 1024 * 1024)
    pulled = 0

    def counting_chunks() -> Iterator[bytes]:
        nonlocal pulled
        for chunk in chunked(payload):
            pulled += len(chunk)
            yield chunk

    with pytest.raises(ArchiveRejectedError):
        list(iter_source_files(counting_chunks(), LIMITS, FRESH))

    # Well under the 256 MiB extracted cap and under the 64 MiB download cap:
    # it is the ratio that fired, not either byte budget, and it fired after
    # reading a small fraction of the archive.
    assert pulled < len(payload) // 2


def test_ratio_guard_tolerates_a_small_well_compressing_archive() -> None:
    """Below the floor the ratio is not consulted.

    A handful of kilobytes of repetitive source compresses far past 100:1, and
    rejecting that would refuse ordinary repositories.
    """
    payload = make_source_tar({"src/a.ts": b"const a = 1;\n" * 4000})
    assert read(payload) == ["src/a.ts"]


def test_ratio_guard_ignores_a_high_ratio_under_the_floor() -> None:
    limits = replace(LIMITS, max_compression_ratio=2, ratio_floor_bytes=1024 * 1024)
    payload = make_source_tar({"src/a.ts": b"a" * 4096})
    assert read(payload, limits) == ["src/a.ts"]


def test_ratio_guard_fires_once_past_the_floor() -> None:
    limits = replace(LIMITS, max_compression_ratio=2, ratio_floor_bytes=1024)
    payload = make_source_tar({"src/a.ts": b"a" * 200_000})
    with pytest.raises(ArchiveRejectedError):
        read(payload, limits)


# --------------------------------------------------------------------------
# Deadline
# --------------------------------------------------------------------------


def test_expired_deadline_aborts_between_members() -> None:
    payload = make_source_tar({"src/a.ts": b"1", "src/b.ts": b"2"})
    with pytest.raises(AnalysisTimeoutError):
        read(payload, LIMITS, Deadline.after(-1))


def test_live_deadline_does_not_abort() -> None:
    payload = make_source_tar({"src/a.ts": b"1"})
    assert read(payload, LIMITS, Deadline.after(60)) == ["src/a.ts"]


# --------------------------------------------------------------------------
# Malformed streams
# --------------------------------------------------------------------------

MALFORMED_PAYLOADS: dict[str, bytes] = {
    "empty": b"",
    "not gzip": b"this is not a gzip stream at all",
    "gzip of garbage": gzip.compress(b"nowhere near a tar header" * 100),
    "gzip magic only": b"\x1f\x8b",
    "gzip of zeros": gzip.compress(bytes(100_000)),
}


@pytest.mark.parametrize("name", sorted(MALFORMED_PAYLOADS), ids=sorted(MALFORMED_PAYLOADS))
def test_malformed_stream_raises_only_the_typed_error(name: str) -> None:
    payload = MALFORMED_PAYLOADS[name]
    try:
        result = read(payload)
    except ArchiveRejectedError:
        return
    # A gzip stream of pure zeros is a valid *empty* tar — the end-of-archive
    # marker is exactly that — so yielding nothing is also a correct answer.
    assert result == []


def test_truncated_archive_is_rejected() -> None:
    payload = make_source_tar({f"src/f{i}.ts": noise(2048, seed=i) for i in range(32)})
    with pytest.raises(ArchiveRejectedError):
        read(payload[: len(payload) // 2])


def test_truncated_mid_member_is_rejected() -> None:
    whole = gzip.decompress(make_source_tar({"src/a.ts": noise(100_000)}))
    with pytest.raises(ArchiveRejectedError):
        read(gzip.compress(whole[:2048]))


def test_declared_size_larger_than_the_data_is_rejected() -> None:
    # The header claims more bytes than the archive holds, so the stream ends
    # inside the member. tarfile raises; it must not escape as a TarError.
    payload = make_oversized_header(f"{ROOT}/a.ts", b"x", 100_000)
    with pytest.raises(ArchiveRejectedError):
        read(payload)


# --------------------------------------------------------------------------
# Error contract
# --------------------------------------------------------------------------

HOSTILE_NAMES: list[str] = [
    *TRAVERSING_NAMES,
    *ABSOLUTE_NAMES,
    *SURROGATE_NAMES,
    *[f"{root}/x.ts" for root in BAD_ROOTS],
    f"{ROOT}/\udcff\\..\\..\x00/x",
    ROOT,
    "",
    "\\",
    ":",
    "C:",
    "\udcff",
]


@pytest.mark.parametrize("name", HOSTILE_NAMES, ids=range(len(HOSTILE_NAMES)))
def test_no_hostile_name_escapes_as_an_untyped_exception(name: str) -> None:
    """Nothing but the typed errors ever leaves this module.

    A bare ValueError or UnicodeError escaping here would break the error
    contract *and* would carry the offending name into a traceback, which is
    the disclosure rule in docs/SECURITY.md.
    """
    try:
        read(make_member_with_name(name))
    except AppError as error:
        assert isinstance(error, ArchiveRejectedError | RepositoryTooLargeError)


def test_rejection_body_never_contains_the_offending_path() -> None:
    secret = f"{ROOT}/../../home/operator/.ssh/id_ed25519"
    errors: list[AppError] = []
    for payload in (make_member_with_name(secret), make_member_with_name("/etc/shadow")):
        with pytest.raises(ArchiveRejectedError) as caught:
            read(payload)
        errors.append(caught.value)

    bodies = [error.body("req-1") for error in errors]
    # Byte-identical regardless of which hostile path caused them.
    assert bodies[0] == bodies[1]
    assert bodies[0]["error"]["code"] == ErrorCode.ARCHIVE_REJECTED.value
    assert "ssh" not in str(bodies[0])
    assert "etc" not in str(bodies[0])
