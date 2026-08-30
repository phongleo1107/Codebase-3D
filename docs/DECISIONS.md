# Architecture Decisions

All ADRs below were agreed during planning on 2026-08-29, **before any code was written**. They are accepted as the design to build toward, not as descriptions of existing code.

---

## ADR-001 — Python/FastAPI backend, React/Three.js frontend, no database

### Decision
Two services: a Python 3.14 + FastAPI backend and a React 19 + TypeScript + Three.js frontend. No database, no auth, no persistent storage. Deployed with `docker compose up`.

### Reason
Follows the PRD's suggested stack. Analysis is stateless and each request is self-contained, so a database would add operational surface with nothing to store. Docker Compose gives a reproducible one-command run and a container boundary around the code that handles untrusted archives.

### Alternatives considered
- Single Node.js service (would allow reusing the TS compiler API for parsing, but the PRD specifies Python/FastAPI)
- Serverless functions (analysis can run 60s and needs bounded concurrency — a poor fit)
- Adding Postgres/Redis for caching (contradicts the PRD's no-database, no-retention goals)

### Status
Accepted

---

## ADR-002 — Custom React Three Fiber scene, not a force-graph library

### Decision
Build the visualization as a custom R3F scene using `InstancedMesh` for nodes and a single `LineSegments` for edges, rather than adopting `react-force-graph-3d` / `three-forcegraph`.

### Reason
Four requirements — directory collapse/expand aggregation, per-state de-emphasis, hierarchy-aware layout, and the specific restrained visual language the PRD demands — are precisely the things a wrapper library would have to be fought on. The PRD states the visualization *is* the product. `react-force-graph-3d` is also an imperative non-R3F canvas that carries `three` as a hard dependency (duplicate-instance risk) and declares `react: "*"`, so React 19 support is untested rather than guaranteed.

### Alternatives considered
- `react-force-graph-3d` — fastest to a first render, but styling and collapse/expand control are the blockers
- `three-forcegraph` directly — same wrapper problems, less React integration
- Raw Three.js without R3F — loses declarative composition and the drei helpers for camera control
- 2D libraries (Sigma.js, Cytoscape, D3) — the product is explicitly 3D

### Status
Accepted

---

## ADR-003 — Never write repository data to disk

### Decision
Stream the GitHub tarball from the network directly into `tarfile` in non-seeking mode, read only accepted members into memory one at a time, parse, and discard. No temporary directory, no extraction to disk.

### Reason
This eliminates path traversal, symlink escape, hardlink escape, and extraction-directory TOCTOU **as a class**, because there is no write syscall to exploit. It is strictly stronger than "resolve paths and verify they stay inside the temp dir." It also makes the PRD's "temporary data is deleted after analysis, including on error or timeout" trivially true — there is no cleanup step that a crash or SIGKILL can skip. Peak memory becomes bounded by one file plus the graph, not by repository size, and the container can run read-only.

Logical member paths are still validated in full, because they become node IDs and are echoed back to `/api/source`. `safe_relative_path()` is still implemented and tested so the guarantee holds if disk I/O is ever added.

### Alternatives considered
- Extract to a temp directory with canonical-path validation (the conventional approach; strictly weaker and needs reliable cleanup)
- `git clone --depth 1` (requires `subprocess`, and git honors `.gitattributes` filters and `core.*` config — attacker-influenced behavior)
- Zip archives (need random access, so they cannot be streamed and aborted cheaply)

### Status
Accepted

---

## ADR-004 — Hierarchy-aware layout computed in a worker, then frozen

### Decision
Two-phase client-side layout in a Web Worker: deterministic nested Fibonacci-sphere placement over the directory tree, then an anchored force pass where import edges only bend nodes away from their structural position. Run to completion, transfer positions, then freeze — the render loop never runs physics.

### Reason
A pure force simulation over 3000 nodes is a visually mushy blob in which directory structure is imperceptible, and it reshuffles the entire scene on every collapse/expand. Anchoring to a structural position keeps the hierarchy legible while still letting dependencies inform placement. Freezing means the scene is a handful of static draw calls, so 60fps is free rather than fought for. A collapsed directory's aggregate node lands exactly where its children were orbiting, so collapse is a local contraction and nothing else moves.

Layout stays on the client because it is a presentation concern; putting it in the backend would make the API own a visual decision.

### Alternatives considered
- Pure client-side `d3-force-3d` (simpler, but mushy and unstable across collapse)
- Server-precomputed positions (backend owns a visual concern; no benefit)
- Live simulation in the render loop (needless per-frame cost for a static graph)

### Status
Accepted

---

## ADR-005 — External packages are not graph nodes

### Decision
Bare specifiers such as `react` or `node:fs` do not become graph nodes. They are recorded as `externalImports` / `unresolvedImports` counts on the importing file node and aggregated into response stats.

### Reason
The PRD fixes the node type to `"directory" | "file"`, so an `"external"` type would break the declared contract. Independently, on any real repository the popular packages become mega-hubs that dominate the force layout and destroy the structural signal the product exists to show.

### Alternatives considered
- A third `"external"` node type (contract break, and visually harmful)
- Dropping the information entirely (the inspector genuinely benefits from listing external dependencies)

### Status
Accepted

---

## ADR-006 — Directory hierarchy on a `parent` field, not as edges

### Decision
Each node carries `parent: string | null`. Edges are exclusively import relationships.

### Reason
The PRD fixes `relationship` to `"imports"`, so a `"contains"` edge type would break the contract and make `stats.dependencies != len(edges)`. It would also force the frontend to filter the edge array to separate structure from semantics. A `parent` field builds the tree in one pass and is O(1) to consume.

### Alternatives considered
- A `"contains"` edge relationship
- A separate `tree` object alongside `nodes` (redundant — `parent` already encodes it)

### Status
Accepted

---

## ADR-007 — Zero-retention source preview via `raw.githubusercontent.com`, gated by an HMAC token

### Decision
`POST /api/source` re-fetches a single file from `raw.githubusercontent.com` at the commit SHA pinned during analysis. Nothing is cached. Access requires an HMAC token that the analyzer emits on each file node, and the same secret-file filter is re-applied server-side.

### Reason
Embedding source for every file would make the analyze response tens of megabytes to serve a panel the user opens on a fraction of a percent of files. Keeping the extracted repository alive behind a TTL would reintroduce exactly the retention the PRD asks us to avoid. Re-fetching honors "processed temporarily and not stored" literally, at the cost of one round-trip per file opened.

A naive version of this endpoint is an arbitrary-file proxy, so it needs two independent gates. The HMAC makes it *provably impossible* to request a path the analyzer did not itself approve, and re-running the deterministic secret filter from the shared module means even a forged token cannot extract `.env`. Pinning to the SHA also guarantees the preview matches the graph even if the branch moves mid-session.

### Alternatives considered
- Embed truncated source in the analyze response (payload size)
- Short-lived server-side cache of the extracted repo or tarball (faster, but weakens the retention claim; revisit only if preview latency proves unacceptable)

### Status
Accepted

---

## ADR-008 — Hand-rolled rate limiter instead of `slowapi`

### Decision
Implement per-IP sliding-window rate limiting and a global concurrency semaphore in-process (~40 lines), rather than adding `slowapi`.

### Reason
`slowapi`'s real value is its pluggable Redis/Memcached storage backends, which are irrelevant with no datastore — its in-memory backend is single-process, exactly like ours. It is thinly maintained (roughly annual releases, dev dependencies pinned to long-superseded FastAPI/Starlette) and untested against current Starlette and Python 3.14. A local implementation is fewer moving parts, fully unit-testable, and gives exact control over the 429 body and `Retry-After`.

Revisit if the service is ever scaled to multiple processes, at which point a shared store becomes necessary regardless.

### Alternatives considered
- `slowapi` (maintenance and compatibility risk)
- `fastapi-limiter` (requires Redis)
- `asgi-ratelimit` (unmaintained since 2022)

### Status
Accepted

---

## ADR-009 — The GitHub token is a per-request header, never a client default

### Decision
`app/fetch/github.py` builds its `httpx.Client` with no `Authorization` header. The token is passed as a per-request header on the two calls to `api.github.com` and nowhere else. The download request is constructed by `download_request()`, which additionally pops any inherited `Authorization` before returning the request.

### Reason
docs/SECURITY.md requires that the tarball download carry no credential, so that a bypass of the redirect host allowlist cannot also hand the operator's token to the attacker's host. The obvious implementation of that rule — set the header on the client for the API call, delete it before the download — makes the credential's absence depend on a `del` that a future refactor can drop, reorder, or skip on an early-return path. Making the header per-request inverts the default: a request carries the token only if a call site names it, and the only call site that names it targets `api.github.com`.

This is the "eliminate the vulnerability class architecturally rather than defend against it procedurally" rule from CLAUDE.md applied to one header. The `pop` in `download_request` is retained as belt-and-braces for a caller-supplied client this module did not build.

### Alternatives considered
- Set on the client, delete before the download (the sequence-dependent version this replaces)
- Two separate clients, one authenticated and one not (more connections, and the invariant becomes "use the right client", which is the same class of mistake)

### Status
Accepted

## ADR-010 — Parser cost is bounded structurally; `progress_callback` is not used

### Decision
`app/analysis/parser.py` calls `Parser.parse(source)` with a `bytes` source and **no `progress_callback`**. A pathological file is refused by two structural bounds instead: `MAX_PARSE_BYTES` (1 MiB) before parsing, and `_is_pathological` — a cap on the width of any ERROR node (`MAX_ERROR_NODE_CHILDREN`, 1000) with a walk budget (`MAX_PARSE_TREE_VISITS`, 100 000) as a backstop — between parsing and querying. The `Deadline` is checked either side of the parse but cannot interrupt one in progress.

### Reason
The plan of record was a deadline-driven `progress_callback`, described in docs/SECURITY.md as the thing that "aborts a pathological parse". **It does not work in tree-sitter 0.26.0**, which is the current release — there is no newer version to upgrade to, and `Parser.timeout_micros`, the older mechanism, has been removed. Measured on Python 3.14.7:

- With a `bytes` source the callback is silently ignored. tree-sitter emits `UserWarning: The progress_callback is ignored when parsing a bytestring` and never invokes it; returning `True` does not abort. Under this project's `filterwarnings = ["error"]` that warning is itself a test failure.
- With the chunked-reader source form the callback *is* wired up and **segfaults the interpreter** as soon as it fires, with or without an abort — the C stack lands in `PyObject_IsTrue` inside `_binding`. A segfault kills the worker process and no `except` can catch it, which is strictly worse than the hang it was meant to prevent.

So the intended control was unavailable, and the threat it addressed turned out to be misattributed anyway. Parsing is cheap and roughly linear: the worst hostile 1 MiB input measured parses in ~3.1 s, and most in well under a second. **The hang is in the query.** tree-sitter's query engine is quadratic in the *width* of an ERROR node, and a 1 MiB file of `(` recovers into one ERROR node with a million flat children: 0.23 s to parse, then roughly **eleven minutes** to query. Measured 0.09 / 0.95 / 3.97 s at 10 000 / 40 000 / 80 000 children — fourfold per doubling. Every pattern in the query costs the same, so it is the traversal and not the pattern, and nesting *depth* is not the trigger (a legitimate 20 000-deep expression queries in 4 ms).

That reframing is what makes a structural bound viable. The pathological shape is cheap to detect and unmistakable: across realistic files the widest ERROR node is 0–4 children, while every hostile input is as wide as the file. Detection is close to free because `has_error` is O(1) and false for almost every real file, and the walk prunes into error-bearing subtrees only, so the hostile case is settled after one or two node visits. Across a sweep of 21 hostile 1 MiB inputs the guarded worst case is ~3.3 s, and that remainder is parse time.

The guard deliberately keys on ERROR *width* rather than on `has_error`, because skipping every file with a syntax error would discard the partial recovery that motivated using tree-sitter at all — a file truncated mid-import still yields the imports above the break.

### Consequences
A single file can occupy a worker for a few seconds, and the 60 s `Deadline` stops the *next* file rather than the current one. That is a real weakening versus a working in-parse timeout, and it is accepted: the bound is a few seconds rather than unbounded, and `asyncio.wait_for` at the request layer remains as a net. If py-tree-sitter fixes the segfault, `progress_callback` becomes the better mechanism and should be reinstated — **re-test both source forms before trusting it**.

### Alternatives considered
- **Chunked-reader source plus `progress_callback`** — segfaults. This is the version the plan assumed.
- **Skip any file where `has_error` is true** — one O(1) check, but it drops real imports from every file using syntax the grammar does not know, which is common enough in real repositories to matter.
- **Skip any error tree over a node-count threshold** (`descendant_count` is O(1)) — cheaper than the walk, but it discards a large legitimate file that has one trailing syntax error, for the same reason as above.
- **Run the parse in a subprocess with a kill timer** — genuinely preemptive, but it means a process pool, serializing file bytes across a pipe, and a new failure mode per file, to bound something already bounded at a few seconds.

### Status
Accepted
