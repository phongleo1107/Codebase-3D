# Security Model

> **Almost no security control in this document is implemented.** As of 2026-08-29 only the backend contract layer exists (config, errors, models, logging) — there is no network, archive, parsing, or routing code, so every ingestion and resource-limit control below is still `Planned`. Rows marked `Implemented` name the file; treat every other row as aspirational. Flip a row only when you have seen the code, and name the file in the same edit.
>
> Note in particular that the limit *constants* now exist in `app/config.py`. **A constant is not a control.** Every row describing enforcement of a download, extraction, ratio, count, or duration limit stays `Planned` until code reads that constant and rejects something.

## Trust Model

The analyzed repository is **hostile input**, in full: its URL, its archive structure, its file paths, its file contents, and its configuration files. Nothing derived from it is trusted, and none of it is ever executed.

The operator's environment (server filesystem, network position, `GITHUB_TOKEN`) is the thing being protected from it.

## Assets

| Asset | Why it matters |
|---|---|
| `GITHUB_TOKEN` | Optional, for API rate limits. Leaking it exposes the operator's GitHub account. |
| Server filesystem | Traversal or symlink escape could read or overwrite host files. |
| Internal network | The server can reach hosts a remote user cannot — the classic SSRF prize (cloud metadata endpoints, internal services). |
| Server CPU / memory / bandwidth | Finite and shared; exhaustion is a denial of service against all users. |
| Analyzed source code | A user's repository content. Must not be logged, retained, or leaked to other users. |
| Secrets inside analyzed repos | Repositories commonly contain committed `.env` files and keys. We must not surface them. |
| The browser session | XSS via repository content would run in the origin of the app. |

## Threats and Controls

Every control below is **`Planned`** unless its Status cell says otherwise.

### Network / SSRF

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| Arbitrary URL fetched from user input | Critical | Accept only `https://github.com/<owner>/<repo>`, parsed against a strict grammar; the download host is never user-supplied | Planned |
| Redirect to an attacker host | Critical | `follow_redirects=False` everywhere; exactly one redirect permitted, its host compared by **string equality** against a one-element allowlist (`codeload.github.com`) — never `endswith`, which `codeload.github.com.evil.com` defeats | Planned |
| DNS rebinding / hostile resolver | High | Resolve the host and require every returned address to be globally routable (`ipaddress.is_global`, after unmapping `::ffff:`) | Planned |
| Private / loopback / link-local targets | Critical | Rejected by both the URL grammar and the resolved-IP check. Explicitly: `localhost`, `127.0.0.1`, `::1`, `10/8`, `172.16/12`, `192.168/16`, `169.254.169.254` | Planned |
| Non-HTTPS or non-HTTP schemes | High | Scheme must be exactly `https`; kills `file:`, `ftp:`, `javascript:`, and protocol-relative `//evil` Locations | Planned |
| Proxy env vars redirecting traffic | Medium | `trust_env=False` on the HTTP client, ignoring `HTTP_PROXY` / `ALL_PROXY` / `NO_PROXY` | Planned |
| Credential leak to a redirect target | High | The tarball download request is issued **without** an `Authorization` header. If host validation were ever bypassed, no token leaves the process | Planned |
| Homograph / lookalike hostnames | Medium | ASCII-only check on the raw URL before parsing | Planned |

### Command injection

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| Shell metacharacters in a repo URL or path | Critical | **No `subprocess` in the ingestion or analysis path at all.** No `git`, no shell, never `shell=True`. Downloading is an HTTP request; extraction is `tarfile` | Planned |
| Git-mediated code execution | High | `git clone` is deliberately not used — git honors `.gitattributes` filters and `core.*` config, which are attacker-controlled | Planned |

