"""Containment check: traversal, absolute paths, prefix siblings, and symlinks.

The symlink cases are the reason this module cannot be replaced by a lexical
`is_relative_to`, so they are built as *real* symlinks under `tmp_path` rather
than mocked — a stubbed `realpath` would only prove the test agrees with
itself.
"""

from pathlib import Path

import pytest

from app.security.path_safety import safe_relative_path


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """An existing base directory, with a sibling and an outsider beside it."""
    root = tmp_path / "base"
    (root / "src").mkdir(parents=True)
    (root / "src" / "index.ts").write_text("export {}\n")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("token\n")
    return root


# --------------------------------------------------------------------------
# Accepted
# --------------------------------------------------------------------------


def test_resolves_a_path_inside_base(base: Path) -> None:
    result = safe_relative_path(base, "src/index.ts")
    assert result == base.resolve() / "src" / "index.ts"
    assert result.is_absolute()


def test_accepts_a_path_that_does_not_exist_yet(base: Path) -> None:
    """Resolution is non-strict, so a caller can check *before* creating."""
    result = safe_relative_path(base, "generated/deep/new.json")
    assert result == base.resolve() / "generated" / "deep" / "new.json"
    assert not result.exists()


def test_interior_dot_dot_that_stays_inside_is_accepted(base: Path) -> None:
    assert safe_relative_path(base, "src/../src/index.ts") == base.resolve() / "src/index.ts"


def test_empty_rel_resolves_to_base_itself(base: Path) -> None:
    assert safe_relative_path(base, "") == base.resolve()


def test_symlink_pointing_inside_base_is_accepted(base: Path) -> None:
    (base / "alias").symlink_to(base / "src")
    assert safe_relative_path(base, "alias/index.ts") == base.resolve() / "src" / "index.ts"


def test_symlinked_base_is_resolved_too(tmp_path: Path) -> None:
    """A caller may hand us a symlinked directory.

    `/tmp` is `/private/tmp` on macOS and `tmp_path` inherits that, so a base
    left unresolved would never equal a resolved candidate and *every* call
    would be refused.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    assert safe_relative_path(link, "a.ts") == real.resolve() / "a.ts"


# --------------------------------------------------------------------------
# Rejected: lexical traversal
# --------------------------------------------------------------------------

TRAVERSALS: list[str] = [
    "..",
    "../outside",
    "../outside/secret.txt",
    "../../etc/passwd",
    "src/../../outside/secret.txt",
    "src/../../../../../../etc/passwd",
    "./../../etc/passwd",
]


@pytest.mark.parametrize("rel", TRAVERSALS, ids=TRAVERSALS)
def test_rejects_traversal(base: Path, rel: str) -> None:
    with pytest.raises(ValueError):
        safe_relative_path(base, rel)


ABSOLUTE: list[str] = [
    "/etc/passwd",
    "/",
    "//etc/passwd",
]


@pytest.mark.parametrize("rel", ABSOLUTE, ids=ABSOLUTE)
def test_rejects_absolute_path(base: Path, rel: str) -> None:
    # `base / "/etc/passwd"` is `/etc/passwd`: a leading separator discards the
    # base outright, so this must be refused before the join, not after it.
    with pytest.raises(ValueError):
        safe_relative_path(base, rel)


def test_rejects_embedded_nul(base: Path) -> None:
    # os.stat would raise ValueError itself, but with the offending path in the
    # message. Screened first so the rejection stays wordless.
    with pytest.raises(ValueError):
        safe_relative_path(base, "src/index.ts\x00.png")


def test_rejects_sibling_directory_sharing_a_prefix(tmp_path: Path) -> None:
    """The case `str.startswith(base)` gets wrong.

    `/…/base-evil` has `/…/base` as a string prefix and is not inside it.
    `commonpath` compares whole components, so it returns `/…` and the check
    fails as it should.
    """
    root = tmp_path / "base"
    root.mkdir()
    evil = tmp_path / "base-evil"
    evil.mkdir()
    (evil / "payload.ts").write_text("x\n")

    assert str(evil).startswith(str(root))  # the naive check would pass
    with pytest.raises(ValueError):
        safe_relative_path(root, "../base-evil/payload.ts")


# --------------------------------------------------------------------------
# Rejected: symlink escape — the case a lexical check cannot see
# --------------------------------------------------------------------------


def test_rejects_escape_through_a_symlinked_directory(base: Path, tmp_path: Path) -> None:
    """`base/link` -> `../outside`, so `link/secret.txt` is lexically inside.

    Every component of `link/secret.txt` is an ordinary name — no `..`, nothing
    absolute — so a check that only inspected the string would accept it and
    the caller would then read a file outside the base.
    """
    (base / "link").symlink_to(tmp_path / "outside", target_is_directory=True)

    # The lexical reading really is clean; the escape is entirely in the link.
    assert ".." not in "link/secret.txt"
    assert (base / "link" / "secret.txt").read_text() == "token\n"

    with pytest.raises(ValueError):
        safe_relative_path(base, "link/secret.txt")


def test_rejects_a_symlink_that_is_itself_the_target(base: Path, tmp_path: Path) -> None:
    (base / "leak").symlink_to(tmp_path / "outside" / "secret.txt")

    with pytest.raises(ValueError):
        safe_relative_path(base, "leak")


def test_rejects_escape_through_a_symlink_below_the_first_component(
    base: Path, tmp_path: Path
) -> None:
    """The link is nested, so the escape is not visible from the first segment."""
    (base / "src" / "vendor").symlink_to(tmp_path / "outside", target_is_directory=True)

    with pytest.raises(ValueError):
        safe_relative_path(base, "src/vendor/secret.txt")


def test_rejects_a_symlink_to_an_absolute_system_path(base: Path) -> None:
    (base / "etc").symlink_to("/etc", target_is_directory=True)

    with pytest.raises(ValueError):
        safe_relative_path(base, "etc/passwd")


def test_dangling_symlink_out_of_base_is_rejected(base: Path, tmp_path: Path) -> None:
    """Non-strict resolution still follows a link to a target that is absent."""
    (base / "ghost").symlink_to(tmp_path / "outside" / "not-there")

    with pytest.raises(ValueError):
        safe_relative_path(base, "ghost")


# --------------------------------------------------------------------------
# The rejection says nothing
# --------------------------------------------------------------------------

REJECTED: list[str] = [*TRAVERSALS, *ABSOLUTE, "src/index.ts\x00.png"]


@pytest.mark.parametrize("rel", REJECTED, ids=REJECTED)
def test_error_message_never_echoes_the_offending_path(base: Path, rel: str) -> None:
    """docs/SECURITY.md: no attacker-controlled string in a message or traceback.

    The path is checked in the fragments that survive normalization, since the
    message is a fixed literal and must contain none of them.
    """
    with pytest.raises(ValueError) as caught:
        safe_relative_path(base, rel)

    message = str(caught.value)
    assert "passwd" not in message
    assert "outside" not in message
    assert "index.ts" not in message
    assert str(base) not in message


def test_rejection_messages_are_drawn_from_a_fixed_set(base: Path) -> None:
    messages = set()
    for rel in REJECTED:
        with pytest.raises(ValueError) as caught:
            safe_relative_path(base, rel)
        messages.add(str(caught.value))

    assert messages == {
        "path escapes the base directory",
        "path is absolute",
        "path contains a NUL byte",
    }
