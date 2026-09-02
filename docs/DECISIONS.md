# Architecture Decisions

**ADR-001 … ADR-008** were agreed during planning on 2026-08-29, **before any code was written**. They are accepted as the design to build toward, not as descriptions of existing code.

**ADR-009 onward were written during implementation** and *do* describe existing code. Each records a decision forced by something discovered while building — a library that did not behave as the plan assumed (ADR-010), a scope cut made against a deadline (ADR-011 … ADR-013), a channel the plan left unspecified (ADR-015), or a seam that needed defining (ADR-016).

> **Numbering note (2026-08-31).** ADR-011 and ADR-012 were briefly used for two *different* decisions — the archive's commit-SHA channel and the pipeline's output contract — on the branch merged as PR #2. PR #3 landed its own ADR-011 … ADR-013 concurrently, and the merge kept PR #2's **code** alongside PR #3's **ADR log**, so `archive.py` and `pipeline.py` were left citing numbers that had come to mean something else entirely. The pushed numbers are authoritative and are **not** renumbered — history is never rewritten. The two displaced decisions are restored below as **ADR-015** and **ADR-016**, and the code references were corrected to match. If you find an old commit message or comment citing "ADR-011" for the SHA out-parameter or "ADR-012" for the content-free file list, it means ADR-015 and ADR-016 respectively.

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

