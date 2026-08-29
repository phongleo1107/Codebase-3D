# CLAUDE.md

## Project Overview

**Codebase 3D** — paste a public GitHub URL, get an interactive 3D dependency graph of a TypeScript/JavaScript codebase.

**Current MVP goal:** the smallest product that feels genuinely impressive when someone pastes a repo URL and watches their codebase become a navigable 3D structure. V1 supports **TS/JS only**.

> **Status: pre-implementation.** As of 2026-08-29 the repository contains `LICENSE`, `README.md`, `PRD.md`, this documentation, and an installable but **empty** `backend/` package (dependencies pinned and resolving; zero application code). Documents describing the system use the future tense or an explicit status marker — see [docs/CURRENT_STATE.md](docs/CURRENT_STATE.md) before assuming anything is built.

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
backend/             Python package — dependency scaffold only, no code yet
  pyproject.toml     Pinned deps; pytest, ruff and mypy config all live here
  uv.lock            Committed lockfile
  app/               Empty packages: api/ models/ security/ fetch/ analysis/
  tests/             Empty package, no tests yet
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

`frontend/` does not exist yet. Every module ARCHITECTURE.md describes under `backend/app/` is likewise unwritten — the directories are placeholders. Create them as work begins, and update this section when you do.

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
