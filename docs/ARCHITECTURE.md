# Architecture

> **Build status: the backend contract layer, the URL/egress security boundary, the GitHub client, the streaming archive reader, the secret and path-safety filters, the import extractor, and the ingestion+parse pipeline that joins them are implemented.** As of 2026-08-31 `app/config.py`, `app/errors.py`, `app/models/`, `app/logging_setup.py`, `app/security/url_validation.py`, `app/security/net_guard.py`, `app/security/secret_filter.py`, `app/security/path_safety.py`, `app/fetch/github.py`, `app/fetch/archive.py`, `app/analysis/deadline.py`, `app/analysis/parser.py`, and `app/analysis/pipeline.py` exist and are tested; everything else here is the agreed *target* design, recorded so it survives across sessions. `app/analysis/pipeline.py` is the first module that calls another — it sends the download, streams it into the reader, applies `is_secret_path`, and parses what survives — so the fetch → extract → filter → parse half of the flow below is real, and **has been exercised against real GitHub repositories** (`backend/scripts/smoke.py`) rather than fixtures alone — on the happy path only. **Resolution, graph construction, routing, and the frontend still are not**, and `safe_relative_path` still has no caller. Every section carries a status marker; flip it to `Implemented` only when the code exists, and correct the design text if reality diverged.
>
> Legend: `Planned` · `In progress` · `Implemented`

## System Flow

> **MVP scope (2026-08-31, ADR-011/ADR-013):** the 3-day MVP ships the flow below with two simplifications recorded as amendments, not redesigns — the layout worker runs its deterministic sphere-packing phase only (force refinement deferred, ADR-004), and the frontend deploys to Vercel while the backend deploys to a separate persistent host (ADR-011), not `docker compose up` as a single unit.
>
> **ADR-013 removed the LLM narration stage that used to sit in this flow.** There is one request path now, not two: descriptions are extracted during the parse from bytes already in memory, so the second round-trip to `raw.githubusercontent.com` that `POST /api/explain` needed is gone with it.

```
User
 ↓
Frontend (React + Three.js)          [Vercel]
 ↓  POST /api/analyze { repository_url }
FastAPI                              [Railway / Render / Fly]
 ↓
Repository Analyzer   (fetch → stream-extract → parse → describe → resolve → detect routes)
 ↓
Graph Model           (GraphNode[] / GraphEdge[] / Stats / ServiceMap)
 ↓
Component diagram     (deterministic Mermaid, derived from the finished graph — ADR-013)
 ↓
3D Visualization      (worker layout → instanced R3F scene) + diagram/service-map panel
```

Every stage is deterministic, so the whole response is a pure function of the commit SHA — which is what makes a golden-file test over an entire `AnalyzeResponse` possible.

`POST /api/source` (raw code display) and the HMAC token mechanism behind it are *Planned*, deferred past the MVP — see ADR-007's two scope notes. Nothing in the MVP calls them: **file descriptions do not re-fetch anything**, they come from the archive bytes the pipeline already holds.

## Backend — `backend/` · *In progress*

Python 3.14, FastAPI, Pydantic v2 (pure v2 only — `pydantic.v1` is incompatible with 3.14).

| Module | Responsibility | Status |
|---|---|---|
| `app/config.py` | `Settings` + every limit constant | Implemented |
| `app/errors.py` | Error code enum, `AppError` hierarchy, response mapping | Implemented |
| `app/logging_setup.py` | JSON logs + redaction filter | Implemented |
| `app/models/` | Pydantic request/response schemas | Implemented |
| `app/api/` | Routes (`analyze`, `source`, `health`), middleware, rate limiter, concurrency gate | Planned |
| `app/security/` | URL validation, network guard, secret filter, path safety | **Implemented for MVP scope** — `url_validation.py`, `net_guard.py`, `secret_filter.py`, `path_safety.py`. The first two are called by `fetch/github.py`, `secret_filter.py` by `analysis/pipeline.py`; `path_safety.py` still has **no caller**, since all disk I/O remains unwritten. HMAC tokens are **deferred, not missing**: they belonged to `/api/source` and `/api/explain`, both now out of MVP scope (ADR-013) |
| `app/fetch/` | GitHub client, streaming archive reader | **Implemented** — `github.py` (preflight + validated redirect) and `archive.py` (streaming extraction + member validation, and the commit SHA from the archive root). `analysis/pipeline.py` calls both |
| `app/analysis/` | Pipeline, deadline, file filter, tree-sitter parser, description extractor, JSONC reader, module resolver, graph builder, route-detection query (service map), component-diagram generator | **In progress** — `deadline.py`, `parser.py`, `pipeline.py` Implemented; description extractor, resolver, graph builder, route detection, diagram generator Planned. `pipeline.analyze` returns `(path, content, imports)` per file plus the SHA and the skip/truncation counters, and stops there: nothing is resolved. MVP resolver is relative-imports + bare-specifier-as-external only — no `tsconfig.paths`/workspaces (deferred, see TODO.md) |

