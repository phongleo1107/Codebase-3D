# Current State

## Current Goal

Build the MVP defined in [PRD.md](../PRD.md): paste a public GitHub URL → safely analyze a TS/JS repository → render its dependency graph in navigable 3D.

Step 1 of the build order — **the backend contract (config, errors, models, logging) — is done and green.** Next is the security boundary, which must also be green before any network code exists.

## Working

**The backend contract layer and its tests.** There is still no routing, network, parsing, or analysis code: `app/api/`, `app/security/`, `app/fetch/`, and `app/analysis/` remain empty packages.

| Path | Notes |
|---|---|
| `LICENSE` | MIT |
| `README.md` | Two-line description |
| `PRD.md` | Product spec — now staged in git |
| `CLAUDE.md`, `docs/*` | This documentation, created 2026-08-29 |
| `.gitignore` | Both stacks; `.env.example` is un-ignored |
| `backend/pyproject.toml` | Pinned deps + pytest/ruff/mypy config; hatchling build backend |
| `backend/uv.lock` | 39 resolved packages, committed |
| `backend/app/config.py` | `Settings` — all 22 limits, `SecretStr` secrets, `extra="ignore"` |
| `backend/app/errors.py` | `ErrorCode` + 14 `AppError` subclasses; fixed 3-key body |
| `backend/app/models/` | `graph.py`, `api.py`, re-exporting `__init__.py` |
| `backend/app/logging_setup.py` | JSON-line formatter + `RedactingFilter` |
| `backend/tests/` | 160 tests across config, errors, models, logging |
| `backend/app/{api,security,fetch,analysis}/` | Still empty packages |
| `.claude/settings.local.json` | Local tool permissions, not source |

No `frontend/`, no Docker files, no CI.

**Verified locally on 2026-08-29** (Python 3.14.7, uv 0.12.3):

- `uv sync` resolves and installs cleanly; the project installs as an editable package.
- `uv run pytest` → **160 passed**. `uv run mypy` (strict) → clean over 16 files. `uv run ruff check .` → clean.
- tree-sitter ABI spike printed **`14`**, and `QueryCursor` imports successfully alongside `Language`, `Parser`, and `Query`.

Two `pyproject.toml` corrections were needed: `[tool.ruff] src = ["."]` (it was `["app", "tests"]`, which pointed *inside* the package so isort never treated `app` as first-party), and `S105`/`S106` added to the test per-file-ignores because redaction tests must hardcode fake credentials.

## In Progress

- Nothing is under active implementation.

## Broken / Known Issues

- No CI yet.
- The contract layer is unexercised by any route — nothing constructs an `AnalyzeResponse` from real data yet, so field *semantics* are only as good as the documentation.
- `pytest.filterwarnings = ["error"]` now runs against a real suite and is clean; no targeted ignores have been needed.
- The pydantic **mypy plugin is not enabled** (no `plugins` key in `[tool.mypy]`). Constructor type-checking still works via PEP 681 `@dataclass_transform` on pydantic's metaclass. Reviewed and judged unnecessary; do not assume the plugin is present when reading type errors.

## Recently Completed

- **2026-08-29** — **Backend contract layer implemented**: `app/config.py`, `app/errors.py`, `app/models/{graph,api}.py`, `app/logging_setup.py`, plus 160 tests. The wire schema is now frozen, so frontend work can proceed against it in parallel. Scope was contract and plumbing only — no routes, no network, no parsing.
- **2026-08-29** — A multi-lens adversarial review of that contract layer confirmed 17 defects, all fixed and reverified. The four that mattered:
  - `Settings` inherited `extra="forbid"` from `BaseSettings`, so a single unrelated key in a shared `.env` aborted startup — and pydantic's `ValidationError` echoes the offending *value*, so a token under a near-miss name (`GH_TOKEN=ghp_…`) would print in cleartext via the default excepthook, which no logging filter can reach. Now `extra="ignore"`.
  - `models/api.py` hardcoded `300` and `1024` instead of reading `Settings`, so tightening `MAX_URL_LENGTH` had no effect at the boundary meant to enforce it. Bounds are now `AfterValidator`s reading `get_settings()`.
  - Redaction covered only `ghp_` and `github_pat_`. `ghs_` — what GitHub Actions puts in `$GITHUB_TOKEN`, and so the likeliest value an operator pastes into ours — passed through verbatim, as did `gho_`/`ghu_`/`ghr_`. The `Authorization` pattern also missed the `[('authorization', 'Bearer …')]` form that `httpx.Headers.items()` produces, which is exactly what one reaches for when debugging a 401 because httpx masks its own `repr`.
  - `RedactingFilter` scrubbed `record.exc_text`, but filters run *before* formatters, so that field is always `None` at filter time — tracebacks were redacted only by `JsonFormatter`, and a plain handler leaked them. The filter now renders the traceback itself.
- **2026-08-29** — Dependency versions verified live against PyPI and npm. Three traps recorded: R3F 10 is alpha and incompatible with drei 10 (pin R3F 9.7.0); TypeScript must be pinned to 5.9.3 because `typescript-eslint` 8.68 caps at `<6.1.0`; `d3-force-3d` ships no types and no `@types` package exists, so a local `.d.ts` is required. Also: `tree-sitter` 0.25/0.26 removed `Query.captures()` and `Language.query()` in favour of `QueryCursor`.
- **2026-08-29** — This documentation system created. Design rationale captured as ADR-001 … ADR-008 in [DECISIONS.md](DECISIONS.md), so the plan is now self-contained in the repository.
- **2026-08-29** — `.gitignore` added, `PRD.md` staged, `backend/` dependency scaffold created and installed. `pydantic-settings` pinned to **2.15.0** after checking PyPI; `ruff` **0.16.5** and `mypy` **2.3.1** pinned rather than floated, per the "pin dependency versions" rule. `hatchling` **1.32.0** added as the build backend — the only dependency beyond the agreed list, needed because a PEP 517 backend is required for the package to be installable at all.
- **2026-08-29** — tree-sitter ABI spike run for real: `Language(tree_sitter_typescript.language_tsx()).abi_version` → **`14`**. The `QueryCursor` API is present, confirming the 0.25/0.26 migration away from `Language.query()` / `Query.captures()` / `Query.matches()`.

## Next Steps

1. Implement `security/url_validation.py` and `security/net_guard.py` **with their tests passing**, before any code that can make a network request exists.
2. Implement `fetch/archive.py` with in-process malicious-tarball fixtures (`io.BytesIO`), covering traversal, symlinks, bombs, and malformed names. Still no network.
3. When routes land, wire `AppError` into a FastAPI exception handler and map `RequestValidationError` to a bare `INVALID_REQUEST` — pydantic's `detail` embeds the offending input and must never be returned.

The ABI half of the tree-sitter spike is **done** (see above). The `progress_callback` signature on `Parser.parse()` is still unverified and must be confirmed before the extractor is written.

## Last Updated

2026-08-29
