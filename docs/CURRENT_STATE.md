# Current State

## Current Goal

Build the MVP defined in [PRD.md](../PRD.md): paste a public GitHub URL → safely analyze a TS/JS repository → render its dependency graph in navigable 3D.

Steps 1 and 2 of the build order are done and green: **the backend contract** (config, errors, models, logging) and **the URL/egress security boundary** (`security/url_validation.py`, `security/net_guard.py`). Both landed before any code capable of making a network request exists — which is still true: nothing calls the egress guard yet.

## Working

**The backend contract layer, the security boundary, and their tests.** There is still no routing, network, parsing, or analysis code: `app/api/`, `app/fetch/`, and `app/analysis/` remain empty packages, and `app/security/` holds two of its five planned modules.

| Path | Notes |
|---|---|
| `LICENSE` | MIT |
| `README.md` | Two-line description |
| `PRD.md` | Product spec — now staged in git |
| `CLAUDE.md`, `docs/*` | This documentation, created 2026-08-29 |
| `.gitignore` | Both stacks; `.env.example` is un-ignored |
| `backend/pyproject.toml` | Pinned deps + pytest/ruff/mypy config; hatchling build backend |
| `backend/uv.lock` | 39 resolved packages, committed |
| `backend/app/config.py` | `Settings` — all 22 limits, `SecretStr` secrets, `extra="ignore"` |
| `backend/app/errors.py` | `ErrorCode` + 14 `AppError` subclasses; fixed 3-key body |
| `backend/app/models/` | `graph.py`, `api.py`, re-exporting `__init__.py` |
| `backend/app/logging_setup.py` | JSON-line formatter + `RedactingFilter` |
| `backend/app/security/url_validation.py` | `parse_github_url` → frozen `RepoRef`; strict grammar, ASCII-only, allowlisted host |
| `backend/app/security/net_guard.py` | `validate_download_url` (equality allowlist) + `assert_public_ip` (resolved-IP check). **Nothing calls either yet** |
| `backend/tests/conftest.py` | Session-scoped autouse fixture blocking `getaddrinfo`, `gethostbyname`, `create_connection`, and `socket.connect`/`connect_ex`. Raises `NetworkAccessAttempted`, a `RuntimeError` — deliberately *not* an `OSError`, so it travels straight through `assert_public_ip`'s handler instead of being swallowed as a rejection |
| `backend/tests/` | 549 tests across config, errors, models, logging, URL validation, net guard |
| `backend/app/{api,fetch,analysis}/` | Still empty packages |
| `backend/app/security/` | `secret_filter.py`, `path_safety.py`, HMAC tokens still to come |
| `.claude/settings.local.json` | Local tool permissions, not source |

No `frontend/`, no Docker files, no CI.

**Verified locally on 2026-08-29** (Python 3.14.7, uv 0.12.3):

- `uv sync` resolves and installs cleanly; the project installs as an editable package.
- `uv run pytest` → **549 passed**. `uv run mypy` (strict) → clean over 21 files. `uv run ruff check .` → clean.
- tree-sitter ABI spike printed **`14`**, and `QueryCursor` imports successfully alongside `Language`, `Parser`, and `Query`.

Two `pyproject.toml` corrections were needed: `[tool.ruff] src = ["."]` (it was `["app", "tests"]`, which pointed *inside* the package so isort never treated `app` as first-party), and `S105`/`S106` added to the test per-file-ignores because redaction tests must hardcode fake credentials.

## In Progress

- Nothing is under active implementation.

## Broken / Known Issues

- No CI yet.
- **`assert_public_ip` narrows DNS rebinding, it does not close it.** The connection that follows is made by name, so a resolver that answers differently the second time is not caught. Closing it needs connect-by-IP with SNI, which v1 does not do. Recorded in `docs/SECURITY.md`.
- **`ruff format` is not a project gate — do not run it.** `uv run ruff check .` is the gate and is clean. `ruff format --check` reports 5 of 21 files as unformatted: four pre-existing (`app/logging_setup.py`, `tests/test_config.py`, `tests/test_logging.py`, `tests/test_models.py`) and `app/security/net_guard.py`, which is unformatted for the same reason they are — the formatter wants to join wrapped constructs into lines that then exceed the configured `line-length = 100`. Running it would rewrite unrelated code to no benefit. Either adopt it repo-wide as a deliberate decision or leave it alone; do not apply it to one file.
- The contract layer is unexercised by any route — nothing constructs an `AnalyzeResponse` from real data yet, so field *semantics* are only as good as the documentation.
- `pytest.filterwarnings = ["error"]` now runs against a real suite and is clean; no targeted ignores have been needed.
- The pydantic **mypy plugin is not enabled** (no `plugins` key in `[tool.mypy]`). Constructor type-checking still works via PEP 681 `@dataclass_transform` on pydantic's metaclass. Reviewed and judged unnecessary; do not assume the plugin is present when reading type errors.