### Path traversal and archive attacks

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| `../../etc/passwd` in an archive member | High | **No file is ever written to disk** (ADR-003), so traversal has no write target. Member paths are still rejected if any component is `""`, `.`, or `..`, after normalizing `\` to a separator | Planned |
| Absolute paths in members | High | Reject `/…`, `C:\…`, `\\…` | Planned |
| Symlink / hardlink escape | High | Only regular files are read. `issym`, `islnk`, `ischr`, `isblk`, `isfifo` are skipped and counted, never followed | Planned |
| Traversal via the `/api/source` path parameter | High | Path must match an HMAC token issued by the analyzer for that exact `owner/repo@sha:path`; forged paths cannot produce a valid token | Planned |
| Zip/tar bomb | High | Cumulative decompressed cap (256 MiB) **plus** a compression-ratio guard (max 100:1, enforced once past an 8 MiB floor, checked after every member) so a gigabyte-of-zeros bomb trips at ~8 MiB | Planned |
| Malicious member names (NUL, lone surrogates) | Medium | Reject names containing NUL or failing a strict UTF-8 round-trip — `tarfile`'s `surrogateescape` decoding otherwise leaks raw bytes | Planned |
| Archive with multiple root directories | Medium | Root must match `^[A-Za-z0-9._-]+-[0-9a-f]{7,40}$` and be identical across all members | Planned |
| Future disk I/O reintroducing traversal | Medium | `security/path_safety.safe_relative_path()` (realpath + `commonpath`) is implemented and tested even though nothing currently writes files | Planned |

### Malicious repository code

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| Repository code executed | Critical | Parsing only. No dependency install, no build scripts, no package scripts, no interpreters, no Makefiles, no shell scripts | Planned |
| Parser crash or hang on crafted input | Medium | Per-file size cap, binary detection, whole-file `except Exception` plus `RecursionError`/`MemoryError`, and a deadline-driven `progress_callback` that aborts a pathological parse. A failed file is skipped and reported, never fatal | Planned |
| Phantom dependencies from commented-out or stringified imports | Medium (correctness) | tree-sitter AST queries rather than regex, so `// import 'x'`, `/* import */`, `"import 'x'"`, and `` `import('${x}')` `` are correctly ignored | Planned |

### Resource exhaustion / DoS

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| Huge repository | High | GitHub-reported size preflight (256 MiB) before download; compressed cap 64 MiB; extracted cap 256 MiB | Planned |
| Huge individual file | Medium | 2 MiB member cap, 1 MiB parse cap | Planned |
| Thousands of files / deep nesting | Medium | 50 000 archive members, 3 000 parsed source files, depth 32, path length 1024 | Planned |
| Unbounded graph | Medium | 6 000 node / 20 000 edge caps; truncation is deterministic and flagged in stats, never silent | Planned |
| Slow-loris or endless analysis | High | Cooperative `Deadline` (60s) checked between members, between files, and inside the parser. `asyncio.wait_for` cannot kill a thread, so the deadline — not the timeout — is the real mechanism | Planned |
| Request flooding | High | Per-IP sliding window (5/min, 60/hour on analyze) plus a global concurrency semaphore (3) with a fast 503 | Planned |
| Oversized request body | Medium | 4 KiB cap enforced by ASGI middleware checking `content-length` **and** counting bytes on chunked bodies (Starlette does not cap bodies) | Planned |
| Event loop starvation by CPU-bound parsing | Medium | Analysis runs on a worker thread so health checks and the rate limiter stay responsive | Planned |

### XSS and frontend

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| XSS via source code in the preview | High | Syntax highlighting via shiki's `codeToTokens` rendered as React `<span>` text children. **No HTML string is ever constructed**, making the viewer structurally incapable of injection | Planned |
| XSS via repository metadata (paths, names) | High | All repo-derived strings rendered as React text. `react/no-danger` set to `error` so the no-`innerHTML` rule is mechanically enforced, not aspirational | Planned |
| Malicious/oversized API response | Medium | zod validation at the boundary with hard caps; dangling edges dropped rather than throwing | Planned |
| Node IDs used unsafely | Medium | IDs are used only as Map keys and React keys — never as DOM ids, never interpolated into a URL | Planned |
| Script injection via CSP gaps | Medium | `script-src 'self'`, `object-src 'none'`, `base-uri 'none'`, `frame-ancestors 'none'`. Shiki uses its JavaScript regex engine, not WASM, so `wasm-unsafe-eval` is not needed | Planned |

