"""Tests for Phase 6 threshold constant migration (AC-011).

AC-011:
    KEEP_RECENT migrated 3 → 6
    MICROCOMPACT_THRESHOLD == int(TOKEN_THRESHOLD * 0.85)
    COLLAPSE_THRESHOLD  == int(TOKEN_THRESHOLD * 0.85)

Today (RED):
    loop.KEEP_RECENT == 3   (need 6)
    loop.MICROCOMPACT_THRESHOLD does not exist (need int(TOKEN_THRESHOLD * 0.85))
    loop.COLLAPSE_THRESHOLD == int(TOKEN_THRESHOLD * 0.7) (need 0.85)
"""

from __future__ import annotations

import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))


def test_threshold_constants_match_plan() -> bool:
    """AC-011: KEEP_RECENT==6, MICROCOMPACT_THRESHOLD==int(TOKEN_THRESHOLD*0.85),
    COLLAPSE_THRESHOLD==int(TOKEN_THRESHOLD*0.85)."""
    print("\n=== TEST AC-011: Phase 6 threshold constants match the plan ===")

    from src.agent import loop

    token_threshold = loop.TOKEN_THRESHOLD
    expected_85 = int(token_threshold * 0.85)
    print(f"  TOKEN_THRESHOLD = {token_threshold}")
    print(f"  expected 0.85*T = {expected_85}")

    ok = True

    # KEEP_RECENT must be 6.
    keep_recent = getattr(loop, "KEEP_RECENT", None)
    print(f"  KEEP_RECENT = {keep_recent}")
    if keep_recent != 6:
        print(f"  FAIL: KEEP_RECENT = {keep_recent}, expected 6")
        ok = False
    else:
        print("  OK: KEEP_RECENT = 6")

    # MICROCOMPACT_THRESHOLD must exist and equal int(TOKEN_THRESHOLD * 0.85).
    micro_threshold = getattr(loop, "MICROCOMPACT_THRESHOLD", None)
    print(f"  MICROCOMPACT_THRESHOLD = {micro_threshold}")
    if micro_threshold is None:
        print(f"  FAIL: MICROCOMPACT_THRESHOLD not defined in src.agent.loop — "
              f"Phase 6 must add it.")
        ok = False
    elif micro_threshold != expected_85:
        print(f"  FAIL: MICROCOMPACT_THRESHOLD = {micro_threshold}, expected {expected_85}")
        ok = False
    else:
        print(f"  OK: MICROCOMPACT_THRESHOLD = {expected_85}")

    # COLLAPSE_THRESHOLD must equal int(TOKEN_THRESHOLD * 0.85) (was 0.7).
    collapse_threshold = getattr(loop, "COLLAPSE_THRESHOLD", None)
    print(f"  COLLAPSE_THRESHOLD = {collapse_threshold}")
    if collapse_threshold != expected_85:
        print(f"  FAIL: COLLAPSE_THRESHOLD = {collapse_threshold}, expected {expected_85} "
              f"(0.7 → 0.85 migration)")
        ok = False
    else:
        print(f"  OK: COLLAPSE_THRESHOLD = {expected_85}")

    if not ok:
        return False
    assert ok, "Phase 6 threshold constants match the plan"
    return True


def main() -> None:
    results = [
        test_threshold_constants_match_plan(),
    ]
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"  {passed}/{total} tests passed")
    print(f"{'=' * 60}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
