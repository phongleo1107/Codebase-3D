# Security Model

> **Most security controls in this document are still not implemented.** As of 2026-08-29 the backend contract layer (config, errors, models, logging), the **URL/egress security boundary** (`app/security/url_validation.py`, `app/security/net_guard.py`), and the **GitHub client that calls it** (`app/fetch/github.py`) exist. The egress guard now has a caller: every redirect the client sees goes through it. There is still no archive, parsing, or routing code, and **no archive byte is ever fetched** — the client resolves a download URL but does not download it — so every extraction and resource-limit control below is still `Planned`. Rows marked `Implemented` name the file; treat every other row as aspirational. Flip a row only when you have seen the code, and name the file in the same edit.
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
| Arbitrary URL fetched from user input | Critical | Accept only `https://github.com/<owner>/<repo>`, parsed against a strict grammar; the download host is never user-supplied | **Implemented** — the grammar is `app/security/url_validation.py`; `app/fetch/github.py` builds every request URL from `https://api.github.com` plus path segments re-validated against a fixed character set, so no user string chooses a host |
| Redirect to an attacker host | Critical | `follow_redirects=False` everywhere; exactly one redirect permitted, its host compared by **string equality** against a one-element allowlist (`codeload.github.com`) — never `endswith`, which `codeload.github.com.evil.com` defeats | **Implemented** — the equality allowlist is `app/security/net_guard.validate_download_url`; `follow_redirects=False` and the single validated hop are `app/fetch/github.py`. A `Location` on a non-redirect status is not followed, and each refusal test asserts the disallowed hop is never requested |
| DNS rebinding / hostile resolver | High | Resolve the host and require every returned address to be globally routable — `is_global`, **plus** `not is_reserved`, `not is_multicast`, and `not is_site_local`, after unmapping `::ffff:`. All three extras are load-bearing: on CPython 3.14.7 `is_global` is `True` for `::127.0.0.1`, `64:ff9b::7f00:1`, `224.0.0.1`, and every address in `fec0::/10`. A sweep of all 65 536 IPv6 `/16` prefixes confirms those four predicates leave no further gap | **Implemented** — `app/security/net_guard.assert_public_ip`; no caller yet |
| Private / loopback / link-local targets | Critical | Rejected by both the URL grammar and the resolved-IP check. Explicitly: `localhost`, `127.0.0.1`, `::1`, `10/8`, `172.16/12`, `192.168/16`, `169.254.169.254` | **Implemented** — `app/security/url_validation.py` and `app/security/net_guard.py` |
| Non-HTTPS or non-HTTP schemes | High | Scheme must be exactly `https`; kills `file:`, `ftp:`, `javascript:`, and protocol-relative `//evil` Locations | **Implemented** — `app/security/url_validation.py` and `app/security/net_guard.py` |
| Proxy env vars redirecting traffic | Medium | `trust_env=False` on the HTTP client, ignoring `HTTP_PROXY` / `ALL_PROXY` / `NO_PROXY` | **Implemented** — `app/fetch/github.create_client` |
| Credential leak to a redirect target | High | The tarball download request is issued **without** an `Authorization` header. If host validation were ever bypassed, no token leaves the process | **Implemented** — `app/fetch/github.py`. The client holds no `Authorization` default at all; the token is a per-request header on `api.github.com` only, and `download_request()` pops any inherited one (ADR-009) |
| Homograph / lookalike hostnames | Medium | ASCII-only check on the raw URL before parsing | **Implemented** — `app/security/url_validation.py` |
| URL parser differential | Medium | `urlsplit` silently deletes tab/CR/LF anywhere in a URL, so `https://gith<TAB>ub.com/o/r` parses as `github.com`. Both modules screen C0 controls, space, DEL, and `\` **before** parsing, so no accepted URL can read differently to a human than to the parser | **Implemented** — `app/security/url_validation.py` and `app/security/net_guard.py` |

> **Residual risk — DNS rebinding is narrowed, not closed.** `assert_public_ip` resolves the name, but the connection that follows is made *by name*, so a resolver that answers differently the second time is not caught. Closing it fully requires connect-by-IP with SNI, which v1 does not do.

### Command injection

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| Shell metacharacters in a repo URL or path | Critical | **No `subprocess` in the ingestion or analysis path at all.** No `git`, no shell, never `shell=True`. Downloading is an HTTP request; extraction is `tarfile` | **Partial** — the ingestion path that exists (`app/fetch/github.py`) is HTTP only and imports no `subprocess`; owner, repo, and ref are percent-encoded per path segment after a character-set check. Extraction and analysis are not written |
| Git-mediated code execution | High | `git clone` is deliberately not used — git honors `.gitattributes` filters and `core.*` config, which are attacker-controlled | **Partial** — `app/fetch/github.py` fetches the tarball URL over HTTPS; no git invocation exists anywhere. Stays `Partial` until the archive reader lands and the claim covers the whole path |

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
| Future disk I/O reintroducing traversal | Medium | `security/path_safety.safe_relative_path()` (realpath + `commonpath`) **is to be** implemented and tested even though nothing currently writes files. The module does not exist yet | Planned |

### Malicious repository code

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| Repository code executed | Critical | Parsing only. No dependency install, no build scripts, no package scripts, no interpreters, no Makefiles, no shell scripts | Planned |
| Parser crash or hang on crafted input | Medium | Per-file size cap, binary detection, whole-file `except Exception` plus `RecursionError`/`MemoryError`, and a deadline-driven `progress_callback` that aborts a pathological parse. A failed file is skipped and reported, never fatal | Planned |
| Phantom dependencies from commented-out or stringified imports | Medium (correctness) | tree-sitter AST queries rather than regex, so `// import 'x'`, `/* import */`, `"import 'x'"`, and `` `import('${x}')` `` are correctly ignored | Planned |

