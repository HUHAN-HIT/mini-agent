"""Tests for system-prompt + user-envelope byte stability (Phase 1).

AC-001 (system prompt SHA-256 stable within session):
    Within a single AgentLoop.run session, the system prompt is byte-stable:
    three consecutive calls to ContextBuilder.build_messages yield SHA-256-identical
    messages[0]['content']. Sleeping 61 seconds between call 2 and call 3 must NOT
    change the hash (current_datetime no longer in system prompt).

AC-012 (user envelope always renders three blocks in order):
    User message envelope always renders <workspace-state>, <persistent-memory>,
    <recalled-memories> blocks in fixed order, even when empty, before raw user text.

These tests FAIL today because:
  - Current `_SYSTEM_PROMPT` in src/agent/context.py still embeds `{current_datetime}`
    and `{memory_summary}` as format fields, so calls at different minutes / different
    counter states produce different bytes.
  - Current `build_messages` does not produce the three-block user envelope; it only
    conditionally renders <recalled-memories> when recalls exist, and renders no
    <workspace-state> / <persistent-memory> blocks at all.
"""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path
from typing import List

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))


def _build_context():
    """Construct a ContextBuilder with a fixed ToolRegistry + WorkspaceMemory."""
    from src.agent.context import ContextBuilder
    from src.agent.memory import WorkspaceMemory
    from src.agent.tools import ToolRegistry

    registry = ToolRegistry()
    memory = WorkspaceMemory()
    return ContextBuilder(registry=registry, memory=memory, persistent_memory=None)


def test_system_prompt_sha256_stable_within_session() -> bool:
    """AC-001: SHA-256 of messages[0]['content'] must be identical across 3 calls,
    including across a 61-second sleep."""
    print("\n=== TEST AC-001: system prompt SHA-256 stable within session ===")
    ctx = _build_context()

    hashes: List[str] = []

    # Call 1
    msgs1 = ctx.build_messages(user_message="x", history=None)
    hashes.append(hashlib.sha256(msgs1[0]["content"].encode()).hexdigest())

    # Call 2
    msgs2 = ctx.build_messages(user_message="x", history=None)
    hashes.append(hashlib.sha256(msgs2[0]["content"].encode()).hexdigest())

    # Sleep 61s to prove current_datetime removal
    time.sleep(61)

    # Call 3
    msgs3 = ctx.build_messages(user_message="x", history=None)
    hashes.append(hashlib.sha256(msgs3[0]["content"].encode()).hexdigest())

    unique = set(hashes)
    print(f"  hash[0] = {hashes[0]}")
    print(f"  hash[1] = {hashes[1]}")
    print(f"  hash[2] = {hashes[2]}")
    print(f"  unique hashes: {len(unique)}")

    if len(unique) != 1:
        print(f"  FAIL: expected 1 unique hash, got {len(unique)}")
        print(f"  Reason: system prompt embeds dynamic fields "
              f"(current_datetime / memory_summary) that change between calls.")
        return False

    assert len(unique) == 1, "system prompt SHA-256 stability invariant"
    print("  OK: 3 calls produced SHA-256-identical system prompt across a 61s sleep")
    return True


def test_user_envelope_always_renders_three_blocks() -> bool:
    """AC-012: user message must contain workspace-state, persistent-memory,
    recalled-memories blocks in fixed order, each always rendered, with raw
    'hi' as the final segment."""
    print("\n=== TEST AC-012: user envelope always renders three blocks in order ===")
    ctx = _build_context()

    msgs = ctx.build_messages("hi", history=None)
    user_content = msgs[-1]["content"]

    tags = [
        "<workspace-state>",
        "</workspace-state>",
        "<persistent-memory>",
        "</persistent-memory>",
        "<recalled-memories>",
        "</recalled-memories>",
    ]

    positions = []
    ok = True
    cursor = 0
    for tag in tags:
        idx = user_content.find(tag, cursor)
        if idx < 0:
            print(f"  FAIL: tag {tag!r} not found in user message")
            ok = False
            # mark a sentinel so subsequent find() calls don't fail just because
            # an earlier tag was missing
            idx = cursor
        else:
            print(f"  OK: tag {tag!r} found at position {idx}")
        positions.append(idx)
        cursor = idx + len(tag)

    # Confirm order is non-decreasing
    if ok:
        for i in range(1, len(positions)):
            if positions[i] <= positions[i - 1]:
                print(f"  FAIL: tag {tags[i]!r} at {positions[i]} precedes "
                      f"{tags[i - 1]!r} at {positions[i - 1]}")
                ok = False

    # Confirm raw user text 'hi' appears after </recalled-memories>
    close_recalled = user_content.find("</recalled-memories>")
    if close_recalled >= 0:
        tail = user_content[close_recalled + len("</recalled-memories>"):]
        if "hi" not in tail:
            print(f"  FAIL: raw user text 'hi' not found after </recalled-memories>; "
                  f"tail = {tail!r}")
            ok = False
        else:
            print("  OK: raw user text 'hi' appears after </recalled-memories>")
    else:
        # already reported above; just don't double-fail
        pass

    if not ok:
        return False
    assert ok, "user envelope three-block contract"
    return True


def main() -> None:
    results = [
        test_system_prompt_sha256_stable_within_session(),
        test_user_envelope_always_renders_three_blocks(),
    ]
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"  {passed}/{total} tests passed")
    print(f"{'=' * 60}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
