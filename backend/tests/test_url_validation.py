"""Repository URL grammar: what is accepted, and the much longer list of what is not.

Every rejection must be the same typed error carrying the same static body, so
the last test in this file asserts that as a single invariant over the whole
reject table rather than trusting each case individually.
"""

import dataclasses

import pytest

from app.config import Settings
from app.errors import AppError, ErrorCode, InvalidRepositoryUrlError
from app.security import url_validation
from app.security.url_validation import RepoRef, parse_github_url

# --------------------------------------------------------------------------
# Accepted
# --------------------------------------------------------------------------

ACCEPTED: list[tuple[str, RepoRef]] = [
    # Canonical.
    ("https://github.com/facebook/react", RepoRef("facebook", "react", None)),
    # Trailing slash.
    ("https://github.com/facebook/react/", RepoRef("facebook", "react", None)),
    # .git suffix, alone and combined with a trailing slash.
    ("https://github.com/facebook/react.git", RepoRef("facebook", "react", None)),
    ("https://github.com/facebook/react.git/", RepoRef("facebook", "react", None)),
    # Branch.
    ("https://github.com/facebook/react/tree/main", RepoRef("facebook", "react", "main")),
    # Branch names may contain slashes.
    (
        "https://github.com/facebook/react/tree/feat/nested-name",
        RepoRef("facebook", "react", "feat/nested-name"),
    ),
    (
        "https://github.com/o/r/tree/release/v1/hotfix",
        RepoRef("o", "r", "release/v1/hotfix"),
    ),
    # Trailing slash after a branch.
    ("https://github.com/o/r/tree/main/", RepoRef("o", "r", "main")),
    # A tag or a commit SHA is just a ref.
    ("https://github.com/o/r/tree/v1.2.3", RepoRef("o", "r", "v1.2.3")),
    ("https://github.com/o/r/tree/a1b2c3d", RepoRef("o", "r", "a1b2c3d")),
    # www. prefix.
    ("https://www.github.com/facebook/react", RepoRef("facebook", "react", None)),
    # Mixed case in owner and repo is preserved — GitHub is case-insensitive
    # here and the metadata preflight returns the canonical spelling.
    ("https://github.com/MicroSoft/TypeScript", RepoRef("MicroSoft", "TypeScript", None)),
    # Host and scheme are case-insensitive per RFC 3986.
    ("https://GitHub.com/o/r", RepoRef("o", "r", None)),
    ("HTTPS://github.com/o/r", RepoRef("o", "r", None)),
    # Punctuation that GitHub actually permits in a repository name.
    ("https://github.com/o/my.repo_name-1", RepoRef("o", "my.repo_name-1", None)),
    # Hyphens inside an owner.
    ("https://github.com/some-org/some-repo", RepoRef("some-org", "some-repo", None)),
    # Surrounding whitespace from a paste is forgiven.
    ("  https://github.com/o/r\n", RepoRef("o", "r", None)),
    # Boundary: 39-character owner, 100-character repository name.
    ("https://github.com/" + "a" * 39 + "/r", RepoRef("a" * 39, "r", None)),
    ("https://github.com/o/" + "r" * 100, RepoRef("o", "r" * 100, None)),
]


@pytest.mark.parametrize(("raw", "expected"), ACCEPTED, ids=[case[0] for case in ACCEPTED])
def test_accepts_valid_repository_url(raw: str, expected: RepoRef) -> None:
    assert parse_github_url(raw) == expected


# --------------------------------------------------------------------------
# Rejected
# --------------------------------------------------------------------------

