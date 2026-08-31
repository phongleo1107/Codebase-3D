"""Description extraction — a file's own leading header comment, quoted.

`GraphNode.description` is a **quotation from the repository, never a
generation** (ADR-013). This module produces it: given the bytes of one source
file, it returns that file's leading header comment — a JSDoc ``/** … */``, a
plain ``/* … */`` block, or an unbroken run of ``//`` lines — normalized and
bounded, or ``None`` when there is none, which is the ordinary case.

It is also the module that puts **repository-authored text into a response body
for the first time**. Everything the API carried before this was structure
*about* a repository: paths, counts, line numbers. A comment is attacker-
controlled, so every bound and every strip below happens here, at extraction,
rather than at serialization (docs/SECURITY.md, "Repository-authored text in
responses"). What leaves this module is already short, already single-line, and
already free of anything that is not a printable character.

## Why there is no tree

docs/ARCHITECTURE.md planned this as a second pass over the tree
`extract_imports` already builds. It is a byte-prefix scan instead, and the
reason is not cost — it is that **at byte 0 there is no lexical context to get
wrong** (ADR-020). Everything that makes JS tokenization ambiguous — is this
``/`` a division or a regex, is this ``//`` inside a string or a template — is
a question about what preceded it, and nothing precedes the first byte of a
file. A scanner anchored at position 0 is exactly as correct as a parser there,
so a tree buys nothing and costs a second parse of every file.

That argument is deliberately narrow, and it is the whole reason this module
splits into a *locator* and a *normalizer*. The second consumer ADR-013
promises — ``ServiceEndpoint.summary``, the comment above a route handler — is
**not** at byte 0, and for it the argument reverses: a comment deep in a file
can only be located unambiguously from the tree. So route detection, when it
exists, will find that comment as a sibling node and hand its text to
`normalize_comment`, which is the half that is genuinely shared. Locating is
per-caller; normalizing is not. `header_description` is simply the locator for
the byte-0 case.

## The normalization, and what each step is for

Applied in this order, to the whole comment including its markers:

1. **Markers stripped.** ``/*``/``*/``, the JSDoc ``*`` that opens each
   continuation line, and the ``//`` of each line in a run.
2. **Whitespace collapsed** to single spaces, so a comment is a label rather
   than a document. A thousand-line banner comment must not become a
   thousand-line tooltip.
3. **Non-printable characters dropped.** ``str.isprintable()`` is False for
   exactly the categories that have no business in a label: C0 and C1 controls
   (a NUL, an ANSI escape introducer), surrogates, unassigned code points, and
   — the one worth naming — ``Cf`` format characters, which include U+202E
   RIGHT-TO-LEFT OVERRIDE and its family. Those are the Trojan Source
   characters: they reorder how the *rest* of a line displays without changing
   what it is, which is a display-spoofing primitive aimed at exactly the kind
   of surface this text lands on.
4. **Truncated to ``MAX_DESCRIPTION_CHARS``**, counted in output characters and
   enforced *while* cleaning rather than after, so the loop is bounded by the
   limit rather than by the size of the comment.
5. **Empty becomes ``None``.** ``/** */`` and no comment at all are the same
   fact, and `app/models/graph.py` requires ``min_length=1`` anyway.

No ellipsis is appended at the cut. A description is a quotation, and three
characters we authored inside one would be the only text in it that the
repository did not write.

## Two behaviours that look like bugs and are not

**Undecodable bytes are replaced, not refused.** ``errors="replace"``, where
`parser._specifier` uses strict decoding — and the difference is deliberate. A
specifier must match an archive path byte-for-byte, so a U+FFFD there
manufactures an edge that can never resolve; a description is displayed, not
compared, so U+FFFD is an honest "this byte was not text" and costs nothing.
There is also a case strict decoding gets actively wrong: the scan window below
is a fixed byte count and can land in the middle of a multi-byte character in a
perfectly valid UTF-8 file, and strict decoding would then discard the whole
description of that file because of where our window happened to fall.

**`normalize_comment` refuses input that is not comment syntax**, returning
``None`` rather than passing the text through. It is not a general-purpose text
sanitizer, and the future route-summary caller will be handing it a node it
located from a tree; if that node is ever the wrong one, the failure should be
an absent summary and not a line of source code in a response body.
"""

