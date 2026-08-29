# Security Model

> **Many security controls in this document are still not implemented.** As of 2026-08-29 the backend contract layer (config, errors, models, logging), the **URL/egress security boundary** (`app/security/url_validation.py`, `app/security/net_guard.py`), the **GitHub client that calls it** (`app/fetch/github.py`), and the **streaming archive reader** (`app/fetch/archive.py`) exist. Two further security modules — `app/security/secret_filter.py` and `app/security/path_safety.py` — are implemented and tested but **have no callers at all**; a rule nothing applies protects nothing, which is why the row each one backs is not simply `Implemented`. There is still no parsing, analysis, or routing code, and **nothing in this codebase has ever sent the download request** — `github.py` resolves and builds it, `archive.py` consumes the bytes it would return, and no caller joins the two. The archive controls below are therefore implemented and tested against in-process tarballs, not against a real download. Rows marked `Implemented` name the file; treat every other row as aspirational. Flip a row only when you have seen the code, and name the file in the same edit.
>
> Note in particular that the limit *constants* have existed in `app/config.py` since before anything read them. **A constant is not a control.** Every remaining row describing enforcement of a node/edge, file-count, rate, or duration limit stays `Planned` until code reads that constant and rejects something.

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
| Shell metacharacters in a repo URL or path | Critical | **No `subprocess` in the ingestion or analysis path at all.** No `git`, no shell, never `shell=True`. Downloading is an HTTP request; extraction is `tarfile` | **Partial** — the whole ingestion path now exists (`app/fetch/github.py`, `app/fetch/archive.py`) and neither imports `subprocess`; owner, repo, and ref are percent-encoded per path segment after a character-set check. Analysis is not written |
| Git-mediated code execution | High | `git clone` is deliberately not used — git honors `.gitattributes` filters and `core.*` config, which are attacker-controlled | **Implemented** — ingestion is an HTTPS fetch plus `tarfile`; no git invocation exists anywhere in the repository. Archive members are read as bytes and never interpreted, so `.gitattributes` is just another file |

### Path traversal and archive attacks

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| `../../etc/passwd` in an archive member | High | **No file is ever written to disk** (ADR-003), so traversal has no write target. Member paths are still rejected if any component is `""`, `.`, or `..`, after normalizing `\` to a separator | **Implemented** — `app/fetch/archive._check_member_name`. The `\` normalization is load-bearing below the root directory, where the root-name pattern cannot help: `root-sha/src\..\..\evil.ts` is otherwise one component that is not `..` |
| Absolute paths in members | High | Reject `/…`, `C:\…`, `\\…` | **Implemented** — `app/fetch/archive._check_member_name`. Redundant by design (a leading separator becomes an empty component; a drive letter becomes a root that fails `ROOT_PATTERN`), and annotated as such in the code |
| Symlink / hardlink escape | High | Only regular files are read. `issym`, `islnk`, `ischr`, `isblk`, `isfifo` are skipped and counted, never followed | **Implemented** — `app/fetch/archive.iter_source_files`. `extractfile` is called only after `isfile()`, so a link target is never resolved and `linkname` is never read |
| Traversal via the `/api/source` path parameter | High | Path must match an HMAC token issued by the analyzer for that exact `owner/repo@sha:path`; forged paths cannot produce a valid token | Planned |
| Zip/tar bomb | High | Cumulative decompressed cap (256 MiB) **plus** a compression-ratio guard (max 100:1, enforced once past an 8 MiB floor) so a gigabyte-of-zeros bomb trips at ~8 MiB | **Implemented** — `app/fetch/archive._DecompressedStream`. Both are metered on the *decompressed stream*, on every read, not per accepted member: a non-seeking `tarfile` reads past the body of a member this module skips for being oversized, so a 1 GiB bomb yields no files at all and would be invisible to a per-member accounting |
| Malicious member names (NUL, lone surrogates) | Medium | Reject names containing NUL or failing a strict UTF-8 round-trip — `tarfile`'s `surrogateescape` decoding otherwise leaks raw bytes | **Implemented** — `app/fetch/archive._check_member_name`. A NUL is reachable only through a pax `path` header; the ustar name field is NUL-terminated, so `tarfile` truncates there |
| Archive with multiple root directories | Medium | Root must match `^[A-Za-z0-9._-]+-[0-9a-f]{7,40}$` and be identical across all members | **Implemented** — `app/fetch/archive.ROOT_PATTERN` and the root-equality check in `iter_source_files`. Applied to regular files; directory and link members are skipped before it, which is safe only because nothing is written |
| Unicode-normalization traversal | Medium | Member names are compared as the exact code points the archive carried — **nothing normalizes them**. NFKC would fold U+FF0E to `.` and U+FF0F to `/`, turning an inert component into `../etc/passwd` | **Implemented** — by construction, in `app/fetch/archive._check_member_name`, and pinned by test. The residual risk is *downstream*: anything that later normalizes a yielded path — a normalizing filesystem, a `unicodedata.normalize` before a comparison, a database collation — undoes the guarantee. A yielded path is a node ID and an `/api/source` subject, so it must be compared byte-for-byte |
| Future disk I/O reintroducing traversal | Medium | `security/path_safety.safe_relative_path()` — realpath the base *and* the candidate, then require `commonpath` to equal the base. Implemented and tested even though nothing currently writes files, so the first caller to need disk I/O finds a correct primitive rather than an `os.path.join` | **Implemented** — `app/security/path_safety.py`; no caller, by design. `commonpath` rather than `str.startswith`, which accepts the sibling `/srv/base-evil` as a child of `/srv/base`; the base is resolved too, or a symlinked base would refuse every call |

### Malicious repository code

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| Repository code executed | Critical | Parsing only. No dependency install, no build scripts, no package scripts, no interpreters, no Makefiles, no shell scripts | Planned |
| Parser crash or hang on crafted input | Medium | Per-file size cap, binary detection, whole-file `except Exception` plus `RecursionError`/`MemoryError`, and a deadline-driven `progress_callback` that aborts a pathological parse. A failed file is skipped and reported, never fatal | Planned |
| Phantom dependencies from commented-out or stringified imports | Medium (correctness) | tree-sitter AST queries rather than regex, so `// import 'x'`, `/* import */`, `"import 'x'"`, and `` `import('${x}')` `` are correctly ignored | Planned |