### Resource exhaustion / DoS

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| Huge repository | High | GitHub-reported size preflight (256 MiB) before download; compressed cap 64 MiB; extracted cap 256 MiB | **Partial** — the preflight is `app/fetch/github.get_repo_metadata`, reading `MAX_REPO_API_SIZE_KB` at call time and refusing before any download URL is resolved. The compressed and extracted caps are the archive reader's job and do not exist |
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
| Private-repo existence oracle | Medium | GitHub `403` and `404` both map to one opaque `REPOSITORY_NOT_FOUND` | **Implemented** — `app/fetch/github.get_repo_metadata`; a test asserts the two are byte-identical in type, status, and body |
| Secrets in frontend code | High | No token ever reaches the client; GitHub is contacted only server-side | Planned |

### Information disclosure via errors

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| Stack traces / filesystem paths in responses | Medium | Fixed body shape `{"error": {code, message, requestId}}`; unhandled exceptions return only a request ID, with the traceback logged server-side | **Partial** — the contract exists in `app/errors.py` (static messages; `AppError.__init__` takes no arguments, so dynamic detail cannot reach a body). No FastAPI exception handler exists yet, so nothing actually returns it |
| Pydantic validation echoing user input | Medium | `RequestValidationError` mapped to a generic 422 — never return Pydantic's `detail`, which embeds the offending input | Planned |
| A *stdlib* exception echoing user input | Medium | `urlsplit` is not total: a malformed bracketed host (`https://[evil.com]/o/r`) raises `ValueError` with the host quoted in the message. Every partial call in the security modules is wrapped and re-raised as a typed `AppError` with `from None`, so the value never reaches a body and no traceback renders it. Note `from None` clears `__cause__` and suppresses *display* of `__context__`; the original exception object is still reachable via `.__context__`, so an error handler must never serialize exception attributes. A fuzz sweep in each test file asserts nothing but the typed error escapes | **Implemented** — `app/security/url_validation.py`, `app/security/net_guard.py` |

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

Malicious archives are to be built in-process with `tarfile` + `io.BytesIO` rather than checked in as binary fixtures, so each attack is readable in the diff. **No security test may touch the network** — this is enforced, not trusted: `tests/conftest.py` replaces `getaddrinfo`, `create_connection`, and `socket.connect` for the whole session with a call that raises, so a test that forgets to stub fails loudly instead of quietly resolving a real name.

Done — `backend/tests/test_url_validation.py`, `backend/tests/test_net_guard.py`, `backend/tests/test_github_client.py` (477 cases):

- URL validation: homographs, userinfo, ports, private IPs, `evil.com/github.com/o/r`, encoded and literal traversal, NUL and other control characters, oversized input, and the `urlsplit` tab/CR/LF differential.
- Redirect handling: `302` to an attacker host, to a suffix of the allowlisted host, downgraded to `http`, protocol-relative, relative, and to the metadata endpoint; a second redirect; a missing `Location`. Each asserts the disallowed hop is **never requested**.
- Resolved-IP rejection, including the three addresses `is_global` alone would admit.
- The rejection body is asserted byte-identical across every rejected input, so no offending value can reach a response.
- **The codeload request carries no `Authorization`.** Both requests are driven through one client with a token configured, in the order the pipeline will make them; the download request is then sent and its headers asserted bare — under the header name *and* by searching every header value for the token. A second case supplies a client that was deliberately built with an `Authorization` default and asserts the download is still bare. Both were confirmed by mutation: removing the header strip fails the suite.
- `follow_redirects=False`, `trust_env=False`, and the absence of a client-level `Authorization` are each asserted directly, so a change to the client constructor cannot silently drop one.
- Preflight behaviour: the `403`/`404` collapse (byte-identical bodies), the size refusal (including at the exact limit and with the limit tightened through the environment), a malformed or hostile `/repos` body — wrong types, missing fields, a repository name of `../../etc/passwd`, a `default_branch` of `..` — and a hostile owner/repo/ref asserted to never reach the wire at all.
- The 12 controls in `app/fetch/github.py` were **mutation-tested**, one deletion at a time: all 12 are caught. The first pass had one survivor — removing the redirect-status check still failed on the missing `Location`, so a `200 OK` carrying a `Location` would have been treated as a download target. A test for exactly that was added.

Still required: archive traversal, symlinks, hardlinks, bombs, and malformed names; secret filtering as an end-to-end invariant over the whole response; parser robustness on truncated, binary, and pathological input; limit enforcement; rate limiting and concurrency; and an assertion that **no error body ever contains a traceback, a filesystem path, or a token**.
