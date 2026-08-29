# TODO

Priority order: security → correctness → core functionality → performance → UX → nice-to-have.

## Now

- [x] Add `.gitignore` (`.venv/`, `__pycache__/`, `node_modules/`, `dist/`, `.env`) and stage `PRD.md` — *staged, not yet committed*
- [x] Scaffold `backend/pyproject.toml` with pinned deps and `requires-python >=3.14,<3.15` — `uv sync` green; pytest/ruff/mypy configured in the same file
- [x] `app/config.py` — `Settings` + all limit constants
- [x] `app/errors.py` + `app/models/` + `app/logging_setup.py` — API contract frozen; frontend work can start in parallel
- [ ] `app/security/url_validation.py` + tests (private IPs, homographs, userinfo, ports, `evil.com/github.com/o/r`)
- [ ] `app/security/net_guard.py` + tests (host equality allowlist, resolved-IP check, redirect chain, **no `Authorization` on the codeload request**)

## Next

- [ ] `app/fetch/archive.py` + in-process malicious-tarball fixtures — traversal, symlinks, hardlinks, bombs, malformed names, multi-root
- [ ] `app/security/secret_filter.py` + `path_safety.py` + tests
- [ ] Verify the tree-sitter spike — ABI load (**done: ABI 14**) and `QueryCursor` API (**done: imports**); `progress_callback` signature still unverified, confirm before writing the extractor
- [ ] `app/analysis/parser.py` — import extraction, incl. negative cases regex would get wrong
- [ ] `app/analysis/resolver.py` + `jsonc.py` — extensions, index files, `.js`→`.ts`, tsconfig `paths`, workspaces
- [ ] `app/analysis/graph_builder.py` + `pipeline.py` + deadline plumbing
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
