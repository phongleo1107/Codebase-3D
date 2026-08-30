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

- [x] `app/fetch/archive.py` + `app/analysis/deadline.py` + in-process malicious-tarball fixtures — traversal (incl. backslash spellings), symlinks, hardlinks, devices, bombs, malformed names, multi-root, and every resource cap — 142 tests, 24 controls mutation-tested

- [x] `app/security/secret_filter.py` + `path_safety.py` + tests — two golden lists plus a 20 000-path determinism sweep; path safety tested against real symlinks under `tmp_path`. 142 tests, all 18 controls mutation-tested. **Neither module has a caller** — wiring them is part of the two tasks below

- [x] Verify the tree-sitter spike — ABI load (**ABI 14**), `QueryCursor` API (**present**), and `progress_callback` (**unusable — ignored for a bytes source, segfaults for a callback source; ADR-010**)

- [x] `app/analysis/parser.py` — import extraction, incl. the negative cases a regex gets wrong — 75 tests, 26 controls mutation-tested

- [x] `app/analysis/pipeline.py` — **the join**: one `Deadline` per request threaded into both consumers, `download_request()` actually sent with `stream=True`, `response.iter_raw()` into `iter_source_files`, commit SHA out of the archive root (ADR-011), `is_secret_path` on every path, grammar by extension, `MAX_SOURCE_FILES` as the parse cap, skips counted. 60 tests plus 9 for the new `ArchiveInfo` channel; 33 controls mutation-tested, 32 caught, the survivor annotated. Output contract is ADR-012

## Next

- [ ] **The second `is_secret_path` call site, in `/api/source`.** The pipeline applies it during analysis; the SECURITY.md row describes it applied *independently* in both places and stays `Partial` until the endpoint exists. A `.env` is filtered out of the graph today, but nothing yet stops a future source endpoint from serving one
- [ ] `app/analysis/resolver.py` + `jsonc.py` — extensions, index files, `.js`→`.ts`, tsconfig `paths`, workspaces. **Includes harvesting the config files themselves**: the pipeline collects only source files, so `tsconfig.json` and workspace manifests are not yet read (`MAX_CONFIG_FILES` is in `Settings` waiting)
- [ ] `app/analysis/graph_builder.py` — nodes, `parent` hierarchy, external/unresolved counts, `MAX_NODES`/`MAX_EDGES`, and the determinism the pipeline deliberately leaves to it: sorting, dedup, self-edge removal, `stats.dependencies == len(edges)`
- [ ] `app/api/` — routes, body-size middleware, rate limiter, concurrency gate, error handlers. Map `RequestValidationError` to a bare `INVALID_REQUEST`: pydantic's `detail` embeds the offending input. Also where `analyze_repository` gets its worker thread and its `asyncio.wait_for` net
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
- [ ] **Run the pipeline against a real GitHub repository.** Everything is respx and in-process tarballs today; real codeload responses, redirect shapes, chunk sizes, and timing are unverified
- [ ] Decide whether `extract_imports` should report skips, so parser-level drops can be counted in stats (today an unparseable file is a node with zero imports)
- [ ] Confirm bounded RSS under `docker stats` during a large analysis
- [ ] Confirm no repository content appears in container logs
- [ ] Remove CSP `style-src 'unsafe-inline'` via `sheet.insertRule()` color-class mapping
- [ ] Profile picking at 5000 nodes; add `three-mesh-bvh` only if measured to be needed
- [ ] `frameloop="demand"` as a battery optimization
- [ ] CI: run backend and frontend tests on push

## Blocked

- Nothing.
