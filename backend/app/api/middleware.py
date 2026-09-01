"""Two pure-ASGI wrappers around the HTTP boundary.

Both are written against the raw ASGI interface rather than Starlette's
``BaseHTTPMiddleware``. That is not stylistic: `BodySizeLimitMiddleware` has to
sit *between* the server and whatever reads the body, and `BaseHTTPMiddleware`
buffers the request into an anyio stream before the endpoint sees it — which is
the very thing the cap exists to prevent.

**The body cap is enforced before the application runs, not while it reads.**
Starlette does not bound a request body at all, so without this an unbounded
POST is read into memory before any model rejects it (docs/SECURITY.md,
"Oversized request body"). Both halves the threat model asks for are here: the
declared ``Content-Length``, refused before a byte is read, and a running count
of the bytes actually delivered, since a chunked body declares no length and a
declared one is not a promise. An oversized body therefore never reaches
application code at all.

**Why this middleware writes its own response instead of raising.** The tidier
design — raise `PayloadTooLargeError` from inside ``receive()`` and let
`app/api/app.py`'s one `AppError` handler shape it — does not survive contact
with FastAPI. `fastapi.routing.get_request_handler` wraps the body read in
``except Exception: raise HTTPException(400, "There was an error parsing the
body")``, so a typed error raised from ``receive()`` is swallowed and the client
is told its JSON was malformed with a **400**, not that its body was too large
with a 413. Verified against fastapi 0.141.1. The response is still built from
`PayloadTooLargeError`, so the body *shape* has exactly one source; only the
sending is local.

The buffered body is bounded by the cap plus one chunk, and only requests that
declare a body are drained — a GET is passed through untouched, so a health
check never waits on a body message.
"""

import json
import logging
from collections import deque
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import PayloadTooLargeError

logger = logging.getLogger(__name__)

# Echoed on every response and carried in every error body, so an operator can
# join a user's screenshot to a log line without either one carrying a URL.
REQUEST_ID_HEADER = "x-request-id"


def request_id_of(scope: Scope) -> str:
    """The id `RequestIdMiddleware` put on the scope, or a fresh one.

    Total by design. An error body's ``requestId`` is not worth an
    `AttributeError` inside an exception handler, and a handler that ran with
    the middleware somehow absent should still produce a well-formed body.
    """
    state = scope.get("state")
    if isinstance(state, dict):
        existing = state.get("request_id")
        if isinstance(existing, str):
            return existing
    return uuid4().hex


class RequestIdMiddleware:
    """Assign one id per request; put it on the scope and on the response.

    Generated here and never taken from an inbound header: a client-supplied id
    is client-controlled text that would end up in our log lines.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_id)


class BodySizeLimitMiddleware:
    """Refuse a request body larger than ``max_bytes`` (`MAX_REQUEST_BODY_BYTES`)."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _declares_a_body(scope):
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            # Refused before a single byte is read, rather than after
            # `max_bytes` of it are.
            await self._refuse(scope, send, "declared length over the cap")
            return

        buffered: list[Message] = []
        seen = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                # A disconnect. Hand it on and let the application decide.
                break
            # Counted independently of the header, because a chunked body
            # declares no length and a declared one is not a promise.
            seen += len(message.get("body", b""))
            if seen > self.max_bytes:
                await self._refuse(scope, send, "delivered bytes over the cap")
                return
            if not message.get("more_body", False):
                break

        await self.app(scope, _Replay(buffered, receive), send)

    async def _refuse(self, scope: Scope, send: Send, reason: str) -> None:
        """Send the one error body this API returns, built from `errors.py`."""
        logger.info("request body refused: %s", reason)
        error = PayloadTooLargeError()
        payload = json.dumps(error.body(request_id_of(scope))).encode()
        await send(
            {
                "type": "http.response.start",
                "status": error.status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def _declares_a_body(scope: Scope) -> bool:
    """True when the request says it is sending one.

    A request with neither header carries no body under any HTTP version, so
    draining it would be waiting for a message that is only coming because the
    server synthesizes an empty one. Skipping it keeps a GET — `/api/health`,
    every preflight — untouched by this middleware.
    """
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            return True
        if name == b"transfer-encoding" and b"chunked" in value.lower():
            return True
    return False


def _declared_length(scope: Scope) -> int | None:
    """``Content-Length`` as an int, or ``None`` if absent or unreadable.

    ``None`` is not a bypass: it means the byte counter is the only check, which
    is exactly the chunked case. A header that cannot be parsed is treated the
    same way rather than rejected here — the server's HTTP parser owns malformed
    framing, and this module owns size.
    """
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class _Replay:
    """Hand the application the messages already read, then the real stream."""

    def __init__(self, buffered: list[Message], receive: Receive) -> None:
        self._pending = deque(buffered)
        self._receive = receive

    async def __call__(self) -> Message:
        if self._pending:
            return self._pending.popleft()
        return await self._receive()
