# CLAUDE.md

## Project Overview

**Codebase 3D** — paste a public GitHub URL, get an interactive dependency graph of a TypeScript/JavaScript codebase. *(Name predates the 2026-09-01 2D pivot below; not renamed.)*

**Current MVP goal:** the smallest product that feels genuinely impressive when someone pastes a repo URL and watches their codebase become a navigable graph. V1 supports **TS/JS only**.

> **2026-09-01 — visualization is 2D via Cytoscape.js, not 3D via Three.js/React Three Fiber (ADR-022 in [docs/DECISIONS.md](docs/DECISIONS.md), supersedes ADR-002 and ADR-004).** Owner-driven scope change, not a discovery made while building: minimize bespoke frontend code and keep the codebase small enough for one person to read end to end. [PRD.md](PRD.md) is updated to match. This is a rendering-layer decision only — the wire contract, the backend, and everything in the graph model are unaffected.

> **Status: early implementation.** As of 2026-08-31 the backend has its contract layer (config, errors, models, logging), its URL/egress security boundary, the GitHub client, the streaming archive reader, the secret/path-safety filters, the import extractor, the **ingestion+parse pipeline that joins them** (`app/analysis/pipeline.py`), the **MVP module resolver** (`app/analysis/resolver.py`), the **description extractor** (`app/analysis/descriptions.py`), the **graph builder** (`app/analysis/graph_builder.py`), and **route detection** (`app/analysis/routes.py`), with 1349 tests. That pipeline sends the download, streams it into the reader, applies the secret filter before anything is parsed, and returns unresolved imports per file plus the commit SHA; the resolver then answers each specifier by pure set membership against that same file list — a file, an external package, or unresolved — with `tsconfig` `paths`/`baseUrl`/workspaces deferred and their seam decided in ADR-017; the description extractor quotes each file's own leading header comment with a byte-prefix scan rather than a second parse (ADR-020), which is **the first repository-authored *text* to reach a response body**; the graph builder turns all of it into sorted, deduplicated nodes, edges, and stats (ADR-018); and route detection reads a *second* set of facts off the very same tree the import query used, because `parser.py` now splits into `parse_source` (every guard, one tree) and `extract_imports` (the query) rather than letting a second module re-parse and re-implement five security guards (ADR-021). The pipeline caps the total import count at `MAX_IMPORTS`, which is what bounds the resolution phase — it runs after the 60 s deadline is spent and takes no clock of its own (ADR-019). It **has** been run against real GitHub repositories (`backend/scripts/smoke.py`, 2026-08-31), but on the happy path only: no security control has ever been triggered by data we did not construct, and the pytest suite itself stays hermetic and fixture-built. There is still **no HTTP routing and no frontend**; nothing calls the graph builder yet, `path_safety.py` still has **no caller**, and **`MAX_NODES`/`MAX_EDGES` are enforced nowhere** — they belong to the unwritten routing layer. (Mind the word collision: `app/analysis/routes.py` detects routes *in an analyzed repository*; `app/api/` would serve *our own* HTTP routes and is still empty.) Documents describing the rest use the future tense or an explicit status marker; see [docs/CURRENT_STATE.md](docs/CURRENT_STATE.md) before assuming anything is built.

> **2026-08-31 — scope narrowed to a 3-day MVP sprint (ADR-011, ADR-012 in [docs/DECISIONS.md](docs/DECISIONS.md)).** *(The ADR-012 half of this note is **superseded** — see the next paragraph. Kept for history.)* Three features were added beyond the original PRD — a brief C4 diagram, an API service map, and per-file explanations, all narrated by an LLM over the deterministic graph, never used to determine it — and two amendments were made for the deadline: the frontend deploys to Vercel with the backend on a separate persistent host instead of a single `docker compose up`, and the 3D layout ships sphere-packing only, with force refinement deferred. Day-by-day plan in [docs/TODO.md](docs/TODO.md).

> **2026-08-31 (later the same day) — the LLM layer is removed entirely (ADR-013 supersedes ADR-012).** It did not fit the deadline. **There is no LLM anywhere in this project**: no `app/llm/`, no `POST /api/explain`, no provider, no API key. The three features it backed survive as deterministic output — a **file's description is its own leading header comment**, quoted from the repository rather than generated; a **route's summary is the comment above the handler**; and the diagram becomes a **deterministic component diagram** built from the graph itself (top-level directories as containers, external packages as external systems, detected routes as the API surface), with the response field renamed `c4` → `componentDiagram` because it is not a C4 model. Route detection was always deterministic and is unchanged. The whole `/api/analyze` response is now a pure function of the commit.

**Planned technologies:** Python 3.14 + FastAPI + tree-sitter (backend); React 19 + TypeScript + Cytoscape.js (2D graph) + Vite + `mermaid` for the component diagram (frontend); Vercel (frontend) + a separate persistent host — Railway/Render/Fly (backend) for the MVP release (ADR-011), Docker Compose remaining the longer-term self-hosted target (ADR-001). No database, no auth, no persistent storage, **no LLM or AI API of any kind** (ADR-013).

**Architectural principles**

- The analyzed repository is **untrusted data**, always — including its comments, which are now quoted into API responses as node descriptions (ADR-013).
- **Deterministic analysis only, end to end.** No LLM determines imports, dependencies, graph structure, descriptions, or anything else — because the project contains no LLM at all (ADR-013). Do not reintroduce one without superseding that ADR. The same commit must produce byte-identical JSON.
- Repository-authored text that reaches a response (descriptions, route summaries) is **display-only and bounded**: size-capped and stripped of control characters at extraction, rendered as a text node, never as HTML and never via `dangerouslySetInnerHTML`.
- Graph analysis is independent of the visualization layer; the frontend receives structured graph data, never raw parser output.
- Validate at every boundary — HTTP request in, GitHub response in, API response into the frontend.
- Prefer eliminating a vulnerability class architecturally over defending against it procedurally.