from typing import Final

from app.config import Settings, get_settings

# How much of a file's head is examined. A leading header comment is by
# definition at byte 0, so this is a prefix scan and therefore O(1) in file
# size — `parser._BINARY_SNIFF_BYTES` is the same shape of constant, chosen for
# the same reason, and like it this is a module constant rather than a
# `Settings` field because it is an internal scanning detail and not an
# operational limit anyone would tune. `MAX_DESCRIPTION_CHARS` is the bound
# docs/SECURITY.md names, and it is the one in `Settings`.
#
# 4 KiB is eight times that limit in bytes: it holds 500 four-byte characters
# with room left for their JSDoc markup, so it cannot cut a description short
# of the cap in any encoding. It also bounds the cleaning loop for the one
# input that defeats the early stop — a comment made entirely of whitespace and
# control characters, which produces no output characters to count.
_SCAN_BYTES: Final = 4 * 1024

# Stripped for the same reason `parser._BOM` is: so that position 0 means the
# same thing here as it does there. Unlike there it is load-bearing — a BOM
# sits between the start of the file and the `/`, so leaving it would hide the
# header comment of every file a Windows editor saved.
_BOM: Final = b"\xef\xbb\xbf"

_BLOCK_OPEN: Final = "/*"
_BLOCK_CLOSE: Final = "*/"
_LINE_OPEN: Final = "//"


def header_description(source: bytes, settings: Settings | None = None) -> str | None:
    """The file's leading header comment, normalized and bounded, or ``None``.

    ``source`` is the raw file bytes, exactly as `fetch/archive.py` yielded
    them. Nothing is decoded beyond the scanned prefix, and nothing is parsed.

    Total: there is no input for which this raises. A binary file, an
    undecodable file, and a file whose first byte is not ``/`` all return
    ``None`` by the same path — the leading-comment test simply fails.
    """
    window = source[:_SCAN_BYTES].removeprefix(_BOM)
    # Decoded once, and only the window: the scan needs `str.splitlines`, which
    # unlike its `bytes` counterpart treats U+2028 and U+2029 as line breaks —
    # and those are line terminators in JS, so they end a `//` comment. Scanning
    # the bytes would run a `//` run straight through one and quote the code on
    # the other side of it.
    return _normalize(_leading_comment(window.decode("utf-8", errors="replace")), settings)


def normalize_comment(raw: bytes | None, settings: Settings | None = None) -> str | None:
    """Normalize one comment's own text — markers included — into a description.

    The half of this module that is shared. `header_description` locates a
    comment by scanning from byte 0; route detection will locate one from the
    tree, as the sibling before a handler, and both then arrive here with the
    same thing: the comment exactly as it appears in the file, ``/**`` and all.

    ``None`` in, ``None`` out, so a caller that found no comment does not need a
    branch. ``None`` also comes back for text that is not a comment at all; see
    the module docstring on why that is a refusal rather than a pass-through.
    """
    if raw is None:
        return None
    return _normalize(raw.decode("utf-8", errors="replace"), settings)


def _normalize(comment: str | None, settings: Settings | None) -> str | None:
    """Markers off, cleaned, bounded, empty-to-``None``. The whole pipeline."""
    if comment is None:
        return None
    body = _strip_markers(comment)
    if body is None:
        return None
    limit = (settings if settings is not None else get_settings()).MAX_DESCRIPTION_CHARS
    # `.rstrip()` because the cleaner stops the moment it has `limit`
    # characters, and the character that took it there may be the space it
    # emitted before a word it never reached.
    return _clean(body, limit).rstrip() or None


