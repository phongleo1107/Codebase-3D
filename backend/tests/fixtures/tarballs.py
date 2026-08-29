"""Malicious and benign tarballs, built in process.

docs/SECURITY.md requires attack archives to be constructed with `tarfile` and
`io.BytesIO` rather than committed as binary blobs, so every attack is legible
in the diff and nobody has to trust an opaque fixture file.

Everything here returns gzipped `bytes`. Feed them to the reader through
`chunked()`, which imitates `httpx.Response.iter_bytes()` — the chunk size is
not cosmetic, because the compression-ratio guard measures compressed bytes
*delivered to the decompressor*, and handing over an entire archive in one call
would let the read-ahead run ahead of the guard.
"""

import gzip
import io
import random
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, field

# A plausible GitHub legacy-tarball root: "<owner>-<repo>-<short sha>".
ROOT = "acme-widgets-a1b2c3d"

# httpx streams in 64 KiB chunks by default; matching that keeps the tests
# honest about how much compressed data is buffered ahead of the guard.
DEFAULT_CHUNK = 64 * 1024


@dataclass(frozen=True, slots=True)
class TarMember:
    """One archive entry. `name` is the full path *including* the root dir."""

    name: str
    data: bytes = b""
    type: bytes = tarfile.REGTYPE
    linkname: str = ""
    mode: int = 0o644
    pax_headers: dict[str, str] = field(default_factory=dict)


def chunked(payload: bytes, size: int = DEFAULT_CHUNK) -> Iterator[bytes]:
    """The payload as a byte iterator, the way an HTTP response arrives."""
    for offset in range(0, len(payload), size):
        yield payload[offset : offset + size]


def noise(size: int, *, seed: int = 0) -> bytes:
    """Deterministic incompressible filler.

    Needed wherever a test measures *how much of the stream was read*: a
    megabyte of `b"x"` gzips to a few hundred bytes, so the whole archive
    arrives in the decompressor's first read and every laziness or
    early-abort assertion becomes vacuously false.
    """
    return random.Random(seed).randbytes(size)  # noqa: S311 — filler, not a secret


def make_tar(
    members: list[TarMember],
    *,
    compresslevel: int = 6,
    tar_format: int = tarfile.GNU_FORMAT,
) -> bytes:
    """A gzipped tar containing exactly `members`, in order."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tar_format) as tar:
        for member in members:
            info = tarfile.TarInfo(member.name)
            info.type = member.type
            info.linkname = member.linkname
            info.mode = member.mode
            info.size = len(member.data)
            if member.pax_headers:
                info.pax_headers = dict(member.pax_headers)
            # A link/device/fifo entry carries no data blocks; passing a file
            # object for one makes tarfile write a body tarfile will not read.
            payload = io.BytesIO(member.data) if member.type == tarfile.REGTYPE else None
            tar.addfile(info, payload)
    return gzip.compress(buffer.getvalue(), compresslevel)


def make_source_tar(files: dict[str, bytes], *, root: str = ROOT) -> bytes:
    """The happy path: a root directory entry plus regular files under it."""
    members = [TarMember(name=root, type=tarfile.DIRTYPE, mode=0o755)]
    members += [TarMember(name=f"{root}/{path}", data=data) for path, data in files.items()]
    return make_tar(members)


def make_member_with_name(name: str, data: bytes = b"") -> bytes:
    """A one-member tar whose file sits at exactly `name`, unmodified.

    `tarfile` does not normalize what it writes, so a traversing, absolute, or
    otherwise malformed path survives the round trip verbatim — which is what
    makes this a usable attack fixture rather than a test of `tarfile`.
    """
    return make_tar([TarMember(name=name, data=data)])


def make_pax_name(name: str) -> bytes:
    """A tar whose *effective* path comes from a pax `path` header.

    The ustar name field is NUL-terminated, so a NUL cannot reach a reader
    through it. A pax extended header carries an arbitrary byte string and can,
    which is the only way to build the NUL-in-a-name case.
    """
    return make_tar(
        [
            TarMember(
                name=f"{ROOT}/placeholder.ts",
                pax_headers={"path": name},
            )
        ],
        tar_format=tarfile.PAX_FORMAT,
    )


def make_oversized_header(name: str, data: bytes, declared_size: int) -> bytes:
    """A member whose header claims more bytes than the archive contains.

    `tarfile` refuses to *write* this — `addfile` raises on a short body — so
    the header is emitted directly. A reader following the declared size runs
    off the end of the stream, which is the failure mode being tested.
    """
    info = tarfile.TarInfo(name)
    info.size = declared_size
    body = data + b"\0" * (-len(data) % tarfile.BLOCKSIZE)
    return gzip.compress(info.tobuf(tarfile.GNU_FORMAT) + body + b"\0" * 2 * tarfile.BLOCKSIZE)


def make_symlink_member(name: str, target: str) -> bytes:
    """A tar holding one symlink, plus one real file so the archive is usable."""
    return make_tar(
        [
            TarMember(name=name, type=tarfile.SYMTYPE, linkname=target),
            TarMember(name=f"{ROOT}/real.ts", data=b"export const ok = 1;\n"),
        ]
    )


def make_hardlink_member(name: str, target: str) -> bytes:
    return make_tar(
        [
            TarMember(name=f"{ROOT}/real.ts", data=b"export const ok = 1;\n"),
            TarMember(name=name, type=tarfile.LNKTYPE, linkname=target),
        ]
    )


_ZERO_MEMBER_SIZE = 1024 * 1024


def make_bomb(uncompressed_bytes: int) -> bytes:
    """A tar bomb: one member declaring `uncompressed_bytes`, all of them zero.

    "No files" in the sense that matters — the member is far past
    `MAX_MEMBER_BYTES`, so nothing is ever yielded from it. It still has to be
    decompressed in full, because a non-seeking `tarfile` reads past a skipped
    member to reach the next header. That is exactly the case the ratio guard
    exists for, and exactly the case that summing the sizes of *accepted*
    members would miss.

    Built by concatenating gzip members — `gzip` decodes a concatenated stream
    as one continuous output — so a gigabyte-sized bomb costs a megabyte of
    memory and a fraction of a second to construct, instead of a gigabyte of
    `BytesIO`.
    """
    info = tarfile.TarInfo(f"{ROOT}/bomb.bin")
    info.size = uncompressed_bytes
    header = info.tobuf(tarfile.GNU_FORMAT)

    # Payload padded to a block boundary, then the two-block end-of-archive
    # marker. All of it is zeros, so it is all interchangeable.
    padded = -(-uncompressed_bytes // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
    zeros = padded + 2 * tarfile.BLOCKSIZE

    whole, remainder = divmod(zeros, _ZERO_MEMBER_SIZE)
    zero_member = gzip.compress(b"\0" * _ZERO_MEMBER_SIZE, 6)
    return (
        gzip.compress(header, 6) + zero_member * whole + gzip.compress(b"\0" * remainder, 6)
    )


def make_many_members(count: int, *, root: str = ROOT, compresslevel: int = 1) -> bytes:
    """`count` empty regular files, for exercising the member-count cap."""
    members = [TarMember(name=f"{root}/f{index:06d}.ts") for index in range(count)]
    return make_tar(members, compresslevel=compresslevel)