REJECTED: list[str] = [
    # --- wrong scheme ---
    "http://github.com/o/r",
    "ftp://github.com/o/r",
    "file:///etc/passwd",
    "file://github.com/o/r",
    "javascript:alert(1)",
    "javascript:https://github.com/o/r",
    "data:text/html,<script>alert(1)</script>",
    "ssh://git@github.com/o/r",
    # --- no scheme at all ---
    "git@github.com:o/r",
    "github.com/o/r",
    "//github.com/o/r",
    "//evil.com/o/r",
    "/o/r",
    "",
    # --- structurally incomplete ---
    "https://github.com",
    "https://github.com/",
    "https://github.com/owner",
    "https://github.com/owner/",
    "https://github.com//repo",
    "https://github.com/o/r//",
    "https:///o/r",
    # --- host is not github.com ---
    "https://github.com.evil.com/o/r",
    "https://evil.com/github.com/o/r",
    "https://evil.com/o/r",
    "https://gitlab.com/o/r",
    "https://raw.githubusercontent.com/o/r",
    "https://notgithub.com/o/r",
    "https://github.como/o/r",
    # Trailing root dot: valid DNS, not one of the accepted forms.
    "https://github.com./o/r",
    "https://www.github.com./o/r",
    # A subdomain is not the apex.
    "https://gist.github.com/o/r",
    "https://api.github.com/o/r",
    # --- userinfo ---
    "https://user:pass@github.com/o/r",
    "https://github.com@evil.com/o/r",
    "https://github.com:x@evil.com/o/r",
    # Browsers read the backslash as a path separator; urlsplit reads the host
    # as evil.com. Rejected before either interpretation can matter.
    "https://github.com\\@evil.com/o/r",
    # --- ports ---
    "https://github.com:8080/o/r",
    "https://github.com:443/o/r",
    "https://github.com:notaport/o/r",
    "https://github.com:/o/r",
    # --- private, loopback, and link-local targets ---
    "https://localhost/o/r",
    "https://localhost:8000/o/r",
    "https://127.0.0.1/o/r",
    "https://127.1/o/r",
    "https://[::1]/o/r",
    # Malformed bracketed hosts: urlsplit raises ValueError on these, and the
    # message quotes the host back, so an escape would be an echo as well as a
    # contract break.
    "https://[evil.com]/o/r",
    "https://[::1/o/r",
    "https://[]/o/r",
    "https://[1:2:3]/o/r",
    "https://[v1.fe80::a]/o/r",
    "https://169.254.169.254/o/r",
    "https://169.254.169.254/latest/meta-data/",
    "https://10.0.0.1/o/r",
    "https://192.168.1.1/o/r",
    "https://172.16.0.1/o/r",
    "https://metadata.google.internal/o/r",
    # --- homographs / non-ASCII ---
    # Cyrillic small letter Byelorussian-Ukrainian i (U+0456) for the "i".
    "https://gіthub.com/o/r",  # noqa: RUF001
    # Cyrillic small letter o (U+043E) inside ".com".
    "https://github.cоm/o/r",  # noqa: RUF001
    # Punycode for the first of those: a different host, spelled honestly.
    "https://xn--gthub-x9d.com/o/r",
    # Non-ASCII anywhere at all, even in an otherwise valid path.
    "https://github.com/o/ré",
    "https://github.com/аwner/r",  # noqa: RUF001
    # Fullwidth solidus, which some parsers fold to "/".
    "https://github.com／o/r",  # noqa: RUF001
    # --- encoded traversal: rejected as a character-set violation, never decoded ---
    "https://github.com/%2e%2e/r",
    "https://github.com/%2e%2e%2f%2e%2e/r",
    "https://github.com/o/%2e%2e",
    "https://github.com/o/r%2f..%2f",
    "https://github.com/o/r/tree/%2e%2e",
    # --- literal traversal ---
    "https://github.com/o/r/tree/../..",
    "https://github.com/o/r/tree/main/../../etc",
    "https://github.com/../../etc/passwd",
    "https://github.com/o/..",
    "https://github.com/o/.",
    "https://github.com/./r",
    # --- control characters ---
    # NUL, embedded and trailing.
    "https://github.com/o\x00/r",
    "https://github.com/o/r\x00",
    "https://github.com/o/r\x00.evil.com",
    # urlsplit deletes tab/CR/LF outright, so these would otherwise parse as a
    # valid github.com URL that no human reading the input would recognise.
    "https://gith\tub.com/o/r",
    "https://github.com/o\n/r",
    "https://github.com/o\r/r",
    "https://evil.com\t/o/r",
    # Internal space.
    "https://github.com /o/r",
    "https://github.com/o /r",
    # Unicode whitespace padding. These are the reason the ASCII check must run
    # BEFORE .strip(): str.strip() removes any character where isspace() is
    # true, including U+00A0 and U+3000, which would leave a clean ASCII URL
    # behind and let non-ASCII input through unnoticed.
    "\u00a0https://github.com/o/r",
    "https://github.com/o/r\u00a0",
    "\u3000https://github.com/o/r",
    "\u2007https://github.com/o/r",
    # --- oversized ---
    "https://github.com/o/" + "a" * 10_000,
    "https://github.com/" + "a" * 10_000 + "/r",
    # --- character-set violations in owner or repo ---
    "https://github.com/-owner/r",
    "https://github.com/owner-/r",
    "https://github.com/own er/r",
    "https://github.com/own$er/r",
    "https://github.com/o/r$",
    "https://github.com/o/r?x=1",
    "https://github.com/o/r#readme",
    "https://github.com/o/r?tab=readme-ov-file",
    "https://github.com/.git",
    "https://github.com/o/.git",
    # Owner 40 chars, repository name 101 chars: one past each boundary.
    "https://github.com/" + "a" * 40 + "/r",
    "https://github.com/o/" + "r" * 101,
    # --- path shapes that are not /owner/repo[/tree/ref] ---
    "https://github.com/o/r/blob/main/src/index.ts",
    "https://github.com/o/r/pull/1",
    "https://github.com/o/r/tree",
    "https://github.com/o/r/tree/",
    "https://github.com/o/r//tree/main",
    "https://github.com/o/r/tree/main//nested",
    "https://github.com/o/r/tree/.hidden",
    "https://github.com/o/r/tree/main.lock",
    "https://github.com/o/r/tree/ref.",
    "https://github.com/o/r/tree/a..b",
    "https://github.com/o/r/archive/refs/heads/main.tar.gz",
]