### Resource exhaustion / DoS

| Threat | Risk | Mitigation | Status |
|---|---|---|---|
| Huge repository | High | GitHub-reported size preflight (256 MiB) before download; compressed cap 64 MiB; extracted cap 256 MiB | **Implemented** — the preflight is `app/fetch/github.get_repo_metadata`; the compressed cap is `app/fetch/archive._CountingRawStream` (enforced eagerly as bytes arrive, so it bounds bandwidth and the adapter's own buffer) and the extracted cap is `_DecompressedStream` |
| Huge individual file | Medium | 2 MiB member cap, 1 MiB parse cap | **Partial** — the 2 MiB member cap is `app/fetch/archive.iter_source_files`; an oversized member is skipped, not fatal. The 1 MiB parse cap belongs to the parser, which does not exist |
| Thousands of files / deep nesting | Medium | 50 000 archive members, 3 000 parsed source files, depth 32, path length 1024 | **Partial** — the member cap, depth cap, and path-length cap are `app/fetch/archive.iter_source_files`. Depth and length are measured on the path *after* the archive root is stripped, so they do not vary with the repository's name. The 3 000-file parse cap belongs to the analysis pipeline |
| Unbounded graph | Medium | 6 000 node / 20 000 edge caps; truncation is deterministic and flagged in stats, never silent | Planned |
| Slow-loris or endless analysis | High | Cooperative `Deadline` (60s) checked between members, between files, and inside the parser. `asyncio.wait_for` cannot kill a thread, so the deadline — not the timeout — is the real mechanism | **Partial** — `app/analysis/deadline.py` exists (monotonic, frozen so a step cannot extend its own budget) and is checked between archive members by `app/fetch/archive.iter_source_files`. The between-files and in-parser checks, and the request-scoped construction, do not exist |
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
| Returning `.env`, keys, credentials | High | Deterministic secret-path filter applied during analysis **and** re-applied independently in `/api/source` from the same shared module, so a forged token still cannot extract them | **Partial** — the rule is `app/security/secret_filter.is_secret_path`, implemented and tested against two golden lists and mutation-tested control by control. **Neither call site exists**: there is no analysis pass and no `/api/source`, so nothing is filtered today. This row becomes `Implemented` when both call it, not before |
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

ADR-003's half is now real for the ingestion path: `app/fetch/archive.py` streams the tarball through `gzip` and `tarfile` and yields member bytes, and it opens no file and imports no `os`, `pathlib.Path`, `shutil`, or `tempfile`. The claim still cannot be published, because nothing downstream of it exists — the parser, the graph builder, and the routes could each reintroduce a write.

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

Done — `backend/tests/test_archive.py` and `backend/tests/test_deadline.py` (142 cases), over archives built by `backend/tests/fixtures/tarballs.py`:

- Traversal in every spelling: `../../etc/passwd`, `..\..\x`, mixed `src\../../evil.ts`, `.` and empty components, and — the case that matters most — backslashes *below* the root directory, where the root-name pattern offers no cover.
- Symlinks, hardlinks, character and block devices, and FIFOs: each is skipped while a real file beside it is still yielded, so "skipped" is proven distinct from "aborted the archive".
- Absolute paths: POSIX, `C:\`, `c:/`, and UNC `\\server\share`.
- Malformed names: a NUL delivered through a pax `path` header, and lone surrogates from undecodable header bytes — plus two tests that assert the *fixtures* really produce a NUL and really write raw `\xff`, so the attack tests cannot pass by accident on a clean archive. A valid non-ASCII name is asserted to be **accepted**, because the rule is "decodable", not "ASCII".
- Roots: nine rejected root shapes, a second root, and a well-formed second root.
- **Unicode normalization is asserted *absent*.** A tar member name is bytes; neither `tarfile` nor this module normalizes it, and the tests pin that, because normalizing would *create* the attack rather than defend against one — under NFKC, U+FF0E folds to `.` and U+FF0F folds to `/`, so a name that is one inert component beforehand becomes `../etc/passwd` afterwards. Both lookalikes are asserted to survive as a single component, NFC and NFD spellings of the same grapheme are asserted to round-trip as distinct code points (a yielded path becomes a node ID and the subject of an `/api/source` token, so silently folding one into the other would make the token miss the file the user clicked), and real ASCII `..` is asserted still rejected beside them. Confirmed by mutation: inserting an NFKC normalization before the component check fails three of these tests.
- Bombs: a 1 GiB payload of zeros rejected after reading under half of a 1 MiB compressed archive, asserted to raise the *ratio* error rather than the extracted-size one, so the test would notice if it were the 256 MiB cap doing the work. Alongside it, two tests that the guard does **not** fire below the floor, since ordinary source compresses well past 100:1 in the first few kilobytes.
- Every cap: compressed download, extracted total, member count (including a real 50 000-member archive), member size, path depth, and path length — each with an at-the-limit case, and each in isolation with the other guards lifted so a test cannot pass on the wrong control.
- Streaming behaviour: abandoning the generator after one file is asserted to leave most of the archive unread, and the download cap is asserted to trip having pulled no more than the cap plus one chunk. Both use incompressible filler, because a megabyte of `b"x"` arrives in the decompressor's first read and makes such assertions vacuous.
- Malformed streams: empty input, non-gzip, gzip of garbage, truncated archive, truncated mid-member, and a header declaring more bytes than the archive contains. None escapes as a bare `TarError`, `EOFError`, or `zlib.error`.
- The rejection body is asserted byte-identical across different hostile paths, and asserted not to contain them.
- The 24 controls in `app/fetch/archive.py` were **mutation-tested**, one deletion at a time: 21 caught. The three survivors are documented in the code — the absolute-path check is subsumed by the empty-component and root-name checks, and a negative member size is unreachable because `tarfile` raises on the offset first. The first pass had a fourth survivor that was **not** redundant: deleting the `\` → `/` normalization left the suite green, because every backslash case then in the suite failed on the root-name pattern instead. Cases with backslashes below the root were added and the mutation is now caught.

Done — `backend/tests/test_secret_filter.py` and `backend/tests/test_path_safety.py` (142 cases):

- The secret filter is specified by two golden lists. The *allowed* list is the load-bearing one: every entry is a plausible source file that a sloppier rule eats — `src/secrets.ts` if the exact-name rule became a prefix, `monkey.ts` and `keyboard.tsx` if the `.key` suffix rule became a substring search, `server.ts` if `server.*` were read literally, `.gitignore` and `src/build.ts` if the directory rules matched by prefix or applied to the final component.
- Case variants of every family (`.ENV`, `ID_RSA`, `Private.PEM`, `NODE_MODULES/…`), because the machine that produced the repository is often case-insensitive and we are not.
- Fail-closed shapes — empty, `.`, `..`, traversing, absolute — asserted secret. None can reach the filter while `archive._check_member_name` runs first; the assertion is about what happens once that assumption breaks.
- A 20 000-path determinism sweep over boundary fragments (each rule's trigger, each rule's near-miss, separators, and characters where `casefold` is non-trivial: U+212A KELVIN SIGN, `ß`, `İ`). It asserts the filter is a pure function, returns a plain `bool`, and never raises — the analysis pass and `/api/source` judge the same path in two different requests, so a filter that could disagree with itself makes the second check worthless.
- Path safety is tested against **real symlinks** under `tmp_path`, not a stubbed `realpath`: a link to a sibling directory, one nested below the first path component, one to `/etc`, one dangling, and one pointing *inside* the base that must still be accepted. Plus the prefix-sibling case (`/…/base-evil` against `/…/base`) that `str.startswith` gets wrong, and a symlinked *base*, which an implementation that resolved only the candidate would refuse outright.
- Every rejection message is asserted to come from a fixed three-element set and to contain no fragment of the offending path.
- The 12 controls in `secret_filter.py` and the 6 in `path_safety.py` were **mutation-tested**, one deletion at a time: all 18 are caught. Both widening mutations are caught too — scoping the directory rules to every component instead of the parents, and swapping `commonpath` for `startswith`.

Still required: secret filtering as an end-to-end invariant over the whole response — the module exists, but nothing calls it; parser robustness on truncated, binary, and pathological input; node/edge and file-count limit enforcement; rate limiting and concurrency; and an assertion that **no error body ever contains a traceback, a filesystem path, or a token**.
