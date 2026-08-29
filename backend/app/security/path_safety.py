"""Containment check for any path built from untrusted input.

**Nothing in this codebase writes a file today** (ADR-003): the archive reader
streams members through memory and never touches the filesystem, which removes
the traversal class architecturally rather than defending against it. This
module exists anyway, tested, so that the first piece of code to need disk I/O
— a cache, a temp file, an export — has a correct primitive sitting there
instead of an `os.path.join` and an optimistic comment.

The check is realpath-then-`commonpath`, and both halves are load-bearing:

*realpath first*, on the candidate **and** on the base. A purely lexical check
(``PurePath.is_relative_to`` on unresolved paths) is blind to symlinks: if
``base/link`` points at ``/etc``, then ``base/link/passwd`` is lexically inside
``base`` and physically is not. Resolving the base too matters because a caller
can legitimately hand us a symlinked directory — ``/tmp`` on macOS is
``/private/tmp``, and `pytest`'s ``tmp_path`` inherits that — where an
unresolved base would never equal a resolved candidate and every call would
fail.

*`commonpath`, not `str.startswith`*. ``startswith`` accepts ``/srv/base-evil``
as a child of ``/srv/base``: the prefix matches, the component boundary does
not. ``commonpath`` compares whole components, so the sibling is rejected.

Resolution is non-strict, so a path that does not exist yet is still checked —
which is the whole point for a caller that is about to *create* the file.

Failure is a plain ``ValueError``. This is a low-level utility and not an HTTP
boundary, so it raises no ``AppError``; its caller is responsible for mapping
it. The messages are fixed literals and never quote the offending path
(docs/SECURITY.md, "A *stdlib* exception echoing user input") — the path is
attacker-controlled and would otherwise reach a log or a traceback verbatim.
"""

import os
from pathlib import Path

_ESCAPE = "path escapes the base directory"


def safe_relative_path(base: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``base``, or raise ``ValueError``.

    Returns the absolute, symlink-resolved path. ``rel`` must be relative;
    an empty ``rel`` resolves to ``base`` itself, which is inside ``base``.

    Raises:
        ValueError: ``rel`` is absolute, contains a NUL, or resolves to a
            location outside ``base`` — whether by ``..``, by a symlink, or by
            naming a sibling directory that merely shares a prefix.
    """
    # os.stat raises ValueError on an embedded NUL with the argument in the
    # message. Screening first keeps the path out of the traceback and keeps
    # every rejection on this function's own static wording.
    if "\x00" in rel:
        raise ValueError("path contains a NUL byte")
    # `Path("/base") / "/etc/passwd"` is `/etc/passwd` — a leading separator
    # discards the base entirely. commonpath would still catch the result, but
    # only after the join has already silently thrown the base away.
    if Path(rel).is_absolute():
        raise ValueError("path is absolute")

    base_real = Path(os.path.realpath(base))
    candidate = Path(os.path.realpath(base_real / rel))

    try:
        common = os.path.commonpath([base_real, candidate])
    except ValueError:
        # Raised when the two share no root at all (different drives on
        # Windows). "No common ancestor" is an escape.
        raise ValueError(_ESCAPE) from None

    # An `assert` would be stripped under `python -O`, which is exactly the
    # deployment where this check still needs to hold.
    if Path(common) != base_real:
        raise ValueError(_ESCAPE)
    return candidate
