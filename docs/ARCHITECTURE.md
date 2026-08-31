# Architecture

> **Build status: the backend contract layer, the URL/egress security boundary, the GitHub client, the streaming archive reader, the secret and path-safety filters, the import extractor, and the analysis pipeline that joins them are implemented.** As of 2026-08-30 `app/config.py`, `app/errors.py`, `app/models/`, `app/logging_setup.py`, `app/security/url_validation.py`, `app/security/net_guard.py`, `app/security/secret_filter.py`, `app/security/path_safety.py`, `app/fetch/github.py`, `app/fetch/archive.py`, `app/analysis/deadline.py`, `app/analysis/parser.py`, and `app/analysis/pipeline.py` exist and are tested; everything else here is the agreed *target* design, recorded so it survives across sessions. **`app/analysis/pipeline.py` is what turned a pile of modules into a system**: it sends the download request, streams the response into the archive reader, applies the secret filter, and parses each file, on one `Deadline` per request. `safe_relative_path` remains the only module with no caller, by design — nothing writes to disk. **As of 2026-08-31 the ingestion path has been exercised against real GitHub repositories** (`backend/scripts/smoke.py`), so the design below is no longer only fixture-verified — though the happy path is all that real data has touched. There is still no resolver, no graph builder, no routing, and no frontend, so nothing yet turns a specifier into an edge or an analysis into a response body. Every section carries a status marker; flip it to `Implemented` only when the code exists, and correct the design text if reality diverged.
>
> Legend: `Planned` · `In progress` · `Implemented`

## System Flow

```
User
 ↓
Frontend (React + Three.js)
 ↓  POST /api/analyze { repository_url }
FastAPI
 ↓
Repository Analyzer   (fetch → stream-extract → parse → resolve)
 ↓
Graph Model           (GraphNode[] / GraphEdge[] / Stats)
 ↓
3D Visualization      (worker layout → instanced R3F scene)
```

A second, independent path serves the inspector's source preview:

```
Frontend  →  POST /api/source { repository_url, commit_sha, path, token }
          →  raw.githubusercontent.com @ pinned SHA  →  read-only viewer
```

## Backend — `backend/` · *In progress*

Python 3.14, FastAPI, Pydantic v2 (pure v2 only — `pydantic.v1` is incompatible with 3.14).

| Module | Responsibility | Status |
|---|---|---|
| `app/config.py` | `Settings` + every limit constant | Implemented |
| `app/errors.py` | Error code enum, `AppError` hierarchy, response mapping | Implemented |
| `app/logging_setup.py` | JSON logs + redaction filter | Implemented |
| `app/models/` | Pydantic request/response schemas | Implemented |
| `app/api/` | Routes (`analyze`, `source`, `health`), middleware, rate limiter, concurrency gate | Planned |
| `app/security/` | URL validation, network guard, secret filter, path safety, HMAC tokens | **In progress** — `url_validation.py`, `net_guard.py`, `secret_filter.py`, `path_safety.py` Implemented; HMAC tokens Planned. The first two are called by `fetch/github.py`; `secret_filter.py` is called by `analysis/pipeline.py` and still needs its second call site in `/api/source`; `path_safety.py` has **no caller**, by design, since nothing writes to disk |
| `app/fetch/` | GitHub client, streaming archive reader | **Implemented** — `github.py` (preflight + validated redirect) and `archive.py` (streaming extraction + member validation). Joined by `analysis/pipeline.py`, which sends the download and feeds the response to the reader |
| `app/analysis/` | Pipeline, deadline, file filter, tree-sitter parser, JSONC reader, module resolver, graph builder | **In progress** — `deadline.py`, `parser.py`, and `pipeline.py` Implemented (the file filter is `security/secret_filter.py` plus the extension map in `pipeline.py`); JSONC reader, resolver, and graph builder Planned |

Limits live in `Settings` and nowhere else — request models read them through
`get_settings()` at validation time rather than restating a number, so
tightening a limit in the environment is actually enforced at the boundary.

### Repository ingestion · *Implemented* — driven by `app/analysis/pipeline.py`

