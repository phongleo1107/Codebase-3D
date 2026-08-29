# Current State

## Current Goal

Build the MVP defined in [PRD.md](../PRD.md): paste a public GitHub URL → safely analyze a TS/JS repository → render its dependency graph in navigable 3D.

Immediate goal is **step 1 of the build order**: the backend contract (config, errors, models, logging) followed by the security boundary, both green before any network code exists.

## Working

**The backend dependency scaffold — and nothing else.** There is still no application code: no routes, no models, no analyzer. `backend/app/` is a tree of empty `__init__.py` files.

| Path | Notes |
|---|---|
| `LICENSE` | MIT |
| `README.md` | Two-line description |
| `PRD.md` | Product spec — now staged in git |
| `CLAUDE.md`, `docs/*` | This documentation, created 2026-08-29 |
| `.gitignore` | Both stacks; `.env.example` is un-ignored |
| `backend/pyproject.toml` | Pinned deps + pytest/ruff/mypy config; hatchling build backend |
| `backend/uv.lock` | 39 resolved packages, committed |
| `backend/app/{api,models,security,fetch,analysis}/` | Empty packages |
| `backend/tests/` | Empty package, no tests yet |
| `.claude/settings.local.json` | Local tool permissions, not source |

No `frontend/`, no Docker files, no CI.

**Verified locally on 2026-08-29** (Python 3.14.7, uv 0.12.3):

- `uv sync` resolves and installs cleanly; the project installs as an editable package.
- `uv run ruff check .` → clean. `uv run mypy` (strict) → clean over 7 files.
- `uv run pytest` → `collected 0 items`, no errors. Note the process **exit code is 5** (`EXIT_NOTESTSCOLLECTED`), not 0 — CI must tolerate this until the first test lands.
- tree-sitter ABI spike printed **`14`**, and `QueryCursor` imports successfully alongside `Language`, `Parser`, and `Query`.

## In Progress

- Nothing is under active implementation.

## Broken / Known Issues

- No test harness content, no CI. Linting and typing are configured but have nothing to check.
- `uv run pytest` exits 5 on an empty suite (see above).
- `pytest.filterwarnings = ["error"]` is set but untested against real dependency warnings; expect to add targeted ignores once tests exist.

## Recently Completed

- **2026-08-29** — Requirements read, design agreed, and full implementation plan produced (backend ingestion/parsing/security, frontend rendering/layout, deployment).
- **2026-08-29** — Dependency versions verified live against PyPI and npm. Three traps recorded: R3F 10 is alpha and incompatible with drei 10 (pin R3F 9.7.0); TypeScript must be pinned to 5.9.3 because `typescript-eslint` 8.68 caps at `<6.1.0`; `d3-force-3d` ships no types and no `@types` package exists, so a local `.d.ts` is required. Also: `tree-sitter` 0.25/0.26 removed `Query.captures()` and `Language.query()` in favour of `QueryCursor`.
- **2026-08-29** — This documentation system created. Design rationale captured as ADR-001 … ADR-008 in [DECISIONS.md](DECISIONS.md), so the plan is now self-contained in the repository.
- **2026-08-29** — `.gitignore` added, `PRD.md` staged, `backend/` dependency scaffold created and installed. `pydantic-settings` pinned to **2.15.0** after checking PyPI; `ruff` **0.16.5** and `mypy` **2.3.1** pinned rather than floated, per the "pin dependency versions" rule. `hatchling` **1.32.0** added as the build backend — the only dependency beyond the agreed list, needed because a PEP 517 backend is required for the package to be installable at all.
- **2026-08-29** — tree-sitter ABI spike run for real: `Language(tree_sitter_typescript.language_tsx()).abi_version` → **`14`**. The `QueryCursor` API is present, confirming the 0.25/0.26 migration away from `Language.query()` / `Query.captures()` / `Query.matches()`.

## Next Steps

1. `app/config.py`, `app/errors.py`, `app/models/graph.py`, `app/logging_setup.py` — the API contract first, since the frontend can be built against it in parallel.
2. Implement `security/url_validation.py` and `security/net_guard.py` **with their tests passing**, before any code that can make a network request exists.
3. Implement `fetch/archive.py` with in-process malicious-tarball fixtures (`io.BytesIO`), covering traversal, symlinks, bombs, and malformed names. Still no network.

The ABI half of the tree-sitter spike is **done** (see above). The `progress_callback` signature on `Parser.parse()` is still unverified and must be confirmed before the extractor is written.

## Last Updated

2026-08-29
