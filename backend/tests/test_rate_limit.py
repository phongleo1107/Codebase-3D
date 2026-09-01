"""ADR-008: the per-IP sliding-window limiter and the concurrency gate.

Two layers are exercised. `SlidingWindowLimiter` and `ConcurrencyGate` are
tested directly, with `now` passed explicitly so the window arithmetic is not
at the mercy of real wall-clock timing. The rest of this file drives the real
`POST /api/analyze` route the way `tests/test_api_routes.py` does, to pin the
two things that can only be seen at that seam: the 429 carries `Retry-After`
and the contract-shaped body, the limiter is scoped per client IP rather than
shared across every caller, and the concurrency gate refuses a second request
with a 503 *before* a worker thread for it ever starts.

Both checks are reachable with a URL that never gets far enough to open a
socket — the limiter runs before `parse_github_url`, and the concurrency test
replaces `_analyze_blocking` outright — so nothing here needs `respx`.
"""

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api.app import create_app
from app.api.rate_limit import ConcurrencyGate, SlidingWindowLimiter
from app.config import Settings
from app.errors import ErrorCode, NoSupportedFilesError

ANALYZE = "/api/analyze"
SUBMITTED_URL = "https://github.com/acme/widgets"


# --------------------------------------------------------------------------
# SlidingWindowLimiter
# --------------------------------------------------------------------------


def test_allows_up_to_the_limit_then_refuses() -> None:
    limiter = SlidingWindowLimiter()
    windows = [(3, 60)]

    for _ in range(3):
        assert limiter.retry_after("1.2.3.4", windows, now=0.0) is None

    wait = limiter.retry_after("1.2.3.4", windows, now=0.0)
    assert wait == pytest.approx(60.0)


def test_a_refused_request_is_not_recorded() -> None:
    """Otherwise a client stuck at the limit would never recover."""
    limiter = SlidingWindowLimiter()
    windows = [(1, 60)]

    assert limiter.retry_after("k", windows, now=0.0) is None
    assert limiter.retry_after("k", windows, now=1.0) is not None
    assert limiter.retry_after("k", windows, now=1.0) is not None
    assert limiter.retry_after("k", windows, now=61.0) is None


def test_the_window_rolls_forward() -> None:
    limiter = SlidingWindowLimiter()
    windows = [(1, 60)]

    assert limiter.retry_after("k", windows, now=0.0) is None
    assert limiter.retry_after("k", windows, now=30.0) == pytest.approx(30.0)
    assert limiter.retry_after("k", windows, now=61.0) is None


def test_every_window_supplied_is_enforced() -> None:
    """The hourly cap can bind even while the per-minute one has room."""
    limiter = SlidingWindowLimiter()
    windows = [(5, 60), (1, 3600)]

    assert limiter.retry_after("k", windows, now=0.0) is None
    wait = limiter.retry_after("k", windows, now=1.0)
    assert wait == pytest.approx(3599.0)


def test_keys_are_independent() -> None:
    limiter = SlidingWindowLimiter()
    windows = [(1, 60)]

    assert limiter.retry_after("a", windows, now=0.0) is None
    assert limiter.retry_after("b", windows, now=0.0) is None
    assert limiter.retry_after("a", windows, now=0.0) is not None


def test_hits_older_than_the_widest_window_are_pruned() -> None:
    """So a long-lived process does not grow one deque per IP forever."""
    limiter = SlidingWindowLimiter()
    windows = [(1, 60)]

    limiter.retry_after("k", windows, now=0.0)
    limiter.retry_after("k", windows, now=1_000.0)

    assert len(limiter._hits["k"]) == 1


# --------------------------------------------------------------------------
# ConcurrencyGate
# --------------------------------------------------------------------------


def test_gate_refuses_once_the_limit_is_reached() -> None:
    gate = ConcurrencyGate()

    assert gate.try_acquire(2) is True
    assert gate.try_acquire(2) is True
    assert gate.try_acquire(2) is False


