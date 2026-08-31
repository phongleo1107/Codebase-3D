"""Description extraction: the first repository *text* to reach a response.

Every other analysis module in this project produces structure — paths, counts,
line numbers. This one produces a quotation, so the tests split along a
different line than usual:

*The three forms and their absence.* A JSDoc block, a plain block, a `//` run,
and a file with none. These are the feature.

*Normalization as a control, not as tidying.* Control characters, a NUL, an
ANSI escape, U+202E, a comment past the cap, and a comment that is only markers
and whitespace. Each of these is a row in docs/SECURITY.md rather than a
cosmetic preference, and each is asserted on the *output* — what a response body
would carry — rather than on an intermediate.

*Totality.* Undecodable bytes, an unterminated comment, an empty file, a binary
blob. `header_description` has no failure mode: everything it cannot use is
`None`, and nothing it is handed raises.

The extractor is deliberately not given a tree (ADR-020), so there is no
grammar, no `Language`, and no `Deadline` anywhere in this file — which is
itself the point being made.
"""

import pytest

from app.analysis.descriptions import (
    _SCAN_BYTES,
    header_description,
    normalize_comment,
)
from app.config import Settings

SETTINGS = Settings()
LIMIT = SETTINGS.MAX_DESCRIPTION_CHARS


def describe(source: bytes, settings: Settings = SETTINGS) -> str | None:
    return header_description(source, settings)


# --------------------------------------------------------------------------
# The three forms, and their absence
# --------------------------------------------------------------------------


def test_a_jsdoc_header_becomes_the_description() -> None:
    source = b"""\
/**
 * The user repository.
 *
 * Reads and writes users.
 */
import { db } from './db';
"""

    assert describe(source) == "The user repository. Reads and writes users."


def test_a_plain_block_header_becomes_the_description() -> None:
    assert describe(b"/* Shared HTTP helpers. */\nexport const get = 1;\n") == (
        "Shared HTTP helpers."
    )


def test_a_run_of_line_comments_becomes_the_description() -> None:
    source = b"// Entry point.\n// Boots the server.\nimport './app';\n"

    assert describe(source) == "Entry point. Boots the server."


def test_a_file_with_no_header_comment_has_no_description() -> None:
    assert describe(b"import { db } from './db';\nexport const x = 1;\n") is None


def test_an_empty_file_has_no_description() -> None:
    assert describe(b"") is None


# --------------------------------------------------------------------------
# What counts as *leading*
# --------------------------------------------------------------------------


def test_leading_blank_lines_do_not_hide_the_header() -> None:
    assert describe(b"\n\n\t/** Spaced out. */\n") == "Spaced out."


def test_a_byte_order_mark_does_not_hide_the_header() -> None:
    """Load-bearing here, unlike in the parser.

    `parser._BOM` is annotated as a known mutation survivor because both
    grammars tolerate a BOM. Here the BOM sits between the start of the file and
    the `/`, so leaving it would silently cost the description of every file a
    Windows editor saved. Deleting the strip fails this test.
    """
    assert describe(b"\xef\xbb\xbf/** Saved on Windows. */\n") == "Saved on Windows."


def test_a_comment_after_a_statement_is_not_a_header() -> None:
    """A header comment is at the top of the file, not merely near it."""
    assert describe(b"'use strict';\n/** Not the header. */\n") is None


def test_a_shebang_hides_the_header_comment() -> None:
    """A documented gap, pinned so it is deliberate rather than incidental.

    `#!/usr/bin/env node` is not one of the three comment forms, so the scan
    stops at `#` and a CLI entry point gets no description even when the line
    below it is a perfectly good JSDoc header. Recorded in
    docs/CURRENT_STATE.md; the fix is to skip one leading `#!` line.
    """
    assert describe(b"#!/usr/bin/env node\n/** The CLI. */\n") is None


def test_only_the_first_comment_form_is_taken() -> None:
    """`/* a */ // b` is one header, not two."""
    assert describe(b"/* First. */ // Second.\n") == "First."


def test_a_blank_line_ends_a_line_comment_run() -> None:
    """Two blocks separated by a blank line are two comments; the header is the
    first one."""
    assert describe(b"// Header.\n\n// A note about the next function.\nconst x = 1;\n") == (
        "Header."
    )


def test_a_statement_ends_a_line_comment_run() -> None:
    assert describe(b"// Header.\nconst x = 1;\n// Trailing note.\n") == "Header."


def test_an_indented_line_comment_run_is_still_a_run() -> None:
    assert describe(b"  // One.\n  // Two.\n") == "One. Two."


def test_a_line_break_separates_two_lines_that_have_no_space_of_their_own() -> None:
    """The reason whitespace is tested *before* printability in the cleaner.

    A line break is not printable, so a cleaner that checked printability first
    would drop it — and every other fixture here hides that, because ``// One.``
    leaves a space after the marker that separates the lines anyway. Without the
    conventional space there is nothing else holding the words apart, and the
    two sentences run together into one word.
    """
    assert describe(b"//One.\n//Two.\n") == "One. Two."


# --------------------------------------------------------------------------
# Marker stripping
# --------------------------------------------------------------------------


