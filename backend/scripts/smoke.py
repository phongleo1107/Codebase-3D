"""End-to-end smoke check against a real public repository.

**This is deliberately not a pytest test.** `tests/conftest.py` blocks the
socket entry points for the whole session, and its docstring makes that a
guarantee rather than a default: a per-test ``monkeypatch`` undo restores the
*block*, not the real socket module. Adding a `network` marker that lifts it
would put an escape hatch in the one place the project promises there is none,
and the hatch would be available to every future test rather than to this one.
A separate entry point keeps the suite hermetic by construction.

What it is for: every other exercise of `analyze_repository` swaps httpx's
transport with respx and feeds it a tarball built in process. That leaves real
codeload responses, real redirect shapes, real chunk sizes, and real timing
unverified — the gap docs/CURRENT_STATE.md records as "nothing has been run
against GitHub". This runs the actual path.

Usage::

    uv run python scripts/smoke.py                       # a default small repo
    uv run python scripts/smoke.py https://github.com/o/r

Exits non-zero on any `AppError`, so it is usable as a check. It prints a
summary only: never a specifier, never a line of source, never a token — the
same rule the logger follows, because this output is the sort of thing that
gets pasted into an issue.
"""

import sys
import time
from collections import Counter

from app.analysis.pipeline import RepositoryAnalysis, analyze_repository
from app.errors import AppError
from app.security.url_validation import parse_github_url

# Small, stable, and genuinely TS/JS. Chosen so the first real run is a few
# hundred KiB rather than a stress test; pass a URL to point it elsewhere.
DEFAULT_URL = "https://github.com/sindresorhus/p-limit"


def _summarize(analysis: RepositoryAnalysis, elapsed: float) -> None:
    """Counts and extensions only. No specifiers, no paths, no content."""
    by_language: Counter[str] = Counter(f.language for f in analysis.files)
    imports = sum(len(f.imports) for f in analysis.files)

    print(f"  repository   {analysis.owner}/{analysis.name} @ {analysis.ref}")
    print(f"  commit_sha   {analysis.commit_sha}")
    print(f"  files        {len(analysis.files)} ({dict(sorted(by_language.items()))})")
    print(f"  imports      {imports}")
    print(f"  loc          {sum(f.loc for f in analysis.files)}")
    print(f"  bytes        {sum(f.size_bytes for f in analysis.files)}")
    print(f"  skipped      {analysis.skipped_files} {dict(sorted(analysis.skipped.items()))}")
    print(f"  truncated    {analysis.truncated}")
    print(f"  elapsed      {elapsed:.2f}s")


def main(argv: list[str]) -> int:
    url = argv[1] if len(argv) > 1 else DEFAULT_URL
    print(f"analyzing {url}")

    # `parse_github_url` first, exactly as a route would: `analyze_repository`
    # documents that it does not re-validate and is not a second guard.
    repo = parse_github_url(url)

    started = time.monotonic()
    try:
        analysis = analyze_repository(repo)
    except AppError as exc:
        # The static message, which is all a client would ever see.
        print(f"FAILED  {type(exc).__name__}: {exc.message}", file=sys.stderr)
        return 1
    elapsed = time.monotonic() - started

    _summarize(analysis, elapsed)

    # The invariants worth asserting on real data, as opposed to on a fixture
    # whose root name we chose ourselves.
    if analysis.commit_sha is None:
        print("FAILED  no commit SHA harvested from the archive root", file=sys.stderr)
        return 1
    if not analysis.files:
        print("FAILED  no files", file=sys.stderr)
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