1. *Implemented* — Preflight `GET /repos/{owner}/{repo}` → default branch, canonical case, size. Reject oversized repos before any archive byte moves. `404` and `403` collapse to one opaque error so a configured token cannot become a private-repo existence oracle. The pipeline additionally refuses a repository the API reports as `private`, with the same opaque error: the collapse hides *existence*, but a configured token that can actually read a private repository would otherwise let anyone render it.
2. *Implemented* — `GET /repos/{owner}/{repo}/tarball/{ref}` with `follow_redirects=False`.
3. *Implemented* — Validate the single redirect (see [SECURITY.md](SECURITY.md)). The re-request is built **without credentials** by `download_request()` and sent by the pipeline.
4. *Implemented* — Stream the download into `tarfile` in non-seeking mode. **Nothing is written to disk** — see ADR-003. `app/fetch/archive.iter_source_files` takes the byte iterator and yields `(PurePosixPath, bytes)` for each acceptable regular file, with the root directory stripped.

   The gzip step is **ours, not `tarfile`'s**: `mode="r|"` over an explicit `gzip.GzipFile`, rather than `r|gz`. That is what creates a seam to meter the decompressed side at. It matters because a non-seeking `tarfile` must read *past* the body of every member — including ones the reader skips for being oversized — so a bomb whose payload is a single 1 GiB member yields no files at all and is invisible to any accounting that sums accepted members.

   The byte iterator is `response.iter_raw()`, **not** `response.iter_bytes()`. httpx transparently decodes a `Content-Encoding` before the caller sees anything, and every budget in `archive.py` — `MAX_DOWNLOAD_BYTES`, and the compression-ratio guard's denominator — is defined on wire bytes. `stream=True` is likewise a control: without it httpx buffers the whole body before returning, and the download cap would be enforced against bytes already in memory.
5. *Implemented* — The commit SHA is captured from the tar root directory name and pins all later source fetches. That root name is authoritative: `get_download_url` returns a SHA only when the redirect target happens to pin one, which it does not for a branch ref (`.../legacy.tar.gz/refs/heads/main`). `archive.py` validates the root against `^[A-Za-z0-9._-]+-([0-9a-f]{7,40})$`, requires it to be identical across members, and writes the captured group to the caller's `ArchiveInfo` (ADR-011) as soon as the first accepted member establishes it — so it survives a caller that stops early at `MAX_SOURCE_FILES`.

The token is never a client-level header — see ADR-009.

### Analysis pipeline · *Implemented* — `app/analysis/pipeline.py`

`analyze_repository(RepoRef) -> RepositoryAnalysis` is the one code path that runs a repository. It constructs **exactly one `Deadline` per request** from `ANALYSIS_TIMEOUT_S` and threads that same frozen object into `iter_source_files` and every `extract_imports` call, so no stage can extend its own budget. Between them the two consumers check it once per archive member and twice per file, which brackets every unit of work in the loop; the pipeline adds no third check.

**The deadline stops the next unit of work, not the current one** (ADR-010). There is no in-parse timeout in tree-sitter 0.26.0, so a single hostile file can hold a worker for a few seconds — ~3.3 s for the worst of a 21-input sweep — after the budget has expired.

Per member, in this order: `is_secret_path` (which also excludes `node_modules`, `dist`, and `build`), then the extension → grammar map, then the `MAX_SOURCE_FILES` cap. The cap is checked **after** the filters, so it bounds files that would become nodes rather than members the archive happened to contain, and it `break`s — abandoning the rest of the download — rather than continuing. It is a different limit at a different layer from `archive.py`'s 50 000-member cap: exceeding that one rejects the whole archive, exceeding this one sets `truncated`.

Skips are counted here because nothing below can: `archive.py` tallies its own and only logged them, and `parser.py` logs a reason and returns nothing. The published counts are files that produced **no node** — archive-dropped members (directories excluded, since they are not files), secret-filtered paths, and unsupported extensions. A file the *parser* gave up on is still a node with zero imports, so it is not counted; that blind spot is recorded in [CURRENT_STATE.md](CURRENT_STATE.md).