There is **no `app/llm/`**, and there must not be one. ADR-013 removed the LLM layer from the project; adding it back requires superseding that ADR.

Limits live in `Settings` and nowhere else — request models read them through
`get_settings()` at validation time rather than restating a number, so
tightening a limit in the environment is actually enforced at the boundary.

### Repository ingestion · *Implemented*

Driven end to end by `app/analysis/pipeline.analyze`, which owns the request's
single `Deadline` and threads it through every step below.

1. *Implemented* — Preflight `GET /repos/{owner}/{repo}` → default branch, canonical case, size. Reject oversized repos before any archive byte moves. `404` and `403` collapse to one opaque error so a configured token cannot become a private-repo existence oracle.
2. *Implemented* — `GET /repos/{owner}/{repo}/tarball/{ref}` with `follow_redirects=False`.
3. *Implemented* — Validate the single redirect (see [SECURITY.md](SECURITY.md)). The re-request is built **without credentials** by `download_request()` and sent by `analysis/pipeline._download`, which streams the response and always closes it, so an archive abandoned part-way releases the connection.
4. *Implemented* — Stream the download into `tarfile` in non-seeking mode. **Nothing is written to disk** — see ADR-003. `app/fetch/archive.iter_source_files` takes the byte iterator and yields `(PurePosixPath, bytes)` for each acceptable regular file, with the root directory stripped.

   The gzip step is **ours, not `tarfile`'s**: `mode="r|"` over an explicit `gzip.GzipFile`, rather than `r|gz`. That is what creates a seam to meter the decompressed side at. It matters because a non-seeking `tarfile` must read *past* the body of every member — including ones the reader skips for being oversized — so a bomb whose payload is a single 1 GiB member yields no files at all and is invisible to any accounting that sums accepted members.
5. *Implemented* — The commit SHA is harvested from the tar root directory name and pins all later source fetches. That root name is authoritative: `get_download_url` returns a SHA only when the redirect target happens to pin one, which it does not for a branch ref (`.../legacy.tar.gz/refs/heads/main`), and the pipeline **discards that hint rather than using it as a fallback** — a fallback is how a response ends up pinned to a commit the graph did not come from. `archive.ROOT_PATTERN` validates the root against `^[A-Za-z0-9._-]+-(?P<sha>[0-9a-f]{7,40})$`, requires it identical across members, and now *captures* the SHA into an `ArchiveRoot` out-parameter the caller reads after iteration. The SHA is taken from the same match that validated the root, so the check and the harvest cannot disagree; and because the hex run may not contain a hyphen and must reach the end of the name, a hyphenated repository name (`my-cool-repo-a1b2c3d`) is unambiguous.

   The out-parameter is deliberate. The SHA is one fact about the *archive*, not about any file, so putting it on every yielded tuple would repeat a constant once per file; and a generator cannot hand a return value to an ordinary `for` loop. It is `str | None` until the first regular file has been validated — a property inherent to a streaming reader rather than introduced by this choice — and the pipeline reads it only on the path where at least one file was yielded, which is exactly when it is set.

6. *Implemented* — `is_secret_path` is applied to every yielded path **before the bytes reach the parser**, then the grammar is chosen by extension, then `extract_imports` runs. `MAX_SOURCE_FILES` (3000) is counted over accepted files rather than archive members, so a repository of assets is not truncated by its assets; reaching it abandons the generator, and therefore the download. `NoSupportedFilesError` when nothing parseable survives.

The token is never a client-level header — see ADR-009.

### Source parsing · *Implemented* — `app/analysis/parser.py`

