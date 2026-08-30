"""Application settings — every operational limit in one place.

Each limit here backs a control in docs/SECURITY.md; no other module may
hardcode its own copy. Values can be overridden through the environment or a
local ``.env`` file (e.g. ``MAX_NODES=1000``).

Secrets are held as ``SecretStr`` so they cannot leak through ``repr()``,
``str()``, or a serialized dump by accident.
"""

from functools import lru_cache
from secrets import token_bytes

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _fresh_source_token_secret() -> SecretStr:
    """256-bit random HMAC key for `/api/source` tokens (ADR-007).

    Generated once per process via :func:`get_settings`. A restart rotates the
    key and invalidates outstanding tokens — acceptable, since tokens only ever
    live inside one analyze/preview session and nothing is persisted.
    """
    return SecretStr(token_bytes(32).hex())


class Settings(BaseSettings):
    # extra="ignore" is deliberate and load-bearing. BaseSettings defaults to
    # "forbid", under which one unrelated key in a shared .env (the normal
    # Docker Compose pattern) aborts startup — and pydantic's ValidationError
    # echoes the offending value, so a mistyped GH_TOKEN=ghp_… would be printed
    # in cleartext by the default excepthook, which no logging filter can reach.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    # --- Client -> API boundary ---
    MAX_REQUEST_BODY_BYTES: int = 4096
    MAX_URL_LENGTH: int = 300

    # --- GitHub -> analyzer boundary ---
    # The GitHub repos API reports `size` in KiB: 262_144 KiB == 256 MiB.
    MAX_REPO_API_SIZE_KB: int = 262_144
    # Outbound HTTP. A hung upstream must not pin an analysis slot open until
    # the 60s deadline expires, so both phases are bounded well below it.
    GITHUB_CONNECT_TIMEOUT_S: float = 5.0
    GITHUB_READ_TIMEOUT_S: float = 15.0
    MAX_GITHUB_CONNECTIONS: int = 4
    MAX_DOWNLOAD_BYTES: int = 64 * 1024 * 1024
    MAX_EXTRACTED_BYTES: int = 256 * 1024 * 1024
    # Ratio guard trips only past the floor, so a tiny archive with one
    # well-compressed file is not rejected while a gigabyte-of-zeros bomb
    # still dies at ~8 MiB extracted.
    MAX_COMPRESSION_RATIO: int = 100
    RATIO_FLOOR_BYTES: int = 8 * 1024 * 1024
    MAX_ARCHIVE_MEMBERS: int = 50_000
    MAX_MEMBER_BYTES: int = 2 * 1024 * 1024
    MAX_PATH_DEPTH: int = 32
    MAX_PATH_LENGTH: int = 1024

    # --- Analysis ---
    MAX_SOURCE_FILES: int = 3000
    MAX_PARSE_BYTES: int = 1024 * 1024
    MAX_CONFIG_FILES: int = 200
    # Pathological-parse-tree guard (app/analysis/parser.py). tree-sitter's
    # query engine is quadratic in the *width* of an ERROR node, so a file that
    # is one enormous syntax error costs minutes even though parsing it takes
    # milliseconds. These two bound that; see the module docstring for the
    # measurements. Real source stays orders of magnitude below both — the
    # widest ERROR node in a truncated file is single digits.
    MAX_ERROR_NODE_CHILDREN: int = 1000
    MAX_PARSE_TREE_VISITS: int = 100_000
    ANALYSIS_TIMEOUT_S: int = 60
    MAX_NODES: int = 6000
    MAX_EDGES: int = 20_000

    # --- Source preview ---
    MAX_PREVIEW_BYTES: int = 256 * 1024

    # --- Rate limiting: (max requests, window seconds) per client IP ---
    RATE_LIMIT_ANALYZE: tuple[int, int] = (5, 60)
    RATE_LIMIT_ANALYZE_HOURLY: tuple[int, int] = (60, 3600)
    RATE_LIMIT_SOURCE: tuple[int, int] = (60, 60)
    MAX_CONCURRENT_ANALYSES: int = 3

    # --- Secrets ---
    # Optional; raises GitHub API rate limits only. Never sent to codeload.
    GITHUB_TOKEN: SecretStr | None = None
    SOURCE_TOKEN_SECRET: SecretStr = Field(default_factory=_fresh_source_token_secret)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide Settings instance.

    Application code must use this rather than constructing ``Settings()``
    directly — it is what gives the default ``SOURCE_TOKEN_SECRET`` its
    per-process lifetime.
    """
    return Settings()