Output is `RepositoryAnalysis` (ADR-012): repository coordinates, commit SHA, a tuple of content-free `SourceFile` records in archive order, the skip tally, and `truncated`. No resolution, no sorting, no graph.

### Source parsing · *Implemented* — `app/analysis/parser.py`

tree-sitter with the TypeScript and TSX grammars. The **TSX grammar is a superset that parses plain JS/JSX**, so `.tsx .js .jsx .mjs .cjs` all use it; `.ts` needs the TypeScript grammar, whose `<T>expr` type assertion TSX reads as a JSX tag. `extract_imports` takes the `Language` as a parameter; choosing it by extension is `pipeline.py`'s job (`_BY_EXTENSION`).

That split is not cosmetic, and the cost of getting it wrong is silent. Measured on tree-sitter-typescript 0.23.2: a `.ts` file containing `const x = <Foo>bar;` between two imports yields both imports under the TypeScript grammar and **only the first** under TSX, because the phantom JSX element swallows the rest of the file into an ERROR node. It is symmetric — a genuine `<div>` element loses the second import the same way under the TypeScript grammar. Both directions are pinned by test. `.mts` and `.cts` are deliberately outside the v1 set.

A single query captures ESM imports, side-effect imports, `import type`, `export … from`, `export * from`, `import x = require()`, dynamic `import()`, and `require()`. Predicate filtering for `require` happens in Python rather than via `#eq?` — and it has to run over `QueryCursor.matches()`, because `captures()` returns the callees and the strings as two independently ordered lists with the match association discarded.

Extraction stops at the specifier: `extract_imports` yields `(specifier, line)` with the specifier exactly as written, 0-indexed line. Resolution is the next stage's job.

Parsing never aborts the run: oversized, binary, undecodable, and malformed files are skipped with a fixed-literal reason. A recoverable syntax error is **not** fatal — imports found before the error are still harvested, which is precisely why the hang guard keys on the width of an ERROR node rather than on `has_error`. The one exception that propagates is `AnalysisTimeoutError`, which describes the run rather than the file.

**There is no in-parse timeout.** `progress_callback` is unusable in tree-sitter 0.26.0 (ignored for a `bytes` source, segfault for a callback source) and `timeout_micros` was removed, so per-file cost is bounded structurally — by `MAX_PARSE_BYTES` on the way in and by a pathological-parse-tree guard that refuses the shape which makes the *query* quadratic. See ADR-010 and docs/SECURITY.md.

*Skips are logged, never counted here — the counting belongs to `pipeline.py`. Note the seam's limitation: a skip is reported by yielding nothing, so the pipeline cannot tell it from a file with no imports, and a parser-skipped file remains a node with zero imports.*

### Dependency extraction / resolution · *Planned*

Resolution is pure set-membership against `RepositoryAnalysis.files` — the files the pipeline actually parsed, not every member the archive contained (ADR-012). No filesystem access, so it can only ever produce a real file, and because the target set *is* the node set it cannot produce an edge with no node on the far end. Order: relative → tsconfig `paths` → `baseUrl` → workspace packages → external. TS ESM `.js`→`.ts` mapping is tried before literal `.js`. `tsconfig.json` is parsed as JSONC — and note the pipeline does not currently harvest config files, so collecting them is part of this step's work.

**External packages are not graph nodes** (ADR-005). They are recorded as counts on the importing file node and aggregated into stats.

### Graph model · *Implemented* — `app/models/graph.py`, `app/models/api.py`

Matches the PRD contract. `type` is `"directory" | "file"`; `relationship` is `"imports"`. Directory hierarchy travels on a **`parent` field on the node**, not on edges (ADR-006), so `stats.dependencies == len(edges)` holds exactly.

Nodes carry the metadata the frontend needs to size and color without recomputation: `bytes`, `loc`, `language`, `imports`, `importedBy`, `depth`, and for directories `fileCount` / `totalBytes`.

Every model sets `extra="forbid"`. `parent` is required rather than defaulted — the analyzer must state it for every node, with `None` reserved for the root.

