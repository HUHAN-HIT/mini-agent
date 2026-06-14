"""Verification suite — wraps the per-AC test functions in unittest.TestCase.

The e2e-dev-harness VERIFIED gate replays the recorded verification command
and only allows canonical test runners (`python -m pytest`, `python -m unittest`,
`pytest`, `go test`, etc.). This file bridges the project's idiomatic
assert-style test functions (see tests/test_streaming.py convention) into a
unittest.TestCase so `python -m unittest tests.test_verification_suite` runs
every acceptance item end-to-end.

Each test_* method delegates to the existing test function in the per-AC test
files; failure (return False) becomes a unittest failure. Diagnostics printed
by the underlying function are visible in unittest's captured output.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from tests import (
    test_context_stability,
    test_cache_stats_parsing,
    test_cli_cache_line,
    test_cache_warning_threshold,
    test_silent_compress_gating,
    test_phase6_thresholds,
    test_long_conversation_cache,
    test_mock_team,
    test_streaming,
)


class PromptCachingP0Verification(unittest.TestCase):
    """One TestCase method per acceptance criterion (AC-001 … AC-012, minus
    the manual-e2e AC-003 which cannot run without API credentials)."""

    def test_ac_001_system_prompt_sha256_stable(self) -> None:
        self.assertTrue(
            test_context_stability.test_system_prompt_sha256_stable_within_session(),
            "AC-001: system prompt SHA-256 must be byte-stable within a session",
        )

    def test_ac_002_openai_cached_tokens_ratio_threshold(self) -> None:
        self.assertTrue(
            test_cache_stats_parsing.test_openai_cached_tokens_parsed_ratio_threshold(),
            "AC-002: OpenAI/DeepSeek cache ratio >= 0.80 from turn 2",
        )

    # AC-003 is manual-e2e (requires live OpenRouter/Anthropic credentials).
    # See docs/agent-runs/20260614T030102Z-prompt-caching-p0/manual-e2e/anthropic-cache-read.md

    def test_ac_004_cli_cache_line_format(self) -> None:
        self.assertTrue(
            test_cli_cache_line.test_cache_ratio_line_format(),
            "AC-004: CLI emits [cache: NK/NK cached, NN%] on available turns",
        )

    def test_ac_005_cache_warning_threshold(self) -> None:
        self.assertTrue(
            test_cache_warning_threshold.test_warning_fires_on_three_low_ratios(),
            "AC-005: cache_warning fires on 3 consecutive low ratios",
        )

    def test_ac_006_no_silent_compress_below_threshold(self) -> None:
        self.assertTrue(
            test_silent_compress_gating.test_no_silent_compress_below_threshold(),
            "AC-006: no silent_compress below 0.85 * TOKEN_THRESHOLD",
        )

    def test_ac_007_silent_compress_fires_above_threshold(self) -> None:
        self.assertTrue(
            test_silent_compress_gating.test_silent_compress_fires_above_threshold(),
            "AC-007: silent_compress fires above threshold with valid layer",
        )

    def test_ac_008_long_conversation_ratio(self) -> None:
        self.assertTrue(
            test_long_conversation_cache.test_20_turn_maintains_ratio(),
            "AC-008: 20-turn conversation maintains cache ratio >= 0.70",
        )

    def test_ac_009_stream_fallback_parses_cache_stats(self) -> None:
        self.assertTrue(
            test_cache_stats_parsing.test_stream_fallback_parses_cache_stats(),
            "AC-009: stream fallback path populates cache_stats",
        )

    def test_ac_010_missing_usage_unavailable(self) -> None:
        self.assertTrue(
            test_cache_stats_parsing.test_missing_usage_returns_unavailable_cache_stats(),
            "AC-010: missing usage block → cache_stats.is_available == False",
        )

    def test_ac_011_phase6_threshold_constants(self) -> None:
        self.assertTrue(
            test_phase6_thresholds.test_threshold_constants_match_plan(),
            "AC-011: KEEP_RECENT=6, MICROCOMPACT/COLLAPSE_THRESHOLD=int(T*0.85)",
        )

    def test_ac_012_user_envelope_three_blocks(self) -> None:
        self.assertTrue(
            test_context_stability.test_user_envelope_always_renders_three_blocks(),
            "AC-012: user envelope always renders 3 blocks in fixed order",
        )


class PromptCachingP0RegressionGuard(unittest.TestCase):
    """Pre-existing suites that must stay green because the P0 changes
    (system-prompt SHA-256 stability + 3-block user envelope) are upstream
    of every caller that builds a user message. Catches envelope regressions
    that the per-AC tests can't see because they exercise ContextBuilder
    directly, not via SubAgentRunner / TeamRunner / streaming dispatch.
    """

    def test_regression_team_dag_still_merges(self) -> None:
        self.assertTrue(
            test_mock_team.test_team_dag(),
            "REGRESSION: spawn_team DAG must still reach [MERGE] after the user "
            "envelope is wrapped around the user message",
        )

    def test_regression_team_upstream_injection(self) -> None:
        self.assertTrue(
            test_mock_team.test_team_upstream_injection(),
            "REGRESSION: upstream_context injection must survive the user envelope",
        )

    def test_regression_streaming_dispatch(self) -> None:
        self.assertTrue(
            test_streaming.test_stream_chat_routes_reasoning_and_text(),
            "REGRESSION: stream_chat reasoning/text dispatch must survive the "
            "user envelope",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
