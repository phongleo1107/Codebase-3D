# Codebase 2D

Paste a public GitHub URL, get back an interactive dependency graph of a
TypeScript/JavaScript codebase — nodes are files, edges are imports, plus a
deterministic component diagram and detected API routes.

**V1 supports TypeScript/JavaScript only.**

The name now matches the rendering layer: the graph renders in **2D via
Cytoscape.js**, not 3D (ADR-022 in [docs/DECISIONS.md](docs/DECISIONS.md)).
It was renamed from "Codebase 3D" on 2026-09-02.

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

The MVP deploys as two services (ADR-011, ADR-028): the **frontend** on Vercel
and the **backend** on a persistent host — Render is the documented starting
point ([`render.yaml`](render.yaml)); Railway and Fly work with equivalent
settings. The backend is a long-running process, not a serverless function: it
streams a tarball download and parses with tree-sitter under a 60s analysis
deadline, which is why it never runs on Vercel's Python runtime (ADR-011).

Backend (Render, from [`render.yaml`](render.yaml)):

1. **New Blueprint** → import this repository. Root directory is `backend` (set
   in the blueprint), build is `uv sync`, start is `uvicorn app.main:app
   --host 0.0.0.0 --port $PORT`.
2. Set `CORS_ALLOWED_ORIGINS` as a JSON array naming the frontend's origin
   (below). `GITHUB_TOKEN` is optional.
3. Confirm `GET /api/health` returns `{"status": "ok"}` and that one real
   repository analyzes end to end.

Frontend (Vercel):

1. Import this repository. **Framework preset: Vite**, **Root directory:
   `frontend`** — [`frontend/vercel.json`](frontend/vercel.json) pins the
   framework, build command (`npm run build`) and output directory (`dist`).
2. Add the project environment variable **`VITE_API_URL`** = the deployed
   backend's base URL. It is read at build time by
   `frontend/src/api/client.ts`, so it is baked into the bundle on every deploy
   — change it and redeploy.
3. Deploy `main`. The browser then calls the backend cross-origin; the backend
   allowlist must name exactly this origin.

CORS is an **explicit origin allowlist** (`backend/app/config.py`
`CORS_ALLOWED_ORIGINS`), enforced by the outermost middleware in
`backend/app/api/app.py` (ADR-028). Empty (the default) allows no cross-origin
request, which is correct locally, where the Vite dev server proxies `/api`.
Never set it to `*`.

Required environment variables:

- **`VITE_API_URL`** (frontend, build-time) — base URL of the deployed backend.
  Read in `frontend/src/api/client.ts`.
- **`CORS_ALLOWED_ORIGINS`** (backend) — JSON array naming the exact origin(s)
  that may call the API from a browser, e.g.
  `["https://codebase-2d.vercel.app"]`.
- **`GITHUB_TOKEN`** (backend, optional) — raises GitHub API rate limits only.

See [`.env.example`](.env.example) for a template. Operational notes: the rate
limiter and concurrency gate are in-process (no Redis — ADR-008), so run the
backend as a **single instance**; and verify tree-sitter's native grammar
wheels load on the target host before committing to it
([docs/TODO.md](docs/TODO.md)).

**Live URL:** _not deployed yet — placeholder._
