"""Team tool: dispatch a YAML-defined multi-agent team."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.agent.presets import list_presets, load_preset
from src.agent.subagent import SubAgentContext
from src.agent.team import TeamPreset, TeamRunner
from src.agent.tools import BaseTool, ToolRegistry
from src.providers.chat import ChatLLM

logger = logging.getLogger(__name__)


class TeamTool(BaseTool):
    """Dispatch a multi-agent team defined by a YAML preset.

    The team runs as a DAG: agents in the same layer execute in parallel,
    upstream outputs flow into downstream agents via structured context.
    """

    name = "spawn_team"
    description = (
        "Dispatch a multi-agent team to handle a complex task in parallel. "
        "Use when: research with multiple angles, code review from different perspectives, "
        "or any task benefiting from specialist division of labor."
    )
    parameters = {
        "type": "object",
        "properties": {
            "preset": {
                "type": "string",
                "description": "Preset name (e.g. 'research_team', 'code_review_team').",
            },
            "variables": {
                "type": "object",
                "description": "Variables to interpolate into the preset (e.g. {\"topic\": \"...\"}).",
                "additionalProperties": True,
            },
        },
        "required": ["preset"],
    }
    repeatable = True
    is_readonly = False

    @classmethod
    def check_available(cls) -> bool:
        return False

    def __init__(
        self,
        parent_llm: ChatLLM,
        parent_registry: ToolRegistry,
        parent_run_dir: Path,
        parent_ctx: SubAgentContext,
        event_cb: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self._parent_llm = parent_llm
        self._parent_registry = parent_registry
        self._parent_run_dir = Path(parent_run_dir)
        self._parent_ctx = parent_ctx
        self._event_cb = event_cb

    def execute(self, **kwargs: Any) -> str:
        preset_name = kwargs["preset"]
        variables = kwargs.get("variables") or {}

        if not isinstance(variables, dict):
            return json.dumps({
                "status": "error",
                "error": "variables must be an object",
            }, ensure_ascii=False)

        try:
            preset = load_preset(preset_name)
        except FileNotFoundError as exc:
            available = list(list_presets().keys())
            return json.dumps({
                "status": "error",
                "error": str(exc),
                "available_presets": available,
            }, ensure_ascii=False)
        except ValueError as exc:
            return json.dumps({"status": "error", "error": f"invalid preset: {exc}"}, ensure_ascii=False)

        runner = TeamRunner(
            parent_llm=self._parent_llm,
            parent_registry=self._parent_registry,
            parent_run_dir=self._parent_run_dir,
            ctx=self._parent_ctx,
            event_cb=self._event_cb,
        )

        result = runner.run(preset, variables)

        agents_summary = []
        for aid, res in (result.get("agents") or {}).items():
            agents_summary.append({
                "id": aid,
                "status": res.get("status", "unknown"),
                "run_dir": res.get("subagent_run_dir", ""),
                "iterations": len(res.get("react_trace", [])) if isinstance(res.get("react_trace"), list) else 0,
            })

        payload = {
            "status": result.get("status", "unknown"),
            "preset": preset_name,
            "summary": (result.get("content") or "")[: TeamRunner.SUMMARY_CHAR_LIMIT],
            "agents": agents_summary,
        }
        if result.get("reason"):
            payload["reason"] = result["reason"]
        return json.dumps(payload, ensure_ascii=False)


__all__ = ["TeamTool"]
