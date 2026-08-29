# Current State

## Current Goal

Build the MVP defined in [PRD.md](../PRD.md): paste a public GitHub URL → safely analyze a TS/JS repository → render its dependency graph in navigable 3D.

Steps 1-4 of the build order are done and green: **the backend contract** (config, errors, models, logging), **the URL/egress security boundary** (`security/url_validation.py`, `security/net_guard.py`), **the GitHub client** (`fetch/github.py`) — the first module in the project that can open a socket — and **the streaming archive reader** (`fetch/archive.py`). Two further security modules, **`security/secret_filter.py`** and **`security/path_safety.py`**, are implemented and tested ahead of their call sites.

**The two halves of ingestion are not joined.** `github.py` builds the credential-free download request but never sends it; `archive.py` consumes a byte iterator that, so far, has only ever come from a test fixture. No archive byte has been fetched over a network by this codebase.

**And two security modules are unwired.** `is_secret_path` and `safe_relative_path` are correct and mutation-tested, and **nothing calls either one**. No `.env` is filtered today, because there is no analysis pass and no `/api/source` to filter it out of. Do not read their presence as protection.

## Working

**The backend contract layer, the security boundary, the GitHub client, the archive reader, and their tests.** There is still no routing, parsing, or analysis code: `app/api/` remains an empty package, `app/analysis/` holds only `deadline.py`, and `app/security/` holds four of its five planned modules.

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
| `backend/app/security/net_guard.py` | `validate_download_url` (equality allowlist) + `assert_public_ip` (resolved-IP check). Both are now called by `fetch/github.py` on every redirect |
| `backend/app/fetch/github.py` | `create_client` (`follow_redirects=False`, `trust_env=False`, no `Authorization` default), `get_repo_metadata` (preflight, size gate, 403/404 collapse), `get_download_url` (validated single hop), `download_request` (the credential-free download GET) |
| `backend/app/fetch/archive.py` | `iter_source_files` — streams the download through `_CountingRawStream` → `gzip` → `_DecompressedStream` → `tarfile("r|")`, yielding `(PurePosixPath, bytes)` per acceptable regular file. `Limits` is a no-default view onto `Settings` |
| `backend/app/security/secret_filter.py` | `is_secret_path` — exact names, prefixes, extension suffixes, and excluded directories, matched case-insensitively over path components. Pure, no I/O. **No caller** |
| `backend/app/security/path_safety.py` | `safe_relative_path` — realpath the base *and* the candidate, then require `commonpath` to equal the base. Raises `ValueError`, never an `AppError` (it is a utility, not an HTTP boundary). **No caller**, by design |
| `backend/app/analysis/deadline.py` | `Deadline` — frozen, monotonic; `check()` raises `AnalysisTimeoutError` |
| `backend/tests/fixtures/tarballs.py` | `make_tar`, `make_source_tar`, `make_member_with_name`, `make_pax_name`, `make_symlink_member`, `make_hardlink_member`, `make_oversized_header`, `make_many_members`, `make_bomb`, `chunked`, `noise` |
| `backend/tests/conftest.py` | Session-scoped autouse fixture blocking `getaddrinfo`, `gethostbyname`, `create_connection`, and `socket.connect`/`connect_ex`. Raises `NetworkAccessAttempted`, a `RuntimeError` — deliberately *not* an `OSError`, so it travels straight through `assert_public_ip`'s handler instead of being swallowed as a rejection |
| `backend/tests/` | 915 tests across config, errors, models, logging, URL validation, net guard, GitHub client, archive reader, deadline, secret filter, path safety |
| `backend/app/api/` | Still an empty package |
| `backend/app/security/` | HMAC tokens still to come |
| `.claude/settings.local.json` | Local tool permissions, not source |

No `frontend/`, no Docker files, no CI.

**Verified locally on 2026-08-29** (Python 3.14.7, uv 0.12.3):

- `uv sync` resolves and installs cleanly; the project installs as an editable package.
- `uv run pytest` → **915 passed** in ~6s. `uv run mypy` (strict) → clean over 33 files. `uv run ruff check .` → clean.
- tree-sitter ABI spike printed **`14`**, and `QueryCursor` imports successfully alongside `Language`, `Parser`, and `Query`.

Two `pyproject.toml` corrections were needed: `[tool.ruff] src = ["."]` (it was `["app", "tests"]`, which pointed *inside* the package so isort never treated `app` as first-party), and `S105`/`S106` added to the test per-file-ignores because redaction tests must hardcode fake credentials.

