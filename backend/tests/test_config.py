"""Settings: documented limit values, env overrides, and secret hygiene."""

import os
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsConfigDict

from app.config import Settings, get_settings


class IsolatedSettings(Settings):
    """Settings that cannot pick up a developer's local .env file."""

    model_config = SettingsConfigDict(env_file=None)

# The limit table from docs/ARCHITECTURE.md + docs/SECURITY.md. A mismatch
# here means either the code or the security documentation is wrong — fix
# whichever one diverged, never this table silently.
EXPECTED_DEFAULTS: dict[str, object] = {
    "MAX_REQUEST_BODY_BYTES": 4096,
    "MAX_URL_LENGTH": 300,
    "MAX_REPO_API_SIZE_KB": 262_144,
    "GITHUB_CONNECT_TIMEOUT_S": 5.0,
    "GITHUB_READ_TIMEOUT_S": 15.0,
    "MAX_GITHUB_CONNECTIONS": 4,
    "MAX_DOWNLOAD_BYTES": 64 * 1024 * 1024,
    "MAX_EXTRACTED_BYTES": 256 * 1024 * 1024,
    "MAX_COMPRESSION_RATIO": 100,
    "RATIO_FLOOR_BYTES": 8 * 1024 * 1024,
    "MAX_ARCHIVE_MEMBERS": 50_000,
    "MAX_MEMBER_BYTES": 2 * 1024 * 1024,
    "MAX_PATH_DEPTH": 32,
    "MAX_PATH_LENGTH": 1024,
    "MAX_SOURCE_FILES": 3000,
    "MAX_PARSE_BYTES": 1024 * 1024,
    "MAX_CONFIG_FILES": 200,
    "MAX_ERROR_NODE_CHILDREN": 1000,
    "MAX_PARSE_TREE_VISITS": 100_000,
    "ANALYSIS_TIMEOUT_S": 60,
    "MAX_NODES": 6000,
    "MAX_EDGES": 20_000,
    "MAX_PREVIEW_BYTES": 256 * 1024,
    "MAX_DESCRIPTION_CHARS": 500,
    "MAX_SERVICE_ENDPOINTS": 200,
    "MAX_ENDPOINT_SUMMARY_CHARS": 300,
    "MAX_COMPONENT_DIAGRAM_CHARS": 20_000,
    "RATE_LIMIT_ANALYZE": (5, 60),
    "RATE_LIMIT_ANALYZE_HOURLY": (60, 3600),
    "RATE_LIMIT_SOURCE": (60, 60),
    "MAX_CONCURRENT_ANALYSES": 3,
}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ambient env vars (a real GITHUB_TOKEN, say) must not leak into tests.

    Matched case-insensitively because BaseSettings sets case_sensitive=False,
    so an exported lowercase `github_token` is picked up just as readily.
    """
    for key in list(os.environ):
        if key.upper() in Settings.model_fields:
            monkeypatch.delenv(key, raising=False)


def make_settings() -> Settings:
    return IsolatedSettings()


def test_defaults_match_documented_limits() -> None:
    settings = make_settings()
    for name, expected in EXPECTED_DEFAULTS.items():
        assert getattr(settings, name) == expected, name


def test_expected_table_covers_all_non_secret_fields() -> None:
    non_secret = set(Settings.model_fields) - {"GITHUB_TOKEN", "SOURCE_TOKEN_SECRET"}
    assert non_secret == set(EXPECTED_DEFAULTS)


def test_lowercase_env_var_is_isolated_by_the_fixture() -> None:
    """Guards the fixture itself: BaseSettings matches env vars
    case-insensitively, so a lowercase export would otherwise leak in."""
    assert make_settings().GITHUB_TOKEN is None


def test_unrelated_dotenv_key_does_not_crash_startup(tmp_path: Path) -> None:
    """A shared .env (the Docker Compose norm, ADR-001) carries keys that are
    not ours. Under the inherited extra="forbid" this raised — and pydantic's
    ValidationError echoes the offending value, so a token under a near-miss
    name like GH_TOKEN would print in cleartext before logging is even armed.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MAX_NODES=99\nLOG_LEVEL=debug\nGH_TOKEN=ghp_" + "A" * 36 + "\nVITE_API_URL=http://x\n"
    )
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert settings.MAX_NODES == 99  # the valid key still applies
    assert settings.GITHUB_TOKEN is None  # GH_TOKEN is not our field


def test_env_overrides_are_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_NODES", "123")
    monkeypatch.setenv("RATE_LIMIT_ANALYZE", "[10, 120]")
    settings = make_settings()
    assert settings.MAX_NODES == 123
    assert settings.RATE_LIMIT_ANALYZE == (10, 120)


def test_github_token_is_secret_and_never_exposed() -> None:
    fake_value = "ghp_" + "a" * 36
    settings = IsolatedSettings(GITHUB_TOKEN=SecretStr(fake_value))
    assert isinstance(settings.GITHUB_TOKEN, SecretStr)
    assert settings.GITHUB_TOKEN.get_secret_value() == fake_value
    for rendering in (repr(settings), str(settings), f"{settings}", repr(settings.GITHUB_TOKEN)):
        assert fake_value not in rendering
        assert "a" * 36 not in rendering


def test_github_token_defaults_to_none() -> None:
    assert make_settings().GITHUB_TOKEN is None


def test_source_token_secret_is_secret_and_never_exposed() -> None:
    settings = make_settings()
    secret = settings.SOURCE_TOKEN_SECRET
    assert isinstance(secret, SecretStr)
    value = secret.get_secret_value()
    # 32 random bytes, hex-encoded.
    assert len(value) == 64
    assert set(value) <= set("0123456789abcdef")
    for rendering in (repr(settings), str(settings), repr(secret), str(secret)):
        assert value not in rendering


def test_source_token_secret_is_fresh_per_instance() -> None:
    first = make_settings().SOURCE_TOKEN_SECRET.get_secret_value()
    second = make_settings().SOURCE_TOKEN_SECRET.get_secret_value()
    assert first != second


def test_get_settings_is_process_wide() -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


def test_settings_are_frozen() -> None:
    settings = make_settings()
    with pytest.raises(ValidationError):
        settings.MAX_NODES = 1
