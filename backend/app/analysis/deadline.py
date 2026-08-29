"""Cooperative deadline for a single analysis.

`asyncio.wait_for` cannot kill a worker thread, and the analysis is CPU-bound
and synchronous, so a timeout has to be *checked* rather than imposed
(docs/SECURITY.md, "Slow-loris or endless analysis"). This is the thing that
gets checked: between archive members, between parsed files, and inside the
tree-sitter progress callback.

Time is read from `time.monotonic`, never the wall clock — a clock adjustment
mid-analysis must not extend or truncate the budget.
"""

import time
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.errors import AnalysisTimeoutError


@dataclass(frozen=True, slots=True)
class Deadline:
    """A monotonic instant past which work must stop.

    Frozen so a long-running step cannot extend its own budget; construct a new
    one if a genuinely separate phase needs a fresh clock.
    """

    expires_at: float

    @classmethod
    def after(cls, seconds: float) -> Deadline:
        return cls(time.monotonic() + seconds)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Deadline:
        """The real analysis budget, `ANALYSIS_TIMEOUT_S` from now."""
        return cls.after((settings or get_settings()).ANALYSIS_TIMEOUT_S)

    def remaining(self) -> float:
        """Seconds left; negative once the deadline has passed."""
        return self.expires_at - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def check(self) -> None:
        """Raise :class:`~app.errors.AnalysisTimeoutError` if the budget is spent."""
        if self.expired():
            raise AnalysisTimeoutError()
