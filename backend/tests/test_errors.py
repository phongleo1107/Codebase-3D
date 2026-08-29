"""Error contract: code/status mapping, exact body shape, static messages."""

import pytest

from app.errors import (
    AnalysisTimeoutError,
    AppError,
    ArchiveRejectedError,
    ErrorCode,
    InternalError,
    InvalidRepositoryUrlError,
    InvalidRequestError,
    NoSupportedFilesError,
    PayloadTooLargeError,
    RateLimitedError,
    RepositoryNotFoundError,
    RepositoryTooLargeError,
    ServerBusyError,
    SourceFileNotAllowedError,
    SourceFileNotFoundError,
    UpstreamUnavailableError,
)

# The full contract from the task spec / docs/ARCHITECTURE.md.
EXPECTED_MAPPING: dict[type[AppError], tuple[ErrorCode, int]] = {
    InvalidRequestError: (ErrorCode.INVALID_REQUEST, 422),
    InvalidRepositoryUrlError: (ErrorCode.INVALID_REPOSITORY_URL, 400),
    RepositoryNotFoundError: (ErrorCode.REPOSITORY_NOT_FOUND, 404),
    RepositoryTooLargeError: (ErrorCode.REPOSITORY_TOO_LARGE, 413),
    ArchiveRejectedError: (ErrorCode.ARCHIVE_REJECTED, 422),
    NoSupportedFilesError: (ErrorCode.NO_SUPPORTED_FILES, 422),
    AnalysisTimeoutError: (ErrorCode.ANALYSIS_TIMEOUT, 504),
    RateLimitedError: (ErrorCode.RATE_LIMITED, 429),
    ServerBusyError: (ErrorCode.SERVER_BUSY, 503),
    UpstreamUnavailableError: (ErrorCode.UPSTREAM_UNAVAILABLE, 502),
    SourceFileNotFoundError: (ErrorCode.FILE_NOT_FOUND, 404),
    SourceFileNotAllowedError: (ErrorCode.FILE_NOT_ALLOWED, 403),
    PayloadTooLargeError: (ErrorCode.PAYLOAD_TOO_LARGE, 413),
    InternalError: (ErrorCode.INTERNAL_ERROR, 500),
}


@pytest.mark.parametrize(
    ("error_class", "expected_code", "expected_status"),
    [(cls, code, status) for cls, (code, status) in EXPECTED_MAPPING.items()],
)
def test_error_maps_to_code_and_status(
    error_class: type[AppError], expected_code: ErrorCode, expected_status: int
) -> None:
    error = error_class()
    assert error.code is expected_code
    assert error.status_code == expected_status


def all_subclasses(cls: type[AppError]) -> set[type[AppError]]:
    """Transitive subclasses. `__subclasses__()` alone is direct-only, which
    would let a class deriving from an existing error escape every
    parametrized test below while still passing the exhaustiveness guard."""
    found: set[type[AppError]] = set()
    for sub in cls.__subclasses__():
        found.add(sub)
        found |= all_subclasses(sub)
    return found


def test_mapping_is_a_bijection_over_all_codes() -> None:
    codes = [code for code, _ in EXPECTED_MAPPING.values()]
    assert len(codes) == len(set(codes))
    assert set(codes) == set(ErrorCode)
    # No AppError subclass, at any depth, exists outside the tested contract.
    assert all_subclasses(AppError) == set(EXPECTED_MAPPING)


@pytest.mark.parametrize("error_class", list(EXPECTED_MAPPING))
def test_body_shape_is_exactly_three_keys(error_class: type[AppError]) -> None:
    body = error_class().body("req-0123456789ab")
    assert set(body) == {"error"}
    detail = body["error"]
    assert set(detail) == {"code", "message", "requestId"}
    assert detail["code"] == error_class.code.value
    assert detail["message"] == error_class.message
    assert detail["requestId"] == "req-0123456789ab"
    assert all(isinstance(value, str) for value in detail.values())


@pytest.mark.parametrize("error_class", list(EXPECTED_MAPPING))
def test_messages_are_static_user_facing_strings(error_class: type[AppError]) -> None:
    error = error_class()
    assert str(error) == error_class.message
    assert error_class.message
    # No interpolation targets — a message is a constant, never a template.
    assert "%" not in error_class.message
    assert "{" not in error_class.message


def test_constructor_refuses_dynamic_detail() -> None:
    """Per-instance detail (paths, exception text) must be structurally
    impossible to attach — it would leak into a response body."""
    with pytest.raises(TypeError):
        InternalError("secret /etc/detail")  # type: ignore[call-arg]


@pytest.mark.parametrize("error_class", list(EXPECTED_MAPPING))
def test_statuses_are_valid_http_error_codes(error_class: type[AppError]) -> None:
    assert 400 <= error_class.status_code <= 599
