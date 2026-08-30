# CLAUDE.md

## Project Overview

**Codebase 3D** — paste a public GitHub URL, get an interactive 3D dependency graph of a TypeScript/JavaScript codebase.

**Current MVP goal:** the smallest product that feels genuinely impressive when someone pastes a repo URL and watches their codebase become a navigable 3D structure. V1 supports **TS/JS only**.

> **Status: early implementation.** As of 2026-08-30 the backend has its contract layer (config, errors, models, logging), its URL/egress security boundary, the GitHub client, the streaming archive reader, the secret/path-safety filters, the import extractor, and **the analysis pipeline that joins them** (`app/analysis/pipeline.py`), with 1065 tests. A repository URL now goes in and a list of files and the module specifiers they name comes out. There is still no resolver, no graph builder, no routing, and no frontend, so nothing turns a specifier into an edge or an analysis into a response body. Two caveats worth carrying: the pipeline has only ever been driven against in-process fixtures with the HTTP transport swapped, so **nothing has been fetched from GitHub itself**; and `safe_relative_path` still has no caller, by design, while `is_secret_path` has one of the two its SECURITY.md row requires. Documents describing the rest use the future tense or an explicit status marker; see [docs/CURRENT_STATE.md](docs/CURRENT_STATE.md) before assuming anything is built.

**Planned technologies:** Python 3.14 + FastAPI + tree-sitter (backend); React 19 + TypeScript + Three.js + React Three Fiber + Vite (frontend); Docker Compose (deploy). No database, no auth, no persistent storage.

**Architectural principles**

- The analyzed repository is **untrusted data**, always.
- Deterministic parsing only. No LLM is used to determine imports or dependencies.
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
    analysis/        deadline.py, parser.py, pipeline.py (the module that joins them)
    api/             Empty package
  tests/             1065 tests; conftest.py blocks the network suite-wide
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

`frontend/` does not exist yet, and several modules ARCHITECTURE.md describes under `backend/app/` are still unwritten — `api/` is an empty placeholder, `analysis/` holds the deadline, the parser, and the pipeline but no JSONC reader, resolver, or graph builder, and `security/` holds four of its five planned modules (HMAC tokens are missing). Create them as work begins, and update this section when you do.

## Development Rules

- Prefer simple solutions over unnecessary abstraction.
- Do not introduce a dependency without a reason. Record notable ones as an ADR.
- Keep frontend visualization separate from graph-analysis logic.
- Keep deterministic analysis separate from any AI functionality.
- Treat repository contents as untrusted data.
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
