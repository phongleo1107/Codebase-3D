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

- [x] **Pipeline (`app/analysis/pipeline.py`)** — one `Deadline` per request from `Settings`, `github.download_request()` sent and streamed, **`response.iter_raw()`** into `archive.iter_source_files`, commit SHA harvested from the tar root via the **`ArchiveInfo`** out-parameter (ADR-015). 60 tests plus 9 for the new channel; 33 controls mutation-tested, 32 caught, the survivor annotated. **The first code path that joins two modules.** *(Corrected 2026-08-31: this entry previously said `iter_bytes()` and `ArchiveRoot`, describing PR #3's parallel implementation rather than the code that merged. `iter_raw()` is deliberate — httpx transparently gunzips a `Content-Encoding` response, which would silently change what `MAX_DOWNLOAD_BYTES` and the compression-ratio denominator measure.)*
- [x] **`is_secret_path` applied** in the pipeline, before the bytes reach the parser, with the skips counted. The SECURITY.md secret-exposure row is **still `Partial`** and correctly so: that row describes a filter applied during analysis *and* re-applied independently at the point of serving, and `/api/explain` — the second caller — is Day 2 work. *(ADR-013, later the same day: `/api/explain` no longer exists. The second caller is now `/api/source`, which is post-MVP, so this row will not flip during the sprint. The analysis-time filter described here is unchanged and still correct.)*
- [x] **`extract_imports` called**, grammar picked by extension via the `_BY_EXTENSION` map. `MAX_SOURCE_FILES` enforced after the filters, truncation deterministic and flagged
- [x] **End-to-end smoke check against a real public repo** — `backend/scripts/smoke.py`, deliberately outside the pytest suite so `conftest.py`'s network block stays absolute (ADR-014). `p-limit`, `zustand`, and `ky` analyzed clean (6/50/54 files, 1.5–2.2 s); `RepositoryNotFoundError` and `NoSupportedFilesError` confirmed against real responses. No production code changed
- [ ] Apply `safe_relative_path` wherever a resolved path is used for anything beyond an in-memory dict key. **Still not applicable, and now for a sharper reason** — the resolver exists and resolves paths, but resolution is set membership against `RepositoryAnalysis.files`, so a resolved path is always a path the archive reader already validated and is only ever used as an in-memory key. Nothing touches disk (ADR-003), so there is no base directory to keep it inside of
- [ ] Add `.mts` and `.cts` to `pipeline._BY_EXTENSION`. ARCHITECTURE.md enumerates the extension list, so the omission was preserved deliberately rather than silently widened; it is a real gap — TS ESM files are counted as skipped and never parsed. **Now a two-file edit:** `resolver.EXTENSIONS` must gain them in the same change (a test asserts the two lists name the same extensions), along with the `.mjs` → `.mts` / `.cjs` → `.cts` rewrites that are absent today because their targets can never be nodes
- [ ] **Wire contract amended for ADR-013** — `c4` → `componentDiagram` (+ `MAX_C4_CHARS` → `MAX_COMPONENT_DIAGRAM_CHARS`), new `GraphNode.description` + `MAX_DESCRIPTION_CHARS`, docstrings de-LLM'd. The only place existing code contradicts the new scope
- [x] `app/analysis/descriptions.py` — a file's leading header comment (JSDoc `/** */`, `/* */`, or a run of `//`), normalized at extraction and carried on `SourceFile` to `GraphNode.description`. **Not from the tree** — a 4 KiB prefix scan, because a header comment is at byte 0 where JS has no lexical ambiguity to get wrong (ADR-020). Only the *normalizer* is shared with `ServiceEndpoint.summary`; route detection will locate its own comment from the tree it already has
- [x] `app/analysis/resolver.py` — **MVP scope: relative imports + bare-specifier-as-external only.** No `tsconfig.json` `paths`, no `baseUrl`, no workspace packages (Deferred — see bottom). Consumes `pipeline.RepositoryAnalysis`: a tuple of content-free `SourceFile` records, each carrying its `ImportRef`s with specifiers exactly as written. Resolution is set-membership against that same collection (ADR-016), so it cannot produce an edge with no node on the far end. **Done 2026-08-31** — 74 tests (1085 → 1159), 23 controls mutation-tested with 22 caught and the survivor annotated. Output is one `ResolvedImport` per `ImportRef` and the config seam is decided but not built (ADR-017). **Nothing calls it yet** — the graph builder is its first caller
- [x] `app/analysis/graph_builder.py` — nodes/edges per the existing model contract; deterministic sort; `stats.dependencies == len(edges)`. **Done 2026-08-31** — 36 tests (1159 → 1195), 22 controls mutation-tested with 21 caught and the survivor annotated. **The resolver's first caller.** Shape decisions recorded in ADR-018: node `id` *is* the path, the repository root is a node (`"."`, `parent: None`, named for the repo), directories are inferred from the parent hierarchy, and ordering is by path *components* rather than by path string. A file/directory path collision and a repeated path — both legal in a tarball — are resolved rather than raised
- [x] `MAX_IMPORTS` — cap the total import count in the pipeline. **Done 2026-08-31** — 9 tests (1195 → 1204), 6 controls mutation-tested with all 6 caught. Closes the import half of SECURITY.md's "Post-parse analysis runs outside the deadline": `resolve_imports` runs with no `Deadline` after the 60 s budget is spent, and nothing capped imports *per file*, so 1 002 000 imports cost 76.1 s to resolve — now 7.7 s. Recorded in ADR-019, including why a count beats a clock here. **The graph-size half is still open** — see `MAX_NODES`/`MAX_EDGES` below
- [ ] **Enforce `MAX_NODES` / `MAX_EDGES`, in the router.** The graph builder is deliberately uncapped (ADR-018) and nothing else caps it either, so this limit exists today only as a constant in `Settings` — which docs/SECURITY.md's banner explicitly says is not a control. **The hand-off is not free**: truncating the builder's returned tuples would immediately falsify `stats.dependencies == len(edges)` and the per-node `imports`/`importedBy` counters, all of which are computed inside the builder. The router must re-derive the stats after capping, or ask the builder for a smaller graph. Do not ship the routes without closing this
- [x] `app/analysis/routes.py` — route-detection queries for the service map. **Done 2026-08-31** — 59 tests plus 10 in the pipeline (1267 → 1336), 30 controls mutation-tested with 25 caught and all 5 survivors equivalent-by-construction and annotated. Ships **Express-style method calls** (`app.get/post/put/patch/delete/head/options/all`, any receiver — covers Express, Koa-router, Fastify, Hono) and the **Next.js App Router file convention** (`app/**/route.ts` exporting `GET`/`POST`/…). **Decorator-style routers (NestJS) are deferred** — see below — as is the Next.js Pages Router, whose default-export handler declares no method. Required a seam: `parser.py` now splits into `parse_source` (every guard, one tree) and `extract_imports` (the query), so route detection reads the tree the pipeline already built instead of parsing a second time and re-implementing five security guards (ADR-021). `ServiceEndpoint.summary` is the comment above the handler, via `descriptions.normalize_comment` as ADR-020 predicted
- [ ] `app/analysis/component_diagram.py` — deterministic Mermaid from the finished graph: top-level directories as containers, external packages as external systems, detected routes as the API surface. Pure function of the graph, so it is golden-file testable
- [ ] `app/api/` routes: `POST /api/analyze`, `GET /api/health`. Wire `AppError` → FastAPI exception handler; map `RequestValidationError` to a bare `INVALID_REQUEST` (pydantic's `detail` embeds the offending input and must never reach a body)
- [ ] Golden-file test over a whole `AnalyzeResponse` — newly possible under ADR-013, since with no LLM in the path the same commit must produce byte-identical JSON

## Day 2 — Frontend

> ADR-013 removed this day's entire backend half (`app/llm/`, `/api/explain`, prompt guardrails, HMAC issuance). **The freed time is buffer, not a slot to fill** — Day 1 is now large, and no archive byte has ever been fetched over a real network, so that risk is still unmeasured. Do not pull Deferred items forward until Day 1 is green end to end.
>
> **2026-09-01 (ADR-022, supersedes ADR-002/ADR-004):** the graph renders in 2D via Cytoscape.js, not a custom Three.js/React Three Fiber scene, and layout uses one of Cytoscape's built-in algorithms rather than the sphere-packing worker. The bullet below is updated; everything else on this day is unaffected.

- [ ] Scaffold `frontend/` (Vite, React 19, TypeScript 5.9.3, Tailwind v4)
- [ ] Frontend types + zod schema for `AnalyzeResponse`, incl. `serviceMap`, `componentDiagram`, and the optional `node.description`
- [ ] 2D Cytoscape.js graph: nodes/edges from the response, directory hierarchy as compound nodes (backs collapse/expand), one of Cytoscape's built-in layouts (candidates: `cola`/`elk`/`dagre`, chosen during implementation)
- [ ] Inspector panel: click node → `description` (already present in the response — no second request, no loading state, no client cache) + imports/importedBy
- [ ] Component diagram panel: render `componentDiagram` Mermaid source via the `mermaid` package
- [ ] Service map panel: grouped list, route → file → summary where one exists
- [ ] **Rendering guardrail:** `description`, `summary`, and the diagram source are repository content — render as React text nodes / hand the source to the Mermaid renderer. Never build an HTML string, never `dangerouslySetInnerHTML`. This is the one ADR-012 security rule that survives ADR-013, because the sink outlived the model

## Day 3 — Deploy + harden

- [ ] Backend → Railway/Render/Fly (ADR-011) — verify tree-sitter's native grammar wheels load in the target runtime before committing to it
- [ ] Frontend → Vercel, `VITE_API_URL` pointed at the backend, CORS locked to the Vercel domain
- [ ] Rate limiter + concurrency gate on `/api/analyze` (ADR-008 design) — bounds CPU and bandwidth; there is no LLM spend to bound any more
- [ ] Body-size middleware (4 KiB cap, `content-length` **and** chunked-body byte counting)
- [ ] Extend `backend/scripts/smoke.py` past ingestion once the graph exists: medium TS repo, one with circular imports, and one whose files carry JSDoc headers (to exercise descriptions on real comments rather than fixtures). The ingestion half already runs green against three real repositories
- [ ] **Adversarial run against a *hostile* repository.** The happy path is verified (`backend/scripts/smoke.py`, three real repositories) but **no security control has ever been triggered by data we did not construct** — every guard is fixture-only. This is the gap that matters most before a public deploy
- [ ] Security pass: malicious URL rejected, localhost/private-IP rejected, a `.env`-containing repo produces no leaked content in the graph or response, and a repository comment containing `<script>` / control characters / a megabyte of text yields a bounded plain-text description. *(The extractor half is done and tested — `tests/test_descriptions.py`, `tests/test_pipeline.py`. What is left is the same assertion over a whole HTTP response, and the rendering side.)*
- [ ] README: architecture summary, security model summary, how to run locally, link to the live Vercel URL

## Deferred (post-MVP, not cancelled)

- `tsconfig.json` `paths` / `baseUrl` / workspace-package resolution in `resolver.py`. **The seam is decided, not open**: ADR-017 fixes it as parse-in-the-pipeline-loop, carried already-narrowed on `RepositoryAnalysis` — not as raw bytes (breaks ADR-016's tested invariant) and not as a second archive pass (ADR-003 keeps nothing, so it means a second download). Needs a JSONC reader in the pipeline, and slots into `resolver._resolve_one` between the relative attempt and the external fallback
- ~~Force-directed layout refinement pass (ADR-004 phase two)~~ — moot: ADR-022 (2026-09-01) superseded ADR-004 outright in favor of a Cytoscape.js layout, so there is no worker phase two to schedule
- `POST /api/source` raw code viewer UI — **and now the whole ADR-007 mechanism with it**: the endpoint, HMAC token issuance and verification, and the serving-time re-application of `is_secret_path`, deferred as one unit. ADR-013 deleted `/api/explain`, which was the only MVP caller keeping that machinery in scope. `GraphNode.sourceToken` stays in the wire contract and stays `None`
- **Decorator-style route detection (NestJS `@Controller` + `@Get`), Fastify's `fastify.route({method, url})` object form, chained `router.route('/x').get(h)`, and the Next.js Pages Router.** Each is a real route shape `app/analysis/routes.py` misses today, and each has a test pinning the miss so it stays deliberate (ADR-021). The decorator case needs class-level prefix joining to be useful at all, and a wrong join reports a URL that does not exist — which is the one failure mode the module is built to avoid
- Any LLM/AI feature (ADR-012's model-written C4 diagrams, file explanations, route summaries). **Not deferred — removed.** Reinstating requires superseding ADR-013, not merely scheduling it
- Richer description sources: directory `README.md`, `package.json` `description` for monorepo packages. Considered and deliberately excluded from MVP — the file header comment alone is the scope (ADR-013)
- Search / tree panel, camera-focus-on-search
- `docker-compose.yml` + both Dockerfiles, verified `docker compose up` from a clean clone (ADR-001's original target; ADR-011 is the MVP-only substitute)
- End-to-end pass over the full PRD §15 test matrix (monorepo structure, huge repo, thousands of files, deep nesting, oversized individual file)
- Confirm bounded RSS under load; confirm no repository content in logs
- Decide whether `extract_imports` should report skips, so parser-level drops can be counted in stats. Today an oversized, binary, or pathologically malformed file is reported by yielding nothing, which is indistinguishable from a file with no imports — so it stays in the analysis as a node with real bytes, real lines, and zero imports, and is absent from `skipped`. A test pins the current behaviour so it is deliberate rather than incidental
- CSP `style-src 'unsafe-inline'` removal via `sheet.insertRule()`
- Picking performance profiling at 5000+ nodes; `three-mesh-bvh` only if measured necessary
- `frameloop="demand"` battery optimization
- CI (run backend and frontend tests on push)

## Blocked

- Nothing.