def test_a_comment_that_is_only_markers_and_whitespace_yields_none() -> None:
    """`min_length=1` on the wire model, and the same fact as no comment at all."""
    for source in (b"/** */\n", b"/**/\n", b"/*\n *\n *\n */\n", b"//\n//\n", b"/***/\n"):
        assert describe(source) is None, source


def test_only_the_first_star_of_a_continuation_line_is_a_marker() -> None:
    """One marker per line is the convention; the rest is the author's text.

    The second case is the one that discriminates. ``* ** bold **`` cannot tell
    "drop one star" from "drop the leading run of stars", because a space
    follows the first one either way — only a line whose stars are *adjacent*
    does, and stripping the run would silently eat emphasis, bullets, and ASCII
    banners out of a quotation.
    """
    assert describe(b"/**\n * ** bold **\n */\n") == "** bold **"
    assert describe(b"/**\n ** Double.\n */\n") == "* Double."


def test_a_continuation_line_with_no_star_is_kept_whole() -> None:
    source = b"/**\n * Summary.\n   Continued without a star.\n */\n"

    assert describe(source) == "Summary. Continued without a star."


def test_an_unterminated_block_comment_is_still_a_description() -> None:
    """The file ends inside the comment. The text at the top is still the header."""
    assert describe(b"/** Truncated mid-thought") == "Truncated mid-thought"


def test_a_lone_block_close_is_not_a_comment() -> None:
    assert describe(b"*/ const x = 1;\n") is None


def test_the_close_is_searched_for_past_the_opener() -> None:
    """`/*/` is an unterminated opener, not a complete comment.

    The `*` and the `/` in the middle are the second character of `/*` and the
    first of `*/`, and they cannot be both. Searching for the close from index 0
    instead of past the opener finds one at offset 1 and truncates every comment
    of this shape to nothing — which is why the start offset is a mutation this
    test catches rather than a stylistic argument to `find`.
    """
    assert describe(b"/*/ Tricky but real. */\nconst x = 1;\n") == "/ Tricky but real."


# --------------------------------------------------------------------------
# Normalization as a control (docs/SECURITY.md)
# --------------------------------------------------------------------------


def test_whitespace_collapses_to_single_spaces() -> None:
    """A description is a label, not a document."""
    source = b"/**\n *\tOne.\n *\n *\n *     Two.\n */\n"

    result = describe(source)

    assert result == "One. Two."
    assert "\n" not in (result or "")
    assert "\t" not in (result or "")


def test_control_characters_and_a_nul_are_stripped() -> None:
    source = b"/** be\x00fore\x1b[31m after\x07 */\n"

    result = describe(source)

    # The ESC is gone and `[31m` is left behind as ordinary text, which is the
    # point: what made it an escape sequence was the byte that is no longer here.
    assert result == "before[31m after"
    assert not any(char < " " for char in result or "")


def test_a_bidi_override_is_stripped() -> None:
    """U+202E and friends are display-spoofing primitives, not text.

    They reorder how the rest of a line renders without changing what it is,
    which is precisely the attack this text's sink is exposed to. `Cf` is one of
    the categories `str.isprintable()` is False for, so they go with the
    controls rather than needing a rule of their own.
    """
    # Written as escapes, not literals: U+202E and U+200B are invisible, which
    # is the entire premise of the attack and would make this test unreadable in
    # a diff. Same convention `tests/test_archive.py` uses for its homoglyphs.
    source = "/** safe \u202egnorw \u200b*/\n".encode()

    result = describe(source)

    assert result is not None
    assert "\u202e" not in result
    assert "\u200b" not in result
    assert result == "safe gnorw"


def test_a_description_is_truncated_to_the_configured_limit() -> None:
    source = b"/** " + b"a" * (LIMIT * 4) + b" */\n"

    result = describe(source)

    assert result is not None
    assert len(result) == LIMIT


def test_truncation_tracks_settings_rather_than_a_hardcoded_500() -> None:
    """The same discipline `app/models/graph.py` follows, asserted the same way."""
    source = b"/** " + b"a" * 200 + b" */\n"

    assert describe(source, Settings(MAX_DESCRIPTION_CHARS=20)) == "a" * 20
    assert describe(source, Settings(MAX_DESCRIPTION_CHARS=7)) == "a" * 7


def test_truncation_leaves_no_trailing_space() -> None:
    """The cut can land on the space the cleaner emitted before a word it never
    reached."""
    result = describe(b"/** " + b"ab " * 400 + b"*/\n", Settings(MAX_DESCRIPTION_CHARS=9))

    assert result == "ab ab ab"


def test_a_megabyte_comment_costs_nothing_beyond_the_scan_window() -> None:
    """The prefix scan is O(1) in file size — the point of ADR-020's shape."""
    source = b"/** " + b"x" * (2 * 1024 * 1024) + b" */\n"

    result = describe(source)

    assert result == "x" * LIMIT


