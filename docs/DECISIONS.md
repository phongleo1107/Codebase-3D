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

> **MVP scope note (2026-08-31):** under the 3-day deadline, only the first phase — deterministic nested-sphere placement over the directory tree — ships initially. The anchored force-refinement pass is deferred, not abandoned; the graph is still legible and fully deterministic without it, and adding force refinement later is additive to this same worker, not a redesign.

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