> **Amended by [ADR-022](#adr-022--2d-cytoscapejs-graph-replaces-the-3d-react-three-fiber-scene-supersedes-adr-002-amends-adr-001-adr-004) (2026-09-01).** The frontend term of this decision changes from Three.js to Cytoscape.js (2D). The backend/no-database terms are unchanged.

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
Superseded by [ADR-022](#adr-022--2d-cytoscapejs-graph-replaces-the-3d-react-three-fiber-scene-supersedes-adr-002-amends-adr-001-adr-004) (2026-09-01). The product is no longer explicitly 3D, so the "2D libraries — the product is explicitly 3D" rejection below no longer holds; Cytoscape.js is adopted instead. Kept for history.

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
Superseded by [ADR-022](#adr-022--2d-cytoscapejs-graph-replaces-the-3d-react-three-fiber-scene-supersedes-adr-002-amends-adr-001-adr-004) (2026-09-01). Cytoscape.js's own layout algorithms replace this custom worker; there is no sphere to pack once the scene is 2D. Kept for history.

> **MVP scope note (2026-08-31, superseded):** under the 3-day deadline, only the first phase — deterministic nested-sphere placement over the directory tree — ships initially. The anchored force-refinement pass is deferred, not abandoned; the graph is still legible and fully deterministic without it, and adding force refinement later is additive to this same worker, not a redesign.

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

> **MVP scope note (2026-08-31):** the raw source *viewer* UI is deferred post-MVP — three days does not fit a full read-only code panel alongside everything else. The fetch-and-filter mechanism this ADR specifies ships anyway, because [ADR-012](#adr-012--llm-generated-c4-diagrams-service-map-summaries-and-file-explanations-over-untrusted-repository-content) reuses it verbatim as the backing fetch for `POST /api/explain`. The HMAC token and the secret-filter re-application are not optional extras added later — they are load-bearing for the first caller this endpoint actually gets.
>
> **Second scope note (2026-08-31, later the same day — [ADR-013](#adr-013--repository-authored-descriptions-replace-the-llm-narration-layer) supersedes ADR-012):** the note above is now void. `POST /api/explain` does not exist, so this ADR has **no caller in the MVP at all**, and the entire mechanism — the endpoint, HMAC issuance and verification, and the independent re-application of `is_secret_path` at serving time — is deferred as one unit. File descriptions no longer need a re-fetch: they are extracted during the analysis pass from bytes already in memory, so no token authorizes anything and no second network path exists to guard. This ADR's design is unchanged and still governs whenever a source viewer is built; it is simply not being built now. `GraphNode.sourceToken` remains in the wire contract and stays `None`.

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

---

## ADR-011 — Split deployment: frontend on Vercel, backend on a persistent host

### Decision
The frontend (React/Vite) deploys to Vercel. The backend (FastAPI) deploys to a separate host that supports a long-running process and native dependencies — Railway, Render, or Fly, chosen at deploy time — not Vercel's serverless Python runtime.

### Reason
A 2026-08-31 scope decision, made to hit a 3-day MVP deadline. Vercel's Python serverless functions cap execution time (10s on the free tier, 60s on Pro) and cold-start per invocation, which is a poor fit for a streaming tarball download, a tree-sitter parse, and now a per-request LLM call (ADR-012) — several of which can legitimately approach the existing 60s analysis `Deadline`. It also risks tree-sitter's native grammar wheels not loading correctly in that runtime, which would not surface until deploy time. Splitting keeps ADR-001's stateless-backend, no-database reasoning completely intact while using Vercel for what it is actually strong at: static/edge frontend hosting.

This **amends ADR-001's deployment mechanism** (`docker compose up`) for the MVP release only. Docker Compose remains the target for anyone self-hosting both services together on one machine, and should still be built once there is time to verify it end-to-end — but the fastest path to a public, working demo under this deadline is the split above.

### Alternatives considered
- Vercel serverless functions for the backend too — forces re-architecting the streaming/parsing/LLM path around short execution windows, with a real chance tree-sitter's native module fails to load in that runtime. Likely burns a full day debugging the platform rather than building features.
- Docker Compose deploy to a single rented VM — matches ADR-001 exactly, but provisioning, securing, and pointing a domain at a VM in three days is slower than Vercel plus a PaaS free tier.

### Status
Accepted (supersedes ADR-001's deployment mechanism for the MVP release; ADR-001's no-database/stateless-backend reasoning is unchanged and still governs both services)

> **Note (2026-08-31, [ADR-013](#adr-013--repository-authored-descriptions-replace-the-llm-narration-layer)):** the reasoning above cites "a per-request LLM call (ADR-012)" as one of the workloads a serverless runtime fits poorly. That call no longer exists. **The decision is unaffected** — it never rested on that clause. A streaming tarball download and a tree-sitter parse under a 60 s `Deadline` are on their own a poor fit for a capped serverless execution window, and the risk that tree-sitter's native grammar wheels fail to load in that runtime is entirely unchanged. Read the LLM mention as one dropped example among several, not as load-bearing.

---

## ADR-012 — LLM-generated C4 diagrams, service-map summaries, and file explanations, over untrusted repository content

### Decision
Add a bounded, display-only LLM layer on top of the deterministic graph:

- One call per analysis produces a brief C4 Context + Container diagram, written directly in **Mermaid `C4Context`/`C4Container` syntax**, and one-line summaries for a service map.
- The service map's endpoints themselves are found **deterministically** — a new tree-sitter query detects route-defining calls (e.g. Express `app.get/post/...`, decorator-style routers, Next.js `app/api/*/route.ts` file convention) — the LLM only narrates over that structural output, it does not decide what an endpoint is.
- File explanations are generated **lazily**, one LLM call per file, fired only when a user opens that file's inspector panel — not upfront for every file in the repository.
- The LLM never determines imports, edges, or any graph structure. That remains 100% deterministic tree-sitter output, per the PRD's "no LLM to determine imports or dependencies" rule, which this ADR does not touch.

File content reaches the LLM through the **same fetch-and-filter mechanism ADR-007 already specified for `/api/source`**: a single-file re-fetch from `raw.githubusercontent.com` at the commit SHA pinned during analysis, gated by the same HMAC token, with `is_secret_path` re-applied before anything leaves the process. A new endpoint, `POST /api/explain`, is this mechanism's second caller — the first productized use of ADR-007's design, ahead of a raw source viewer.

### Reason
The user asked for three specific reference features — a brief C4 model, an API service map, and per-file explanations — that all require summarizing *what code means*, which a pure import-graph parser cannot produce by construction. Mermaid's `C4Context`/`C4Container` syntax lets the frontend render real C4 diagrams with one library call instead of custom diagram UI, which matters under a 3-day budget.

This introduces a trust boundary `docs/SECURITY.md` did not previously cover: untrusted repository content now reaches an LLM prompt. The mitigations, recorded as new rows in `docs/SECURITY.md`:
- File content is size-capped before inclusion in a prompt, on the same philosophy as the parser's `MAX_PARSE_BYTES`.
- Content is wrapped in explicit delimiters with an instruction to treat it as inert data, never as instructions to follow — a prompt-injection string in a comment or a string literal can at worst produce a misleading description, never an action.
- LLM output is rendered as plain text or as Mermaid source only, **never** as raw HTML / `innerHTML` — the same discipline the PRD already requires for source code display.
- LLM output never controls program flow: it does not choose what to fetch, parse, filter, or render structurally. It is a leaf value, display-only, on every path.
- The secret filter runs on the fetched file **before** it reaches the prompt, exactly as ADR-007 requires for the raw preview — a `.env` is refused for explanation for the same reason it is refused for display.

### Alternatives considered
- **Heuristic-only, no LLM** — regex/AST-derived exports and JSDoc for "explanations", route detection alone (no summary) for the "service map", and no true C4 diagrams at all (they need semantic understanding no heuristic fakes convincingly). Zero ongoing API cost and no new trust boundary, but ships materially weaker versions of exactly the three features requested.
- **Upfront LLM call per file at analysis time** — simpler code path, but multiplies cost and latency by file count for files most users never open. Lazy, on-click generation keeps spend proportional to actual use.

### Status
**Superseded by [ADR-013](#adr-013--repository-authored-descriptions-replace-the-llm-narration-layer) (2026-08-31).** The LLM layer is removed from the project entirely — it did not fit the deadline, and the three features it backed are now produced deterministically instead. Nothing described above was ever implemented; the only code it reached was the wire contract (`serviceMap`, `c4`, three `Settings` limits), which ADR-013 amends rather than deletes. The reasoning above is retained because ADR-013's argument is a direct response to it.

---

## ADR-013 — Repository-authored descriptions replace the LLM narration layer

### Decision

**Remove the LLM from the system.** There is no `app/llm/`, no `POST /api/explain`, no model provider, and no API key. The PRD's "do not use an LLM to determine imports/dependencies" rule is now vacuously satisfied, because nothing in the project calls a model at all.

The three features [ADR-012](#adr-012--llm-generated-c4-diagrams-service-map-summaries-and-file-explanations-over-untrusted-repository-content) introduced are kept, each re-specified as deterministic output:

1. **Per-file descriptions come from the file itself.** The description is the file's own leading header comment — a JSDoc `/** … */`, a plain `/* … */` block, or an unbroken run of `//` lines — appearing before the first declaration. It is extracted from the tree-sitter tree the parser **already builds**, from bytes already in memory, during the existing single pass. A file with no such comment simply has no description. It travels as `GraphNode.description`.

2. **Route summaries come from the route's own comment.** Same extractor, applied to the comment immediately preceding a detected route handler. `ServiceEndpoint.summary` survives unchanged in shape; only its provenance changes, from "the model's one-line gloss" to "what the author wrote above the handler".

3. **The C4 diagram becomes a deterministic component diagram**, generated from the finished graph rather than described by a model: top-level directories become containers, external packages (ADR-005) become external systems, and detected routes become the API surface. It is emitted as Mermaid source, as before. **The field is renamed `c4` → `componentDiagram`**, because what this produces is a component/container sketch derived from structure, not a C4 model — C4 encodes *intent*, which no import graph contains. Keeping the name would have been the documentation lying about the artifact.

Route *detection* is unchanged and was always deterministic; ADR-012 only ever had the LLM narrating over it.

### Reason

The proximate reason is the deadline: the LLM layer was most of sprint Day 2, and it did not fit. But the change is an improvement on its own terms, and would be worth making without the schedule pressure.

**It deletes a trust boundary instead of defending one.** ADR-012 added the first sink for untrusted repository content other than the deterministic parser — an LLM prompt — and paid for it with five new `Planned` rows in [SECURITY.md](SECURITY.md): prompt injection, LLM output rendered as HTML, secret files reaching a prompt, cost inflation, and API-key exposure. Four of those five cease to be threats rather than becoming mitigated ones. That is CLAUDE.md's "prefer eliminating a vulnerability class architecturally over defending against it procedurally" applied to a whole feature, and it is the same reasoning as [ADR-003](#adr-003--never-write-repository-data-to-disk): the strongest control over a hazard is not having the hazard.

**It makes the entire response a pure function of the commit.** With a model in the path, no golden-file test over a whole `AnalyzeResponse` is possible — the narrated fields differ run to run. Without one, the same commit produces byte-identical JSON, which is exactly the property [ARCHITECTURE.md](ARCHITECTURE.md)'s graph-model section already demands of nodes and edges and could not previously demand of the response.

**It removes a network hop, a per-request cost, and a latency source** from a request that already has a 60 s budget, a streaming tarball download, and a tree-sitter parse in it.

**And a header comment is a quotation, not a guess.** It is what the repository's own authors said the file is for. The trade is real and goes the other way on coverage: an uncommented file gets no description, where a model would have produced one for every file. Weaker coverage is accepted in exchange for never being wrong — a fabricated description of a file the user knows well is worse than no description at all, and it is the failure mode most visible in a demo.

### The one thing this does *not* simplify

Descriptions are the **first repository *content* to enter a response body.** Everything the API returned before was structure about the repository — paths, counts, line numbers — not text authored inside it. A comment is attacker-controlled text, and it is now rendered in a browser. So the ADR-012 mitigations that were about the *sink* rather than the model survive, and are re-recorded in [SECURITY.md](SECURITY.md) under a new heading:

- Descriptions are size-capped (`MAX_DESCRIPTION_CHARS`) at extraction, before they reach a response model.
- Comment markers are stripped, control characters are stripped, and the result is collapsed to a single line — a description is a label, not a document.
- Rendering is as a React text node, **never** as an HTML string and never via `dangerouslySetInnerHTML`. This rule is inherited verbatim from ADR-012 and the PRD's rule for source display; the sink outlived the model.
- `is_secret_path` already runs before a file is parsed, so a secret file is never parsed and therefore can never produce a description. This is a property of the existing pipeline ordering, not a new check.

### Consequences

- **`POST /api/explain` is removed from the design.** It was ADR-007's only in-scope caller, so the HMAC token mechanism, token issuance, and the independent re-application of `is_secret_path` at serving time all return to Deferred alongside `POST /api/source`, as one unit. See ADR-007's second scope note. `GraphNode.sourceToken` stays in the wire contract, unpopulated, belonging to that deferred design.
- **Descriptions ship in `/api/analyze`, not from a second endpoint.** ADR-012 generated them lazily per click specifically to control model spend. With no spend to control, that machinery is pointless: the content is already in memory during the parse, so extracting it upfront costs one tree walk and removes a whole request path, a loading state, and a client-side cache.
- **SECURITY.md's secret-exposure row can no longer flip via `/api/explain`.** It stays `Partial` until `/api/source` lands, which is now post-MVP. That is a documentation consequence, not a regression — the analysis-time filter is unchanged.
- Sprint Day 2 loses its backend half, leaving buffer against the fact that no archive byte has yet been fetched over a real network.

### Alternatives considered

- **Keep the LLM layer as specified (ADR-012).** Ships stronger descriptions and genuine C4 diagrams. Rejected on the deadline first, but also on a cost the deadline made visible: a whole trust boundary, a provider dependency, a key to hold, and non-determinism in the response — all for commentary alongside a graph that is the actual product.
- **Keep the LLM behind a feature flag, off by default.** Superficially the best of both. Rejected because the off-path is the shipped path, so the on-path is untested code that ships anyway, and every SECURITY.md row would have to stay `Planned`-but-reachable — the worst of both readings of "is this control real?".
- **Infer descriptions heuristically** from exported symbol names, file name, and import fan-in ("a utility module imported by 12 files"). Rejected: it is fabrication with no author behind it, and it reads as generated filler precisely where a real comment would have read as documentation. If a file says nothing about itself, saying nothing is the honest output.
- **Rename nothing and keep `c4` as the field name** for the deterministic diagram. Rejected — the field would then promise a C4 model to every frontend and schema that consumes it, and the first person to compare the output against the C4 standard would find the contract dishonest.

### Status
Accepted (supersedes ADR-012 in full; amends ADR-007's MVP scope note and the LLM clause of ADR-011's reasoning)

---

## ADR-014 — The real-network check is a script, not a pytest test

### Decision
End-to-end verification against real GitHub lives in `backend/scripts/smoke.py`, run by hand. It is not collected by pytest (`testpaths = ["tests"]`), carries no marker, and there is no supported way to make the automated suite reach the network.

### Reason
`tests/conftest.py` replaces `getaddrinfo`, `create_connection`, `gethostbyname`, and `socket.connect`/`connect_ex` for the whole session, and its docstring states the property deliberately: because the block is installed at session scope, a per-test `monkeypatch.setattr` undo restores *the block*, not the real socket module. docs/SECURITY.md turns that into a rule — "no security test may touch the network" — and calls it enforced rather than trusted.

The obvious way to add a real-repository test is a `@pytest.mark.network` that lifts the block and is deselected by default. That trades a guarantee for a default. Once the hatch exists it is available to every future test, not only to this one, and the failure it guards against is silent in exactly the way that matters: a test that quietly resolves a real name passes on the author's machine, passes in CI with egress, and fails or hangs only where there is no DNS — or, worse, passes everywhere while making a real request nobody intended.

The thing being verified also does not need to be a test. It is a one-time question — *does this work against real GitHub at all* — not an invariant regressing under change. It has no assertions the fixture suite does not already make, it cannot run in CI without granting egress to a build box, and its result is a paragraph in docs/CURRENT_STATE.md rather than a red bar.

This is ADR-009's reasoning applied to the test suite: make the safe thing structural rather than conditional, so a request reaches the network only if someone names a call site that does.

### Consequences
Real-network coverage is not automated and will drift — nothing fails when GitHub changes a redirect shape, and only a person running the script notices. Accepted, because the alternative regresses a security property to catch a class of change that is rare and loud when it arrives. The script prints counts and extensions only, never a specifier, a path, or a token, because its output is the kind of thing pasted into an issue.

If real-network coverage must ever be automated, the right move is a separate suite with its own conftest and its own invocation — not a marker inside the hermetic one.

### Alternatives considered
- **`@pytest.mark.network`, deselected by default** (the escape hatch this rejects; the block stops being a guarantee for every test, not just the new one)
- **A second conftest-less test directory** (workable, and the recommended path *if* automation becomes necessary — rejected now as more machinery than a one-time check justifies)
- **VCR-style recorded responses** (a recording is another in-process fixture; it answers a different question than "does the live path work")
- **Not verifying against real GitHub at all** (leaves the transport, redirect shape, and chunking entirely unexercised — the gap CURRENT_STATE.md carried as a Known Issue)

### Status
Accepted

---

## ADR-015 — The archive's commit SHA travels on an out-parameter, not in the yielded tuple

> Restored. This decision was originally numbered ADR-011 on the branch merged as PR #2; that number now belongs to the split-deployment decision. See the numbering note at the top. `app/fetch/archive.py` cites this ADR.

### Decision
`app/fetch/archive.iter_source_files` keeps yielding `(PurePosixPath, bytes)`. Facts about the archive *as a whole* — the commit SHA captured from the root directory, and the per-reason skip counts — are written into an optional `ArchiveInfo` dataclass the caller passes in. The parameter defaults to `None`, so every existing three-argument call still works.

### Reason
The reader validates the root directory name (`^[A-Za-z0-9._-]+-[0-9a-f]{7,40}$`) but never returned the SHA inside it, which blocked ingestion step 5. The pipeline needs it: it is the commit every later source fetch is pinned to, and `get_download_url` supplies one only when the ref was already a SHA — a branch ref redirects to `.../legacy.tar.gz/refs/heads/main`, which names no commit.

Three channels were available.

**A changed tuple** — `(path, content, sha)` — repeats a constant on every member. It invites a caller to read the *last* copy as authoritative rather than relying on the root-equality check that already guarantees they agree, and it rewrites the call shape in 145 existing tests to carry a value almost none of them use.

**The generator's `return` value**, via `StopIteration.value`, is delivered only on exhaustion. The pipeline stops at `MAX_SOURCE_FILES` without draining the generator, so the channel would be empty in precisely the case that needs it. This is the option that looks cleanest and is wrong.

**A mutable out-parameter** is filled the moment the first accepted member establishes the root, so it survives an early break, and it costs one optional argument. Mutation-by-side-effect is the cost; it is paid down by the field being on a named dataclass whose docstring says when it is populated, and by a test that asserts the SHA is present after a single `next()`.

Folding the skip counts into the same object was nearly free — `iter_source_files` already computed them for a log line and threw them away — and the pipeline needs them, because a symlink or an oversized member is a file that produced no graph node and nothing below the pipeline can count it.

### Alternatives considered
- A three-element yield tuple (repeats a constant; churns every existing caller and test)
- A generator `return` value (unavailable on the early-exit path that matters)
- Turning the reader into a class with a `commit_sha` property (a larger interface change, and either two entry points or a rewrite of every existing call)
- Re-deriving the SHA in the pipeline (impossible — the reader strips the root before yielding, which is the whole point)

### Status
Accepted

---

## ADR-016 — The pipeline hands the graph builder a content-free file list

> Restored. This decision was originally numbered ADR-012 on the branch merged as PR #2; that number now belongs to the (since superseded) LLM-layer decision. See the numbering note at the top. `app/analysis/pipeline.py` cites this ADR.

### Decision
`app/analysis/pipeline.analyze_repository` returns a `RepositoryAnalysis`: the repository coordinates, the commit SHA, a tuple of `SourceFile` (path, language, byte count, line count, and the `ImportRef`s found in it), a skip tally keyed by fixed-literal reasons, and a `truncated` flag. It carries **no file content**, no resolution, and no ordering guarantee beyond archive order.

### Reason
*No content* is ADR-003 held at one more seam. `loc` and `size_bytes` are computed while the bytes are in hand precisely so nothing downstream needs to keep them; a field carrying `bytes` would make peak memory the size of the repository again and quietly undo the property the streaming reader exists to provide. A test asserts no field of `SourceFile` is `bytes`.

*No resolution* keeps the deterministic stages separable. Specifiers come out exactly as written, so the resolver can be built and tested against a fixture list rather than against a live download.

*Only parsed files are in the list*, and this is the load-bearing consequence: resolution is set-membership against exactly this collection, so it can only ever produce a file that is also a node. Publishing a wider list — every member the archive contained — would let the resolver resolve an import to a path that was secret-filtered, vendored, or never parsed, and manufacture an edge with no node on the far end. The narrower list makes dangling edges unrepresentable rather than something the graph builder has to filter out.

*Archive order, not sorted order.* docs/ARCHITECTURE.md assigns sorting, dedup, and the `stats.dependencies == len(edges)` invariant to the graph builder. Sorting here would put half of a determinism guarantee in one module and half in another.

### Consequences
A description extractor (ADR-013) needs the parse tree or the bytes, both of which exist only inside the pipeline loop — so a file's description must be extracted *there* and carried as a field on `SourceFile`, not recovered later from content this contract deliberately drops.

`extract_imports` reports a skipped file by yielding nothing, so the pipeline cannot distinguish "parsed, no imports" from "not parsed". A file the parser gave up on therefore stays in the list as a node with real bytes, real lines, and zero imports — honest, since it is a real file, but it means parser-level skips are absent from `skipped`.

### Alternatives considered
- Returning `(path, content)` and letting the graph builder measure (reintroduces whole-repository memory)
- Returning every archive member so the resolver has a wider target set (allows edges to non-nodes)
- Returning fully-built `GraphNode` objects (couples the pipeline to the wire contract and to node-ID decisions that belong to the graph builder)
- Sorting here (splits the determinism guarantee across two modules)

### Status
Accepted

---

## ADR-017 — Resolver output is a flat per-import record; config will arrive already parsed, on `RepositoryAnalysis`

### Decision

Two decisions about `app/analysis/resolver.py`, taken together because the second constrains the first's signature.

**Output shape.** `resolve_imports(analysis) -> tuple[ResolvedImport, ...]` returns **one record per `ImportRef`**, in file order then import order. Each record carries the importing path, the specifier exactly as written, the line, a `Resolution` of `RESOLVED` / `EXTERNAL` / `UNRESOLVED`, and a target path that is set **exactly when** the resolution is `RESOLVED` — enforced in `__post_init__`, not merely documented. Nothing is sorted, deduplicated, or aggregated here.

**The config seam — decided, deliberately not built.** When `tsconfig.json` `paths` / `baseUrl` and workspace packages land (Deferred, see TODO.md), configuration reaches the resolver by being **parsed inside the pipeline loop and carried on `RepositoryAnalysis` as an already-narrowed structure** — a base directory, an alias table, a workspace glob list. Not as raw bytes, and not by a second pass over the archive. `resolve_imports` already takes the whole `RepositoryAnalysis` rather than the bare file list it currently reads, so adding that field is additive rather than a signature change at every call site.

### Reason

**On the output shape.** The graph builder needs two different things from the same records: an edge per resolved import, and per-node `externalImports` / `unresolvedImports` counts (ADR-005). A flat sequence serves both — grouping is one `Counter` over `.source` — while a mapping keyed by source file serves only the second and has to reconstruct the first.

The flat form also makes the module's exhaustiveness a *single* assertion: `len(result) == sum(len(f.imports) for f in analysis.files)`. That is the same kind of checkable identity as the pipeline's `len(files) + skipped_files`, and it was chosen for the same reason — it converts "we handled every import" from a claim into a test. A dict-of-lists loses it (a missing key and an empty list are the same reading), and a tuple of bare `(source, target)` pairs loses `line` and loses the two non-resolving outcomes entirely.

Making `target` and `resolution` agree structurally rather than by convention matters because the alternative lands downstream: a record claiming both `EXTERNAL` and a target is a graph builder bug that would present as an edge to a node the user never imported. `AppError.__init__` takes no arguments for the same class of reason.

**On the config seam.** Three channels were available and the deciding constraint is that two of them break something the project has already pinned.

*Carrying raw config bytes on `RepositoryAnalysis`* is the cleanest-looking seam and it directly violates [ADR-016](#adr-016--the-pipeline-hands-the-graph-builder-a-content-free-file-list), which is not a preference but a tested invariant — a test asserts no field of `SourceFile` is `bytes`, and the whole point of that contract is that peak memory does not become the size of the repository. A `tsconfig.json` is small, so the memory argument is weak in this one case; the argument that is not weak is that "content-free, except for the files where it is not" is a rule nobody can apply. It is also the shape whose ambiguity the PR #2/#3 merge had just finished paying for.

*A separate harvest pass in the resolver* is impossible rather than merely costly. Under [ADR-003](#adr-003--never-write-repository-data-to-disk) nothing is kept, so "re-enter the archive after ingestion" means **downloading the repository a second time** — a second network round-trip, a second decompression, and a second chance for the two passes to disagree about what the commit was.

*Parsing inside the pipeline loop* is left, and it is better than a last-resort. The bytes are already in hand exactly once, in the same place `loc` and `size_bytes` are computed and for the same reason. What travels is the *parsed, narrowed* result, which is derived structure and no more file content than `loc` is — so ADR-016 holds as written and its test keeps passing. It is also the same shape [ADR-013](#adr-013--repository-authored-descriptions-replace-the-llm-narration-layer) already forced for descriptions: extract in the loop, carry the small derived value, drop the bytes.

The cost is real and is accepted: the pipeline gains a second thing it recognizes by filename, and a JSONC reader lands there rather than in the resolver. That is one module knowing about two file kinds instead of two modules re-reading one archive.

### Consequences

- The resolver takes a `RepositoryAnalysis` today even though it reads only `.files`. That is mild over-coupling on purpose, so the deferred work is additive.
- `resolve_imports` is pure and total: no I/O, no clock, no `Deadline`, no exception. ~~It needs no deadline because it does at most fifteen set lookups per import over a file list already capped at `MAX_SOURCE_FILES`.~~ **Corrected 2026-08-31, by measurement.** The per-import bound is real; the conclusion drawn from it was not, because `MAX_SOURCE_FILES` caps *files* and nothing anywhere caps imports *per file* — so the total work is bounded only by `MAX_EXTRACTED_BYTES` (256 MiB), and it runs after `analyze_repository` has already spent its whole 60 s budget. Measured: 1 002 000 imports from an ~11 MiB repository resolve in **78.7 s**. Recorded as an open control in docs/SECURITY.md ("Post-parse analysis runs outside the deadline") and docs/CURRENT_STATE.md. The decision this ADR actually makes — the flat per-import output shape — is unaffected; only this consequence bullet was wrong.
- The module has **no logger**, which is how docs/SECURITY.md's "never log import specifiers" rule is satisfied here — structurally rather than by discipline.
- Resolution being set membership against `RepositoryAnalysis.files` means `security/path_safety.py` still has no caller and still should not: there is no base directory on disk for a resolved path to escape from.

### Alternatives considered

- **A mapping keyed by source path** (`dict[PurePosixPath, tuple[ResolvedImport, ...]]`) — directly serves the per-node counts, but loses the one-record-per-import identity and makes "a file with no imports" and "a file we forgot" the same value.
- **A result object with pre-split `edges` / `external` / `unresolved` collections** — saves the graph builder one pass, at the cost of this module deciding what an edge is. Dedup, self-edge removal, and ordering are the graph builder's by ADR-016, and splitting them across two modules is exactly what that ADR declines to do.
- **Bare `(source, target)` tuples** — the smallest thing that draws edges, and it silently discards the two outcomes ADR-005 says to count.
- **Config as raw bytes on `RepositoryAnalysis`** — see above; breaks a tested invariant.
- **Config harvested by a second pass** — see above; requires a second download.

### Status
Accepted

---

## ADR-018 — The repository root is a node, node identity is the path, and directories are inferred

### Decision

`app/analysis/graph_builder.build_graph` fixes four things the wire contract left open, because the contract describes the *shape* of a `GraphNode` without saying which nodes exist.

**A node's `id` is its repository-relative path**, identical to its `path`, and edges name nodes by that string. There is no second identifier space.

**Directory nodes are inferred from the parent hierarchy** (ADR-006) — one per directory that is an ancestor of some analyzed file, and none for a directory that contains nothing analyzable. The archive reader yields no directory entries, so there is nothing else they could come from.

**The repository root is itself a node**: `id`/`path` `"."`, `depth` 0, `parent: None`, and `name` set to the repository's name. It is the single node ADR-006 reserves `parent: None` for.

**Ordering is by path components** — the `PurePosixPath.parts` tuple — for both nodes and edges, not by the path string.

Two contradictions that untrusted archives can produce are resolved rather than raised. A path that is both a regular file and another file's ancestor directory (`components.ts` beside `components.ts/x.ts`, both legal in a tarball) becomes **one file node**: *observed beats inferred*. A path appearing twice in `RepositoryAnalysis.files` becomes one node whose metadata comes from the **first** record.

### Reason

**On identity.** The path is already unique, already validated by `fetch/archive.py`, and already what the resolver returns, so any other id would be a mapping to build, keep in sync, and debug through. It also makes an edge readable in a response body without a lookup table. The cost — ids as long as paths — is a payload-size question, and the answer to that is capping the number of nodes, not renaming them.

**On the root node.** ADR-006 says `None` marks *the* root, singular, which a forest of top-level nodes does not give you. A single root also gives the frontend one entry point for a tree walk and one container for the sphere-packing layout, and makes `root.fileCount == stats.files` / `root.totalBytes` checkable identities instead of numbers only the stats carry. `PurePosixPath(".")` is not an invention: it is where `.parents` terminates for every path in the analysis, its `str()` is `"."` so it satisfies the contract's `min_length=1` without a special case, and its `parts` is `()` so it sorts first for free. Its `name` is `""`, which the contract forbids and which would be a useless label anyway — the repository's name is the only honest thing to call the repository's root.

**On component ordering.** Determinism is satisfied by any total order, so this is chosen for a second property. Sorting path *strings* places `src/a.ts` after `src-b`, because `-` is 0x2d and `/` is 0x2f — a directory gets separated from its own children by an unrelated sibling. Sorting components keeps a directory immediately before its contents and puts the root first, so **a parent always precedes its children** and a consumer can build the tree in a single forward pass.

**On the two contradictions.** Both inputs come from a tarball we did not write, and `ValueError` on either would let one strange archive fail an entire analysis — the wrong trade against untrusted input, and one this project has repeatedly declined to make elsewhere (a member past a budget is skipped, not fatal; an unresolvable import is counted, not thrown). *Observed beats inferred* is the tiebreaker because the file was actually in the archive and the directory was deduced; keeping the file leaves ids unique, leaves no edge dangling, leaves no count wrong, and leaves the child's `parent` naming a node that exists. It renders as a file with children, which is strange and true.

### Consequences

- `len(nodes) != len(analysis.files)`: nodes are files plus inferred directories plus the root. `stats.files` and `stats.directories` are counted off the **emitted nodes**, so the collision case is reported the way it was resolved.
- `node.imports` / `node.importedBy` are counted off the **finished** edge set, after dedup and self-edge removal, so `sum(imports) == sum(importedBy) == len(edges) == stats.dependencies`. Counting `imports` from the raw import list instead would put two differently-defined numbers side by side in the inspector.
- `externalImports` / `unresolvedImports` are **statement counts, not distinct-package counts**. Dedup is specified for edges and only for edges, and the resolver deliberately does not extract a package name from a specifier.
- Directory `fileCount` / `totalBytes` are recursive over all descendants, which is what a containment layout sizes a shell by.
- The builder is **uncapped**: `MAX_NODES` / `MAX_EDGES` belong to the routing layer. That is a real gap while no router exists, and it is not a free hand-off — a cap cannot simply truncate the returned tuples, because `stats.dependencies == len(edges)` and the per-node counters are computed here and would immediately be false. Whoever applies the cap must re-derive the stats or ask this module for a smaller graph. *(**2026-09-01, ADR-023**: the router took the second branch. `build_graph` accepts an optional `GraphLimits` and builds a smaller graph; `None` — every other caller — still builds the whole one, so nothing above is retracted, only chosen between.)*
- Like `analysis/resolver.py`, the module is pure and has **no logger**: paths are the only repository text it handles, and docs/SECURITY.md keeps those out of records above `DEBUG`. Two tests pin this structurally — one builds a graph with the `os` filesystem primitives torn out, one asserts no log record is emitted at any level.
- The two preconditions a caller could break — a resolved import whose `source`, or whose `target`, is not in the analysis — raise `ValueError` with a fixed literal message containing no path. A programming error, like `path_safety.safe_relative_path`'s, not an `AppError`.

### Alternatives considered

- **No root node; top-level entries carry `parent: None`.** A forest. Simpler by one node, but it contradicts ADR-006's singular "the root", gives the frontend no single container, and loses the `root.fileCount == stats.files` identity.
- **Opaque or hashed node ids.** Shorter payloads and a stable id across renames, neither of which the MVP needs, in exchange for a mapping every consumer has to hold.
- **Sorting by path string.** One character shorter as a key and splits directories from their children; see above.
- **Raising on a file/directory collision or a repeated path.** Turns a weird-but-legal archive into a failed analysis.
- **Preferring the inferred directory in a collision.** Requires also dropping that file's edges and correcting its counts — more code, and the resulting graph silently omits a file the repository really contains.

### Status
Accepted

---

## ADR-019 — `MAX_IMPORTS` caps resolution in the pipeline, instead of giving the resolver a `Deadline`

### Decision

`app/analysis/pipeline._analyze` counts every import as it comes out of `extract_imports` and stops parsing at `MAX_IMPORTS` (100 000, `app/config.py`), setting `truncated` and a new `imports_truncated` on `RepositoryAnalysis`.

The count is a **running total across all files**, not a per-file limit, and it is checked **per import**, not per file — so the stop can land in the middle of a file. That file is **kept**, with the imports collected so far.

`analysis/resolver.py` and `analysis/graph_builder.py` still take **no `Deadline`**. The bound on them is this count, upstream, not a clock of their own.

An import-cap stop is **not** a key in `RepositoryAnalysis.skipped`.

### Reason

**The phase after the pipeline has no clock, and it is the expensive one.** `resolve_imports` runs once `analyze_repository` has returned, after the whole `ANALYSIS_TIMEOUT_S` budget has been spent. Its cost is linear in the import count at ~77 µs per unresolvable relative specifier — the worst case, since each tries all ~15 candidates before failing, and also the cheapest string for an attacker to write. Nothing bounded that count: `MAX_SOURCE_FILES` caps *files*, and one 1 MiB file can hold tens of thousands of imports, so the real ceiling was `MAX_EXTRACTED_BYTES` (256 MiB). Measured 2026-08-31 on 3000 files × 334 unresolvable imports: **1 002 000 imports, 76.1 s to resolve, 81.8 s total** — more than the entire analysis budget, in a phase with no clock, off a 21.7 MiB fixture. Re-measured with the cap in place: **100 000 imports, 7.7 s to resolve, 8.2 s total.**

**A count, not a clock, because a `Deadline` in the resolver produces a worse output.** Threading one through was the alternative the previous docstring named. But resolution is not incremental work that can be stopped halfway and still mean something: a partial resolution yields a graph missing edges it could have drawn, with no way to distinguish "this import resolves to nothing" from "we ran out of time before checking". `AnalysisTimeoutError` instead of a graph is the other branch, and it turns a large-but-legitimate repository into a failure that depends on how loaded the host was. A count is deterministic — the same commit produces the same graph, which is the property CLAUDE.md requires of the whole `/api/analyze` response — and it fails in the direction the project already fails everywhere else: a bounded, flagged, honest partial result.

**Upstream, because downstream cannot.** The cap cannot be applied in the resolver or the builder without paying for the thing it is trying to avoid: the resolver would have to resolve the imports to discard them, and the builder computes `stats.dependencies == len(edges)` and the per-node `imports`/`importedBy` counters from what it is given, so slicing its output afterwards falsifies all three (ADR-018's consequence bullet, now the second instance of the same argument). The pipeline is the last place the number can be reduced before anything depends on it.

**Per import, and the partial file is kept.** A per-file check would overshoot by however many imports the last file contained — up to ~87 000 for a single `MAX_PARSE_BYTES` file of twelve-byte `import"./a"` statements, which is most of the cap again. Keeping the partially-read file is the same call ADR-018 made for the file/directory collision and the pipeline made for a file the parser gave up on: the file *is* in the repository, its bytes and line count are true, and other files may legitimately import it. Dropping it would delete a node to hide a short list that `imports_truncated` already reports.

**Not a `skipped` key**, though the task that prompted this suggested it as an option. `skipped` maps a reason to *files that produced no node*; it is what `skipped_files` sums and what will feed `stats.skippedFiles`. The file that hit the cap produced a node. Folding it in would inflate a file count with something that is not a file skip — precisely the error `_NON_FILE_SKIPS` already exists to prevent for directory entries — and would break `len(files) + skipped_files` as a description of what the archive produced.

### Consequences

- **`truncated` no longer means `MAX_SOURCE_FILES`.** It means *a* cap stopped the run. `imports_truncated` says which, and the distinction matters to a consumer: the file cap drops whole files off the end of archive order, while the import cap can additionally leave the last file present with a partial import list. A test pins that the two flags are distinguishable.
- **`RepositoryAnalysis` gains `import_count`**, a derived property rather than a stored field, so it cannot drift from `files`. It is by construction the length of the sequence `resolve_imports` returns.
- Reaching the cap **breaks**, which abandons the generator and therefore the download — identical to `MAX_SOURCE_FILES`, and for the same reason. As there, no count of what was left behind can be reported without paying for the rest of the transfer.
- **`MAX_IMPORTS` is now the number that governs post-parse cost**, and raising it spends time in a phase with no clock. 100 000 × ~77 µs ≈ 7.9 s, ~13% of `ANALYSIS_TIMEOUT_S` again; the measured 7.7 s confirms it. For scale, `sindresorhus/ky` is 186 imports over 54 files and `pmndrs/zustand` 163 over 50 — ~3.5 per file, so a repository that dense filling all of `MAX_SOURCE_FILES` lands near 10 500. The cap is ~10× that.
- The worst case is still **not zero cost**: an analysis can now spend ~60 s parsing and then ~8 s resolving. This bounds the total, it does not make the second phase free. `MAX_CONCURRENT_ANALYSES` (3) and the unwritten rate limiter are what bound the aggregate.
- `MAX_NODES` / `MAX_EDGES` remain enforced nowhere. This ADR closes the *import* half of "post-parse analysis runs outside the deadline"; the graph-size half is still the router's, and the builder measured linear and cheap (300 000 imports in 0.22 s), so it was never the cost problem. *(**2026-09-01**: closed by ADR-023, which took ADR-018's "or ask this module for a smaller graph" branch for the reason this bullet's neighbours give — the cap belongs where the numbers are still being produced.)*
- Six mutations of the new control were tested one at a time and **all six are caught**: the check deleted, `>` for `>=`, the counter reset per file, the inner `break` without the outer one, `imports_truncated` set without `truncated`, and the partial file dropped instead of kept.

### Alternatives considered

- **Thread the `Deadline` through `resolve_imports` and `build_graph`.** Non-deterministic output, and a partial resolution is indistinguishable from a genuinely unresolvable import. See above.
- **Give the resolver its own separate time budget.** Same determinism problem, plus a second clock for a step to award itself time with — the exact thing `Deadline` being frozen exists to prevent.
- **Cap imports per file instead of in total.** 3000 files × the per-file cap is the same hole one order of magnitude along.
- **Make the resolver faster instead of smaller.** Worth doing on its own merits — the ~15-candidate loop for a failing relative specifier is the hot path — but a constant factor does not bound an unbounded input, and the number of imports would still be limited only by `MAX_EXTRACTED_BYTES`.
- **Reject the repository outright past the cap**, as `MAX_ARCHIVE_MEMBERS` does. That line is drawn at "a shape an honest `git archive` could not produce"; a repository with a great many imports is merely large, so it truncates, like `MAX_SOURCE_FILES`.

### Status
Accepted

## ADR-020 — The description is a byte-prefix scan, not a second pass over the AST

### Decision

`app/analysis/descriptions.py` extracts a file's leading header comment with a **lexical scan over the first `_SCAN_BYTES` (4 KiB) of the file**. It builds no tree, imports no grammar, takes no `Deadline`, and does not touch `app/analysis/parser.py`, whose "reports nothing but imports" contract is unchanged.

The module splits deliberately in two:

- **`header_description(source, settings)`** — the *locator* for the byte-0 case. Skips a BOM and leading whitespace, recognizes `/** … */`, `/* … */`, or an unbroken run of `//` lines, and hands the comment on.
- **`normalize_comment(raw, settings)`** — the *normalizer*, which takes one comment's own text with its markers attached and returns the bounded description. This is the half `ServiceEndpoint.summary` will share.

Normalization happens **at extraction**: markers stripped, whitespace collapsed to single spaces, non-printable characters dropped, truncated to `MAX_DESCRIPTION_CHARS` while cleaning, and empty mapped to `None`.

The result rides on `SourceFile.description` (`app/analysis/pipeline.py`) and is copied onto `GraphNode.description` by the graph builder. Directory nodes and the repository root have none.

### Reason

**At byte 0 there is no lexical context to get wrong.** This is the whole argument, and it is narrower than "a scanner is good enough". Everything that makes JS tokenization genuinely ambiguous is a question about what *preceded* the current position — is this `/` a division or a regex delimiter, is this `//` inside a string or a template literal, is this `/*` inside a comment already. Nothing precedes the first byte of a file. A scanner anchored at position 0 is therefore not an approximation of what a parser would say there; it is the same answer, and the tree buys nothing.

**The tree also is not free, and the seam it would need is worse than either.** Three options were on the table:

*Widening `extract_imports`* is one parse but changes the signature and the documented contract of the module with the most tests in the project (75) and the most carefully bounded behaviour, to carry a value that has nothing to do with imports. It would also make a description depend on a *successful* parse: `parser._collect` returns early for oversized, binary, and pathologically malformed files, so exactly the files with no imports would also silently lose their header comment.

*A separate `descriptions.py` that re-parses* keeps the contract and doubles the parse cost. Measured: 3000 files parse in ~5.7 s, and the worst single hostile file measures ~3.1 s — real money against a 60 s budget, spent to answer a question the parse was never needed for.

*The prefix scan* costs, measured over the whole `MAX_SOURCE_FILES` cap at 3000 files:

| input | per file | × 3000 |
|---|---|---|
| no header comment (the common case) | 0.9 µs | 0.003 s |
| ordinary JSDoc header | 10.3 µs | 0.031 s |
| 1 MiB file, short header | 4.0 µs | 0.012 s |
| comment longer than the scan window | 71.4 µs | 0.214 s |
| 4 KiB window of NUL bytes (worst measured) | 235.3 µs | **0.706 s** |

So the worst adversarial case across an entire analysis is under a second, against ~5.7 s for a second parse of ordinary files — and the common case, a file with no header comment at all, is free because the first non-whitespace byte is not a `/`.

**The argument is narrow on purpose, and that is why the module splits in two.** ADR-013 promises the same extractor serves `ServiceEndpoint.summary`, the comment above a route handler. That comment is *not* at byte 0, and there the argument reverses completely: locating it means knowing where the handler starts and what token precedes it, which only the tree can say. Scanning backwards from an offset over `*/` would be exactly the context-dependent guess this ADR avoids. So the shared piece is **normalizing, not locating**. Route detection will have a tree already, will take the comment as a sibling node, and will hand `node.text` to `normalize_comment` — the same input shape, markers included, that a comment node yields.

**Normalizing at extraction rather than at serialization** is ADR-013's surviving security rule, and it belongs here because this is the last point where the raw comment exists. `MAX_DESCRIPTION_CHARS` on the model is a second, independent application of the bound, not the one relied on.

### Consequences

- **`SourceFile` now carries repository-authored text**, which it never did before, and ADR-016 still holds. That invariant is "nothing on this record scales with the size of a file": a description is bounded by a constant before it is assigned, so a 1 MiB file and a 12-byte file each contribute at most 500 characters, and the whole file list caps near 1.5 MB against the 256 MiB of extracted bytes the reader may stream. The existing `bytes`/`bytearray` test passes because a `str` is not those types — which is true but is not the argument, so `test_the_only_repository_text_carried_is_a_bounded_description` was added beside it to assert the bound itself.
- **Non-printable characters are dropped via `str.isprintable()`**, which is False for exactly the right set: C0/C1 controls, surrogates, unassigned code points, and — the one worth naming — the `Cf` format characters, including U+202E RIGHT-TO-LEFT OVERRIDE. Those are the Trojan Source characters, and a description rendered in a browser is precisely the sink they are aimed at. Getting them for free from a rule written for ANSI escapes is luck, so it is now a test.
- **Undecodable bytes are replaced, not refused** (`errors="replace"`), deliberately unlike `parser._specifier`'s strict decode. A specifier must match an archive path byte-for-byte, so U+FFFD there invents an edge that can never resolve; a description is displayed, never compared. There is also a case strict decoding gets actively wrong: the window is a fixed byte count and can land mid-character in a *valid* UTF-8 file, and strict would then discard that file's whole description for a reason having nothing to do with the file.
- **A description costs nothing when the parser gives up.** A binary or oversized file yields no imports but still yields its header comment, because the extractor never needed the tree. Pinned by test.
- **The scan window is a real bound with a real cost**: a description that begins after 4 KiB of padding is not found. Every other test in the file passes with the slice deleted, so this one is pinned explicitly.
- **`normalize_comment` refuses input that is not comment syntax**, returning `None` rather than passing text through. It is not a general sanitizer; if route detection ever hands it the wrong node, the failure should be an absent summary and not a line of source code in a response body.
- **23 controls were mutation-tested one at a time; all 23 are caught, with no survivors and nothing to annotate.** Three initially survived and all three were genuine test gaps rather than equivalent mutations, which is worth recording because it is the opposite of the last three modules: `normalize_comment`'s decode had no direct coverage; `* ** bold **` cannot distinguish "drop one star" from "drop the run of stars" because a space follows either way; and every `//` fixture wrote `// One.` with the conventional space, which hides a dropped line break because the space separates the words anyway. `//One.` was the input that discriminated.
- ARCHITECTURE.md's "Description extraction" section previously said this runs "over the tree `extract_imports` already built". That sentence is now wrong and has been corrected rather than left as aspiration.

### Alternatives considered

- **Widen `extract_imports` to return the header comment**, via a signature change or an `ArchiveInfo`-style out-parameter (ADR-015's shape, and it would have worked mechanically). Rejected: it couples a description to a successful parse, and it spends the contract of the project's most bounded module on a value that is not an import.
- **A separate module that re-parses.** Clean separation at roughly double the parse cost, to obtain a fact the parse cannot answer better than a four-line scan.
- **Scan backwards from an offset, so one locator serves both callers.** This is where the byte-0 argument stops holding, and it is the reason the split is between locating and normalizing rather than between "header" and "summary".
- **Skip a leading `#!` line**, so a CLI entry point's header is found. One line, and genuinely useful in the npm ecosystem — but a shebang is not one of the three comment forms ADR-013 names, so it is recorded as a known gap in CURRENT_STATE.md with a test pinning the current behaviour, rather than widened quietly here.
- **Special-case `/// <reference … />`**, which becomes a useless-but-accurate description on some TypeScript files. Declined: it is the file's leading comment, so it is not *wrong*, and the rule "quote the header comment" survives better without an exception list.
- **Drop a description that is entirely U+FFFD**, which is what a comment of undecodable bytes produces. Declined as a heuristic: the file really does have a comment we cannot read, the cap already bounds it, and "absent but never wrong" does not require "absent whenever ugly".

### Status
Accepted

---

## ADR-021 — One guarded parse, many readers: the tree is a seam, not a private local

### Decision

`app/analysis/parser.py` splits in two, and `app/analysis/routes.py` is the first module to benefit:

- **`parse_source(source, path, language, deadline, settings) -> Tree | None`** — the *seam*. Every guard that stands between untrusted bytes and tree-sitter lives here and only here: `MAX_PARSE_BYTES`, the binary sniff, the BOM strip, the deadline check before the parse, the pathological-tree refusal, and the deadline check after it. Returns `None` when the file is refused, having logged a fixed-literal reason. Total except for `AnalysisTimeoutError`.
- **`extract_imports(tree, path) -> Iterator[tuple[str, int]]`** — the import query, over a tree it is handed. Same output as before; it no longer parses and no longer takes a `Deadline` or a `Settings`.
- **`string_literal_text(node)`** — `_specifier` promoted to a shared primitive, because a route path wants exactly the same strict answer a module specifier does.

`app/analysis/routes.py` runs its own two queries over that same tree and yields `ServiceEndpoint` records directly:

- **Method calls** — `app.get('/users/:id', handler)`. One query over member-expression calls, verb filtered in Python, covering Express, Koa's router, Fastify's shorthand, Hono, and the many libraries that copied the shape.
- **The Next.js App Router file convention** — `app/**/route.ts` exporting `GET`/`POST`/…, where the path is the *directory* rather than anything written in the file.

`ServiceEndpoint.summary` is the comment directly above the handler, normalized by `descriptions.normalize_comment`, which gains a `limit` parameter so the summary is bounded by `MAX_ENDPOINT_SUMMARY_CHARS` while cleaning rather than truncated afterwards.

The pipeline parses once and feeds both readers. Routes ride on `SourceFile.routes` and flatten through the new `RepositoryAnalysis.service_map`.

### Reason

**A security control with two implementations has two chances to be wrong.** Route detection needs a tree; `extract_imports` built one in a local and discarded it. The alternative to this split was a second module that re-parses, which is what `descriptions.py` was explicitly *allowed* to avoid — and the difference matters. ADR-020 could skip the tree because a header comment is at byte 0, where a scanner and a parser give the same answer. **That argument does not transfer**: a route handler is deep in a file, and locating it lexically is exactly the context-dependent guess ADR-020 refused to make. So route detection must have a tree, and the only question is whether it gets its own. A second parse would mean a second copy of five guards, one of which (`_is_pathological`) is the sole defence against a query that runs for eleven minutes — see ADR-010.

**ADR-020's objection to widening `extract_imports` does not apply here, and that asymmetry is the whole design.** Widening it for *descriptions* would have coupled a description to a successful parse, so a binary file would silently lose its header comment. For *routes* that coupling is not a cost: a file with no tree has no locatable route no matter who looks. So descriptions stay out of the tree path and routes go into it, and both decisions follow from the same question — does this fact exist independently of a successful parse?

**Two conditions beyond the verb set, because a wrong endpoint is worse than a wrong edge.** `map.get('key')` is a member call whose property is an HTTP verb. A detector that stopped at the verb would report it, and this is the route-detection form of the phantom dependency `parser.py` exists to prevent — but with a worse blast radius: a spurious edge is one line in a graph of thousands, while a spurious endpoint is one row in a service map of six, where a reader has no way to tell it from a real one. So the first argument must be a string literal beginning with `/`, and there must be at least one argument after it. The second is what separates a registration from Express's own one-argument settings getter `app.get('trust proxy')`.

**Absent beats invented, again.** `router.route('/x').get(h)` and Fastify's `fastify.route({method, url})` object form are real routes this misses, because the path and the verb live on different nodes. Both are recorded as deliberate gaps with a test pinning the behaviour. This is ADR-013's trade for descriptions applied to routes: a service map that is short is a service map you can trust.

### Consequences

- **`extract_imports` changed shape, and its 75 tests did not.** They all route through one helper in `tests/test_parser.py`, which now composes `parse_source` + `extract_imports` the way the pipeline does. Three `test_pipeline.py` spies moved from `extract_imports` to `parse_source`, and one of them — the secret-filter ordering test — became **strictly stronger**: it now asserts filtered bytes never reach the *parser*, so no reader of the tree can see them. Under the old arrangement a second reader with its own parse would have kept that test green.
- **The post-parse deadline check moved into `parse_source`**, where it guards every query the caller is about to run instead of only the import one.
- **`detect_routes`' deadline check sits *outside* its try block, so the catch-all has no `except AnalysisTimeoutError: raise`.** Mutation testing found that re-raise was dead code — nothing inside the guarded region touches the deadline. Making a timeout structurally unswallowable beats defending against swallowing it, which is ADR-009's pattern applied to control flow.
- **Detection is lazy, and that is a real bound rather than a style.** `_routes` yields; it does not build a list. With an eager list the pipeline's `MAX_SERVICE_ENDPOINTS` stop happens *after* every endpoint in the file exists — measured on the densest legal input (a full `MAX_PARSE_BYTES` of `app.get('/a',h);`) at **61 680 records built to keep 200, 1.11 s for the file, versus 0.31 s stopping at the cap**. The cost is that a mid-file failure now yields a partial service map instead of an empty one, which is the better failure and the same call ADR-019 made about the half-read file. A test counts model constructions, because the difference is invisible in the returned value.
- **`MAX_SERVICE_ENDPOINTS` does not set `truncated`.** The other two caps abandon the download; this one only stops adding to the service map, and the graph is complete when it fires. `routes_truncated` is a separate flag for the same reason `imports_truncated` is: the consequences differ, so conflating them would overstate what was lost.
- **A tree-sitter `//` run is one node per line.** `prev_named_sibling` gives only the last line, so `_comment_above` walks the run backwards and reassembles it — the one place where locating from the tree is *harder* than locating from bytes, since `descriptions._line_run` gets it for free. Found by a failing test, not by reading the grammar. A run is never glued to a block comment, because `normalize_comment` dispatches on the first two characters and would leave the `//` markers in the output.
- **Only the enclosing statement is examined for a summary, never an ancestor.** A climb-until-you-find-a-comment search gives every route inside a documented function that function's JSDoc.
- **`_endpoint` catches `ValidationError`**, and mutation testing is why. Anything raised there escapes into `detect_routes`' catch-all, which abandons the whole file — so one malformed record would silently delete every *other* route beside it, indistinguishable from a file that declares none. Two separate mutations survived by hiding behind exactly that. One bad record must cost one record.
- **The route query is a third traversal, and it is cheap on real input.** Measured across 3000 ordinary files: parse 1.45 s, import query 0.94 s, route query **0.48 s** — 17% of the total. `tree.language` returns the identical `Language` object the caller passed, so `lru_cache`d query compilation still hits; a miss per file would cost ~8.8 ms × 3000 ≈ 26 s of a 60 s budget, which was checked before the design was committed to rather than after.
- **`analysis/` now imports `models/api`**, following `graph_builder.py`'s precedent with `models/graph`. Route detection produces wire records directly rather than an intermediate type, because `ServiceEndpoint` is already exactly the shape — method, path, file, line, optional summary — and a parallel dataclass would exist only to be copied field for field.
- **25 of 30 controls mutation-tested one at a time are caught; the 5 survivors are equivalent by construction and each is annotated in the code.** They are: the `app`-segment test in `_is_next_route_file` (re-checked by `_next_route_path`); `parts[:-1]` in that same line (the stem test already forces the filename); case-sensitivity of the Next verb set (a non-uppercase name is refused one line later by `HttpMethod`); the `comment` node-type test (`normalize_comment` refuses non-comment input — the first evidence ADR-020's safety net actually holds); and the explicit path-length check (`MemberPath` refuses the same value). Each is kept because relying on a downstream refusal is a worse contract than not producing the value.

### Alternatives considered

- **A second parse in a self-contained `routes.py`**, mirroring `descriptions.py`'s shape and leaving `parser.py` untouched. Rejected on guard duplication, not on cost — the cost was addressable with a byte-substring prefilter, but the five guards were not, and a prefilter would have added a method-set drift hazard on top.
- **Merging the route patterns into `IMPORT_QUERY`** for a single traversal, dispatching on the pattern index. One traversal instead of two, at the price of entangling two unrelated concerns in one string; the measured 0.48 s across 3000 files did not justify it.
- **NestJS decorator routers** (`@Controller('/base')` + `@Get('/x')`). Deferred: useful only if the class-level prefix is joined onto each method path, which is the most machinery for the least coverage, and a wrong join produces a confidently wrong URL.
- **The Next.js Pages Router** (`pages/api/*.ts`). Rejected for MVP: the handler is a default export and declares no method, so every endpoint's `method` would be a guess.
- **Rewriting Next.js `[id]` to Express `:id`** for a uniform service map. Declined: a service map quotes a repository, it does not translate between frameworks, and `[id]` is what a reader finds when they open the file.
- **Special-casing `@slot` parallel routes and `_private` folders** alongside route groups. Declined for now — each is another framework rule encoded here, and route groups were included only because omitting them produces a URL that genuinely does not resolve.

### Status
Accepted

---

## ADR-022 — 2D Cytoscape.js graph replaces the 3D React Three Fiber scene (supersedes ADR-002; amends ADR-001, ADR-004)

### Decision
The frontend renders the dependency graph as a 2D graph with Cytoscape.js instead of a custom 3D scene built with Three.js/React Three Fiber. Cytoscape's built-in layout algorithms (compound-node-aware, e.g. `cola`/`elk`) replace the hand-written two-phase sphere-packing/force-refinement worker (ADR-004).

### Reason
This is an owner-driven scope change, not a discovery made while building: the goal shifts from matching the original PRD's "3D visualizer" framing to shipping something small enough for one person to read, own, and learn from end to end, with the least amount of bespoke ("vibecoded") rendering code. ADR-002 rejected 2D libraries specifically because "the product is explicitly 3D" — that premise no longer holds, so the rejection no longer holds either.

Cytoscape.js is a mature, documented graph library whose public API is close to the entire surface area the frontend needs. It already does directory collapse/expand via compound nodes and hierarchy-aware layout — the same requirements ADR-002 used to justify writing a custom R3F scene instead of adopting a 3D wrapper library — so adopting it removes both the custom scene code and the custom two-phase layout worker (ADR-004) in one move, without giving up the collapse/expand or hierarchy-legibility requirements.

### Alternatives considered
- Plain Three.js without R3F — still true 3D and still most of the bespoke rendering/layout code this decision exists to remove
- `3d-force-graph` (a thin Three.js wrapper) — less code than raw R3F, but there is no reason to keep 3D once the goal is minimal and learnable rather than maximal fidelity to the original PRD
- `d3-force` + hand-rolled SVG — the most transparent option (every line drawn is code you wrote), but re-implements collapse/expand and compound grouping that Cytoscape provides directly; more code to learn, not less

### Consequences
- ADR-002 is superseded in full; ADR-004 is superseded in full. Both are kept in this file for history, per this project's rule against rewriting past decisions.
- ADR-001's frontend term changes from Three.js to Cytoscape.js (2D); its backend and no-database terms are unchanged.
- ADR-005 (external packages are not graph nodes) and ADR-006 (hierarchy on a `parent` field, not edges) are unaffected — they are graph-shape decisions independent of the rendering library, and Cytoscape's compound nodes consume a `parent`-shaped hierarchy directly.
- The wire contract (`GraphNode`, `GraphEdge`, `AnalyzeResponse`) is unaffected — this is a rendering-layer decision only, consistent with "keep frontend visualization separate from graph-analysis logic."
- `PRD.md` is updated alongside this ADR (title, visualization section, and stack section) rather than left to silently disagree with it, since it was the origin of the "3D" requirement ADR-002 cited.
- Layout runs on the client, same reasoning as ADR-004: it is a presentation concern, and the backend continues to own no visual decision.

### Status
Accepted

---

## ADR-023 — The routing layer: a capped graph from the builder, a worker thread with no `wait_for`, and one exception handler per way out

### Decision

`app/api/` exists: `routes.py` (`POST /api/analyze`, `GET /api/health`), `middleware.py` (request id, request-body cap), `app.py` (the factory and every exception handler), and `app/main.py` as the ASGI entry point. Four decisions inside it are not obvious from the code alone.

**`MAX_NODES` / `MAX_EDGES` are enforced by asking `build_graph` for a smaller graph, not by truncating the graph it returns.** `build_graph` gains an optional `limits: GraphLimits` — a no-default view onto `Settings`, the same shape as `fetch/archive.Limits` — and `None` still builds the whole graph, which is what every direct caller and every determinism test wants. When a cap fires, the node list is cut to a **prefix of component order**, edges naming a dropped node are removed, the edge list is then cut to its own cap, and **only then** is every counter derived. `stats.truncated` is set, sharing the flag the pipeline's two caps already use.

**Nothing is filtered out of `AnalyzeResponse.serviceMap` when the graph is capped**, even when the file declaring a route is no longer a node.

**The analysis runs on a worker thread and there is no `asyncio.wait_for` around it**, contrary to the plan recorded in TODO.md and CURRENT_STATE.md.

**Four exception handlers cover every way a response can leave the application** — `AppError`, `RequestValidationError`, Starlette's `HTTPException`, and a catch-all `Exception` — and `_body` is the single function that constructs an error response.

### Reason

**On capping in the builder.** ADR-018 scoped the cap to the routing layer and named the two ways to apply it: re-derive the stats downstream, or ask the builder for a smaller graph. The first is not a hand-off, it is a re-implementation. A slice of the returned tuples falsifies `stats.dependencies == len(edges)`, every per-node `imports`/`importedBy`, `stats.files`, `stats.directories`, both external/unresolved totals, and every directory's `fileCount`/`totalBytes` — and it can leave an edge naming a node that is gone, which is the one outcome ADR-016's set-membership design exists to make unrepresentable. Rebuilding all of that at the boundary would put two implementations of "how a count is derived" in the project, and the second would be the one nobody mutation-tested. Capping before the counters are computed makes them true by construction, which is the same trade ADR-019 made when it capped imports upstream rather than downstream: *the cap belongs wherever the numbers are still being produced.*

**On dropping the tail of component order.** Determinism alone allows any rule; this one is chosen for a second property that ADR-018 already paid for. A path's parent is a prefix of it in `parts` order, so a parent always sorts before its children and **any prefix of the sorted node list is closed under `parent`**. No surviving node can name a parent that was dropped, with no repair pass and no second traversal. A smarter policy — drop the least-connected leaves, drop the deepest subtrees — would each need their own closure argument and their own justification for which files a user loses; "the front of a deterministic order" is the rule `MAX_SOURCE_FILES` already uses.

**On counts describing the emitted graph.** A capped response reports `externalImports` over the files it contains, not over the repository. The alternative is a total that no set of nodes in the response adds up to, which is exactly the class of lie this ADR exists to prevent; `truncated` is the honest signal that the view is partial. Same reasoning for `stats.files`, which is now counted off emitted file nodes rather than off `analysis.files`.

**On the service map surviving.** It is not part of the graph. It is a separate deterministic view keyed by path, capped independently by `MAX_SERVICE_ENDPOINTS`, and rendered as a list rather than as something clickable-by-node-id. Shortening a repository's reported API surface because a *graph size* cap fired would make a deterministic list quietly wrong — the same "absent beats invented" concern from ADR-021, running the other way. A frontend that resolves `endpoint.file` against the node map must tolerate a miss, which docs/SECURITY.md already requires of it for dangling edges.

**On the worker thread, and on the absence of a timeout around it.** `analyze_repository` blocks for a streaming download plus a tree-sitter parse per file; `resolve_imports` and `build_graph` add a clockless CPU phase measured at up to ~8 s (ADR-019). On the event loop that stalls `/api/health` — and, later, the rate limiter — for the whole 60 s budget, so the entire blocking span goes to `asyncio.to_thread` in one piece. A `wait_for` around it was the plan of record and is declined: docs/SECURITY.md already says it is not the real mechanism, because it cannot kill a thread. It would return a 504 while the work continued, and a client that retried on that 504 would *add* live threads rather than shed them — a timeout that makes an overloaded server worse. The bound that actually holds is cooperative and already built: `Deadline` between archive members and around every parse, httpx's connect/read timeouts under it, and `MAX_IMPORTS` over the phase with no clock. Shedding load is `MAX_CONCURRENT_ANALYSES`' job and it is Day 3's.

**On the handlers, and on why the body cap writes its own response.** `RequestValidationError` must be mapped to a bare `INVALID_REQUEST` because pydantic's `detail` embeds the offending `input` verbatim, which is docs/SECURITY.md's "Pydantic validation echoing user input" row; FastAPI's default handler returns exactly that, so the override is the control. `HTTPException` needs one too: an unknown path and a wrong method are the two most reachable responses in the service and would otherwise be the only two that answer `{"detail": …}` instead of the contracted body.

`BodySizeLimitMiddleware` is the one place that builds a response outside `app.py`, and it has to be. The tidier design — raise `PayloadTooLargeError` from inside `receive()` and let the `AppError` handler shape it — does not survive contact with FastAPI: `fastapi.routing.get_request_handler` wraps the body read in `except Exception: raise HTTPException(400, "There was an error parsing the body")`, so the typed error is swallowed and the client is told its JSON was malformed **with a 400** rather than that its body was too large with a 413. Verified against fastapi 0.141.1. The middleware therefore refuses the request itself, *before the application runs* — which is also the stronger property: an oversized body never reaches application code at all.

### Consequences

- **`MAX_NODES` / `MAX_EDGES` are enforced for the first time**, closing the last open row of docs/SECURITY.md's "Unbounded graph". The bound that already existed was indirect (`MAX_SOURCE_FILES` caps file nodes and nothing else); directory nodes and edges now have one.
- **A directory whose children all fell past the cut survives as a node with `fileCount` 0**, because it sorts before them. Honest — the directory is really in the repository — and it keeps `root.fileCount == stats.files` true. Pinned by test.
- **`_edges` runs before the cap**, so its two preconditions still fire when the mispaired record's file would have been dropped. A programming error that reports itself only sometimes is worse than one that never does.
- **`stats.truncated` now has three producers** — `MAX_SOURCE_FILES`, `MAX_IMPORTS`, and the graph caps. They are deliberately not distinguished on the wire: a consumer's response to all three is the same, and `Stats` is a frozen contract the frontend mirrors.
- **The request has an id**, generated per request and never read from an inbound header, carried on `scope["state"]`, echoed as `X-Request-ID` and in every error body, and attached to the router's log records via `logging_setup`'s existing `request_id` extra.
- **`/docs` and `/redoc` are disabled; `/openapi.json` is not.** Both rendered pages load their JavaScript from a public CDN, which would make a third-party script the only remote resource this backend serves — a supply-chain surface bought for a convenience the frontend gets from the schema anyway.
- **`configure_logging()` lives in `app/main.py`, not in `create_app()`.** It replaces the root logger's handlers, which is right for a server and would tear `caplog` out from under a test that built an app.
- **`AnalyzeResponse.componentDiagram` shipped as `None` with a TODO**, because `app/analysis/component_diagram.py` did not exist on this branch. The field defaults to absent by contract (ADR-013), so the response was valid without it. *(**Joined 2026-09-01**, the same day: the generator landed in parallel as [ADR-024](#adr-024--the-component-diagram-draws-containers-one-unnamed-external-system-and-routes-node-ids-are-synthetic) and `_analyze_blocking` now calls it on `build_graph`'s **capped** output — the cap matters, since a diagram built from the uncapped `RepositoryAnalysis` would name containers holding no node the client received.)*
- **19 controls were mutation-tested one at a time; all 19 are caught.** Two needed work rather than being caught first time. One "survivor" turned out to be a **no-op mutation** — `_nodes` is only ever handed the surviving files, so there is no line to delete; rewritten as passing the full mapping instead, it fails four tests. The other was a **real gap**: deleting the declared-`Content-Length` check left everything green, because the byte counter catches the same request one cap's worth of bytes later. The difference the header check buys is whether an oversized body is *read*, so a test now asserts that not one chunk is pulled.
- **`tests/` uses `httpx.ASGITransport`, not `starlette.testclient`.** TestClient's httpx integration is deprecated in this Starlette and emits a `DeprecationWarning`, which `filterwarnings = ["error"]` turns into a failure; the transport drives the same app in process, opens no socket, and needs no new dependency.
- Still not done at this layer and still Day 3's: the rate limiter, the concurrency gate, and CORS.

### Alternatives considered

- **Re-derive the stats in the router after truncating `build_graph`'s output.** ADR-018's other branch. Six counters and two structural invariants re-implemented at the boundary; see above.
- **Cap inside `resolve_imports` instead.** Same shape as ADR-019's rejected option and wrong for the same reason: the resolver does not know what a node is, and the builder would still compute its counters from whatever it was handed.
- **Raise a typed error from the body-cap middleware.** Swallowed by FastAPI into a 400; measured, not assumed.
- **`asyncio.wait_for` around the analysis.** Returns a response while the thread keeps running; converts a retry into a thread leak. The `Deadline` is the bound.
- **A `nodesTruncated` / `edgesTruncated` flag beside `truncated`.** `Stats` is a frozen wire contract the frontend mirrors verbatim, and the distinction changes nothing a consumer does. The server-side log line carries the caps and the emitted counts, which is where an operator needs it.
- **Filtering `serviceMap` to surviving nodes.** Makes a deterministic list depend on an unrelated cap.

### Status
Accepted

---

## ADR-024 — The component diagram draws containers, one unnamed external system, and routes; node ids are synthetic

### Decision

`app/analysis/component_diagram.build_component_diagram(nodes, edges, stats, service_map)` turns the finished graph into Mermaid `flowchart LR` source. It fixes five things ADR-013 left as a sentence.

**Its input is the finished graph and the service map, and nothing else.** It is not given `RepositoryAnalysis`, and it is not given the resolver's `ResolvedImport` records.

**A container is a top-level directory, derived from *file node paths* rather than from directory nodes** — the first path component, or a synthetic root container for files sitting directly in the repository. Containers are selected by file count when there are more than `_MAX_CONTAINERS`, and rendered in `PurePosixPath.parts` order so the root comes first, matching [ADR-018](#adr-018--the-repository-root-is-a-node-node-identity-is-the-path-and-directories-are-inferred).

**External packages become one box with a total, not one box per package.** It is labelled `External packages · N imports`, and every container with a positive `externalImports` count points at it with its own count on the arrow.

**Node identifiers are synthetic** — `c0`, `r0`, `ext`, `api`, `repo`. No repository-authored string is ever concatenated into an identifier, an arrow, or a subgraph name; repository text occupies exactly one position in the output, inside a double-quoted label, where `_label` drops non-printables and a small set of Mermaid/HTML metacharacters and caps the result.

**`MAX_COMPONENT_DIAGRAM_CHARS` is enforced while writing.** `_Writer` refuses a line that would not fit; a subgraph's closing `end` is reserved before its header is written; a block that ends up with no contents is rewound; and an arrow is written only when both of its endpoints were emitted.

### Reason

**On the external system being singular and unnamed.** [ADR-005](#adr-005--external-packages-are-not-graph-nodes) decided that a bare specifier is a count on the importing node and never a node of its own, so by the time the graph is finished the package *names* are gone: `resolver.ResolvedImport.specifier` is the last place one exists, and this module is deliberately downstream of it. Taking the resolver's output as a second input would have bought named boxes at the price of a new class of repository-authored text in a response body — package specifiers — which is an ADR-013-sized decision about a sink, not a detail of a drawing. `graph_builder.py`'s docstring says extracting a package name from a specifier is "the component diagram's job later"; *later* is the operative word, and this is a note that the seam is understood, not that it is open. The honest drawing of a count is a box with a count in it.

**On containers coming from file paths.** Reading them off directory *nodes* is the obvious implementation and it is wrong on one real input: ADR-018 resolves a `components.ts` / `components.ts/x.ts` collision in the file's favour and creates no directory node, so `x.ts`'s container would silently vanish. First-path-component needs no node to exist and cannot disagree with the node set.

**On synthetic ids, which is the whole security argument.** A directory name, a route path and a route summary are all attacker-controlled (CLAUDE.md: the repository is untrusted data, *including its comments*). Sanitizing them is necessary and is done, but sanitizing is a denylist and a denylist is a claim about a grammar we did not implement. Making identifiers synthetic removes the position from which any of that text could become syntax, which is CLAUDE.md's "prefer eliminating a vulnerability class architecturally" applied to a text format — the same move [ADR-003](#adr-003--never-write-repository-data-to-disk) makes about disk. The sanitizer is then the second line, not the only one, and a test asserts the *structural* property directly by checking that no marker string appears anywhere outside a quoted label.

**On the cap being applied while writing.** Truncated Mermaid is not Mermaid: cutting the string leaves an unclosed `subgraph` or a half-written label. This is [ADR-020](#adr-020--the-description-is-a-byte-prefix-scan-not-a-second-pass-over-the-ast)'s "the cap is applied while cleaning, not after", restated for output that has structure rather than output that is a label. The item caps exist for a different reason and are not the bound: a repository with 300 top-level directories has no readable component diagram at any character count, and the caps are sized so the worst legible case (measured: 8 685 characters) lands well under the 20 000 default, which keeps the character limit a safety net rather than the thing that decides ordinary output.

### Consequences

- **`MAX_COMPONENT_DIAGRAM_CHARS` has a producer.** It was the last limit in `Settings` that bounded nothing (docs/SECURITY.md). It is now enforced twice, as `MAX_DESCRIPTION_CHARS` is: while writing, which is the one that matters, and again by `ComponentDiagramSource` at the model boundary, which catches a future producer that forgets.
- **The diagram is the third and last repository-authored text sink in the response**, after `GraphNode.description` and `ServiceEndpoint.summary`, and the only one where the text lands inside a *format* rather than beside one. docs/SECURITY.md's "Repository-authored text in responses" section gains its diagram half.
- **`None` is a normal return**, for a graph with no file nodes and for a character limit too small to hold one container. `AnalyzeResponse.componentDiagram` is optional for exactly this.
- **Ordering inside the output is a priority, not a layout.** Containers are written before routes so that a short budget spends itself on the thing ADR-013 names first; in an `LR` flowchart it is the arrows that place a box, so the picture is unaffected.
- Like `resolver.py` and `graph_builder.py`, the module is pure and has **no logger**, both pinned by test.
- **It shipped with no caller**, the third module in a row to do so. *(**Joined 2026-09-01**, the same day: `app/api/routes.py` calls it on `build_graph`'s capped output. The cap matters — a diagram built from the uncapped `RepositoryAnalysis` would name containers holding no node the client received.)*

### Alternatives considered

- **One external system per package**, taking `tuple[ResolvedImport, ...]` as a fourth input and deriving names from specifiers (`@scope/pkg`, `lodash/fp` → `lodash`, `node:fs`). Much better for a demo. Rejected for this change: it contradicts the stated input (the finished graph), and it introduces repository-authored package names into a response body, which needs its own decision rather than arriving as a side effect of a diagram. The seam is one parameter wide if that decision is ever made.
- **Escaping repository text rather than removing characters from it.** Mermaid has entity codes (`#quot;`), so a faithful escaper is possible. Rejected as the primary defence for the reason synthetic ids exist: an escaper is a claim about a grammar, and the grammar belongs to a renderer we do not control and a version we do not pin. Removal is lossy and legible; a broken escape is neither.
- **Substituting a placeholder character for a removed one.** Rejected for `descriptions.py`'s reason about the ellipsis: a label is a quotation, and the substituted character would be the only text in it the repository did not write.
- **Deriving containers from directory nodes at `depth == 1`.** One line shorter, and silently loses a container on the file/directory collision ADR-018 already decided to tolerate.
- **Truncating the finished string to the character cap.** Produces invalid Mermaid at exactly the moment the cap matters.
- **Emitting `<br/>` to put a route summary on its own line.** Rejected: it means writing markup into a document whose whole safety argument is that repository text never becomes markup, and then maintaining the distinction between our HTML and theirs. A hyphen costs one line of legibility and no argument.

### Status
Accepted

---

## ADR-025 — The component diagram is adopted from an inert document behind an element allowlist, not injected as HTML

### Decision

`frontend/src/ui/ComponentDiagram.tsx` renders `AnalyzeResponse.componentDiagram` with **`mermaid` 11.17.2**, exact-pinned. Four things are decided here.

**Mermaid is configured `securityLevel: 'strict'` and `htmlLabels: false`**, and both are security controls rather than styling. `htmlLabels` is set at the **top level**, not as `flowchart.htmlLabels`, which is deprecated at this version and logs a warning.

**The SVG string is never injected as markup.** `mermaid.render()` returns a string, so something must turn it into nodes; that something is not `innerHTML` and not `dangerouslySetInnerHTML`. The string is parsed as `image/svg+xml` into an **inert** document, inspected there, and only then `importNode`d into the live one.

**The inspection is an element allowlist and refuses rather than scrubs.** Fifteen elements are permitted — `circle defs feDropShadow filter g linearGradient marker path polygon rect stop style svg text tspan` — plus a denylist over the scriptable attribute classes (`on*`, `href`, `*:href`, `base`). Anything else, a failed XML parse, or a root that is not an `<svg>` in the SVG namespace blanks the panel and shows why.

**A Mermaid parse failure reports a fixed message and never the exception.** Mermaid's parse errors quote the offending source line, and that line contains repository text.

### Reason

**On why this render needed a decision at all.** `GraphNode.description` and `ServiceEndpoint.summary` are strings that sit *beside* a format: React escapes them and the argument ends. `componentDiagram` is different in kind — [ADR-024](#adr-024--the-component-diagram-draws-containers-one-unnamed-external-system-and-routes-node-ids-are-synthetic) puts repository text *inside* Mermaid source, and the renderer hands back markup. Every other sink in the app is satisfied by "render it as a text node". This one cannot be, because by the time we hold it, it *is* markup.

**On `htmlLabels: false` being the control.** With html labels on, Mermaid emits a `foreignObject` full of HTML and *widens* its own DOMPurify pass to preserve it (`ADD_TAGS: ['foreignobject']`, `HTML_INTEGRATION_POINTS: { foreignobject: true }`). Turning it off means a label is the text content of an SVG `<text>`/`<tspan>`, which has no markup parser — the same structural argument `scene/GraphCanvas.tsx` makes about painting Cytoscape labels to a `<canvas>`, and the same move ADR-024 makes about identifiers: remove the sink instead of filtering what flows into it.

**On reading the renderer rather than trusting it.** Two things about Mermaid 11.17.2 are the opposite of what its reputation implies, and both were read out of the installed package and then confirmed in a browser:

- `sanitizeText` looks like it escapes angle brackets. It does not, here. The half that does — `sanitizeMore` — is **gated on html labels being on**, so with them off it is a no-op. What runs unconditionally is `DOMPurify.sanitize(text, { FORBID_TAGS: ['style'] })`. **The protection is DOMPurify, not entity-escaping**, and a comment claiming otherwise would have been confidently wrong.
- Mermaid's html-label output is **not well-formed XML** (a `foreignObject` carrying `<img src="x">` fails with "Opening and ending tag mismatch: img"). That is what makes the XML parse a *gate* rather than a formality: if `htmlLabels` is ever flipped back on, the panel goes blank and says so instead of quietly growing an HTML sink. The config mistake that matters is the one it fails on.

**On why the safety cannot live in the injection call.** Both obvious options are unsafe for arbitrary markup, for reasons worth writing down because both are widely believed to be safe:

- `innerHTML` runs the HTML fragment parser, which flags fragment-parsed `<script>` as already-started so it never executes — but it does **nothing** about `onload=` / `onbegin=` / `onerror=`, which fire normally. "innerHTML doesn't run scripts" is true and is not the property needed.
- `DOMParser` + `importNode` is not automatically better: an SVG `<script>` created through the DOM **does** execute on insertion.

So the safety has to come from *what* is injected, which is why the check exists and why it runs against an inert document — one with no browsing context, where nothing executes and no handler fires while it is being examined.

**On refusing rather than scrubbing.** `api/limits.ts` already states the house preference: a valid thing rejected is loud, a thing silently altered is not. A scrubbed diagram is a picture that is subtly not the repository's; a refused one is a message.

**On the allowlist being observed rather than designed.** The fifteen elements are the measured output for the golden fixture, for subgraphs and arrow labels, and for a diagram built entirely of hostile labels. A general-purpose SVG allowlist would have been longer and would have admitted things Mermaid never emits. The version is exact-pinned, so the set can only drift on a deliberate upgrade — at which point the panel refuses and names the element, which is the intended way to find out.

**On the attribute rule being a denylist while the element rule is an allowlist.** The asymmetry is deliberate. The scriptable attribute space in SVG is small and well characterized; the decorative space is large and grows across patch releases. An attribute allowlist would blank the panel over a new `stroke-linejoin`.

### Consequences

- **`componentDiagram` has a sink**, and docs/SECURITY.md's "Repository comment rendered as HTML (XSS)" row gains the first control in that document **verified against an observed browser render** rather than by reading code. Hostile labels were driven through the real renderer: `<script>…</script>` is deleted, `<img src=x onerror=…>` survives only as literal characters in a `<text>` node, a `</text><script>` break-out disappears, and the emitted SVG carries no `<script`, no `on*=` and no `foreignObject`.
- **All four refusal paths were driven, and each refused.** ~~and each fired at its intended layer — which is the part that makes the design testable rather than merely arguable.~~ **Corrected 2026-09-01; see "Correction" below.** Three of the four fire where this ADR said; the `click … href` path does not. A `click c0 "javascript:…"` directive makes Mermaid emit an `<a>` (its href already dropped by Mermaid's own `sanitize-url`) and the element allowlist refuses it, so no anchor ever entered the live DOM; a `click c0 href "https://…"` directive is refused by the **element allowlist and the attribute denylist**, not by the XML parse; malformed Mermaid and an unknown diagram type both surface as the fixed message.
- ~~**The row stays `Partial`, for a new reason.** The evidence is real and not repeatable: the verification was interactive and the frontend has **no test runner at all**. Automating it is now the highest-value frontend test, and it needs no backend.~~ **The automation landed 2026-09-01, later the same day** — `frontend/vitest.config.ts`, `frontend/src/ui/ComponentDiagram.test.tsx`, `frontend/src/no-markup-sinks.test.ts`: **vitest 4.1.11 + jsdom 30.0.1, 35 tests, `npm test`**, all passing. It is what found the error corrected above. The row stays `Partial` anyway, for a *third* reason, narrower than the second: **no real browser is driven**, so this is jsdom's parser and DOMPurify under it rather than Chrome's — see "Correction" for the two stubs and the one parser patch that makes jsdom conform.
- **The frontend's dependency count is no longer small.** `mermaid` pulls 159 transitive packages, including a second copy of `cytoscape`. docs/SECURITY.md's "Compromised or unmaintained dependency" row is updated to say so rather than leave "kept deliberately small" standing as a claim it no longer supports. The trade was taken knowingly: hand-writing a Mermaid renderer is worse in every direction, and the mitigation is that the package's *output* is treated as untrusted markup, not that the package is trusted.
- **`src/graph/fixture.ts`'s `componentDiagram` was rewritten** to the shape the generator actually emits — synthetic ids, `%%` header comments, `flowchart LR` — derived from the fixture's own numbers. The previous value used `src` / `api` / `lib` as node *identifiers*, which read as though directory names became syntax; they never do (ADR-024). It also gained a standing markup probe in a label, the diagram-shaped counterpart of the `<script>` already planted in a description.
- **The panel is not wired into `App.tsx`.** This is the render, not the layout decision, and nothing imports it yet.
- **One residual is recorded rather than closed.** `<style>` is on the allowlist, because Mermaid scopes its generated CSS with `#id` selectors and the panel is unreadable without it. CSS cannot execute script, so the residual is exfiltration via `url()` in a crafted stylesheet — unreachable from repository text (ADR-024 confines it to quoted labels) and closed by the planned CSP `default-src 'self'`.

### Correction — 2026-09-01, when the interactive verification was automated

Automating this ADR's browser session (vitest 4.1.11 + jsdom 30.0.1, 35 tests) contradicted one of its factual claims and turned up one environment trap worth recording. **The decision is unchanged and no control moved**; what was wrong was a description of *which layer* refuses one input.

**1. `click c0 href "https://…"` does not emit `xlink:href`, and the XML parse is not what refuses it.**

The Consequences bullet above said the directive "emits `xlink:href` with the prefix undeclared, and the XML parse refuses it". At mermaid 11.17.2 that is not what happens. Observed in `ComponentDiagram.test.tsx` (`click c0 href "https://..." is refused by the element and attribute checks, not the XML parse`):

- mermaid emits a **plain `href`** on an `<a>`: `<a href="https://example.com/" data-look="classic" transform="…">`.
- There is no `xlink:href` and no `xmlns:xlink` anywhere in the output.
- Confirmed against the **raw serialization under `securityLevel: 'loose'`** — the one branch that skips mermaid's whole-SVG DOMPurify pass — so this is mermaid's own output, not a DOMPurify rewrite.
- The document is therefore well-formed XML and **step 2 never fires**.

The panel still refuses, twice over: `a` is outside `ALLOWED_ELEMENTS` *and* `href` is on the `isForbiddenAttribute` denylist. Observed message: `unexpected <a> in the rendered diagram`. So this is **security-neutral** — a defect in this ADR's account of the mechanism, not a hole in the control. The XML-well-formedness gate keeps its stated value for the case it was actually argued from, html labels producing markup that is not well-formed XML; that case is still exercised, and the undeclared-prefix path is exercised too, both by handing the component a crafted document rather than by relying on mermaid to produce one.

**2. parse5 8.0.1 is missing one row of the HTML spec's "adjust SVG tag names" table, and it silently breaks the allowlist under jsdom.**

`ComponentDiagram.test.tsx` patches a single entry into parse5's `SVG_TAG_NAMES_ADJUSTMENT_MAP`. The reason is a trap rather than a convenience, so it is recorded here:

- parse5 8.0.1 — jsdom's HTML parser — carries **36** of the table's **37** rows. The one it lacks is `fedropshadow` → `feDropShadow`. Verified locally against the installed package: its key set is exactly the spec table's minus that row, with no extras.
- mermaid emits `<feDropShadow>` in **every** `theme: 'dark'` flowchart, inside its node-shadow filter — not an edge case reachable only from a hostile fixture.
- mermaid's own DOMPurify pass **reparses the serialized SVG as HTML**, and the HTML parser lowercases foreign element names unless that table restores them.

So under stock jsdom the element arrives as `fedropshadow`, misses `ALLOWED_ELEMENTS`'s camelCase `'feDropShadow'`, and the panel refuses **every** diagram — while a real browser, implementing the full table, draws it. Without the patch a correct component looks broken; the first version of that suite reported exactly this as a production bug. The patch makes the environment conform to the spec the component is written against, and is itself asserted (the gap was really there, and closing it changes the parse).

**What the suite does and does not establish.** It covers mermaid's real output for the hostile-label fixture (no `<script>`, no `on*`, no `foreignObject`), the panel's live DOM after mounting, all four refusal paths plus the not-an-SVG path — each isolated by stubbing `mermaid.render`'s return value so the exact refusal message can be pinned — that a mermaid parse failure surfaces only the fixed message and never the exception (mermaid does quote repository text in its parse errors, and that is asserted rather than assumed), the `htmlLabels: false` and non-`loose` `securityLevel` controls observed through their effects, and that no file in `src/` uses `dangerouslySetInnerHTML` / `innerHTML` / `outerHTML` / `insertAdjacentHTML` / `document.write`.

It does **not** drive a real browser. Two SVG-metric stubs (`getComputedTextLength`, `getBBox`) are installed because jsdom implements no SVG text metrics; both move geometry only and nothing asserted is geometric. The standing gap is that **the `feDropShadow` behaviour has not been re-confirmed in Chrome** since the suite was written — the element-allowlist assertions rest on the assumption that a real browser restores the camelCase name, which is what the spec requires and what the original interactive session implicitly showed by rendering at all, but it has not been checked deliberately. Worth one browser run; recorded as open rather than quietly assumed.

### Alternatives considered

- **`dangerouslySetInnerHTML`, on the grounds that Mermaid already DOMPurifies its output.** It does, and the premise is true: `securityLevel` is not `loose`, so `render` runs the whole SVG through DOMPurify. Rejected because it makes the app's safety a property of a third-party version bump. CLAUDE.md asks for validation at every boundary, and the browser is a boundary we own.
- **Plain `innerHTML` instead**, on the grounds that it neutralizes `<script>`. Rejected on the facts: it does not neutralize event-handler attributes, which is the actual SVG XSS primitive.
- **Rendering into an iframe with `sandbox`.** Genuinely strong, and Mermaid even has a `securityLevel: 'sandbox'` that does it. Rejected for the MVP: the diagram has to size itself inside a flex panel and be legible at the app's theme, and an iframe makes both awkward — it needs postMessage height plumbing and its own stylesheet. Worth revisiting if the panel ever becomes interactive, which is when the trade flips.
- **Scrubbing the offending nodes instead of refusing the document.** Rejected for `limits.ts`'s reason: silent alteration of a picture is indistinguishable from a correct picture.
- **A full attribute allowlist alongside the element allowlist.** Rejected as brittle in the wrong direction — see the asymmetry argument above.
- **Sanitizing the Mermaid *source* on arrival, in addition to the backend's `_label`.** Rejected as the wrong layer twice over: it re-implements a grammar we did not write (ADR-024's stated reason for not escaping), and it defends the input to a renderer whose *output* is the thing that reaches the DOM.
- **Skipping the version check and pinning a recalled `mermaid` version.** Not a real option — CLAUDE.md forbids it — but worth recording that 11.17.2 was confirmed against the registry, and that `flowchart.htmlLabels` turned out to be deprecated at it, which a recalled config would have got wrong.

### Status
Accepted

---

## ADR-026 — File-dominant rendering with fCoSE layout; compound directory boxes demoted to a color-tint grouping cue (amends ADR-022)

### Decision

Replace the `cose` layout with `cytoscape-fcose` in `frontend/src/scene/style.ts`. Stop rendering directory compound nodes as filled, bordered, padded boxes; keep `GraphNode.parent` flowing into Cytoscape's `data.parent` (unchanged wire contract, unchanged `elements.ts` drop-invariants) purely as a layout/grouping signal for fCoSE and a possible future collapse/expand feature, not as a dominant visual element. Directory membership is instead conveyed to the user via a per-file `background-color` tint keyed to its top-level path segment (`frontend/src/scene/directoryColors.ts`), with an HTML/CSS legend outside the canvas (`frontend/src/ui/Legend.tsx`) rather than in-canvas directory labels.

### Reason

Hands-on testing against `expressjs/express` (141 files, 41 directories, 153 real import edges — confirmed via a direct API call and DOM inspection, not assumed) showed nested compound boxes as deep as 5 levels, each padded 14px, consuming most of the canvas and reducing files to near-invisible dots at the center; directories dominated the visual, imports did not drive clustering, and canvas margins were mostly empty compound-box padding. The backend's dependency data was verified correct and rich — the defect was entirely in frontend rendering/layout choice, not analysis.

Separately, the `cose` layout's replacement was always an open decision: `style.ts`'s own prior comment and `docs/ARCHITECTURE.md` both said the final pick among `cola`/`elk`/`dagre` was deferred pending "real repository-sized graphs to measure against, not a ten-node fixture" — that measurement had never happened before this. `cytoscape-fcose` is chosen over those alternatives because it treats compound nesting as a soft constraint rather than a dominant structural signal (so `parent` can still shape the layout without forcing deep nested boxes) and because it is built to scale into the thousands-of-nodes range this project's real ceiling requires (`MAX_NODES=6000`/`MAX_EDGES=20000`, `frontend/src/api/limits.ts` and `backend/app/config.py`), not just a 141-node demo repo.

### Alternatives considered

- **`cytoscape-cola`** — weaker compound-node handling than fCoSE and slower convergence at the node counts this project's ceiling requires.
- **`cytoscape-elk`** — excellent for strictly layered/hierarchical diagrams, but import graphs are not DAGs (real codebases have import cycles); a layered algorithm would visually imply a hierarchy the data doesn't guarantee, reintroducing "structure dominates" through a different mechanism. Also the heaviest of the three — it bundles a full Java-derived layout engine port.
- **`cytoscape-dagre`** — same rank-direction/tree assumption problem as `elk`.
- **Keep compound boxes, just shrink padding/`nestingFactor`.** Tested informally; does not address the deeper problem that compound nesting itself — not merely its padding — is the wrong dominant visual signal for an import graph.
- **Drop `parent` from `elements.ts` entirely**, since directories are no longer drawn. Rejected: it would block a future collapse/expand (PRD §7) for no benefit, since retaining `parent` costs nothing once directories aren't rendered as boxes, and fCoSE still uses it as a grouping signal.

### Consequences

- New dependency: `cytoscape-fcose@2.2.0` (peer-dep compatible with the pinned `cytoscape@3.34.2`, confirmed against the npm registry; pulls one small transitive, `cose-base@2.2.0`). `@types/cytoscape-fcose@2.2.5` added as a devDependency for the strict + `exactOptionalPropertyTypes` build; its `ready`/`stop` handler types disagree with cytoscape's own `BaseLayoutOptions` under that flag, absorbed with one explained cast at the `GRAPH_LAYOUT` export boundary in `style.ts` rather than at each call site.
- ADR-022's "Cytoscape's compound nodes consume a `parent`-shaped hierarchy directly" is amended, not superseded: `parent` is still consumed directly by Cytoscape, but the *rendering* of that hierarchy as filled/bordered boxes is removed in favor of a color-tint-plus-legend cue. ADR-022's core decision (Cytoscape.js, 2D, client-side layout) is unaffected.
- ADR-005 (external packages are not graph nodes) and ADR-006 (hierarchy on a `parent` field, edges are imports-only) are unaffected — this is a rendering-layer decision only; the wire contract does not change.
- A real, separate bug was fixed alongside this change: `GraphCanvas.tsx`'s `ResizeObserver` callback called `cy.resize()` then `cy.fit()` synchronously against `container.clientWidth/clientHeight`, which could read a stale box mid-reflow — the observed symptom was a graph staying shrunk in its original bounding box after a later viewport resize, with nothing ever correcting it. Fixed by reading the observer entry's `contentRect` instead of re-querying the DOM, deferring the fit to `requestAnimationFrame`, and adding a bounded (2-attempt) re-check that compares `cy.width()/height()` against the observed size before giving up. This fix is independent of the layout engine choice and would have been needed regardless of which layout replaced `cose`.
- Collapse/expand, search, and camera-focus-on-node (PRD §1/§7) remain unimplemented — out of scope for this change — but unblocked: `parent` data is still present and grouped by fCoSE, so none of the above closes off adding them later.
- Directory compound nodes are still present in the Cytoscape element set (not deleted), styled with zero background opacity and zero border width, purely as a layout hook — no visual footprint, but available if collapse/expand needs them later.

### Correction (2026-09-02) — the "no visual footprint" claim above was false in one state

The consequence directly above held only while nothing was selected. `GraphCanvas.tsx`'s selection effect classed `node.closedNeighborhood().union(node.ancestors())` as `neighbor`, and `ancestors()` on a file node *is* its enclosing directory chain — so selecting any file put the `node.neighbor` class on its directory boxes. In a Cytoscape stylesheet the later rule wins for a shared property, and `node.neighbor` (`border-width: 2`, `overlay-opacity: 0.12`) sits **after** `node.directory` (`border-width: 0`, no overlay). The invisible compound boxes therefore came back on every selection as bordered, blue-washed rectangles spanning large parts of the canvas, and cleared again on deselect — reported as the selection highlight "glitching".

Two changes, both kept, because they guarantee different things:

- **`GraphCanvas.tsx`** no longer classes directory nodes at all. The `.union(node.ancestors())` is dropped and `selected`/`neighbor`/`context`/`faded` are scoped to `cy.elements().difference(cy.nodes('.directory'))`. The ancestors were only ever unioned in to keep a selection from fading the box it sits in — a box that this ADR stopped drawing, so the union had no remaining purpose.
- **`style.ts`** appends a final `node.directory` rule zeroing `background-opacity`, `border-width`, and `overlay-opacity`. Being last, it wins over every state rule, so ADR-026's guarantee no longer depends on one call site's element-set arithmetic staying correct.

Verified in a real browser against `expressjs/express` (141 files, 41 directories) by reading computed Cytoscape styles: across select-hub → select-other → select-hub-again → deselect, zero directory nodes carry a state class and zero have a non-zero border/overlay/background; forcing `neighbor` and `selected` onto a directory node by hand still yields all three at 0.

Two unrelated fixes landed in the same pass and are recorded here rather than as their own ADR, since neither changes a decision:

- **Opening camera.** `fit: true` framed the whole graph, which on a real repository puts the entry point off to one side at an unreadable zoom. The canvas now fits, and then — if that fit zoom is below a legibility floor (`INITIAL_ZOOM` in `style.ts`) — zooms to the floor and centers the highest-degree node, which on a real repository is the entry point.
- **Stale hover.** `mouseout` is not guaranteed to fire for every `mouseover`, so a missed one left a node wearing the hover halo permanently. `hovered` is now a singleton and is cleared on the container's `pointerleave`.

Layout spacing was also loosened (`idealEdgeLength`, `nodeRepulsion` up; `gravity` down) — tuning, not a decision. *(Superseded within the day by ADR-027, which measured that raising them further is counter-productive and settled on 90/8000.)*

### Status
Accepted (with the 2026-09-02 correction above)

---

## ADR-027 — A deterministic post-layout overlap separation pass, and non-interactive edges and directory boxes (amends ADR-026)

### Decision

Add `frontend/src/scene/overlap.ts`: a deterministic, run-once separation pass that runs between fCoSE settling and the camera being aimed, and moves leaf nodes apart until their *rendered footprints* — dot plus the label drawn to its right — no longer overlap. Stop treating fCoSE's force parameters as the anti-overlap mechanism; they are responsible for the graph's shape, and `separateOverlaps` is responsible for legibility. Make edges and directory compound nodes non-interactive (`events: 'no'`), and disable box selection.

### Reason

Three separate user-reported defects, all in the same interaction surface, none of which the previous ADR's tuning could fix:

1. **Overlapping nodes.** fCoSE is a force-directed layout, not a constraint solver — `nodeRepulsion` makes overlap unlikely, never impossible, and its component packer places disconnected subgraphs by bounding box, so nodes from different components routinely land on top of each other. ADR-026's correction tried to fix this by raising the force constants. Measuring that against `expressjs/express` showed it made things *worse*: 12000/110 grew the bounding box from 3385x1726 to 4305x4465 — halving the zoom at which the graph fits — and still left 14 overlapping pairs, because a longer ideal edge scatters clusters into each other's space faster than repulsion clears them. Force tuning is the wrong instrument; separation is a constraint, so it wants a constraint solver.
2. **A drag starting on an edge did nothing instead of panning.** Edges are hairlines at 12–35% opacity — invisible as pointer targets, but Cytoscape still routed the grab to them.
3. **Dragging apparently-empty canvas moved a whole subtree.** A compound node's body is the entire box its children occupy, and dragging it drags every child. ADR-026 made those boxes invisible but left them interactive, so the graph had large regions where a drag silently rearranged the layout.

### Alternatives considered

- **Keep tuning fCoSE's forces.** Measured and rejected — see above. It also cannot address the packer, which is where the worst pile-ups come from.
- **`cytoscape-layout-utilities` / a layout with built-in overlap removal.** A new dependency for one function, and it would still not know about the label geometry, which is the part that actually collides here — the dots are 9–20px and the labels run to 200px+.
- **Separate the dots only, ignoring labels.** Measured: it converges better (1,254 vs 5,627 residual pairs on `astro`) but optimises the wrong thing. Colliding *labels* are what reads as crowding; two dots 20px apart with their names written through each other is the reported defect.
- **Globally scale the layout until nothing overlaps.** Measured on `astro`: ~3x scaling reduces the residual to 959 pairs but sprawls the graph to 14,917 units. It trades a defect visible only when zoomed in for one that is inescapable at every zoom.
- **Make edges non-interactive by `unselectify()`/`ungrabify()` instead of `events: 'no'`.** Those flags do not stop the renderer from treating the element as the pointer target, which is the actual problem.

### Consequences

- **No new dependency.** The pass is ~120 lines of plain geometry over Cytoscape's public position/dimension API.
- **Determinism is preserved** (CLAUDE.md: the same commit produces byte-identical output). Nodes are visited in Cytoscape insertion order, which is the backend's sorted order (ADR-018); every displacement is a pure function of the two footprints; the exactly-coincident case is broken by node index via a golden-angle spiral, never by a random jitter. Note this is a property of the *pass*, not of the layout — fCoSE itself still runs with its default `randomize`, so successive loads of the same commit do not produce the same picture. That was already true before this ADR.
- **It is a guarantee at small and mid sizes and a best effort in the thousands**, measured in a browser rather than asserted: `expressjs/express` (141 file nodes) goes to **0** overlapping pairs in ~10 ms; `withastro/astro` (2,953 file nodes, the largest repository the backend will accept — see below) goes from **171,600 to 5,627** in ~960 ms, a 97% reduction. The pass returns its residual and `GraphCanvas.tsx` logs the count — never a node id, which is a repository path.
- **~1 s of blocked main thread at the top end.** Accepted for now: it happens once, after a 7 s analysis, before first paint. A stall detector (8 iterations without the summed overlap depth improving) stops the pass burning time on a residual it cannot reduce.
- **Layout constants revert to 90/8000** (`idealEdgeLength`/`nodeRepulsion` per unit scale) — below ADR-026's correction, above the original 70/4500 — since preventing overlap is no longer their job.
- **Edges can no longer be clicked, hovered, or selected.** Nothing consumed an edge interaction; selection has always been node-driven.
- **Directory compound nodes are now inert in every sense** — invisible (ADR-026), unclassed by the selection effect and style-zeroed (ADR-026's correction), and now non-interactive and `ungrabify()`d. A future collapse/expand feature will have to re-enable interaction deliberately.
- **`wheelSensitivity` 0.3 → 0.8.** Cytoscape logs a warning for any custom value; it did so at 0.3 too.
- **First tests for a `scene/` module**: `overlap.test.ts`, 9 tests, bringing the frontend suite to 43. They caught three real defects that inspection had not — a float-equality fixed point where exactly-touching boxes reported a ~1e-15 overlap forever; a stall detector keyed to pair *count* that aborted healthy runs, since a pair being separated stays one pair until it is not; and the diffusion behaviour that motivated the spiral pre-scatter (a 1,000-node pile went from 29,805 residual pairs to 18). Two of the tests were also found to be **vacuous** and were fixed: constructing Cytoscape with `elements` and no `layout` runs the default **grid** layout, which discards the positions the test just set.
- **A jsdom caveat worth recording**, since it will mislead the next person: a headless Cytoscape instance does not resolve a stylesheet. `node.width()` returns 1 whatever the style says and `numericStyle('font-size')` returns `undefined`. The tests therefore exercise the algorithm on uniform boxes and say nothing about whether the footprint matches what `style.ts` paints; the browser measurements above are what cover that.

### Correction (2026-09-02, later the same day) — the layout never ran in a hidden tab, and `gravity` was tuned in the wrong direction

Reported as "the distribution looks even worse now", with a screenshot showing a regular grid lattice of nodes and a diagonal smear. Two independent causes, and the more serious one was not a tuning problem at all.

**1. In a hidden document, fCoSE never ran.** `GraphCanvas.tsx` started the initial layout *only* from its `ResizeObserver` callback — a deliberate choice (see the file comment: the container has no resolved height when the effect first runs). But **a hidden document is delivered no `ResizeObserver` callbacks at all**, including the initial one `observe()` normally queues. The realistic path into this is ordinary: paste a URL, switch tabs while the 7 s analysis runs, come back. The canvas mounts hidden, the observer never fires, fCoSE never runs — and what renders is Cytoscape's **default `grid` layout**, which is exactly the lattice in the screenshot. It does not self-correct on return, because the size has not changed and the observer has nothing to report.

Confirmed directly rather than inferred: with `document.hidden === true` and a 368x767 container, a freshly attached `ResizeObserver` produced **zero** callbacks in 1.5 s, `requestAnimationFrame` did not fire within 1.2 s, and the rendered graph measured a bounding box of exactly `467x748` — *byte-identical across reloads and unchanged by flipping `randomize`*, which is what proved a deterministic grid was running instead of a randomized force layout.

Fixed by not making the initial layout depend on any callback being delivered: if the container already has a box when the effect runs (the common case), lay out immediately and synchronously. The observer stays as the fallback for the genuinely-unsized case it was written for, and keeps ownership of later refits; `laidOut` makes whichever path arrives second a no-op. The overlap/camera step was also un-deferred from `requestAnimationFrame` for the same reason. Verified with `document.hidden === true`: 0 overlapping pairs, camera at the 0.55 floor, hub centred — and the same numbers when visible (core-area fraction 0.512 hidden vs 0.511 visible), which is the point.

This also retires a claim made above: the `~90 -> 0` express figure in the pass's own header was measured in a visible window, so the pass was never broken — but in a hidden tab it had nothing to separate, because the grid layout it was handed does not overlap.

**2. `gravity` was scaled in the wrong direction.** The original reasoning — ease it down so a large graph can "breathe outward" — sounds right and produced the worst layouts measured. Gravity is what stops a force layout drifting into a sparse, stringy sprawl. On `expressjs/express`, raising it from 0.265 to 0.6-0.9 lifted the core-area fraction from ~0.32 to ~0.43 and roughly halved the bounding-box area, with residual overlaps at zero. On `withastro/astro` gravity barely moves the bounding box but raising it *increases* residual overlaps (9,180 -> 14,088), since it compresses a graph already at the separation pass's limit. So it now scales *down* with node count from the opposite end of the range: `clamp(0.9 - nodeCount / 3000, 0.1, 0.9)`.

**Two alternatives tested and rejected, both worse:**

- **Drop compound nesting from the layout** (directories are invisible since ADR-026, so the containment looked like pure cost). On express it looked like a clear win — bounding box 4715x1949 to 1328x1246. On astro it was a disaster: core-area fraction 0.72 to 0.134, bounding box to 7722x7706, residual overlaps roughly tripled. Compound containment is doing real work at scale, and ADR-026's decision to keep `parent` stands.
- **Flatten to top-level directories only**, matching the tint palette and legend. Also much worse on astro (core 0.123). Recorded because it is the obvious next idea after the one above.
- **`randomize: false` for a stable picture across loads.** Does not mean "seed deterministically" — it means "start from the nodes' current positions", which on a freshly built graph is every node at (0, 0). fCoSE produces a partial layout from that degenerate seed and never emits `layoutstop`, silently disabling separation and the camera. Reverted to `true`; the run-to-run variance is documented in `style.ts` and left in place, since removing it properly means seeding real initial positions.

### Status
Accepted (with the two 2026-09-02 corrections above)

---

## ADR-028 — CORS as an explicit origin allowlist, plus in-tree deploy scaffolding (frontend on Vercel, backend on Render)

### Decision

Two decisions taken together on 2026-09-02, because each *needs* the other to deploy.

**CORS is an explicit origin allowlist, never a wildcard.** `app/config.py` gained
`CORS_ALLOWED_ORIGINS` (default empty — nothing is allowed cross-origin), enforced by
`CORSMiddleware` as the **outermost** middleware in `app/api/app.py` so a preflight is
answered before the application or the request-id/body-cap middleware runs. Methods are
limited to `GET`/`POST`/`OPTIONS`, headers to `Content-Type`, credentials are off, and an
allowed origin is matched by exact string equality — never `*` and never a reflected
`Origin`.

**The deploy scaffolding lives in the tree.** `frontend/vercel.json` pins the Vite
framework, build command (`npm run build`) and output directory (`dist`); `render.yaml`
is a Render blueprint for the backend (`rootDir: backend`, a `uv`-based build, the
`uvicorn` start command on `$PORT`, `/api/health`); `.env.example` documents
`VITE_API_URL`, `CORS_ALLOWED_ORIGINS`, and the optional `GITHUB_TOKEN`. This keeps
ADR-011's split deployment while making the split reproducible instead of a
step-by-step dashboard walkthrough.

### Reason

The frontend lives on a different origin than the backend, and a cross-origin
`POST /api/analyze` with a JSON body preflights; without CORS headers the browser blocks
it. The allowlist is deliberately narrow because CORS is **not** auth — a disallowed
origin still gets its request processed, the browser just refuses to read the response,
so the widening has to be as small as the deploy allows and the rate limiter/concurrency
gate (ADR-008) stay load-bearing for non-browser clients. Empty-by-default keeps local
development correct with zero configuration: the Vite dev-server proxy keeps the browser
request same-origin, so nothing needs to be set.

Render was chosen over Railway/Fly as the *documented* default only — all three support
the same Python 3.14 + `uv sync` + `uvicorn` shape, and the environment variables are
identical, so switching hosts is a one-line change in `render.yaml`, not a redesign.
Vercel's Python runtime remains explicitly out (ADR-011): execution caps and cold starts
are a poor fit for a 60s streaming-download + tree-sitter analysis, and the native
grammar wheels are unverified there.

### Alternatives considered

- `allow_origins=["*"]` — the deploy would "just work", and would also let any page on
  the internet drive a visitor's browser into `/api/analyze` and read its result.
- Reflecting the request `Origin` — turns CORS into "any origin that asks politely",
  i.e. the same thing as `*` with extra steps.
- `allow_origin_regex` — a prefix/regex match needs to be deliberately tighter than a
  `.vercel.app` suffix, and `endswith`-class mistakes were already rejected once in this
  project (`net_guard.validate_download_url`); exact equality has no such failure mode.
- Deploying the backend on Vercel's serverless Python runtime — rejected in ADR-011 for
  execution-time and native-wheel reasons; not revisited here.

### Status

Accepted
