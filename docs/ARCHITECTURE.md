# Architecture

> **Build status: only the backend contract layer is implemented.** As of 2026-08-29 `app/config.py`, `app/errors.py`, `app/models/`, and `app/logging_setup.py` exist and are tested; everything else here is the agreed *target* design, recorded so it survives across sessions. Every section carries a status marker; flip it to `Implemented` only when the code exists, and correct the design text if reality diverged.
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
| `app/security/` | URL validation, network guard, secret filter, path safety, HMAC tokens | Planned |
| `app/fetch/` | GitHub client, streaming archive reader | Planned |
| `app/analysis/` | Pipeline, deadline, file filter, tree-sitter parser, JSONC reader, module resolver, graph builder | Planned |

Limits live in `Settings` and nowhere else — request models read them through
`get_settings()` at validation time rather than restating a number, so
tightening a limit in the environment is actually enforced at the boundary.

### Repository ingestion · *Planned*

1. Preflight `GET /repos/{owner}/{repo}` → default branch, canonical case, size. Reject oversized repos before any archive byte moves. `404` and `403` collapse to one opaque error so a configured token cannot become a private-repo existence oracle.
2. `GET /repos/{owner}/{repo}/tarball/{ref}` with `follow_redirects=False`.
3. Validate the single redirect (see [SECURITY.md](SECURITY.md)), then re-request **without credentials**.
4. Stream the gzip into `tarfile` in non-seeking mode (`r|gz`). **Nothing is written to disk** — see ADR-003.
5. The commit SHA is harvested from the tar root directory name and pins all later source fetches.

### Source parsing · *Planned*

tree-sitter with the TypeScript and TSX grammars. The **TSX grammar is a superset that parses plain JS/JSX**, so `.tsx .js .jsx .mjs .cjs` all use it and only one grammar package is needed.

A single query captures ESM imports, side-effect imports, `import type`, `export … from`, `export * from`, `import x = require()`, dynamic `import()`, and `require()`. Predicate filtering for `require` happens in Python rather than via `#eq?`.

Parsing never aborts the run: oversized, binary, undecodable, and malformed files are skipped with a recorded reason and counted in stats. A recoverable syntax error is not fatal — imports found before the error are still harvested.

### Dependency extraction / resolution · *Planned*

Resolution is pure set-membership against the file list observed in the archive — no filesystem access, so it can only ever produce a real file. Order: relative → tsconfig `paths` → `baseUrl` → workspace packages → external. TS ESM `.js`→`.ts` mapping is tried before literal `.js`. `tsconfig.json` is parsed as JSONC.

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

## Security Boundaries · *Planned*

Three trust transitions, each with an explicit validation layer:

1. **Client → API** — URL grammar validation, request body cap, rate limit, concurrency gate.
2. **GitHub → Analyzer** — redirect host allowlist, resolved-IP check, download/extraction/ratio limits, per-member path rules, secret filter.
3. **API → Browser** — zod validation with hard caps; source rendered as text nodes, never as an HTML string.

Detail and threat mapping in [SECURITY.md](SECURITY.md).

## External Services

| Service | Use | Auth |
|---|---|---|
| `api.github.com` | Repo metadata, tarball redirect | Optional `GITHUB_TOKEN` (rate limits only) |
| `codeload.github.com` | Tarball download | **None** — credentials deliberately withheld |
| `raw.githubusercontent.com` | Single-file source preview | None |

No database, no cache server, no queue, no third-party analytics.
