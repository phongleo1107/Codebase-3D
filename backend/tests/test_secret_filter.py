"""Secret-path filter: two golden lists and a determinism sweep.

The two lists are the specification. The blocked one is what the filter exists
for; the allowed one is the more interesting half, because every entry in it is
a plausible source file that a sloppier rule would eat — `src/secrets.ts` under
an exact-name rule turned into a prefix, `monkey.ts` and `keyboard.tsx` under a
suffix rule turned into a substring search, `server.ts` under `server.*` read
literally, `.gitignore` under a directory rule that matched by prefix.
"""

import random
from pathlib import PurePosixPath

import pytest

from app.security.secret_filter import is_secret_path

# --------------------------------------------------------------------------
# Blocked
# --------------------------------------------------------------------------

BLOCKED: list[str] = [
    # --- .env* ---
    ".env",
    ".env.local",
    ".env.production.local",
    "src/.env",
    "packages/api/.env.test",
    # A directory named `.env` shields nothing: the name rules run over every
    # component, not just the last one.
    ".env/keys.ts",
    # --- private keys and certificates ---
    "private.pem",
    "certs/fullchain.pem",
    "id_rsa",
    "id_rsa.pub",
    "id_ed25519",
    "id_ed25519.pub",
    "deploy/id_rsa",
    "private.key",
    "config/app.key",
    "cert.p12",
    "keystore/bundle.p12",
    # --- server.* narrowed to credential extensions ---
    "server.crt",
    "server.cert",
    "server.csr",
    "server.der",
    "server.jks",
    "server.keystore",
    "tls/server.crt",
    "server.pem",
    "server.key",
    # --- exact names ---
    ".npmrc",
    "packages/ui/.npmrc",
    "terraform.tfvars",
    "infra/terraform.tfvars",
    "secrets.json",
    "config/secrets.json",
    # --- credential* ---
    "credentials.json",
    "credential.ts",
    "credentials",
    "src/credentials/aws.ts",
    # --- .aws/* and .ssh/* ---
    ".aws/credentials",
    ".aws/config",
    "home/.aws/config",
    ".ssh/config",
    ".ssh/known_hosts",
    ".ssh/id_ed25519",
    # --- excluded directories ---
    "node_modules/react/index.js",
    "node_modules/.bin/tsc",
    "packages/ui/node_modules/lodash/index.js",
    ".git/config",
    ".git/HEAD",
    ".git/refs/heads/main",
    "dist/bundle.js",
    "apps/web/dist/index.js",
    "build/main.js",
    ".next/server/pages/index.js",
    ".nuxt/dist/app.js",
    # --- case: the producing machine is often case-insensitive, we are not ---
    ".ENV",
    ".Env.Local",
    "ID_RSA",
    "Private.PEM",
    "CONFIG/Secrets.JSON",
    "NODE_MODULES/react/index.js",
    "Server.CRT",
    ".SSH/config",
]


@pytest.mark.parametrize("path", BLOCKED, ids=BLOCKED)
def test_blocks_secret_path(path: str) -> None:
    assert is_secret_path(PurePosixPath(path)) is True


# --------------------------------------------------------------------------
# Allowed
# --------------------------------------------------------------------------

ALLOWED: list[str] = [
    # --- the three the brief names, and their neighbours ---
    "src/secrets.ts",
    "monkey.ts",
    "keyboard.tsx",
    "src/lib/monkey-patch.ts",
    "donkey.js",
    "turkey.mjs",
    "key.ts",
    "src/keys.ts",
    # --- server.* is TLS material only; the entry point survives ---
    "server.ts",
    "server.js",
    "src/server.ts",
    "server.json",
    "server.config.ts",
    # --- names that merely start or end like a secret ---
    "env.ts",
    "environment.ts",
    "src/config/env.ts",
    "pem.ts",
    "p12.ts",
    "envelope.ts",
    # --- directory rules match whole components, and parents only ---
    ".gitignore",
    ".gitattributes",
    ".github/workflows/ci.yml",
    "distribution/index.ts",
    "apps/dist-tools/index.ts",
    "builder.ts",
    "src/build.ts",
    "src/builds/config.ts",
    "node_modules_shim/index.js",
    # A Bazel BUILD file: a *file* named `build` is not build output.
    "BUILD",
    "src/BUILD",
    # --- ordinary repository content ---
    "index.ts",
    "src/index.ts",
    "package.json",
    "tsconfig.json",
    "README.md",
    "apps/web/src/components/Button.tsx",
]


@pytest.mark.parametrize("path", ALLOWED, ids=ALLOWED)
def test_allows_ordinary_path(path: str) -> None:
    assert is_secret_path(PurePosixPath(path)) is False


# --------------------------------------------------------------------------
# Fail-closed shapes
# --------------------------------------------------------------------------

# None of these can reach the filter today — archive._check_member_name rejects
# the archive first — so each asserts the answer given when an upstream
# assumption has already broken.
FAIL_CLOSED: list[str] = [
    "",
    ".",
    "..",
    "../etc/passwd",
    "src/../../etc/passwd",
    "/etc/passwd",
    "/",
]


@pytest.mark.parametrize("path", FAIL_CLOSED, ids=FAIL_CLOSED)
def test_fails_closed_on_uninterpretable_path(path: str) -> None:
    assert is_secret_path(PurePosixPath(path)) is True


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------

# Fragments chosen to sit on the boundaries: each rule's trigger, each rule's
# near-miss, separators, case variants, and characters that make casefold do
# something non-trivial (U+212A KELVIN SIGN folds to "k", U+00DF folds to "ss",
# U+0130 folds to two code points).
_FRAGMENTS: list[str] = [
    "",
    ".",
    "..",
    "/",
    "src",
    ".env",
    "env",
    ".ENV",
    "id_rsa",
    "id_ed25519",
    "server",
    "server.",
    ".crt",
    ".key",
    ".pem",
    ".p12",
    ".ts",
    ".tsx",
    "monkey",
    "keyboard",
    "secrets",
    "secret",
    ".json",
    "credential",
    "node_modules",
    "NODE_MODULES",
    ".git",
    ".gitignore",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".aws",
    ".ssh",
    "terraform.tfvars",
    ".npmrc",
    "K",  # noqa: RUF001 - U+212A KELVIN SIGN, deliberately not an ASCII "K"
    "ß",
    "İ",
    "\x00",
    "\\",
    " ",
]


def test_filter_is_deterministic_over_random_paths() -> None:
    """20 000 random paths, each judged twice.

    The filter is a pure function of the string, and every downstream guarantee
    rests on that: the analysis pass and `/api/source` judge the same path in
    two different requests, and a filter that could disagree with itself would
    make the second check worthless. Also asserts the answer is always a plain
    `bool` and that nothing escapes as an exception, since a raise here would
    become a 500 on an ordinary repository.
    """
    rng = random.Random(20260829)  # noqa: S311 - test input, not a secret
    for _ in range(20_000):
        components = [rng.choice(_FRAGMENTS) for _ in range(rng.randint(1, 5))]
        raw = "/".join(components)
        first = is_secret_path(PurePosixPath(raw))
        second = is_secret_path(PurePosixPath(raw))
        assert isinstance(first, bool)
        assert first == second, raw


def test_repeated_calls_agree_on_the_golden_lists() -> None:
    for path in BLOCKED + ALLOWED + FAIL_CLOSED:
        assert is_secret_path(PurePosixPath(path)) is is_secret_path(PurePosixPath(path))


def test_the_two_golden_lists_are_disjoint() -> None:
    """A path in both lists would make one of the two suites a lie."""
    assert set(BLOCKED).isdisjoint(ALLOWED)