def test_gate_frees_a_slot_on_release() -> None:
    gate = ConcurrencyGate()
    assert gate.try_acquire(1) is True
    assert gate.try_acquire(1) is False

    gate.release()

    assert gate.try_acquire(1) is True


# --------------------------------------------------------------------------
# Harness — mirrors tests/test_api_routes.py
# --------------------------------------------------------------------------


@pytest.fixture
def app() -> FastAPI:
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def use_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    monkeypatch.setattr("app.api.routes.get_settings", lambda: Settings(**overrides), raising=True)


def error_of(response: httpx.Response) -> dict[str, str]:
    body = response.json()
    assert set(body) == {"error"}
    detail = body["error"]
    assert set(detail) == {"code", "message", "requestId"}
    return dict(detail)


# --------------------------------------------------------------------------
# The route: rate limiting
# --------------------------------------------------------------------------


async def test_analyze_is_rate_limited_after_the_configured_count(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed URL is enough: the limiter runs before `parse_github_url`."""
    use_settings(monkeypatch, RATE_LIMIT_ANALYZE=(2, 60), RATE_LIMIT_ANALYZE_HOURLY=(1000, 3600))

    for _ in range(2):
        response = await client.post(ANALYZE, json={"repository_url": "not-a-url"})
        assert response.status_code == 400

    refused = await client.post(ANALYZE, json={"repository_url": "not-a-url"})

    assert refused.status_code == 429
    detail = error_of(refused)
    assert detail["code"] == ErrorCode.RATE_LIMITED.value
    assert detail["message"] == "Too many requests. Please try again later."
    retry_after = refused.headers["retry-after"]
    assert retry_after.isdigit() and int(retry_after) > 0


async def test_the_rate_limit_is_scoped_per_client_ip(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch, RATE_LIMIT_ANALYZE=(1, 60), RATE_LIMIT_ANALYZE_HOURLY=(1000, 3600))
    transport_a = httpx.ASGITransport(app=app, client=("1.1.1.1", 1))
    transport_b = httpx.ASGITransport(app=app, client=("2.2.2.2", 1))

    async with (
        httpx.AsyncClient(transport=transport_a, base_url="http://testserver") as a,
        httpx.AsyncClient(transport=transport_b, base_url="http://testserver") as b,
    ):
        first = await a.post(ANALYZE, json={"repository_url": "not-a-url"})
        second = await b.post(ANALYZE, json={"repository_url": "not-a-url"})
        assert first.status_code == 400
        assert second.status_code == 400, "a different IP must not share the window"

        third = await a.post(ANALYZE, json={"repository_url": "not-a-url"})
        assert third.status_code == 429


# --------------------------------------------------------------------------
# The route: the concurrency gate
# --------------------------------------------------------------------------


async def test_the_concurrency_gate_refuses_before_a_worker_thread_starts(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second of two concurrent analyses gets a 503, never a worker thread.

    `_analyze_blocking` is replaced with something that signals it started and
    then blocks, so the test can hold one "analysis" open while a second
    request is made — proving the gate is checked, and refuses, while the
    first is still running.
    """
    use_settings(monkeypatch, MAX_CONCURRENT_ANALYSES=1)
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def blocking(repo: object, settings: object) -> None:
        calls.append(1)
        started.set()
        assert release.wait(timeout=5), "the test never released the first analysis"
        raise NoSupportedFilesError()

    monkeypatch.setattr("app.api.routes._analyze_blocking", blocking)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = asyncio.create_task(client.post(ANALYZE, json={"repository_url": SUBMITTED_URL}))
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set(), "the first request never reached the worker"

        second = await client.post(ANALYZE, json={"repository_url": SUBMITTED_URL})
        assert second.status_code == 503
        detail = error_of(second)
        assert detail["code"] == ErrorCode.SERVER_BUSY.value

        release.set()
        first_response = await first
        assert first_response.status_code == 422, "the first analysis still completed"
        assert calls == [1], "the refused request must never call the worker"
