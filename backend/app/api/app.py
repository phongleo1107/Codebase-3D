"""The FastAPI application, and the four handlers that own every error body.

`app/errors.py` froze the contract — ``{"error": {"code", "message",
"requestId"}}``, exactly those three keys, with static messages that a call site
structurally cannot add detail to. Until now nothing returned it. These handlers
are what make it real, and between them they cover **every** way a response can
leave this application:

``AppError``                 the typed errors every module below raises.
``RequestValidationError``   a malformed request body.
``HTTPException``            Starlette's own 404 / 405 / 415, which would
                             otherwise answer ``{"detail": …}`` and break the
                             fixed shape on the most reachable paths there are.
``Exception``                anything unforeseen, as a bare 500.

**`RequestValidationError` is mapped to a bare `INVALID_REQUEST`, and the
`detail` is discarded.** This is the security control in this module, not a
convenience. Pydantic's validation detail embeds the offending *input* —
``{"loc": ["body", "repository_url"], "input": "https://…"}`` — so returning it
echoes whatever the client sent, which is precisely what docs/SECURITY.md's
"Pydantic validation echoing user input" row forbids. FastAPI's default handler
returns exactly that, so the override is required rather than cosmetic. The
value is not logged either: a repository URL is user input, and the request id
is enough to correlate.

**The catch-all logs the traceback and returns none of it.** `logger.exception`
routes it through `RedactingFilter`, so a token that reached an exception
message is scrubbed on the way to the log and never in the body, which carries
only a request id. Note the handler must not serialize exception attributes —
`app/security/url_validation.py` re-raises with ``from None``, which suppresses
the *display* of the original but leaves it reachable on ``__context__``, and
that original quotes the user's URL.
"""

import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.api.middleware import BodySizeLimitMiddleware, RequestIdMiddleware, request_id_of
from app.api.rate_limit import ConcurrencyGate, SlidingWindowLimiter
from app.api.routes import router
from app.config import get_settings
from app.errors import AppError, InternalError, InvalidRequestError

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Build the application. No side effects beyond constructing it.

    Logging is configured by `app/main.py` rather than here, so a test can build
    an app without replacing the root handlers underneath `caplog`.
    """
    settings = get_settings()

    app = FastAPI(
        title="Codebase 3D",
        # Nothing about the running process reaches a client: no version, and
        # the description says what the API does, not what it is built on.
        description="Dependency-graph analysis for a public GitHub TS/JS repository.",
        # The machine-readable schema stays; the two rendered docs pages go.
        # Both load their JavaScript from a public CDN, which would make a
        # third-party script the only remote resource this backend serves — a
        # supply-chain surface bought for a convenience the frontend gets from
        # `/openapi.json` anyway.
        docs_url=None,
        redoc_url=None,
        middleware=[
            # Outermost, so the id exists before anything can fail.
            Middleware(RequestIdMiddleware),
            Middleware(
                BodySizeLimitMiddleware, max_bytes=settings.MAX_REQUEST_BODY_BYTES
            ),
        ],
    )

    # Per-app, not module-level: a fresh `create_app()` (every test) gets a
    # fresh clock and an empty concurrency count, rather than inheriting hits
    # recorded by a previous app instance (ADR-008).
    app.state.analyze_rate_limiter = SlidingWindowLimiter()
    app.state.analyze_concurrency = ConcurrencyGate()

    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled_error)

    app.include_router(router)
    return app


def _body(request: Request, error: AppError, *, status: int | None = None) -> JSONResponse:
    """The only place in the application that builds an error response.

    ``status`` overrides the error's own, for the one case that needs it: a
    Starlette 404 or 405 keeps its status while borrowing `InvalidRequestError`'s
    code and message.
    """
    return JSONResponse(
        status_code=error.status_code if status is None else status,
        content=error.body(request_id_of(request.scope)),
        headers=error.headers(),
    )


async def _app_error(request: Request, exc: Exception) -> JSONResponse:
    """A typed error from any module below. Its own status, its own static message."""
    # Starlette types a handler as taking `Exception`; narrowing here rather
    # than casting keeps the function total if it is ever registered wider.
    error = exc if isinstance(exc, AppError) else InternalError()
    logger.info(
        "request refused: %s",
        error.code.value,
        extra={"request_id": request_id_of(request.scope)},
    )
    return _body(request, error)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """A malformed request body. `exc` is discarded, on purpose — see the module docstring."""
    return _body(request, InvalidRequestError())


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    """Starlette's own errors, reshaped into the one body this API returns.

    An unknown path and a wrong method are the two most reachable responses in
    the service, and without this they are the only two that do not match the
    documented contract. The status is preserved and the code is chosen by
    class, because `ErrorCode` is a frozen 14-member wire contract with no
    member for "no such route": a 4xx here means the client named something this
    API does not have, which `INVALID_REQUEST` describes honestly enough, and
    nothing from `exc.detail` is echoed.
    """
    status = exc.status_code if isinstance(exc, HTTPException) else 500
    if status >= 500:
        return _body(request, InternalError())
    return _body(request, InvalidRequestError(), status=status)


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """A bug. The traceback goes to the log, the request id goes to the client."""
    logger.exception(
        "unhandled exception", extra={"request_id": request_id_of(request.scope)}
    )
    return _body(request, InternalError())
