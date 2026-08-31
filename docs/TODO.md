# TODO

> **2026-08-31 — scope changed to a 3-day MVP sprint (ADR-011, ADR-012).** Priority order is now: security → the wired end-to-end pipeline → C4/service-map/file-explanation (the three features the sprint exists to deliver) → deploy. Items below are grouped by day, not by theme. Anything not scheduled in Day 1/2/3 is **Deferred**, not cancelled — see the note at the bottom.

> **2026-08-31, later the same day — the LLM layer is cut (ADR-013 supersedes ADR-012).** The three features stay; their implementation becomes deterministic. Descriptions are the file's own leading header comment, route summaries are the comment above the handler, and the diagram is generated from the graph (field renamed `c4` → `componentDiagram`). **`POST /api/explain` and the HMAC token mechanism are out of scope**, so Day 2 loses its entire backend half and Day 1 gains one small extractor. The freed time is deliberately left as buffer — see Day 2's note.

Priority order within each day: security → correctness → core functionality → performance → UX → nice-to-have.

## Done before the sprint (2026-08-29)

- [x] `.gitignore`, `pyproject.toml` (pinned deps, `requires-python >=3.14,<3.15`), `app/config.py`, `app/errors.py`, `app/models/`, `app/logging_setup.py` — contract layer frozen
- [x] `app/security/url_validation.py` + `app/security/net_guard.py` — 389 tests, SSRF/redirect/DNS-rebinding controls mutation-tested
- [x] `app/fetch/github.py` — preflight, validated single redirect, credential-free download request (ADR-009) — 88 tests, 12 controls mutation-tested
- [x] `app/fetch/archive.py` + `app/analysis/deadline.py` — streaming extraction, path/symlink/bomb/multi-root controls — 142 tests, 24 controls mutation-tested
- [x] `app/security/secret_filter.py` + `app/security/path_safety.py` — 142 tests, 18 controls mutation-tested. **No caller yet** — Day 1 wires both in
- [x] `app/analysis/parser.py` — import extraction via tree-sitter — 75 tests, 26 controls mutation-tested. **No caller yet** — Day 1 wires it in
- [x] tree-sitter spike verified: ABI 14, `QueryCursor` present, `progress_callback` unusable (ADR-010)

996 tests total, nothing joined to anything else yet. This was the sprint's starting point; Day 1's first three bullets below have since closed and the modules are joined.

## Day 1 — Backend: wire the pipeline, apply the orphaned security modules, detect routes

- [x] **Wire contract extended for ADR-012** — `ServiceEndpoint`, `AnalyzeResponse.serviceMap`, `AnalyzeResponse.c4`, three `Settings` limits, 17 tests (1013 total). Both fields default to absent, so `/api/analyze` can be built and shipped before the LLM layer exists and still return a valid response

