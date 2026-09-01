# Codebase 3D

Paste a public GitHub URL, get back an interactive dependency graph of a
TypeScript/JavaScript codebase — nodes are files, edges are imports, plus a
deterministic component diagram and detected API routes.

**V1 supports TypeScript/JavaScript only.**

The name predates a scope decision: the graph renders in **2D via
Cytoscape.js**, not 3D (ADR-022 in [docs/DECISIONS.md](docs/DECISIONS.md)).
It has not been renamed.

## Architecture

- **Backend** (`backend/`, Python 3.14 + FastAPI): downloads a repository
  archive from GitHub, streams and filters it, parses TS/JS with
  tree-sitter, resolves imports against the file list, and builds a graph —
  nodes, edges, stats, an optional per-file description (the file's own
  leading comment, quoted verbatim), detected routes, and a Mermaid
  component diagram. All of it is a pure function of the repository commit.
  `POST /api/analyze` and `GET /api/health` are the only two endpoints.
- **Frontend** (`frontend/`, React 19 + TypeScript + Vite): a landing page
  takes the repository URL, calls the backend, and renders the graph on a
  Cytoscape.js canvas with an inspector panel.

Analysis logic and the visualization layer are kept independent — the
frontend only ever sees the validated, structured graph the backend returns.

## Security model

- The analyzed repository is **untrusted input**, including its comments,
  which are quoted into API responses as descriptions — treated as
  display-only text, never HTML, never rendered via
  `dangerouslySetInnerHTML`.
- **Repository code is never executed.** No `npm install`, no build
  scripts, no interpreters — parsing only.
- Only `https://github.com/<owner>/<repo>` URLs are accepted; redirects,
  localhost, private/internal addresses, and other schemes/hosts are
  rejected (SSRF prevention).
- Resource limits are enforced end to end: download size, extracted size,
  compression ratio, file count/size, node/edge count, analysis duration,
  and request body size — plus a per-IP rate limit and a global concurrency
  gate on `/api/analyze`.
- **There is no LLM or AI API anywhere in this project** (ADR-013). Every
  field in the response — the graph, the descriptions, the routes, the
  component diagram — is deterministic: the same commit always produces
  byte-identical JSON.

Full threat model in [docs/SECURITY.md](docs/SECURITY.md).

## Running locally

Backend (from `backend/`):

```bash
uv sync
uv run uvicorn app.main:app --reload
```

Runs on `http://localhost:8000`.

Frontend (from `frontend/`):

```bash
npm install
npm run dev
```

Runs on `http://localhost:5173` and proxies `/api` to `http://localhost:8000`
in dev, so no CORS configuration is needed locally.

Run backend tests with `uv run pytest` (from `backend/`), frontend tests with
`npm test` (from `frontend/`).

## Deploying

The backend has no CORS headers yet — this is a known gap before a real
deploy (see [docs/TODO.md](docs/TODO.md)) and must land first.

Required environment variables:

- **`VITE_API_URL`** (frontend, build-time) — base URL of the deployed
  backend, e.g. `https://api.example.com`. Read in `frontend/src/api/client.ts`.
- **CORS origin allowlist** (backend) — the deployed frontend's origin (e.g.
  the Vercel domain), so the browser's cross-origin request to
  `/api/analyze` is permitted. Not yet implemented; see
  [docs/TODO.md](docs/TODO.md).

Target hosting for the MVP (ADR-011): frontend on Vercel, backend on a
separate persistent host (Railway/Render/Fly).

**Live URL:** _not deployed yet — placeholder._