## In Progress

- Nothing is under active implementation.

## Broken / Known Issues

- No CI yet.
- **The compression-ratio guard and the 50 000-member cap are in tension at the extreme.** An archive of 50 000 *empty* files is mostly zero padding: measured at ~25 MiB extracted from ~355 KiB compressed, a ratio of ~74 against a cap of 100. It passes today, but with under a 1.4× margin that no real repository approaches — ordinary source sits around 5:1. The 50 000-member test therefore lifts the ratio guard, so it tests the count cap alone rather than becoming a hostage to the zlib version. If a legitimate repository is ever refused as a bomb, this is the interaction to look at first; the fix is to raise `RATIO_FLOOR_BYTES` or to exclude header padding from the numerator, not to raise the ratio.
- **`archive.py` never returns the commit SHA**, although it validates the archive root that carries it. ARCHITECTURE.md ingestion step 5 stays `Planned` for that reason. Nothing needs it until a pipeline exists, but do not read the root check as "the SHA is harvested".
- **`secret_filter.py` and `path_safety.py` have no callers.** Both are correct and mutation-tested; neither protects anything yet, because the code that would apply them does not exist. Grepping for the module and finding it is not evidence that a `.env` is filtered — grep for the *call*.
- **`assert_public_ip` narrows DNS rebinding, it does not close it.** The connection that follows is made by name, so a resolver that answers differently the second time is not caught. Closing it needs connect-by-IP with SNI, which v1 does not do. Recorded in `docs/SECURITY.md`.
- **`ruff format` is not a project gate — do not run it.** `uv run ruff check .` is the gate and is clean. `ruff format --check` reports 5 of 21 files as unformatted: four pre-existing (`app/logging_setup.py`, `tests/test_config.py`, `tests/test_logging.py`, `tests/test_models.py`) and `app/security/net_guard.py`, which is unformatted for the same reason they are — the formatter wants to join wrapped constructs into lines that then exceed the configured `line-length = 100`. Running it would rewrite unrelated code to no benefit. Either adopt it repo-wide as a deliberate decision or leave it alone; do not apply it to one file.
- The contract layer is unexercised by any route — nothing constructs an `AnalyzeResponse` from real data yet, so field *semantics* are only as good as the documentation.
- `pytest.filterwarnings = ["error"]` now runs against a real suite and is clean; no targeted ignores have been needed.
- The pydantic **mypy plugin is not enabled** (no `plugins` key in `[tool.mypy]`). Constructor type-checking still works via PEP 681 `@dataclass_transform` on pydantic's metaclass. Reviewed and judged unnecessary; do not assume the plugin is present when reading type errors.

## Recently Completed

- **2026-08-29** — **Secret filter and path-safety guard implemented**: `app/security/secret_filter.py`, `app/security/path_safety.py`, plus 142 tests. `docs/SECURITY.md`'s "Future disk I/O reintroducing traversal" row moved to `Implemented`; the "Returning `.env`, keys, credentials" row moved to **`Partial`, not `Implemented`** — see below.

  Decisions and non-obvious behaviours worth carrying forward:
  - **The secret-exposure row is `Partial` on purpose.** The brief asked for `Implemented`, and the rule *is* implemented; but that row describes a filter "applied during analysis **and** re-applied independently in `/api/source`", and neither call site exists. SECURITY.md's own banner says a constant is not a control, and the last adversarial review caught a row falsely claiming `path_safety.py` was "implemented and tested" when the file did not exist. A rule nothing applies filters nothing. The row flips when both callers call it.
  - **`server.*` is deliberately narrower than the brief.** Read literally it blocks `server.ts` and `server.js` — the most common Node entry point there is — which would delete a real node from the graph, dangle every inbound edge, and 403 a file the user can already read on GitHub. The pattern is aimed at TLS material, so it matches `server.` plus a credential extension (`.crt .cert .csr .der .jks .keystore`); `.pem`, `.key`, and `.p12` were already covered by the global suffix rule, so nothing is lost. Confirmed with the requester before deviating.
  - **The allowed list is the load-bearing half of the spec.** Blocking secrets is easy; the failure mode that actually ships is a rule that eats source. `monkey.ts` and `keyboard.tsx` die to a substring search for `key`, `src/secrets.ts` dies if the exact-name rule becomes a prefix, `.gitignore` dies if the `.git` directory rule matches by prefix, and `src/build.ts` and a Bazel `BUILD` file die if the directory rules are applied to the final component instead of the parents only. Each is a test.
  - **Directory rules match parents; name rules match every component.** The asymmetry is the reason `BUILD` survives while `.env/keys.ts` does not — a *file* named `build` is source, but a *directory* named `.env` shields nothing.
  - **`casefold`, not `lower`.** Every difference between them widens the match, and the machine that produced the repository is frequently case-insensitive: `.ENV` and `ID_RSA` arrive intact and are exactly as sensitive.
  - **`commonpath`, not `str.startswith`** — `startswith` accepts `/srv/base-evil` as a child of `/srv/base`, since the prefix matches but the component boundary does not. And **the base is realpath'd too**: `/tmp` is `/private/tmp` on macOS and `tmp_path` inherits that, so resolving only the candidate would refuse every legitimate call. Both are covered by a test and by a mutation.
  - **An `if`, not an `assert`.** The brief said "assert the result stays inside base"; a real `assert` is stripped under `python -O`, which is exactly the deployment where the check still needs to hold.
  - `path_safety` raises a plain `ValueError` rather than an `AppError`: it is a low-level utility, not an HTTP boundary, and its caller owns the mapping. Its three messages are fixed literals — the NUL check exists partly so `os.stat`'s own `ValueError`, which quotes the path, never surfaces.
  - **The 12 controls in `secret_filter.py` and the 6 in `path_safety.py` were mutation-tested one deletion at a time; all 18 are caught**, including two *widening* mutations (directory rules applied to every component, and `commonpath` swapped for `startswith`). No survivors, so nothing needed a redundancy annotation.

