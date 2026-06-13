"""Delegate tool: dispatch a single subtask to an isolated subagent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.agent.subagent import SubAgentConfig, SubAgentContext, SubAgentRunner
from src.agent.tools import BaseTool, ToolRegistry
from src.providers.chat import ChatLLM


class DelegateTool(BaseTool):
    """Dispatch a subtask to a fresh subagent with isolated context.

    The subagent runs its own ReAct loop with a filtered tool set. Its output
    is returned as a tool result to the parent agent.
    """

    name = "delegate"
    description = (
        "Dispatch a subtask to an isolated subagent. Use for: independent research, "
        "code review from a specific angle, file analysis, or any subtask that would "
        "bloat the main context. The subagent has its own context window and trace."
    )
    parameters = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "Clear, specific task description for the subagent.",
            },
            "role": {
                "type": "string",
                "enum": ["leaf", "specialist"],
                "default": "leaf",
                "description": "leaf=default; specialist=broad expertise (still cannot delegate).",
            },
            "context": {
                "type": "string",
                "description": "Background context from parent conversation (optional).",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tool whitelist for the subagent. Default: inherit parent minus delegate/spawn_team/compact.",
            },
        },
        "required": ["goal"],
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
        goal = kwargs["goal"]
        role = kwargs.get("role", "leaf")
        context = kwargs.get("context", "")
        tools = kwargs.get("tools")

        if isinstance(tools, list) and not tools:
            tools = None

        config = SubAgentConfig(
            role=role,
            goal=goal,
            context=context,
            tools_whitelist=tools,
            max_iterations=15,
            timeout_sec=180,
        )

        runner = SubAgentRunner(
            parent_llm=self._parent_llm,
            parent_registry=self._parent_registry,
            parent_run_dir=self._parent_run_dir,
            ctx=self._parent_ctx,
            event_cb=self._event_cb,
        )

        result = runner.run(config)
        summary = (result.get("content") or "").strip()[: SubAgentRunner.SUMMARY_CHAR_LIMIT]
        react_trace = result.get("react_trace", [])

        payload = {
            "status": result.get("status", "unknown"),
            "role": config.role,
            "summary": summary,
            "subagent_run_dir": result.get("subagent_run_dir", ""),
            "depth": result.get("depth", self._parent_ctx.depth + 1),
            "iterations": len(react_trace) if isinstance(react_trace, list) else 0,
        }
        if result.get("reason"):
            payload["reason"] = result["reason"]
        return json.dumps(payload, ensure_ascii=False)


__all__ = ["DelegateTool"]
