"""Run every RED test file + the pre-existing regression suites sequentially;
exit non-zero if any fails.

Used by the e2e-dev-harness RED/IMPLEMENTED/VERIFIED gates to produce one
command-evidence record that aggregates the per-file exit codes.
Aggregates by OR: any non-zero child → non-zero parent. stdout/stderr
from each child are forwarded so the harness's command-evidence tail
captures which test files failed and why.

The P0 prompt-caching changes (system-prompt SHA-256 stability + 3-block
user envelope) are upstream of every caller that builds a user message, so
the regression suites (test_mock_team, test_streaming) must stay green
alongside the per-AC tests.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

TEST_FILES = [
    # Per-AC P0 prompt-caching suites.
    "test_context_stability.py",
    "test_cache_stats_parsing.py",
    "test_cli_cache_line.py",
    "test_cache_warning_threshold.py",
    "test_silent_compress_gating.py",
    "test_phase6_thresholds.py",
    "test_long_conversation_cache.py",
    # Pre-existing regression suites — P0's user-envelope change is upstream
    # of every caller that constructs a user message, so these must stay green.
    "test_streaming.py",
    "test_mock_team.py",
]


def main() -> int:
    any_failed = False
    for name in TEST_FILES:
        target = REPO_ROOT / "tests" / name
        if not target.is_file():
            print(f"[run_all] SKIP missing: tests/{name}", file=sys.stderr)
            continue
        print(f"[run_all] RUN tests/{name}", flush=True)
        ec = subprocess.call([sys.executable, str(target)], cwd=str(REPO_ROOT))
        print(f"[run_all] tests/{name} -> exit {ec}", flush=True)
        if ec != 0:
            any_failed = True
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