@pytest.mark.parametrize("raw", REJECTED, ids=[repr(case) for case in REJECTED])
def test_rejects_invalid_repository_url(raw: str) -> None:
    with pytest.raises(InvalidRepositoryUrlError):
        parse_github_url(raw)


FUZZ_PARTS = ["", "[", "]", ":", "@", "/", "\\", ".", "%", "?", "#", "-", "a", "1", "::", "\x00"]
FUZZ_TEMPLATES = [
    "https://{}{}{}/o/r",
    "https://github.com{}{}{}/o/r",
    "https://[{}{}{}]/o/r",
    "https://github.com/o/r/tree/{}{}{}",
    "{}{}{}",
]


def test_only_the_typed_error_ever_escapes() -> None:
    """No input may produce a bare exception — requirement, not preference.

    `urlsplit` is the trap: it raises `ValueError` on a malformed bracketed
    host and quotes the offending host in the message, so an escape here would
    be an information leak as well as a broken contract. This sweeps ~20k
    structurally hostile inputs rather than trusting the reject table to have
    thought of every shape.
    """
    checked = 0
    for template in FUZZ_TEMPLATES:
        for first in FUZZ_PARTS:
            for second in FUZZ_PARTS:
                for third in FUZZ_PARTS:
                    candidate = template.format(first, second, third)
                    checked += 1
                    try:
                        parse_github_url(candidate)
                    except InvalidRepositoryUrlError:
                        pass
                    except Exception as exc:
                        pytest.fail(
                            f"{type(exc).__name__} escaped parse_github_url for "
                            f"{candidate!r} (only InvalidRepositoryUrlError may)"
                        )
    assert checked == len(FUZZ_TEMPLATES) * len(FUZZ_PARTS) ** 3


