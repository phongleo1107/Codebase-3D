"""The cooperative deadline the analysis is checked against."""

import time

import pytest

from app.analysis.deadline import Deadline
from app.config import Settings
from app.errors import AnalysisTimeoutError, ErrorCode


def test_a_fresh_deadline_has_time_left() -> None:
    deadline = Deadline.after(60)
    assert not deadline.expired()
    assert 59 < deadline.remaining() <= 60
    deadline.check()


def test_an_elapsed_deadline_is_expired() -> None:
    deadline = Deadline.after(-1)
    assert deadline.expired()
    assert deadline.remaining() < 0
    with pytest.raises(AnalysisTimeoutError) as caught:
        deadline.check()
    assert caught.value.code is ErrorCode.ANALYSIS_TIMEOUT


def test_zero_budget_is_immediately_expired() -> None:
    # `remaining() <= 0`, not `< 0`: a deadline of exactly now has no time in
    # which to do work.
    assert Deadline(expires_at=time.monotonic()).expired()


def test_deadline_uses_the_monotonic_clock() -> None:
    # Not time.time(): an NTP step or a manual clock change mid-analysis must
    # not extend or truncate the budget.
    before = time.monotonic()
    deadline = Deadline.after(10)
    assert before + 10 <= deadline.expires_at <= time.monotonic() + 10


def test_from_settings_uses_the_configured_timeout() -> None:
    settings = Settings(ANALYSIS_TIMEOUT_S=5)
    remaining = Deadline.from_settings(settings).remaining()
    assert 4 < remaining <= 5


def test_deadline_is_frozen() -> None:
    # A step must not be able to extend its own budget.
    deadline = Deadline.after(1)
    with pytest.raises(AttributeError):
        deadline.expires_at = time.monotonic() + 1000  # type: ignore[misc]
