"""Deterministic secret-path filter — the one place a path is judged sensitive.

Repositories routinely contain committed `.env` files, private keys, and cloud
credentials. This module decides which paths never become graph nodes and never
come back out of `/api/source`. Both call sites import *this* function rather
than reimplementing the rule, so a forged or mis-scoped source token still
cannot extract a secret: the filter is applied again, independently, at the
point of serving (docs/SECURITY.md, "Returning `.env`, keys, credentials").

It is a pure function of the path string. No I/O, no content sniffing, no
heuristics over file bodies — a filter that behaves differently on two runs of
the same repository is a filter nobody can reason about.

Three rules, and the reason each is shaped the way it is:

*Exact names* (`.npmrc`, `terraform.tfvars`, `secrets.json`) are compared whole.
`secrets.json` is a credential file; `src/secrets.ts` is a module that probably
*reads* one, and blocking it would put a hole in the dependency graph.

*Prefixes* (`.env…`, `id_rsa…`, `id_ed25519…`, `credential…`) catch the family
of a name: `.env.production.local`, `id_rsa.pub`, `credentials.json`.

*Suffixes* (`.pem`, `.key`, `.p12`) are matched as extensions, never as
substrings. `monkey.ts` and `keyboard.tsx` are ordinary source files, and a
substring test would eat both.

`server.*` is deliberately narrower than the rest. Read literally it blocks
`server.ts` and `server.js` — the most common Node entry point there is — which
would delete a real node from the graph and 403 a file the user can already see
on GitHub. What the pattern is actually aimed at is TLS material, so it is
matched as `server.` plus a credential extension; `.pem`, `.key`, and `.p12`
are already covered by the suffix rule above.

Directories are excluded for two different reasons that happen to want the same
mechanism: `.git`, `.aws`, and `.ssh` hold credentials and history, while
`node_modules`, `dist`, `build`, `.next`, and `.nuxt` hold vendored or generated
code that is not the user's repository and would swamp the graph.

Matching is case-insensitive. A repository is case-preserving but the machines
that produced it often are not, so `.ENV` and `ID_RSA` reach us intact and are
exactly as sensitive as their lowercase spellings.
"""

from pathlib import PurePosixPath
from typing import Final

# Matched against parent components only — these name *directories*. A file
# named `build` or `BUILD` (Bazel) is a source artifact, not build output, and a
# path ending in one of these is not a file inside it.
_EXCLUDED_DIRS: Final = frozenset(
    {
        "node_modules",
        ".git",
        "dist",
        "build",
        ".next",
        ".nuxt",
        # `.aws/*` and `.ssh/*`: the credential is the whole directory.
        ".aws",
        ".ssh",
    }
)

# Whole-name matches. Kept separate from the prefixes precisely so that
# `secrets.json` does not become `secrets*` and swallow `src/secrets.ts`.
_SECRET_NAMES: Final = frozenset({".npmrc", "terraform.tfvars", "secrets.json"})

_SECRET_PREFIXES: Final = (".env", "id_rsa", "id_ed25519", "credential")

# Extensions, not substrings. See the module docstring on `monkey.ts`.
_SECRET_SUFFIXES: Final = (".pem", ".key", ".p12")

# The `server.*` narrowing. `.pem`, `.key`, and `.p12` are omitted because
# _SECRET_SUFFIXES already rejects them under any name.
_SERVER_PREFIX: Final = "server."
_SERVER_CREDENTIAL_SUFFIXES: Final = (".crt", ".cert", ".csr", ".der", ".jks", ".keystore")

# `..` cannot survive `fetch/archive._check_member_name`, and `.` is collapsed
# by PurePosixPath before it ever reaches here. Both are still refused, because
# this function must be total over whatever a future caller hands it and the
# only safe answer to a path we cannot interpret is "secret".
_UNSAFE_COMPONENTS: Final = frozenset({"", ".", ".."})


def is_secret_path(path: PurePosixPath) -> bool:
    """Return ``True`` if ``path`` must never be analyzed, returned, or logged.

    ``path`` is repository-relative, as yielded by
    :func:`app.fetch.archive.iter_source_files` — ``src/index.ts``, not
    ``owner-repo-a1b2c3d/src/index.ts``.

    Fails closed: an absolute, empty, or traversing path is reported secret
    rather than passed through. Those shapes are rejected upstream, so reaching
    this point with one means an assumption has already broken.
    """
    parts = path.parts
    if not parts or path.is_absolute():
        return True
    if any(component in _UNSAFE_COMPONENTS for component in parts):
        return True

    # The directory rules apply to everything the file sits inside; the name
    # rules apply to every component, so a directory called `.env` or
    # `credentials` shields nothing.
    if any(component.casefold() in _EXCLUDED_DIRS for component in parts[:-1]):
        return True
    return any(_is_secret_name(component) for component in parts)


def _is_secret_name(component: str) -> bool:
    """Judge one path component against the name rules."""
    # casefold, not lower: it folds the cases lower() leaves alone, and every
    # difference between the two widens the match rather than narrowing it.
    name = component.casefold()
    if name in _SECRET_NAMES:
        return True
    if name.startswith(_SECRET_PREFIXES):
        return True
    if name.endswith(_SECRET_SUFFIXES):
        return True
    return name.startswith(_SERVER_PREFIX) and name.endswith(_SERVER_CREDENTIAL_SUFFIXES)