def test_rejection_is_a_typed_app_error() -> None:
    with pytest.raises(AppError) as caught:
        parse_github_url("https://evil.com/o/r")
    error = caught.value
    assert isinstance(error, InvalidRepositoryUrlError)
    assert error.code is ErrorCode.INVALID_REPOSITORY_URL
    assert error.status_code == 400


@pytest.mark.parametrize("raw", REJECTED, ids=[repr(case) for case in REJECTED])
def test_rejection_never_echoes_the_input(raw: str) -> None:
    """The response body must be byte-identical for every rejected input.

    `AppError.__init__` takes no arguments, so this holds structurally; the test
    exists so that a later refactor which adds a "helpful" detail argument
    fails here rather than in production.
    """
    with pytest.raises(InvalidRepositoryUrlError) as caught:
        parse_github_url(raw)
    body = caught.value.body("fixed-request-id")
    assert body == {
        "error": {
            "code": "INVALID_REPOSITORY_URL",
            "message": InvalidRepositoryUrlError.message,
            "requestId": "fixed-request-id",
        }
    }
    assert str(caught.value) == InvalidRepositoryUrlError.message


@pytest.mark.parametrize("marker", ["evil", "passwd", "169.254", "localhost", "\x00", "alert"])
def test_no_attacker_marker_reaches_the_body(marker: str) -> None:
    """None of the distinctive strings in the reject table can appear in a body."""
    for raw in REJECTED:
        if marker not in raw:
            continue
        with pytest.raises(InvalidRepositoryUrlError) as caught:
            parse_github_url(raw)
        rendered = repr(caught.value.body("fixed-request-id"))
        assert marker not in rendered


# --------------------------------------------------------------------------
# Limits and value semantics
# --------------------------------------------------------------------------


def test_url_length_limit_is_read_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound is configuration, not a hardcoded number in this module."""
    url = "https://github.com/facebook/react"
    assert parse_github_url(url) == RepoRef("facebook", "react", None)

    monkeypatch.setattr(url_validation, "get_settings", lambda: Settings(MAX_URL_LENGTH=len(url)))
    assert parse_github_url(url) == RepoRef("facebook", "react", None)

    monkeypatch.setattr(
        url_validation, "get_settings", lambda: Settings(MAX_URL_LENGTH=len(url) - 1)
    )
    with pytest.raises(InvalidRepositoryUrlError):
        parse_github_url(url)


def test_length_limit_boundary_is_inclusive() -> None:
    """MAX_URL_LENGTH characters is accepted; one more is not.

    The length is padded into the ref rather than the repository name, which
    has its own much smaller cap.
    """
    limit = Settings().MAX_URL_LENGTH
    prefix = "https://github.com/o/r/tree/"
    head = "a" * 200
    tail = "b" * (limit - len(prefix) - len(head) - 1)
    ref = f"{head}/{tail}"
    url = prefix + ref

    assert len(url) == limit
    assert parse_github_url(url) == RepoRef("o", "r", ref)

    with pytest.raises(InvalidRepositoryUrlError):
        parse_github_url(url + "b")


def test_repo_ref_is_frozen() -> None:
    ref = parse_github_url("https://github.com/o/r")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.owner = "someone-else"  # type: ignore[misc]


def test_repo_ref_compares_by_value() -> None:
    assert parse_github_url("https://github.com/o/r") == RepoRef("o", "r", None)
    assert parse_github_url("https://github.com/o/r") != RepoRef("o", "r", "main")


def test_www_prefix_and_git_suffix_do_not_change_identity() -> None:
    """The four spellings of one repository all parse to the same coordinate."""
    spellings = [
        "https://github.com/facebook/react",
        "https://github.com/facebook/react/",
        "https://github.com/facebook/react.git",
        "https://www.github.com/facebook/react.git/",
    ]
    assert {parse_github_url(url) for url in spellings} == {RepoRef("facebook", "react", None)}
