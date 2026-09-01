"""Per-IP sliding-window rate limiting and a global concurrency gate (ADR-008).

Hand-rolled rather than `slowapi`: the only backend that matters here is
in-process memory, since there is no Redis and never will be (no datastore is
the whole point of ADR-001). Both structures are held on `app.state`, one pair
per `FastAPI` instance, so a fresh app in a test gets a fresh clock rather than
inheriting hits recorded by a previous test.
"""

import time
from collections import defaultdict, deque
from collections.abc import Sequence


class SlidingWindowLimiter:
    """Per-key sliding window(s) over request timestamps, held in memory.

    A key (client IP) is checked against every `(max_requests, window_seconds)`
    pair passed to `retry_after` for that call, so one instance serves both the
    per-minute and per-hour windows on `RATE_LIMIT_ANALYZE` /
    `RATE_LIMIT_ANALYZE_HOURLY` at once.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def retry_after(
        self, key: str, windows: Sequence[tuple[int, int]], *, now: float | None = None
    ) -> float | None:
        """Seconds until `key`'s next request would be allowed, or `None`.

        A request that would be refused is not recorded — it must not count
        against the very window it just failed.
        """
        moment = time.monotonic() if now is None else now
        hits = self._hits[key]
        widest = max((window for _, window in windows), default=0)
        while hits and hits[0] <= moment - widest:
            hits.popleft()

        wait = 0.0
        for limit, window in windows:
            recent = [hit for hit in hits if hit > moment - window]
            if len(recent) >= limit:
                wait = max(wait, recent[0] + window - moment)

        if wait > 0:
            return wait
        hits.append(moment)
        return None


class ConcurrencyGate:
    """A non-blocking cap on the number of analyses running at once.

    Not `asyncio.Semaphore`: that queues a waiter until a slot frees, and the
    point is to shed load immediately with a 503 rather than make a client
    wait behind others (docs/SECURITY.md, "Request flooding").
    """

    def __init__(self) -> None:
        self._current = 0

    def try_acquire(self, limit: int) -> bool:
        if self._current >= limit:
            return False
        self._current += 1
        return True

    def release(self) -> None:
        self._current -= 1
