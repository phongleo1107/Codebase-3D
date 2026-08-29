"""JSON-line logging with credential redaction.

Security invariants (docs/SECURITY.md "Secret exposure"):

- GitHub tokens (``ghp_``, ``ghs_``, ``gho_``, ``ghu_``, ``ghr_``,
  ``github_pat_…``) and ``Authorization`` header values never reach a log sink.
- Every record is exactly one line of JSON, so log processors never see a
  partial or multi-line record.

:class:`RedactingFilter` is the primary control and does all the work,
including rendering the traceback itself so that the cached ``exc_text`` any
downstream formatter reuses is already clean. That ordering matters: filters
run *before* formatters, so a filter that only scrubbed an existing
``exc_text`` would never fire. :class:`JsonFormatter` scrubs again, which
covers a handler that was given the formatter but not the filter.

Redaction is a backstop. The primary rule stands: never log source code,
import specifiers, or tokens in the first place.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

REDACTED = "[REDACTED]"

_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # All GitHub token families, not just classic PATs: `ghs_` is what GitHub
    # Actions puts in $GITHUB_TOKEN, so it is the likeliest value an operator
    # pastes into our own GITHUB_TOKEN. Open-ended length so a future longer
    # format cannot leave its tail behind.
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,255}"),
    re.compile(r"github_pat_\w+"),
)
# Matches every shape a header realistically gets logged in: `Authorization: x`,
# `authorization=x`, `{'Authorization': 'x'}`, and the sequence form
# `[('authorization', 'Bearer x')]` that `httpx.Headers.items()` produces —
# httpx masks the value in its own repr, so calling .items()/.raw is the
# natural debugging move and must not defeat us. The value is scrubbed to end
# of line: a Bearer value contains a space, so anything narrower than
# [^\r\n]+ would leave the token half behind.
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)([\"']?authorization[\"']?\s*(?:[:=]|,)\s*)[^\r\n]+"
)


def redact(text: str) -> str:
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _AUTHORIZATION_PATTERN.sub(rf"\g<1>{REDACTED}", text)


def _safe_message(record: logging.LogRecord) -> str:
    """``record.getMessage()`` that cannot raise.

    ``Handler.filter()`` and ``Formatter.format()`` are not wrapped by
    logging's ``handleError``, so an unguarded ``%``-interpolation failure
    would propagate out of ``logger.warning(...)`` into application code —
    typically inside the very ``except`` block that was reporting something
    else. The fallback never interpolates ``record.args``, because stdlib's
    own ``handleError`` prints them unredacted.
    """
    try:
        return record.getMessage()
    except Exception:
        return f"{record.msg!r} (unformattable log arguments suppressed)"


class RedactingFilter(logging.Filter):
    """Scrubs credentials from a record before any handler formats it.

    The record's lazy ``%``-args are collapsed first so tokens hiding inside
    ``args`` (e.g. a logged headers dict) are caught too.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(_safe_message(record))
        record.args = None
        # Render the traceback here rather than leaving it to the formatter:
        # formatters cache into exc_text, and every downstream formatter reuses
        # that cache, so redacting it now covers plain non-JSON handlers too.
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(_safe_message(record)),
        }
        request_id = getattr(record, "request_id", None)
        if isinstance(request_id, str):
            payload["requestId"] = request_id
        if record.exc_text:
            payload["exception"] = redact(record.exc_text)
        elif record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact(record.stack_info)
        return json.dumps(payload, ensure_ascii=True)


def configure_logging(level: int = logging.INFO) -> None:
    """Route the root logger to stdout as redacted JSON lines. Idempotent —
    existing root handlers are replaced, so a reload cannot double-log."""
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
