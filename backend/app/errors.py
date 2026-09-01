"""Error contract: codes, HTTP statuses, and the fixed response body shape.

Every error leaving the API has the body ``{"error": {"code", "message",
"requestId"}}`` — exactly those three keys, always (PRD §13,
docs/ARCHITECTURE.md "API Boundaries").

Messages are static class attributes, never built at raise time. This is a
security control, not a style choice: interpolating an exception, a path, an
upstream response, or any user input into a message is how stack traces and
internal detail leak (docs/SECURITY.md "Information disclosure via errors").
Anything dynamic belongs in the server-side log, keyed by the request ID.
"""

from enum import StrEnum
from typing import ClassVar, TypedDict


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_REPOSITORY_URL = "INVALID_REPOSITORY_URL"
    REPOSITORY_NOT_FOUND = "REPOSITORY_NOT_FOUND"
    REPOSITORY_TOO_LARGE = "REPOSITORY_TOO_LARGE"
    ARCHIVE_REJECTED = "ARCHIVE_REJECTED"
    NO_SUPPORTED_FILES = "NO_SUPPORTED_FILES"
    ANALYSIS_TIMEOUT = "ANALYSIS_TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_BUSY = "SERVER_BUSY"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_NOT_ALLOWED = "FILE_NOT_ALLOWED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorDetail(TypedDict):
    code: str
    message: str
    requestId: str


class ErrorBody(TypedDict):
    error: ErrorDetail


class AppError(Exception):
    """Abstract base. Subclasses define ``code``, ``status_code``, ``message``.

    ``__init__`` deliberately takes no arguments so a call site *cannot*
    attach per-instance detail that would end up in a response body.
    """

    code: ClassVar[ErrorCode]
    status_code: ClassVar[int]
    message: ClassVar[str]

    def __init__(self) -> None:
        super().__init__(self.message)

    def headers(self) -> dict[str, str]:
        """Extra response headers. Empty for every error except `RateLimitedError`."""
        return {}

    def body(self, request_id: str) -> ErrorBody:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "requestId": request_id,
            }
        }


class InvalidRequestError(AppError):
    code = ErrorCode.INVALID_REQUEST
    status_code = 422
    message = "The request is malformed or fails validation."


class InvalidRepositoryUrlError(AppError):
    code = ErrorCode.INVALID_REPOSITORY_URL
    status_code = 400
    message = "The URL must look like https://github.com/owner/repository."


class RepositoryNotFoundError(AppError):
    """Covers upstream 404 and 403 alike, so a configured token cannot become
    a private-repository existence oracle (docs/ARCHITECTURE.md, ingestion)."""

    code = ErrorCode.REPOSITORY_NOT_FOUND
    status_code = 404
    message = "The repository could not be found or is not publicly accessible."


class RepositoryTooLargeError(AppError):
    code = ErrorCode.REPOSITORY_TOO_LARGE
    status_code = 413
    message = "Repository exceeds the maximum supported size."


class ArchiveRejectedError(AppError):
    code = ErrorCode.ARCHIVE_REJECTED
    status_code = 422
    message = "The repository archive could not be processed safely."


class NoSupportedFilesError(AppError):
    code = ErrorCode.NO_SUPPORTED_FILES
    status_code = 422
    message = "The repository contains no supported TypeScript or JavaScript files."


class AnalysisTimeoutError(AppError):
    code = ErrorCode.ANALYSIS_TIMEOUT
    status_code = 504
    message = "Analysis took too long and was aborted."


class RateLimitedError(AppError):
    """Carries `retry_after_s` for the `Retry-After` header only — never the body.

    The one exception to "no per-instance detail": a wait time is not
    attacker-controlled input and does not appear in `message`, so it cannot
    reintroduce the echoing problem `__init__`'s no-argument rule guards
    against.
    """

    code = ErrorCode.RATE_LIMITED
    status_code = 429
    message = "Too many requests. Please try again later."

    def __init__(self, retry_after_s: int) -> None:
        super().__init__()
        self.retry_after_s = retry_after_s

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after_s)}


class ServerBusyError(AppError):
    code = ErrorCode.SERVER_BUSY
    status_code = 503
    message = "The server is busy with other analyses. Please try again shortly."


class UpstreamUnavailableError(AppError):
    code = ErrorCode.UPSTREAM_UNAVAILABLE
    status_code = 502
    message = "GitHub could not be reached. Please try again later."


class SourceFileNotFoundError(AppError):
    code = ErrorCode.FILE_NOT_FOUND
    status_code = 404
    message = "The requested file was not found in the analyzed repository."


class SourceFileNotAllowedError(AppError):
    code = ErrorCode.FILE_NOT_ALLOWED
    status_code = 403
    message = "The requested file cannot be shown."


class PayloadTooLargeError(AppError):
    code = ErrorCode.PAYLOAD_TOO_LARGE
    status_code = 413
    message = "The request body is too large."


class InternalError(AppError):
    code = ErrorCode.INTERNAL_ERROR
    status_code = 500
    message = "An internal error occurred."