def test_text_past_the_scan_window_is_not_reached() -> None:
    """The window is a real bound, and this is what it costs.

    A description that begins after `_SCAN_BYTES` of padding is not found. That
    is the trade ADR-020 makes deliberately — the window is what keeps the scan
    O(1) in file size — and it is pinned here because it is otherwise invisible:
    every other test in this file would pass with the slice deleted.
    """
    padded = b"/**" + b"\n *" * _SCAN_BYTES + b"\n * Too far down.\n */"

    assert describe(padded) is None


def test_a_comment_of_only_whitespace_past_the_window_still_terminates() -> None:
    """The one input the early stop cannot bound: no output characters to count.

    `_SCAN_BYTES` is what bounds it instead, which is why the constant is not
    just a generous guess.
    """
    assert describe(b"/**" + b" \n" * (2 * 1024 * 1024) + b"*/") is None


# --------------------------------------------------------------------------
# Totality: nothing here raises
# --------------------------------------------------------------------------


def test_undecodable_bytes_survive_as_replacement_characters() -> None:
    """The parser survives a non-UTF-8 file; so must this.

    Deliberately `errors="replace"` where `parser._specifier` is strict. A
    specifier must match an archive path byte-for-byte, so U+FFFD there invents
    an edge that can never resolve. A description is displayed, not compared.
    """
    source = "/** café ".encode("latin-1") + b"*/\n"

    result = describe(source)

    assert result is not None
    assert result.startswith("caf")
    assert "�" in result


def test_a_window_cut_mid_character_does_not_discard_the_description() -> None:
    """The case strict decoding gets actively wrong.

    The window is a fixed byte count and can land inside a multi-byte character
    in a *valid* UTF-8 file. Strict decoding would then return `None` for a
    file whose header is perfectly good, for a reason that has nothing to do
    with the file.
    """
    # `/** x` is five bytes and `é` is two, so the window's last byte lands on
    # the first half of a character rather than between two of them.
    source = b"/** x" + "é".encode() * _SCAN_BYTES + b" */"
    assert len(b"/** x") % 2 == 1

    result = describe(source)

    assert result is not None
    assert len(result) == LIMIT


def test_a_binary_file_has_no_description() -> None:
    assert describe(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8) is None


def test_a_binary_file_that_happens_to_start_with_a_marker_is_still_bounded() -> None:
    """No binary sniff here, because "must start with a comment" already does the
    work — and on the rare blob that does, the strip and the cap do the rest."""
    result = describe(b"/*" + bytes(range(1, 32)) * 200 + b"*/")

    assert result is None


@pytest.mark.parametrize(
    "source",
    [
        b"",
        b"/",
        b"/*",
        b"//",
        b"*",
        b"\x00" * 64,
        b"\xff\xfe\xfd",
        b"\xef\xbb\xbf",
        b"/**",
        b"//\r\n",
        "\u2028".encode(),
    ],
    ids=repr,
)
def test_nothing_raises_on_a_degenerate_file(source: bytes) -> None:
    header_description(source, SETTINGS)


def test_a_line_terminator_inside_a_line_comment_ends_it() -> None:
    """U+2028 is a JS line terminator, so it closes a `//` comment.

    This is why the window is decoded before it is scanned: `bytes.splitlines`
    does not know about U+2028, so a byte-level scan would run the `//` run
    straight through it and quote the code on the other side.
    """
    source = "// Header.\u2028const SECRET = 1;\n".encode()

    assert describe(source) == "Header."


# --------------------------------------------------------------------------
# `normalize_comment` — the half the route-summary caller will share
# --------------------------------------------------------------------------


def test_normalize_comment_accepts_a_comment_node_verbatim() -> None:
    """Shaped for what tree-sitter hands back: `node.text`, markers included."""
    assert normalize_comment(b"/** Get a user by id. */", SETTINGS) == "Get a user by id."
    assert normalize_comment(b"// Get a user by id.", SETTINGS) == "Get a user by id."


def test_normalize_comment_passes_none_through() -> None:
    """So a caller that found no comment needs no branch."""
    assert normalize_comment(None, SETTINGS) is None


def test_normalize_comment_refuses_text_that_is_not_a_comment() -> None:
    """A refusal, not a pass-through — see the module docstring.

    Route detection will locate the node it hands over. If it ever locates the
    wrong one, the failure should be an absent summary, not a line of source
    code in a response body.
    """
    assert normalize_comment(b"const SECRET = 'sk-live-abc';", SETTINGS) is None
    assert normalize_comment(b"", SETTINGS) is None
    assert normalize_comment(b"   ", SETTINGS) is None


def test_normalize_comment_survives_undecodable_bytes_too() -> None:
    """The shared half needs its own coverage, not the header path's.

    Route detection will hand this function `node.text` straight off a
    tree-sitter node, and tree-sitter is perfectly happy to parse a file that is
    not UTF-8 — so this is the caller most likely to meet undecodable bytes, and
    it must not be the one that raises. Asserted here rather than inferred from
    `header_description`, which reaches the same decode by a different route.
    """
    assert normalize_comment("/** café */".encode("latin-1"), SETTINGS) == "caf�"


def test_normalize_comment_applies_the_same_bound_as_the_header_path() -> None:
    assert len(normalize_comment(b"// " + b"a" * (LIMIT * 3), SETTINGS) or "") == LIMIT