tree-sitter with the TypeScript and TSX grammars. The **TSX grammar is a superset that parses plain JS/JSX**, so `.tsx .js .jsx .mjs .cjs` all use it; `.ts` needs the TypeScript grammar, whose `<T>expr` type assertion TSX reads as a JSX tag. `extract_imports` takes the `Language` as a parameter; choosing it by extension is the caller's job, and that caller is `analysis/pipeline.grammar_for` — matching case-insensitively, so `Main.TS` is TypeScript. `.mts` and `.cts` are **not** in the list above and are therefore not analyzed; that is a gap, tracked in TODO.md, not a decision.

A single query captures ESM imports, side-effect imports, `import type`, `export … from`, `export * from`, `import x = require()`, dynamic `import()`, and `require()`. Predicate filtering for `require` happens in Python rather than via `#eq?` — and it has to run over `QueryCursor.matches()`, because `captures()` returns the callees and the strings as two independently ordered lists with the match association discarded.

Extraction stops at the specifier: `extract_imports` yields `(specifier, line)` with the specifier exactly as written, 0-indexed line. Resolution is the next stage's job.

Parsing never aborts the run: oversized, binary, undecodable, and malformed files are skipped with a fixed-literal reason. A recoverable syntax error is **not** fatal — imports found before the error are still harvested, which is precisely why the hang guard keys on the width of an ERROR node rather than on `has_error`. The one exception that propagates is `AnalysisTimeoutError`, which describes the run rather than the file.

**There is no in-parse timeout.** `progress_callback` is unusable in tree-sitter 0.26.0 (ignored for a `bytes` source, segfault for a callback source) and `timeout_micros` was removed, so per-file cost is bounded structurally — by `MAX_PARSE_BYTES` on the way in and by a pathological-parse-tree guard that refuses the shape which makes the *query* quadratic. See ADR-010 and docs/SECURITY.md.

*Skips are logged here but counted by the pipeline.* `Ingested.skipped_files` — the number `Stats.skippedFiles` will carry — counts every file the archive yielded that was not analyzed, so that `len(files) + skipped_files` equals what the reader produced whenever truncation did not fire. A file the *parser* skipped is deliberately **not** in that number: it still exists in the repository and still becomes a node, it simply contributes no edges.

### Description extraction · *Planned* — ADR-013

A node's description is **quoted from the repository, never generated.** The source is the file's own leading header comment: a JSDoc `/** … */`, a plain `/* … */` block, or an unbroken run of `//` lines, appearing before the first declaration. A file without one has `description: None`, which is the common case and not an error.

It runs inside the existing parse, over the tree `extract_imports` already built, from bytes already in memory. That is the whole reason `POST /api/explain` could be deleted rather than reimplemented: there is nothing to re-fetch, no commit to re-pin, and no token to authorize, because the content is already here. Cost is one look at the first node or two of the tree.

Normalization happens **at extraction**, before the text reaches a response model, because the output is untrusted repository content being placed in an API response for the first time:

- Comment markers are stripped — `/**`, `*/`, leading `*` on continuation lines, `//`.
- Control characters are stripped and whitespace is collapsed to single spaces. A description is a label, not a document; a comment containing ANSI escapes or a thousand newlines must not become a thousand-line tooltip.
- The result is truncated to `MAX_DESCRIPTION_CHARS`.
- Empty-after-normalization yields `None`, not `""` — the model requires `min_length=1`, and "the author wrote `/** */`" is the same fact as "the author wrote nothing".

A secret file can never produce a description, because `is_secret_path` runs before the file is parsed at all (ingestion step 6) — a property of the existing ordering, not a second check.

The same extractor serves `ServiceEndpoint.summary`, applied to the comment preceding a detected route handler rather than the file header.

### Dependency extraction / resolution · *Planned*

Resolution is pure set-membership against the file list observed in the archive — no filesystem access, so it can only ever produce a real file. Order: relative → tsconfig `paths` → `baseUrl` → workspace packages → external. TS ESM `.js`→`.ts` mapping is tried before literal `.js`. `tsconfig.json` is parsed as JSONC.

**External packages are not graph nodes** (ADR-005). They are recorded as counts on the importing file node and aggregated into stats.

### Graph model · *Implemented* — `app/models/graph.py`, `app/models/api.py`

Matches the PRD contract. `type` is `"directory" | "file"`; `relationship` is `"imports"`. Directory hierarchy travels on a **`parent` field on the node**, not on edges (ADR-006), so `stats.dependencies == len(edges)` holds exactly.