## Recently Completed

- **2026-08-29** — **URL validation and the network guard implemented**: `app/security/url_validation.py`, `app/security/net_guard.py`, `tests/conftest.py`, plus 389 tests. `docs/SECURITY.md`'s Network/SSRF table moved from all-`Planned` to four `Implemented` rows and two `Partial` ones; the `Partial` halves are client behaviour (`follow_redirects=False`, the one-hop rule, no `Authorization` on the codeload request) and stay `Planned` until `app/fetch/` exists.

  Design decisions made here, none of which change the architecture:
  - **net_guard failures raise `UpstreamUnavailableError` (502), not a 4xx.** A refused redirect is a statement about GitHub's response, not about the URL the user submitted — that one already passed `parse_github_url`. Reusing the existing opaque upstream error also keeps the refusal from telling an attacker which check tripped.
  - **The user-facing grammar is strict where the egress guard is lenient**, deliberately: `parse_github_url` rejects `github.com.` (trailing dot), any port at all, and any query or fragment, while `validate_download_url` strips one trailing dot and permits `:443` and a query string, because codeload's real redirect targets carry a signed query.
  - `_FORBIDDEN_CHARS` is duplicated across the two modules rather than shared, so that tightening the user-facing grammar cannot silently alter what the egress guard accepts.

  Seven non-obvious behaviours were verified empirically and are recorded in code comments — each one is a bypass if you assume otherwise:
  - **`str.strip()` removes Unicode whitespace**, so the ASCII check must run *before* it. Stripping first would launder `"<NBSP>https://github.com/o/r"` into a clean ASCII URL and silently defeat the homograph defence. Mutation testing found this: deleting the `isascii()` call left every test passing until the NBSP/U+3000/U+2007 cases were added.
  - `urllib.parse.urlsplit` **silently deletes tab, CR, and LF from anywhere in a URL**. `urlsplit("https://gith\tub.com/o/r").hostname` is `'github.com'`. Both modules therefore screen bytes *before* parsing.
  - **`urlsplit` is not total.** It raises `ValueError` on a malformed bracketed host — `https://[evil.com]/o/r`, `https://[::1/x`, `https://[]/x` — and the message **quotes the offending host verbatim**. An adversarial review caught this escaping both functions as a bare `ValueError`, which broke the typed-error contract *and* the no-echo rule at once. Both modules now wrap `urlsplit`, and both test files carry a ~20 000-input fuzz sweep asserting nothing but the typed error escapes.
  - `ipaddress.ip_address(a).is_global` is **`True`** for `::127.0.0.1` (IPv4-compatible), `64:ff9b::7f00:1` (NAT64), `224.0.0.1` (multicast), **and every address in `fec0::/10`** (deprecated IPv6 site-local, RFC 3879 — CPython's `_reserved_networks` stops at `fe00::/9` and its `_private_networks` resumes at `fe80::/10`, leaving the block between them uncovered). `is_global` alone is *not* a public-address check. `assert_public_ip` also requires `not is_reserved`, `not is_multicast`, and `not is_site_local`; a sweep of all 65 536 IPv6 `/16` prefixes confirms those four predicates leave no further gap. The `is_site_local` test must be guarded by `isinstance(ip, IPv6Address)` — `IPv4Address` has no such attribute, so an unguarded access would raise `AttributeError` on every IPv4 address and escape the module untyped.
  - CPython 3.13+ `IPv6Address.is_global`/`is_reserved` **delegate to `.ipv4_mapped`** when set. That is the only reason `::ffff:140.82.121.4` is not caught by the `::/8` reserved check, and it is why the explicit unmapping must run *before* that check.
  - `ipaddress.ip_address()` **accepts an `int`**, so an unexpected `(int, bytes)` sockaddr from `getaddrinfo` would be silently read as a packed IPv4 address. Guarded by an explicit `isinstance(address, str)`.
  - `urlsplit(...).port` raises `ValueError` on a non-numeric port but returns `None` for an *empty* one, so `https://github.com:/o/r` slips past a port check. Caught by requiring the whole authority to equal the hostname.

  The suite was **mutation-tested**: 40 single-check mutations, 35 caught. The five survivors are checks that are redundant by design (the userinfo, port, and leading-slash checks in `url_validation` are subsumed by the authority-equality check; `.lower()` and the `::ffff:` unmapping restate stdlib behaviour). Each now carries a comment saying so, so a future reader does not mistake redundancy for an untested control.

  Two rounds of adversarial multi-lens review ran against this code. Round one found the `urlsplit` `ValueError` escape; round two found the `fec0::/10` gap and six stale or false documentation claims, including an `ARCHITECTURE.md` banner and a `SECURITY.md` row that described `path_safety.py` as "implemented and tested" when the module does not exist. Both defects were real, both are fixed, and the doc claims are corrected — worth recording because in both rounds the finding that mattered came from a lens that was told to *verify empirical claims by running them* rather than to read for style.

- **2026-08-29** — **Backend contract layer implemented**: `app/config.py`, `app/errors.py`, `app/models/{graph,api}.py`, `app/logging_setup.py`, plus 160 tests. The wire schema is now frozen, so frontend work can proceed against it in parallel. Scope was contract and plumbing only — no routes, no network, no parsing.
- **2026-08-29** — A multi-lens adversarial review of that contract layer confirmed 17 defects, all fixed and reverified. The four that mattered:
  - `Settings` inherited `extra="forbid"` from `BaseSettings`, so a single unrelated key in a shared `.env` aborted startup — and pydantic's `ValidationError` echoes the offending *value*, so a token under a near-miss name (`GH_TOKEN=ghp_…`) would print in cleartext via the default excepthook, which no logging filter can reach. Now `extra="ignore"`.
  - `models/api.py` hardcoded `300` and `1024` instead of reading `Settings`, so tightening `MAX_URL_LENGTH` had no effect at the boundary meant to enforce it. Bounds are now `AfterValidator`s reading `get_settings()`.
  - Redaction covered only `ghp_` and `github_pat_`. `ghs_` — what GitHub Actions puts in `$GITHUB_TOKEN`, and so the likeliest value an operator pastes into ours — passed through verbatim, as did `gho_`/`ghu_`/`ghr_`. The `Authorization` pattern also missed the `[('authorization', 'Bearer …')]` form that `httpx.Headers.items()` produces, which is exactly what one reaches for when debugging a 401 because httpx masks its own `repr`.
  - `RedactingFilter` scrubbed `record.exc_text`, but filters run *before* formatters, so that field is always `None` at filter time — tracebacks were redacted only by `JsonFormatter`, and a plain handler leaked them. The filter now renders the traceback itself.
- **2026-08-29** — Dependency versions verified live against PyPI and npm. Three traps recorded: R3F 10 is alpha and incompatible with drei 10 (pin R3F 9.7.0); TypeScript must be pinned to 5.9.3 because `typescript-eslint` 8.68 caps at `<6.1.0`; `d3-force-3d` ships no types and no `@types` package exists, so a local `.d.ts` is required. Also: `tree-sitter` 0.25/0.26 removed `Query.captures()` and `Language.query()` in favour of `QueryCursor`.
- **2026-08-29** — This documentation system created. Design rationale captured as ADR-001 … ADR-008 in [DECISIONS.md](DECISIONS.md), so the plan is now self-contained in the repository.
- **2026-08-29** — `.gitignore` added, `PRD.md` staged, `backend/` dependency scaffold created and installed. `pydantic-settings` pinned to **2.15.0** after checking PyPI; `ruff` **0.16.5** and `mypy` **2.3.1** pinned rather than floated, per the "pin dependency versions" rule. `hatchling` **1.32.0** added as the build backend — the only dependency beyond the agreed list, needed because a PEP 517 backend is required for the package to be installable at all.
- **2026-08-29** — tree-sitter ABI spike run for real: `Language(tree_sitter_typescript.language_tsx()).abi_version` → **`14`**. The `QueryCursor` API is present, confirming the 0.25/0.26 migration away from `Language.query()` / `Query.captures()` / `Query.matches()`.

## Next Steps

1. Implement `fetch/github.py` — the first code that can actually make a request, and the first caller of the guard. It must use `follow_redirects=False`, pass every `Location` through `validate_download_url`, call `assert_public_ip` before connecting, set `trust_env=False`, and send **no `Authorization` header** on the codeload request. That last assertion is the one piece of the net_guard task that could not be written without the client, and it is still owed.
2. Implement `fetch/archive.py` with in-process malicious-tarball fixtures (`io.BytesIO`), covering traversal, symlinks, bombs, and malformed names. Still no network.
3. When routes land, wire `AppError` into a FastAPI exception handler and map `RequestValidationError` to a bare `INVALID_REQUEST` — pydantic's `detail` embeds the offending input and must never be returned.

The ABI half of the tree-sitter spike is **done** (see above). The `progress_callback` signature on `Parser.parse()` is still unverified and must be confirmed before the extractor is written.

## Last Updated

2026-08-29