def _leading_comment(window: str) -> str | None:
    """The comment at the head of ``window``, markers included, or ``None``.

    Leading whitespace is skipped; anything else that is not a comment opener
    ends the search. Only the *first* form encountered is taken, so
    ``/* a */ // b`` is one comment and not two — a header comment is the thing
    at the top of the file, not everything above the first declaration.
    """
    text = window.lstrip()

    if text.startswith(_BLOCK_OPEN):
        end = text.find(_BLOCK_CLOSE, len(_BLOCK_OPEN))
        # A block comment left unterminated inside the window is taken to the
        # window's end rather than discarded. It is unterminated because the
        # window cut it, or because the file really does end inside it; either
        # way the text at the top of the file is still the file's header, and
        # the cap is what makes taking "the rest" safe.
        return text if end == -1 else text[: end + len(_BLOCK_CLOSE)]

    if text.startswith(_LINE_OPEN):
        return "\n".join(_line_run(text))

    return None


def _line_run(text: str) -> list[str]:
    """The unbroken run of ``//`` lines at the head of ``text``.

    Unbroken is the whole rule: the first line that is not a ``//`` line ends
    the run, and a blank line is not a ``//`` line. Two comment blocks separated
    by an empty line are two comments, and only the first is the header.
    """
    run: list[str] = []
    for line in text.splitlines():
        if not line.lstrip().startswith(_LINE_OPEN):
            break
        run.append(line)
    return run


def _strip_markers(comment: str) -> str | None:
    """Comment syntax off, comment text left. ``None`` if this is not a comment.

    Line structure is preserved rather than joined here, because `_clean` turns
    a line break into the space that keeps two lines from running together and
    it needs one to still be there.
    """
    text = comment.strip()

    if text.startswith(_BLOCK_OPEN):
        body = text[len(_BLOCK_OPEN) :]
        if body.endswith(_BLOCK_CLOSE):
            body = body[: -len(_BLOCK_CLOSE)]
        # This also strips the second `*` of a `/**` opener, which is why JSDoc
        # needs no separate case: after `/*` is removed the opener is just
        # another line beginning with `*`.
        return "\n".join(_drop_leading_star(line) for line in body.splitlines())

    if text.startswith(_LINE_OPEN):
        return "\n".join(line.lstrip()[len(_LINE_OPEN) :] for line in _line_run(text))

    return None


def _drop_leading_star(line: str) -> str:
    """The JSDoc continuation marker, if this line has one.

    Only the first ``*`` and only when it opens the line. A line of ``***``
    keeps two, which is right — the convention is one marker per line, and the
    rest is the author's text.
    """
    bare = line.lstrip()
    return bare[1:] if bare.startswith("*") else line


def _clean(text: str, limit: int) -> str:
    """Collapse whitespace, drop non-printables, stop at ``limit`` characters.

    One pass, and it stops as soon as it has enough. That matters more than it
    looks: cleaning removes characters, so there is no raw length that reliably
    yields ``limit`` clean ones, and the alternatives are cleaning the whole
    comment first (unbounded work on a comment we already know we will cut) or
    guessing a multiplier (wrong for some encoding, silently). Counting output
    is exact.
    """
    out: list[str] = []
    pending_space = False

    for char in text:
        if char.isspace():
            # Never leading: `out` being empty means nothing has been kept yet,
            # so there is nothing for this space to separate.
            pending_space = bool(out)
            continue
        # False for C0/C1 controls, surrogates, unassigned code points, and the
        # Cf format characters — see the module docstring on U+202E. Whitespace
        # is also non-printable and is deliberately handled above it, or every
        # line break would vanish and words would run together.
        if not char.isprintable():
            continue
        if pending_space:
            out.append(" ")
            pending_space = False
            if len(out) >= limit:
                break
        out.append(char)
        if len(out) >= limit:
            break

    return "".join(out)