- [x] **Pipeline (`app/analysis/pipeline.py`)** — one `Deadline` per request from `Settings`, `github.download_request()` sent and streamed, `response.iter_bytes()` into `archive.iter_source_files`, commit SHA harvested from the tar root via the new `ArchiveRoot` out-parameter. 58 tests (1013 → 1071); nine controls mutation-tested, all caught. **The first code path that joins two modules**
- [x] **`is_secret_path` applied** in the pipeline, before the bytes reach the parser, with the skips counted. The SECURITY.md secret-exposure row is **still `Partial`** and correctly so: that row describes a filter applied during analysis *and* re-applied independently at the point of serving, and `/api/explain` — the second caller — is Day 2 work. *(ADR-013, later the same day: `/api/explain` no longer exists. The second caller is now `/api/source`, which is post-MVP, so this row will not flip during the sprint. The analysis-time filter described here is unchanged and still correct.)*
- [x] **`extract_imports` called**, grammar picked by extension (`grammar_for`). `MAX_SOURCE_FILES` enforced, truncation deterministic and flagged
- [ ] Apply `safe_relative_path` wherever a resolved path is used for anything beyond an in-memory dict key. **Not applicable yet** — nothing touches disk (ADR-003) and no path is resolved until the resolver exists
- [ ] Add `.mts` and `.cts` to `pipeline.grammar_for`. ARCHITECTURE.md enumerates the extension list, so the omission was preserved deliberately rather than silently widened; it is a real gap — TS ESM files are counted as skipped and never parsed
- [ ] **Wire contract amended for ADR-013** — `c4` → `componentDiagram` (+ `MAX_C4_CHARS` → `MAX_COMPONENT_DIAGRAM_CHARS`), new `GraphNode.description` + `MAX_DESCRIPTION_CHARS`, docstrings de-LLM'd. The only place existing code contradicts the new scope
- [ ] `app/analysis/descriptions.py` — extract a file's leading header comment (JSDoc `/** */`, `/* */`, or a run of `//`) from the tree `extract_imports` already built. Normalize **at extraction**: strip comment markers, strip control characters, collapse whitespace, truncate to `MAX_DESCRIPTION_CHARS`, return `None` when empty. Same extractor serves `ServiceEndpoint.summary` off the comment above a route handler
- [ ] `app/analysis/resolver.py` — **MVP scope: relative imports + bare-specifier-as-external only.** No `tsconfig.json` `paths`, no `baseUrl`, no workspace packages (Deferred — see bottom). Consumes `pipeline.Ingested`: `(path, content, imports)` per file, specifiers exactly as written
- [ ] `app/analysis/graph_builder.py` — nodes/edges per the existing model contract; deterministic sort; `stats.dependencies == len(edges)`
- [ ] Route-detection tree-sitter query for the service map — Express `app.get/post/put/delete`, decorator-style routers, Next.js `app/api/*/route.ts` file convention. Deterministic, and now the *only* source of the service map
- [ ] `app/analysis/component_diagram.py` — deterministic Mermaid from the finished graph: top-level directories as containers, external packages as external systems, detected routes as the API surface. Pure function of the graph, so it is golden-file testable
- [ ] `app/api/` routes: `POST /api/analyze`, `GET /api/health`. Wire `AppError` → FastAPI exception handler; map `RequestValidationError` to a bare `INVALID_REQUEST` (pydantic's `detail` embeds the offending input and must never reach a body)
- [ ] Golden-file test over a whole `AnalyzeResponse` — newly possible under ADR-013, since with no LLM in the path the same commit must produce byte-identical JSON
- [ ] End-to-end smoke test against one real small public repo (first time any archive byte in this codebase comes from the network instead of a fixture)

## Day 2 — Frontend

> ADR-013 removed this day's entire backend half (`app/llm/`, `/api/explain`, prompt guardrails, HMAC issuance). **The freed time is buffer, not a slot to fill** — Day 1 is now large, and no archive byte has ever been fetched over a real network, so that risk is still unmeasured. Do not pull Deferred items forward until Day 1 is green end to end.

- [ ] Scaffold `frontend/` (Vite, React 19, TypeScript 5.9.3, Tailwind v4)
- [ ] Frontend types + zod schema for `AnalyzeResponse`, incl. `serviceMap`, `componentDiagram`, and the optional `node.description`
- [ ] 3D scene: instanced nodes, edge lines, camera rig — **layout is sphere-packing only for MVP** (ADR-004 scope note; force-refinement pass Deferred)
- [ ] Inspector panel: click node → `description` (already present in the response — no second request, no loading state, no client cache) + imports/importedBy
- [ ] Component diagram panel: render `componentDiagram` Mermaid source via the `mermaid` package
- [ ] Service map panel: grouped list, route → file → summary where one exists
- [ ] **Rendering guardrail:** `description`, `summary`, and the diagram source are repository content — render as React text nodes / hand the source to the Mermaid renderer. Never build an HTML string, never `dangerouslySetInnerHTML`. This is the one ADR-012 security rule that survives ADR-013, because the sink outlived the model

## Day 3 — Deploy + harden

- [ ] Backend → Railway/Render/Fly (ADR-011) — verify tree-sitter's native grammar wheels load in the target runtime before committing to it
- [ ] Frontend → Vercel, `VITE_API_URL` pointed at the backend, CORS locked to the Vercel domain
- [ ] Rate limiter + concurrency gate on `/api/analyze` (ADR-008 design) — bounds CPU and bandwidth; there is no LLM spend to bound any more
- [ ] Body-size middleware (4 KiB cap, `content-length` **and** chunked-body byte counting)
- [ ] Real-repo smoke test: small JS repo, medium TS repo, one with circular imports, and one whose files carry JSDoc headers (to exercise descriptions on real comments rather than fixtures)
- [ ] Security pass: malicious URL rejected, localhost/private-IP rejected, a `.env`-containing repo produces no leaked content in the graph or response, and a repository comment containing `<script>` / control characters / a megabyte of text yields a bounded plain-text description
- [ ] README: architecture summary, security model summary, how to run locally, link to the live Vercel URL

## Deferred (post-MVP, not cancelled)

- `tsconfig.json` `paths` / `baseUrl` / workspace-package resolution in `resolver.py`
- Force-directed layout refinement pass (ADR-004 phase two)
- `POST /api/source` raw code viewer UI — **and now the whole ADR-007 mechanism with it**: the endpoint, HMAC token issuance and verification, and the serving-time re-application of `is_secret_path`, deferred as one unit. ADR-013 deleted `/api/explain`, which was the only MVP caller keeping that machinery in scope. `GraphNode.sourceToken` stays in the wire contract and stays `None`
- Any LLM/AI feature (ADR-012's model-written C4 diagrams, file explanations, route summaries). **Not deferred — removed.** Reinstating requires superseding ADR-013, not merely scheduling it
- Richer description sources: directory `README.md`, `package.json` `description` for monorepo packages. Considered and deliberately excluded from MVP — the file header comment alone is the scope (ADR-013)
- Search / tree panel, camera-focus-on-search
- `docker-compose.yml` + both Dockerfiles, verified `docker compose up` from a clean clone (ADR-001's original target; ADR-011 is the MVP-only substitute)
- End-to-end pass over the full PRD §15 test matrix (monorepo structure, huge repo, thousands of files, deep nesting, oversized individual file)
- Confirm bounded RSS under load; confirm no repository content in logs
- CSP `style-src 'unsafe-inline'` removal via `sheet.insertRule()`
- Picking performance profiling at 5000+ nodes; `three-mesh-bvh` only if measured necessary
- `frameloop="demand"` battery optimization
- CI (run backend and frontend tests on push)

## Blocked

- Nothing.
