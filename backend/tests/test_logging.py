"""Logging: JSON-line output and credential redaction (docs/SECURITY.md)."""

import json
import logging
from io import StringIO
from typing import Any

import pytest

from app.logging_setup import (
    REDACTED,
    JsonFormatter,
    RedactingFilter,
    configure_logging,
    redact,
)

FAKE_GHP = "ghp_" + "A1b2" * 9  # 36 chars after the prefix, like the real thing
FAKE_PAT = "github_pat_11ABCDE0_" + "x" * 20


def make_logger(stream: StringIO) -> logging.Logger:
    logger = logging.Logger("test-logger")  # unattached: no root propagation
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


def log_and_capture(message: str, *args: object) -> tuple[str, dict[str, Any]]:
    stream = StringIO()
    make_logger(stream).warning(message, *args)
    raw = stream.getvalue()
    parsed: dict[str, Any] = json.loads(raw)
    return raw, parsed


def test_ghp_token_mid_string_is_scrubbed() -> None:
    raw, parsed = log_and_capture(
        f"GitHub preflight failed for request using {FAKE_GHP} after 2 retries"
    )
    assert FAKE_GHP not in raw
    assert REDACTED in parsed["message"]
    assert parsed["message"].startswith("GitHub preflight failed")
    assert parsed["message"].endswith("after 2 retries")


def test_fine_grained_pat_is_scrubbed() -> None:
    raw, parsed = log_and_capture(f"upstream said: bad credentials ({FAKE_PAT})")
    assert FAKE_PAT not in raw
    assert REDACTED in parsed["message"]


@pytest.mark.parametrize("prefix", ["ghp_", "ghs_", "gho_", "ghu_", "ghr_"])
def test_every_github_token_family_is_scrubbed(prefix: str) -> None:
    """`ghs_` is what GitHub Actions puts in $GITHUB_TOKEN, so it is the
    likeliest value an operator pastes into our own GITHUB_TOKEN."""
    token = prefix + "A1b2" * 9
    raw, _ = log_and_capture(f"preflight 401 using {token} upstream")
    assert token not in raw
    assert REDACTED in raw


def test_longer_than_expected_token_leaves_no_tail() -> None:
    token = "ghp_" + "A" * 60
    raw, _ = log_and_capture(f"t={token} end")
    assert "AAAA" not in raw


@pytest.mark.parametrize(
    "line",
    [
        "request failed, Authorization: Bearer abc.def-123 rejected",
        "headers={'Authorization': 'Bearer abc.def-123'}",
        "headers=[('authorization', 'Bearer abc.def-123')]",
        "raw=[(b'authorization', b'Bearer abc.def-123')]",
        "sent authorization=Bearer abc.def-123",
    ],
)
def test_authorization_value_is_scrubbed_in_every_logged_shape(line: str) -> None:
    """httpx masks the value in `repr(Headers)`, so reaching for `.items()`
    or `.raw` is the natural debugging move — those forms use `,` as the
    separator, not `:`, and must not defeat the pattern."""
    raw, _ = log_and_capture(line)
    assert "abc.def-123" not in raw
    assert "Bearer" not in raw  # the whole value goes, not just the token half
    assert REDACTED in raw


def test_token_hiding_inside_lazy_args_is_scrubbed() -> None:
    raw, _ = log_and_capture("request headers: %s", {"Authorization": f"token {FAKE_GHP}"})
    assert FAKE_GHP not in raw
    assert REDACTED in raw


def test_exception_text_is_scrubbed() -> None:
    stream = StringIO()
    logger = make_logger(stream)
    try:
        raise RuntimeError(f"401 for {FAKE_GHP}")
    except RuntimeError:
        logger.exception("preflight blew up")
    raw = stream.getvalue()
    parsed = json.loads(raw)
    assert FAKE_GHP not in raw
    assert REDACTED in parsed["exception"]
    assert "RuntimeError" in parsed["exception"]


def test_traceback_is_scrubbed_for_a_plain_non_json_formatter() -> None:
    """The filter must redact tracebacks by itself. Formatters run *after*
    filters and cache into `exc_text`, so a filter that only scrubbed an
    existing `exc_text` would never fire on a plain handler."""
    stream = StringIO()
    logger = logging.Logger("plain-formatter")
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        raise RuntimeError(f"401 for {FAKE_GHP}")
    except RuntimeError:
        logger.exception("preflight blew up")
    raw = stream.getvalue()
    assert FAKE_GHP not in raw
    assert REDACTED in raw
    assert "RuntimeError" in raw


def test_stack_info_is_scrubbed_and_retained() -> None:
    stream = StringIO()
    make_logger(stream).warning(f"ctx for {FAKE_GHP}", stack_info=True)
    raw = stream.getvalue()
    parsed = json.loads(raw)
    assert FAKE_GHP not in raw
    assert "Stack (most recent call last)" in parsed["stack"]


def test_bad_format_string_does_not_raise_into_the_caller() -> None:
    """`Handler.filter` and `Formatter.format` are not wrapped by logging's
    `handleError`, so an unguarded getMessage() would turn a logging typo into
    an application exception — inside the `except` block doing the reporting."""
    stream = StringIO()
    logger = make_logger(stream)
    logger.warning("cache hit rate 95%% is fine but 95% for %s", FAKE_GHP)
    raw = stream.getvalue()
    assert raw  # the record still reached the sink
    assert FAKE_GHP not in raw  # and the args were not leaked in the fallback


def test_output_is_one_json_line_with_expected_fields() -> None:
    raw, parsed = log_and_capture("analysis finished")
    assert raw.endswith("\n")
    assert raw.count("\n") == 1
    assert parsed["message"] == "analysis finished"
    assert parsed["level"] == "WARNING"
    assert parsed["logger"] == "test-logger"
    assert parsed["ts"].endswith("+00:00")


def test_request_id_is_included_when_present() -> None:
    stream = StringIO()
    make_logger(stream).warning("done", extra={"request_id": "req-42"})
    parsed = json.loads(stream.getvalue())
    assert parsed["requestId"] == "req-42"


def test_multiline_message_stays_one_line() -> None:
    raw, parsed = log_and_capture("first\nsecond")
    assert raw.count("\n") == 1
    assert parsed["message"] == "first\nsecond"


def test_clean_text_passes_through_unchanged() -> None:
    text = "parsed 42 files in src with 7 imports"
    assert redact(text) == text


def test_filter_never_drops_records() -> None:
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=1,
        msg=f"using {FAKE_GHP}", args=None, exc_info=None,
    )
    assert RedactingFilter().filter(record) is True
    assert FAKE_GHP not in record.getMessage()


def test_configure_logging_is_idempotent() -> None:
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        configure_logging()
        configure_logging()
        assert len(root.handlers) == 1
        handler = root.handlers[0]
        assert isinstance(handler.formatter, JsonFormatter)
        assert any(isinstance(f, RedactingFilter) for f in handler.filters)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