- **2026-08-29** — **Streaming archive reader implemented**: `app/fetch/archive.py`, `app/analysis/deadline.py`, `tests/fixtures/tarballs.py`, plus 136 tests. `docs/SECURITY.md`'s "Path traversal and archive attacks" table moved to five `Implemented` rows, and four resource-exhaustion rows moved to `Implemented` or `Partial`.

  Decisions and non-obvious behaviours worth carrying forward:
  - **The gzip layer is ours, not `tarfile`'s** — `mode="r|"` over an explicit `gzip.GzipFile` rather than `r|gz`. This is the single most important design point in the module. `r|gz` leaves no seam between decompression and tar parsing, and metering has to happen at that seam: a non-seeking `tarfile` reads *past* the body of every member, including ones the reader skips for being oversized. A bomb whose payload is one 1 GiB member therefore yields **no files at all**, and any accounting that sums the sizes of accepted members sees zero bytes while a gigabyte goes through the decompressor. Metering the decompressed stream on every read kills it at ~8 MiB.
  - **A tar of pure zeros is not a bomb.** Confirmed empirically: `tarfile` reads the first zero block as the end-of-archive marker and stops, so a gzipped gigabyte of zeros yields zero members after ~10 KiB. The fixture had to become a tar *header* declaring a 1 GiB member followed by zeros. It is built by concatenating gzip members (`gzip` decodes a concatenated stream as one continuous output), so the 1 GiB bomb costs about a megabyte and no measurable time.
  - **The compression-ratio denominator is bytes *delivered to the decompressor*, not bytes pulled from the iterator.** Read-ahead sitting in the adapter's buffer would inflate the denominator and delay the trip. As a consequence the tests must chunk their input realistically — a whole archive handed over in one `read` makes every early-abort assertion vacuous — and any test that measures consumption must use incompressible filler, because a megabyte of `b"x"` arrives in the decompressor's first read.
  - **`Limits` is a no-default frozen view onto `Settings`.** It restates the field *names* but never the numbers, so `Settings` stays the only source of values (CLAUDE.md) while a test can still exercise one control with the others lifted out of the way.
  - **Rejecting the archive and skipping a member are different outcomes**, and the split is deliberate: a path an honest `git archive` could not have produced (absolute, traversing, multi-rooted, malformed) aborts the run; a path merely past a resource budget (too deep, too long, too big) drops that one file. Several tests exist only to prove a given input lands on the right side of that line.
  - **`RepositoryTooLargeError` for byte budgets, `ArchiveRejectedError` for structure.** No new `ErrorCode` was added — the 14 in `errors.py` are the frozen wire contract, and a bomb is adequately described by the existing archive-rejected code. The bomb test asserts the *ratio* error specifically, so it would notice if the 256 MiB extracted cap were quietly doing the work instead.
  - **The 24 controls in the module were mutation-tested one deletion at a time; 21 are caught.** The three survivors are annotated in the code: the absolute-path check is subsumed by the empty-component and root-name checks, and a negative member size is unreachable because `tarfile` raises `ReadError("invalid offset")` on such a header before the member is handed over. A fourth survivor was **not** redundant and was a real hole in the suite — deleting the `\` → `/` normalization left everything green, because every backslash case then in the tests failed on the root-name pattern instead. `root-sha/src\..\..\evil.ts` would have been yielded verbatim. Cases with backslashes *below* the root were added.
  - Two fixture-verifying tests exist on purpose: one asserts the pax fixture really produces a NUL in `member.name`, one asserts the surrogate fixture really writes a raw `\xff`. Without them, a future `tarfile` that sanitized either would turn both attack tests green for the wrong reason.

- **2026-08-29** — **GitHub client implemented**: `app/fetch/github.py` plus 88 tests, and three new `Settings` fields (`GITHUB_CONNECT_TIMEOUT_S`, `GITHUB_READ_TIMEOUT_S`, `MAX_GITHUB_CONNECTIONS` — timeouts are operational limits, so they live in `Settings` like every other one). This is the first module that can open a socket and the first caller of the egress guard. `docs/SECURITY.md` gained four `Implemented` rows (Arbitrary URL, Redirect to attacker host, Proxy env vars, Credential leak) plus the private-repo-oracle row, and two `Partial` ones (Huge repository, the two command-injection rows).

  **The assertion the net_guard task left owing is now written and passing**: both requests run through one client with a token configured, and the codeload request is asserted bare — under the header name, and by searching every header value for the token.

  Decisions and non-obvious behaviours worth carrying forward:
  - **The token is a per-request header, never a client default** (ADR-009). The brief suggested setting it on the client and deleting it before the download; that makes the credential's absence depend on a `del` a refactor can drop. With no client-level header there is nothing to inherit, so a request carries the token only if a call site names it — and the only one that does targets `api.github.com`. `download_request()` still pops an inherited header, for a client this module did not build.
  - **`get_download_url` returns `(url, sha | None)`, not `(url, sha)`.** GitHub redirects a *branch* ref to `.../legacy.tar.gz/refs/heads/main`, which pins no commit; only a SHA-shaped ref produces one. The authoritative SHA still comes from the tar root during extraction, as ARCHITECTURE.md always said. Returning a fabricated or empty string here would have quietly become a wrong commit pin in `/api/source`.
  - **A `Location` header is only honoured on a redirect status.** Mutation testing found this: deleting the status check left the whole suite green, because every non-redirect test case happened to omit `Location`. A `200 OK` carrying one would have been read as a download target. `304` is why the accepted set is enumerated rather than `300 <= status < 400`.
  - **`isinstance(value, bool)` before `isinstance(value, int)`**, because `bool` subclasses `int`: an unguarded integer check would accept `private: 0` and a size field of `True`.
  - **Percent-encoding a path segment is not validation.** `..` is entirely unreserved characters, so `quote("..", safe="")` returns `..` unchanged. Owner, repo, and every ref component are checked against a fixed character set *before* being encoded, and a hostile value is asserted never to reach the wire rather than merely to be encoded on the way out.
  - GitHub's canonical `name`/`owner.login` are re-validated on the way *in*. They are upstream data that gets interpolated into the tarball URL and later into node paths; the API being trustworthy today is not the same as the response being structurally constrained.
  - The **12 controls in the module were mutation-tested one deletion at a time; all 12 are caught** (11 on the first pass, plus the redirect-status check after its test was added).

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

1. **Call `is_secret_path`.** It is written and unwired; the analysis pass and `/api/source` must each apply it independently, from this one module, before the SECURITY.md row can leave `Partial`.
2. Write the pipeline that finally joins `github.py` to `archive.py` — it constructs one `Deadline` per request, sends `download_request()`, and feeds `response.iter_bytes()` to `iter_source_files`. It should also harvest the commit SHA from the archive root, which `archive.py` validates but does not currently return.
3. When routes land, wire `AppError` into a FastAPI exception handler and map `RequestValidationError` to a bare `INVALID_REQUEST` — pydantic's `detail` embeds the offending input and must never be returned.

The ABI half of the tree-sitter spike is **done** (see above). The `progress_callback` signature on `Parser.parse()` is still unverified and must be confirmed before the extractor is written.

## Last Updated

2026-08-29