Nodes carry the metadata the frontend needs to size and color without recomputation: `bytes`, `loc`, `language`, `imports`, `importedBy`, `depth`, and for directories `fileCount` / `totalBytes`.

Every model sets `extra="forbid"`. `parent` is required rather than defaulted — the analyzer must state it for every node, with `None` reserved for the root.

Output is deterministic — nodes sorted by path, edges sorted by `(source, target)`, deduped, self-edges dropped. The same commit produces byte-identical JSON, which is what makes golden-file tests possible. **The models cannot enforce that**: sorting, dedup, and the `stats.dependencies == len(edges)` invariant are the graph builder's job, and it does not exist yet.

**MVP addition — *the fields exist; nothing populates them*.** `AnalyzeResponse` carries two top-level fields derived from the same deterministic graph, computed after nodes and edges are final and never feeding back into them: `serviceMap` (deterministically-detected API routes, as `ServiceEndpoint` objects) and `componentDiagram` (Mermaid source). `GraphNode.description` is a third such field, carried per node.

Under ADR-013 all three are **deterministic**, and their provenance is the repository itself rather than a model:

- `GraphNode.description` — the file's own leading header comment, extracted during the parse (see below). `None` when the file has none.
- `ServiceEndpoint.summary` — the comment immediately above the detected route handler. `None` when there is none.
- `componentDiagram` — Mermaid source generated from the finished graph: top-level directories as containers, external packages (ADR-005) as external systems, detected routes as the API surface. **Renamed from `c4`**, because a diagram derived from imports is a component sketch, not a C4 model — C4 encodes intent, which no import graph carries.

All three still **default to absent** — `serviceMap` to `[]`, the other two to `None`. The original reason (an LLM failing independently of the graph) is gone, but the encoding is kept and is still correct for a better reason: **a file that documents itself is the exception, not the rule.** Absent-by-default is the normal case for `description`, not a degraded one, and route detection succeeding while no comment exists above the handler is likewise ordinary. The frontend must render every one of these as optional.

Their size bounds (`MAX_SERVICE_ENDPOINTS`, `MAX_ENDPOINT_SUMMARY_CHARS`, `MAX_COMPONENT_DIAGRAM_CHARS`, `MAX_DESCRIPTION_CHARS`) live in `Settings` like every other limit and are read at validation time. They now bound **repository-authored text** rather than model output, which if anything strengthens the case for them: a comment is attacker-controlled and can be a megabyte long. **No code produces these fields yet**, so they bound nothing today.

## Frontend — `frontend/` · *Planned*

React 19, TypeScript, Vite, Three.js via React Three Fiber, Tailwind v4, Zustand.

| Directory | Responsibility |
|---|---|
| `src/api/` | Client + zod schemas; validates and caps every response |
| `src/graph/` | Normalization, adjacency, collapse/expand transform, search |
| `src/layout/` | Structural placement, force refinement, layout Web Worker |
| `src/scene/` | R3F canvas, instanced nodes, edge lines, camera rig, picking, highlight |
| `src/ui/` | Landing, loading, tooltip, inspector (description, imports/importedBy), component diagram panel (`mermaid`), service map panel, status bar. Search/tree panel and source preview are deferred post-MVP |
| `src/store/` | Zustand store |

### Rendering · *Planned*

A custom R3F scene, not a force-graph library (ADR-002). Roughly four draw calls: one `InstancedMesh` for files, one for directory shells, one `LineSegments` for all edges, and text labels for shallow directories only.

### Layout · *Planned*

Two phases, both in a Web Worker, then frozen (ADR-004): deterministic nested-sphere placement over the directory tree, then an anchored force pass where imports only bend nodes away from their structural position. The render loop never runs physics.

**MVP scope:** phase one only (sphere-packing). Force refinement is deferred, per ADR-004's 2026-08-31 note — the graph is still legible and fully deterministic without it.

### Component diagram and service map · *Planned*

Rendered client-side with the `mermaid` package, fed the `componentDiagram` field's Mermaid source verbatim from `/api/analyze`. The service map renders as a simple grouped list (route → file → summary, where a summary exists), sourced from the `serviceMap` field. Both are read-only display of server-computed text.

**Descriptions and summaries are repository content and render as text nodes only** — never assembled into an HTML string, never `dangerouslySetInnerHTML`, the same rule the PRD sets for source display. The backend caps and strips them (ADR-013), but the frontend does not rely on that: this is the second of two independent applications of the rule, not the only one.