## Repository Structure

Only what actually exists today:

```
LICENSE
README.md            Two-line project description
PRD.md               Product spec — the source of requirements
CLAUDE.md            This file
.gitignore           Covers both stacks
docs/                Project memory (see below)
backend/             Python package — contract layer, security boundary, ingestion
  pyproject.toml     Pinned deps; pytest, ruff and mypy config all live here
  uv.lock            Committed lockfile
  app/
    config.py        Settings — every operational limit
    errors.py        ErrorCode + AppError hierarchy
    logging_setup.py JSON logs + redaction filter
    models/          Pydantic graph and API schemas
    security/        url_validation.py, net_guard.py, secret_filter.py, path_safety.py
    fetch/           github.py (the only module that opens a socket), archive.py
    analysis/        deadline.py, parser.py, pipeline.py, resolver.py, descriptions.py,
                     graph_builder.py, routes.py
    api/             Empty package
  scripts/           smoke.py — real-network end-to-end check, NOT a pytest test
  tests/             1349 tests; conftest.py blocks the network suite-wide
    fixtures/        tarballs.py — malicious archives built in process
.claude/             Local Claude Code permissions (not source)
```

`docs/`:

| File | Purpose |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Target system design; per-section build status |
| [DECISIONS.md](docs/DECISIONS.md) | ADR log |
| [CURRENT_STATE.md](docs/CURRENT_STATE.md) | Where the project is right now — **read first** |
| [SECURITY.md](docs/SECURITY.md) | Threat model and control status |
| [TODO.md](docs/TODO.md) | Prioritized backlog |

`frontend/` does not exist yet, and most modules ARCHITECTURE.md describes under `backend/app/` are still unwritten — `api/` is an empty placeholder, and `analysis/` holds the deadline, the parser, the ingestion+parse pipeline, the MVP resolver, the description extractor, the graph builder, and route detection but no diagram generator. `security/` is **complete for the MVP** at four modules: the fifth (HMAC tokens) belonged to `/api/source` and `/api/explain`, both of which are now out of MVP scope (ADR-013, and ADR-007's second scope note). There is deliberately no `app/llm/` and there must not be one. Create modules as work begins, and update this section when you do.

## Development Rules

- Prefer simple solutions over unnecessary abstraction.
- Do not introduce a dependency without a reason. Record notable ones as an ADR.
- Keep frontend visualization separate from graph-analysis logic.
- **Add no AI/LLM functionality.** ADR-013 removed it; adding any back requires superseding that ADR first, not a convenient exception.
- Treat repository contents as untrusted data — including comments, which are quoted into responses.
- Never execute analyzed repository code.
- Validate data at system boundaries.
- Do not silently change architecture — see the workflow below.
- Preserve existing functionality when refactoring.
- Pin dependency versions. Verify a version exists before writing it into a manifest; do not rely on recalled version numbers.

## Security Rules

Full model in [docs/SECURITY.md](docs/SECURITY.md). Non-negotiable:

- User-provided repositories are untrusted input.
- Accept only `https://github.com/<owner>/<repo>`. Reject localhost, private IPs, internal hostnames, other schemes, other hosts.
- **Prevent SSRF.** Never follow redirects automatically; validate any redirect target against a single-element host allowlist by string equality, then verify the resolved IP is global.
- **Prevent path traversal.** Never trust a path from an archive or a client.
- **Never execute repository code.** No `npm install`, no build scripts, no interpreters, no Makefiles. Parsing only.
- **Never use unsanitized user input in shell commands.** Prefer no `subprocess` at all; never `shell=True`.
- Enforce resource limits: download size, extracted size, compression ratio, file count, file size, node/edge count, analysis duration, request body size.
- Never expose secrets. Never log source code, import specifiers, tokens, or credentials. Never return `.env`, keys, or credential files to the frontend.
- Clean up temporary repository data. (The chosen design avoids writing it to disk at all — see ADR-003.)
- Do not expose internal errors: no stack traces, filesystem paths, or upstream detail in responses.

## Agent Workflow

**Before implementation**

1. Read `CLAUDE.md`.
2. Read `docs/CURRENT_STATE.md`.
3. Read `docs/ARCHITECTURE.md`.
4. Read `docs/DECISIONS.md` when architectural decisions are relevant.
5. Read `docs/SECURITY.md` before touching repository ingestion, parsing, filesystem, networking, or deployment code.

**Before changing architecture**

1. Explain why the change is necessary.
2. Check existing decisions for a conflict.
3. Update `DECISIONS.md` — add a new ADR, or mark the old one `Superseded` and say what replaced it. Never rewrite history.

**After significant implementation**

1. Update `CURRENT_STATE.md`.
2. Update `TODO.md` if tasks changed.
3. Update `ARCHITECTURE.md` if the architecture changed — including flipping a section's status marker.
4. Update the `SECURITY.md` status column when a control actually lands, with the file that implements it.
5. Add an ADR if a meaningful architectural decision was made.

Do not update documentation for trivial changes.

## Trust Order

When documentation and reality disagree:

```
Actual code  →  Tests  →  Documentation
```

Inspect the code, determine the real behavior, fix the doc, and say so. Never claim a security control exists unless you have seen it in the code.