Output is deterministic — nodes sorted by path, edges sorted by `(source, target)`, deduped, self-edges dropped. The same commit produces byte-identical JSON, which is what makes golden-file tests possible. **The models cannot enforce that**: sorting, dedup, and the `stats.dependencies == len(edges)` invariant are the graph builder's job, and it does not exist yet.

## Frontend — `frontend/` · *Planned*

React 19, TypeScript, Vite, Three.js via React Three Fiber, Tailwind v4, Zustand.

| Directory | Responsibility |
|---|---|
| `src/api/` | Client + zod schemas; validates and caps every response |
| `src/graph/` | Normalization, adjacency, collapse/expand transform, search |
| `src/layout/` | Structural placement, force refinement, layout Web Worker |
| `src/scene/` | R3F canvas, instanced nodes, edge lines, camera rig, picking, highlight |
| `src/ui/` | Landing, loading, tooltip, inspector, source preview, search, tree, status bar |
| `src/store/` | Zustand store |

### Rendering · *Planned*

A custom R3F scene, not a force-graph library (ADR-002). Roughly four draw calls: one `InstancedMesh` for files, one for directory shells, one `LineSegments` for all edges, and text labels for shallow directories only.

### Layout · *Planned*

Two phases, both in a Web Worker, then frozen (ADR-004): deterministic nested-sphere placement over the directory tree, then an anchored force pass where imports only bend nodes away from their structural position. The render loop never runs physics.

### State discipline · *Planned*

**React owns *what graph and what is selected*; Three owns *what it looks like right now*.** Selection and graph shape live in the store; hover is subscribed by exactly one DOM leaf; per-frame data (instance matrices, colors, camera) is mutated imperatively through refs. The canvas subtree does not re-render on hover.

## API Boundaries · *Planned*

| Endpoint | Purpose |
|---|---|
| `POST /api/analyze` | `{repository_url}` → `{repository, nodes, edges, stats}` |
| `POST /api/source` | `{repository_url, commit_sha, path, token}` → single file content |
| `GET /api/health` | Liveness |

Errors are always `{"error": {"code", "message", "requestId"}}` — exactly those three keys. `POST` is used for source so repository paths never reach access logs or a `Referer` header.

The error contract is implemented in `app/errors.py`: 14 codes, each with a fixed HTTP status and a **static** message. `AppError.__init__` takes no arguments, so a call site structurally cannot attach a path or an upstream string that would end up in a response body. The routes and the exception handler that returns these bodies are not written yet.

## Security Boundaries · *In progress*

Three trust transitions, each with an explicit validation layer:

1. **Client → API** — URL grammar validation (`security/url_validation.py`, *Implemented*), request body cap, rate limit, concurrency gate (*Planned*).
2. **GitHub → Analyzer** — redirect host allowlist and resolved-IP check (`security/net_guard.py`, *Implemented*, called on every hop by `fetch/github.py`); the size and visibility preflight (*Implemented*); download/extraction/ratio limits and per-member path rules (`fetch/archive.py`, *Implemented*); secret filter (`security/secret_filter.py`, *Implemented and applied* by `analysis/pipeline.py` — still owed its second, independent call site in `/api/source`).
3. **API → Browser** — zod validation with hard caps; source rendered as text nodes, never as an HTML string (*Planned*).

Every `security/` module is a pure function — none opens a socket, and `path_safety.py` is the only one that touches the filesystem (it resolves paths; it does not read or write). `app/fetch/github.py` is what turns the egress guard into a control: it is the only module that opens a socket, and it calls both guard functions before returning any URL. `app/analysis/pipeline.py` does the same for the secret filter, applying it to every path the archive yields. `path_safety.py` is still waiting for whatever first needs disk, and by ADR-003 nothing should.

Detail and threat mapping in [SECURITY.md](SECURITY.md).

## External Services

| Service | Use | Auth |
|---|---|---|
| `api.github.com` | Repo metadata, tarball redirect | Optional `GITHUB_TOKEN` (rate limits only) |
| `codeload.github.com` | Tarball download | **None** — credentials deliberately withheld (ADR-009) |
| `raw.githubusercontent.com` | Single-file source preview | None |

No database, no cache server, no queue, no third-party analytics.