### State discipline · *Planned*

**React owns *what graph and what is selected*; Three owns *what it looks like right now*.** Selection and graph shape live in the store; hover is subscribed by exactly one DOM leaf; per-frame data (instance matrices, colors, camera) is mutated imperatively through refs. The canvas subtree does not re-render on hover.

## API Boundaries · *Planned*

| Endpoint | Purpose | MVP status |
|---|---|---|
| `POST /api/analyze` | `{repository_url}` → `{repository, nodes, edges, stats, serviceMap, componentDiagram}` | In scope |
| `GET /api/health` | Liveness | In scope |
| `POST /api/source` | `{repository_url, commit_sha, path, token}` → raw single file content, for a code viewer UI | Deferred post-MVP (ADR-007's two scope notes), **together with the HMAC token mechanism** — nothing in the MVP issues or verifies a token |
| `POST /api/explain` | — | **Removed** (ADR-013). Descriptions ship inside `/api/analyze` as `GraphNode.description`, extracted during the parse; there is no second request path and no LLM |

The MVP is therefore **two endpoints**, and `/api/analyze` is the only one that touches the network.

Errors are always `{"error": {"code", "message", "requestId"}}` — exactly those three keys. `POST` is used for source so repository paths never reach access logs or a `Referer` header.

The error contract is implemented in `app/errors.py`: 14 codes, each with a fixed HTTP status and a **static** message. `AppError.__init__` takes no arguments, so a call site structurally cannot attach a path or an upstream string that would end up in a response body. The routes and the exception handler that returns these bodies are not written yet.

## Security Boundaries · *In progress*

Three trust transitions, each with an explicit validation layer:

1. **Client → API** — URL grammar validation (`security/url_validation.py`, *Implemented*), request body cap, rate limit, concurrency gate (*Planned*).
2. **GitHub → Analyzer** — redirect host allowlist and resolved-IP check (`security/net_guard.py`, *Implemented*, called on every hop by `fetch/github.py`); the size preflight (*Implemented*); download/extraction/ratio limits and per-member path rules (`fetch/archive.py`, *Implemented*); secret filter (`security/secret_filter.py`, *Implemented and applied* by `analysis/pipeline.py` before any file reaches the parser — but see SECURITY.md, which keeps that row `Partial` until a serving-time caller re-applies it independently. Under ADR-013 that caller is `/api/source`, which is post-MVP, so the row will not flip during this sprint. The analysis-time filter is unaffected, and it is what stops a `.env` from being parsed, becoming a node, or producing a description).
3. **API → Browser** — zod validation with hard caps; source rendered as text nodes, never as an HTML string (*Planned*).

Every `security/` module is a pure function — none opens a socket, and `path_safety.py` is the only one that touches the filesystem (it resolves paths; it does not read or write). `app/fetch/github.py` is what turns the egress guard into a control: it is the only module that opens a socket, and it calls both guard functions before returning any URL. `app/analysis/pipeline.py` does the same for `secret_filter.py`. `path_safety.py` is still waiting for whatever first needs disk — which, under ADR-003, is nothing.

Detail and threat mapping in [SECURITY.md](SECURITY.md).

## External Services

| Service | Use | Auth |
|---|---|---|
| `api.github.com` | Repo metadata, tarball redirect | Optional `GITHUB_TOKEN` (rate limits only) |
| `codeload.github.com` | Tarball download | **None** — credentials deliberately withheld (ADR-009) |
| `raw.githubusercontent.com` | Single-file fetch, backing `/api/source` — **not contacted in the MVP** (ADR-013 removed its only in-scope caller) | None |

No database, no cache server, no queue, no third-party analytics, **and no LLM or AI provider** (ADR-013). The MVP's entire outbound surface is two GitHub hosts.

## Deployment · *Planned* (ADR-011)

Frontend (Vite build) deploys to **Vercel**. Backend (FastAPI) deploys to a separate host that runs it as a persistent process — Railway, Render, or Fly, decided at implementation time — not Vercel's serverless Python runtime, which is a poor fit for a 60s analysis budget plus a streaming download plus a tree-sitter parse, and where the native grammar wheels may not load at all. `docker compose up` as a single-command local/self-hosted run remains the longer-term target (ADR-001) and should still be built once the split deployment is live and there is time to verify it.