> **Known accepted weakness:** the CSP will need `style-src 'unsafe-inline'` because shiki tokens carry inline `style={{color}}`. Documented upgrade path: a theme has a bounded color set, so a `hex → className` map injected via `sheet.insertRule()` (CSSOM is not CSP-restricted) removes the need. Not planned for v1; inline *style* is orders of magnitude lower risk than inline *script*.

### Secret exposure

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| Returning `.env`, keys, credentials | High | Deterministic secret-path filter applied during analysis **and** re-applied independently in `/api/source` from the same shared module, so a forged token still cannot extract them | Planned |
| Token in logs or error bodies | High | Log filter redacting all GitHub token families (`ghp_ ghs_ gho_ ghu_ ghr_`, `github_pat_…`) and `Authorization` values in every shape they get logged in, including the `[('authorization', 'Bearer …')]` sequence form `httpx.Headers.items()` produces; `GITHUB_TOKEN` and `SOURCE_TOKEN_SECRET` held as `SecretStr` | **Implemented** — `app/logging_setup.py`, `app/config.py` |
| Secret echoed by a settings `ValidationError` at startup | Medium | `Settings` uses `extra="ignore"`. Under `BaseSettings`' inherited `extra="forbid"`, an unrelated `.env` key aborts startup and pydantic prints the offending *value*; that traceback goes to stderr via the default excepthook, which no logging filter can intercept | **Implemented** — `app/config.py` |
| Source code in logs | Medium | Never log file contents or import specifiers (both are repository content). Paths only at `DEBUG`, off by default | Planned |
| Private-repo existence oracle | Medium | GitHub `403` and `404` both map to one opaque `REPOSITORY_NOT_FOUND` | Planned |
| Secrets in frontend code | High | No token ever reaches the client; GitHub is contacted only server-side | Planned |

### Information disclosure via errors

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| Stack traces / filesystem paths in responses | Medium | Fixed body shape `{"error": {code, message, requestId}}`; unhandled exceptions return only a request ID, with the traceback logged server-side | **Partial** — the contract exists in `app/errors.py` (static messages; `AppError.__init__` takes no arguments, so dynamic detail cannot reach a body). No FastAPI exception handler exists yet, so nothing actually returns it |
| Pydantic validation echoing user input | Medium | `RequestValidationError` mapped to a generic 422 — never return Pydantic's `detail`, which embeds the offending input | Planned |

### Supply chain

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| Compromised or unmaintained dependency | Medium | Pinned versions with a lockfile; dependency count kept deliberately small; version and maintenance status verified before adoption | Planned |
| Untrusted grammar/parser binaries | Low | tree-sitter grammars are prebuilt wheels from PyPI; ABI compatibility verified against the core version | Planned |

## Privacy

Intended user-facing statement, valid only once ADR-003 and ADR-007 are actually implemented:

> Repository contents are processed in memory for analysis and are not written to disk or stored.

Do not publish this claim before verifying the implementation. Do not claim stronger guarantees than the code provides.

## Security Testing

None of these tests exist yet. All are required before the MVP is considered done.

Malicious archives are to be built in-process with `tarfile` + `io.BytesIO` rather than checked in as binary fixtures, so each attack is readable in the diff. No security test should touch the network.

Required coverage: URL validation (including homographs, userinfo, ports, private IPs, and `evil.com/github.com/o/r`); redirect handling (including an assertion that the codeload request carries **no** `Authorization`); resolved-IP rejection; archive traversal, symlinks, hardlinks, bombs, and malformed names; secret filtering as an end-to-end invariant over the whole response; parser robustness on truncated, binary, and pathological input; limit enforcement; rate limiting and concurrency; and an assertion that **no error body ever contains a traceback, a filesystem path, or a token**.
