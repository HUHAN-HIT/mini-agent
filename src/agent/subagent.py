"""Subagent runner: dispatch isolated subtasks to fresh AgentLoop instances.

Design:
  - Each subagent gets its own messages list + trace subdir, but shares run_dir
    with parent so file writes land in user-visible location.
  - Tool whitelist intersects parent registry, minus blacklist (delegate/spawn_team/compact)
    to prevent recursive spawning.
  - Depth tracked via SubAgentContext; MAX_DEPTH=2 (parent=0, child=1, grandchild=2).
  - Failures return as dict (status=failed), never raise — parent decides retry.
"""

from __future__ import annotations

import logging
import time
import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry
from src.agent.trace import TraceWriter
from src.providers.chat import ChatLLM

logger = logging.getLogger(__name__)


@dataclass
class SubAgentContext:
    """Spawn lineage context — threaded through nested delegation."""
    depth: int = 0
    parent_run_dir: Optional[Path] = None
    parent_session_id: str = ""
    spawned_at: float = field(default_factory=time.time)


@dataclass
class SubAgentConfig:
    """Configuration for a single subagent invocation."""
    role: str = "leaf"
    goal: str = ""
    context: str = ""
    tools_whitelist: Optional[List[str]] = None
    model_name: Optional[str] = None
    max_iterations: int = 15
    timeout_sec: int = 180


class SubAgentRunner:
    """Runs a subagent in an isolated context with depth-limited spawning."""

    MAX_DEPTH = 2
    TOOLS_BLACKLIST = ("delegate", "spawn_team", "compact")
    SUMMARY_CHAR_LIMIT = 8000

    def __init__(
        self,
        parent_llm: ChatLLM,
        parent_registry: ToolRegistry,
        parent_run_dir: Path,
        ctx: SubAgentContext,
        event_cb: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self._parent_llm = parent_llm
        self._parent_registry = parent_registry
        self._parent_run_dir = Path(parent_run_dir)
        self._ctx = ctx
        self._event_cb = event_cb

    def run(self, config: SubAgentConfig) -> Dict[str, Any]:
        """Execute the subagent. Returns dict with status/content/run_dir/react_trace."""
        if self._ctx.depth >= self.MAX_DEPTH:
            return {
                "status": "failed",
                "reason": f"max spawn depth {self.MAX_DEPTH} exceeded (current={self._ctx.depth})",
                "content": "",
                "react_trace": [],
            }

        if not config.goal.strip():
            return {"status": "failed", "reason": "empty goal", "content": "", "react_trace": []}

        sub_run_dir = self._parent_run_dir / "subagents" / f"{config.role}_{int(time.time() * 1000)}"
        sub_run_dir.mkdir(parents=True, exist_ok=True)

        sub_registry = self._filter_registry(config.tools_whitelist)
        sub_llm = self._resolve_llm(config.model_name)
        sub_memory = WorkspaceMemory(run_dir=str(sub_run_dir))

        child_ctx = SubAgentContext(
            depth=self._ctx.depth + 1,
            parent_run_dir=self._parent_run_dir,
            parent_session_id=self._ctx.parent_session_id,
        )

        def _child_event_cb(event_type: str, data: Dict[str, Any]) -> None:
            if not self._event_cb:
                return
            try:
                prefixed = f"subagent.{event_type}"
                enriched = {**data, "depth": child_ctx.depth, "role": config.role}
                self._event_cb(prefixed, enriched)
            except Exception:
                pass

        from src.agent.loop import AgentLoop

        loop = AgentLoop(
            registry=sub_registry,
            llm=sub_llm,
            memory=sub_memory,
            event_callback=_child_event_cb,
            max_iterations=config.max_iterations,
            persistent_memory=None,
        )

        user_message = self._build_user_message(config)
        parent_trace = TraceWriter(self._parent_run_dir)
        parent_trace.write({
            "type": "subagent_start",
            "role": config.role,
            "goal": config.goal[:500],
            "sub_run_dir": str(sub_run_dir),
            "depth": child_ctx.depth,
            "tools": list(sub_registry._tools.keys()),
        })

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="subagent") as pool:
                future = pool.submit(
                    loop.run,
                    user_message,
                    None,
                    child_ctx.parent_session_id,
                )
                try:
                    result = future.result(timeout=config.timeout_sec)
                except concurrent.futures.TimeoutError:
                    loop.cancel()
                    future.cancel()
                    result = {
                        "status": "failed",
                        "reason": f"subagent timeout after {config.timeout_sec}s",
                        "content": "",
                        "react_trace": [],
                    }
        except Exception as exc:
            logger.exception("SubAgentRunner failed")
            result = {
                "status": "failed",
                "reason": f"runner error: {exc}",
                "content": "",
                "react_trace": [],
            }
        finally:
            parent_trace.write({
                "type": "subagent_end",
                "role": config.role,
                "status": result.get("status", "unknown"),
                "sub_run_dir": str(sub_run_dir),
                "depth": child_ctx.depth,
            })
            parent_trace.close()

        result["subagent_role"] = config.role
        result["subagent_run_dir"] = str(sub_run_dir)
        result["depth"] = child_ctx.depth
        return result

    def _filter_registry(self, whitelist: Optional[List[str]]) -> ToolRegistry:
        """Build a filtered ToolRegistry for the subagent."""
        sub = ToolRegistry()
        parent_tools = self._parent_registry._tools
        for name, tool in parent_tools.items():
            if name in self.TOOLS_BLACKLIST:
                continue
            if whitelist is not None and name not in whitelist:
                continue
            sub.register(tool)
        return sub

    def _resolve_llm(self, model_name: Optional[str]) -> ChatLLM:
        """Reuse parent LLM if model matches; otherwise create new ChatLLM."""
        if model_name is None or model_name == self._parent_llm.model_name:
            return self._parent_llm
        return ChatLLM(model_name=model_name)

    @staticmethod
    def _build_user_message(config: SubAgentConfig) -> str:
        """Build the user message that primes the subagent with role/goal/context."""
        parts: List[str] = []
        parts.append(f"[Subagent Role: {config.role}]")
        parts.append(f"[Goal]\n{config.goal}")
        if config.context:
            parts.append(f"[Background Context]\n{config.context}")
        parts.append(
            "\n[Instructions] You are a focused subagent. Complete the goal above using the tools "
            "available to you. Do NOT delegate further. When done, output a concise summary."
        )
        return "\n\n".join(parts)

    @staticmethod
    def _has_own_trace(run_dir: Path) -> bool:
        """Check whether run_dir already has its own trace.jsonl (i.e., AgentLoop wrote to it)."""
        return (run_dir / "trace.jsonl").exists()


__all__ = ["SubAgentContext", "SubAgentConfig", "SubAgentRunner"]
