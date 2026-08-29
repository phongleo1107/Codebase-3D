# TODO

Priority order: security → correctness → core functionality → performance → UX → nice-to-have.

## Now

- [x] Add `.gitignore` (`.venv/`, `__pycache__/`, `node_modules/`, `dist/`, `.env`) and stage `PRD.md` — *staged, not yet committed*
- [x] Scaffold `backend/pyproject.toml` with pinned deps and `requires-python >=3.14,<3.15` — `uv sync` green; pytest/ruff/mypy configured in the same file
- [x] `app/config.py` — `Settings` + all limit constants
- [x] `app/errors.py` + `app/models/` + `app/logging_setup.py` — API contract frozen; frontend work can start in parallel
- [x] `app/security/url_validation.py` + tests (private IPs, homographs, userinfo, ports, `evil.com/github.com/o/r`)
- [x] `app/security/net_guard.py` + tests (host equality allowlist, resolved-IP check, redirect chain) — 389 cases across the two files; `tests/conftest.py` blocks the network for the whole session
- [x] `app/fetch/github.py` — the client that finally *calls* the guard: `follow_redirects=False`, one hop through `validate_download_url`, `assert_public_ip` before connecting, `trust_env=False`, and **no `Authorization` on the codeload request** — 88 tests, all 12 controls mutation-tested. The credential assertion the net_guard task left owing is written and passing (ADR-009)

- [x] `app/fetch/archive.py` + `app/analysis/deadline.py` + in-process malicious-tarball fixtures — traversal (incl. backslash spellings), symlinks, hardlinks, devices, bombs, malformed names, multi-root, and every resource cap — 136 tests, 24 controls mutation-tested

- [x] `app/security/secret_filter.py` + `path_safety.py` + tests — two golden lists plus a 20 000-path determinism sweep; path safety tested against real symlinks under `tmp_path`. 142 tests, all 18 controls mutation-tested. **Neither module has a caller** — wiring them is part of the two tasks below

## Next

- [ ] Apply `is_secret_path` in the analysis pipeline **and** independently in `/api/source`. Until both call it, the SECURITY.md row stays `Partial` and no `.env` is actually filtered
- [ ] Verify the tree-sitter spike — ABI load (**done: ABI 14**) and `QueryCursor` API (**done: imports**); `progress_callback` signature still unverified, confirm before writing the extractor
- [ ] `app/analysis/parser.py` — import extraction, incl. negative cases regex would get wrong
- [ ] `app/analysis/resolver.py` + `jsonc.py` — extensions, index files, `.js`→`.ts`, tsconfig `paths`, workspaces
- [ ] `app/analysis/graph_builder.py` + `pipeline.py` — `Deadline` exists (`app/analysis/deadline.py`); the pipeline that constructs one per request does not
- [ ] `app/api/` — routes, body-size middleware, rate limiter, concurrency gate, error handlers. Map `RequestValidationError` to a bare `INVALID_REQUEST`: pydantic's `detail` embeds the offending input
- [ ] Decide `Retry-After` on 429. `AppError.__init__` takes no arguments by design (nothing dynamic can reach a body), so the header must be set by the rate limiter at the response layer, not carried on the exception
- [ ] `POST /api/source` + HMAC tokens
- [ ] Scaffold `frontend/` (Vite 8, React 19.2, Tailwind v4 via `@tailwindcss/vite`, TypeScript **5.9.3**)
- [ ] Frontend types, zod schema, and a checked-in mock graph fixture so scene work can proceed without the backend
- [ ] `layout/structural.ts` + layout worker (incl. hand-written `d3-force-3d.d.ts`)
- [ ] `scene/` — instanced nodes, edge lines, camera rig, rAF-throttled picking, highlight buffers
- [ ] `ui/` — landing, loading, tooltip, inspector, search, tree panel, status bar
- [ ] Source preview via shiki `codeToTokens` → React spans (lazy-loaded)
- [ ] `docker-compose.yml` + both Dockerfiles; verify `docker compose up` from a clean clone
- [ ] README: architecture, security model, one-command run

## Later

- [ ] End-to-end pass over the PRD §15 matrix (small JS, medium TS, monorepo, circular imports, malicious URLs, oversized repo, XSS payload in source)
- [ ] Confirm bounded RSS under `docker stats` during a large analysis
- [ ] Confirm no repository content appears in container logs
- [ ] Remove CSP `style-src 'unsafe-inline'` via `sheet.insertRule()` color-class mapping
- [ ] Profile picking at 5000 nodes; add `three-mesh-bvh` only if measured to be needed
- [ ] `frameloop="demand"` as a battery optimization
- [ ] CI: run backend and frontend tests on push

## Blocked

- Nothing.
