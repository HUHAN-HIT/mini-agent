"""ContextBuilder: builds LLM message context for the ReAct AgentLoop."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.agent.memory import WorkspaceMemory
from src.agent.skills import SkillsLoader
from src.agent.tools import ToolRegistry

if TYPE_CHECKING:
    from src.memory.persistent import PersistentMemory

logger = logging.getLogger(__name__)

# Phase 1: the system prompt is now byte-stable within a session.
# Dynamic fields (current_datetime, memory_summary, persistent-memory section)
# have been MOVED OUT into the user-message envelope (see `_build_user_envelope`).
# Only stable post-startup fields remain here: tool_count, skill_count,
# tool_descriptions, skill_descriptions.
_SYSTEM_PROMPT = """You are an intelligent agent with {skill_count} skills, {tool_count} tools, and persistent cross-session memory.

## Tools

{tool_descriptions}

## Skills (use load_skill to read full docs)

{skill_descriptions}

## Guidelines

- Load the relevant skill BEFORE starting any task. Skills contain the exact API contracts and examples.
- Ask the user if critical info is missing. Never guess.
- All file paths are relative to run_dir (auto-injected).
- Respond in the same language the user used.
- You have persistent cross-session memory (`remember` tool). When the user shares preferences or important findings, save them for future sessions.
- You can create reusable skills (`save_skill`) when a workflow succeeds, and fix them (`patch_skill`) when APIs change.

## Tool Usage Discipline (CRITICAL)

- After **3-5 tool calls**, you MUST stop and synthesize an answer from what you have gathered.
- Do NOT repeat similar search queries. If a query returned results, use them — do not re-search.
- It is better to give a partial but useful answer than to keep searching forever.
- If you already have enough information to answer, respond immediately without more tool calls.
- **NEVER** call more than 10 tool calls total for a single user request.

## Subagent & Team Delegation

When to use **delegate** (single subagent):
- The subtask is independent and would otherwise bloat your context (e.g. "read 5 files and summarize")
- You need a focused expert on a narrow problem (e.g. "audit this file for security issues")
- The subtask has clear boundaries and a single deliverable

When to use **spawn_team** (multi-agent team):
- The task naturally splits into parallel, independent perspectives (research, code review)
- You want specialist division of labor with a final synthesized report
- Available presets: call spawn_team with an unknown preset name to list available ones

Anti-patterns:
- Do NOT delegate trivial tasks (single tool call) — do them yourself
- Do NOT use spawn_team for sequential tasks — use delegate for each step
- Do NOT expect subagents to share state — pass context explicitly
"""


class ContextBuilder:
    """Builds message context for AgentLoop."""

    def __init__(self, registry: ToolRegistry, memory: WorkspaceMemory,
                 skills_loader: Optional[SkillsLoader] = None,
                 persistent_memory: Optional[PersistentMemory] = None) -> None:
        self.registry = registry
        self.memory = memory
        self.skills_loader = skills_loader or SkillsLoader()
        self._persistent_memory = persistent_memory
        # Phase 1: hash-based system-prompt cache. The rendered system prompt is
        # a deterministic function of tool/skill inputs, so we memoize on a hash
        # of those inputs. Stored on the instance (not module-global) because
        # ContextBuilder is constructed once per AgentLoop.run; see clarification Q3.
        self._cached_prompt: Optional[str] = None
        self._cached_prompt_hash: Optional[str] = None

    def build_system_prompt(self, user_message: str = "") -> str:
        current_hash = self._compute_prompt_hash()
        if self._cached_prompt is not None and self._cached_prompt_hash == current_hash:
            return self._cached_prompt

        rendered = _SYSTEM_PROMPT.format(
            tool_count=len(self.registry._tools),
            skill_count=len(self.skills_loader.skills),
            tool_descriptions=self._format_tool_descriptions(),
            skill_descriptions=self.skills_loader.get_descriptions(),
        )
        self._cached_prompt = rendered
        self._cached_prompt_hash = current_hash
        return rendered

    def _compute_prompt_hash(self) -> str:
        """SHA-256 over the stable inputs that fully determine the rendered prompt.

        Per clarification Q3: hash sorted tool names + full tool descriptions
        (name + description + parameters JSON) + sorted skill names + each
        skill's description segment. Detects any change to tool/skill surface.
        """
        parts: List[str] = []
        # Sorted tool names.
        for name in sorted(self.registry._tools.keys()):
            parts.append(name)
        # Full tool descriptions in registry insertion order — but to be
        # deterministic across runs we sort the tools alphabetically here too.
        # The hash is computed over the same content each call, so order just
        # needs to be stable.
        sorted_tools = sorted(self.registry._tools.values(), key=lambda t: t.name)
        for tool in sorted_tools:
            parts.append(tool.name)
            parts.append(tool.description)
            try:
                parts.append(json.dumps(tool.parameters, ensure_ascii=False, sort_keys=True))
            except (TypeError, ValueError):
                parts.append(str(tool.parameters))
        # Sorted skill names + descriptions.
        sorted_skills = sorted(self.skills_loader.skills, key=lambda s: s.name)
        for skill in sorted_skills:
            parts.append(skill.name)
            parts.append(skill.description)
        joined = "\n".join(parts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def _build_user_envelope(self, user_message: str) -> str:
        """Compose the always-on three-block user-message envelope.

        Per clarification Q2: workspace-state -> persistent-memory ->
        recalled-memories -> raw user text. All three blocks are ALWAYS
        rendered, even when empty (placeholder "(none)"). The block order is
        fixed so the user-message prefix is byte-stable across turns, which
        matters for providers that do prefix matching into the first user
        message (DeepSeek).
        """
        now = datetime.now()
        datetime_str = now.strftime("%A, %B %d, %Y %H:%M (local)")
        memory_summary = self.memory.to_summary()

        persistent_block = "(none)"
        if self._persistent_memory and getattr(self._persistent_memory, "snapshot", ""):
            persistent_block = self._persistent_memory.snapshot

        recalled_block = "(none)"
        if self._persistent_memory:
            try:
                recalls = self._persistent_memory.find_relevant(user_message, max_results=3)
                if recalls:
                    lines = [
                        f"- **{r.title}** ({r.memory_type}): {r.body[:500]}"
                        for r in recalls
                    ]
                    recalled_block = "\n".join(lines)
            except Exception as exc:
                logger.debug("Auto-recall failed: %s", exc)

        return (
            f"<workspace-state>\n"
            f"{datetime_str}\n"
            f"{memory_summary}\n"
            f"</workspace-state>\n\n"
            f"<persistent-memory>\n"
            f"{persistent_block}\n"
            f"</persistent-memory>\n\n"
            f"<recalled-memories>\n"
            f"{recalled_block}\n"
            f"</recalled-memories>\n\n"
            f"{user_message}"
        )

    def build_messages(self, user_message: str, history: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.build_system_prompt(user_message)},
        ]
        if history:
            messages.extend(history)

        envelope = self._build_user_envelope(user_message)
        messages.append({"role": "user", "content": envelope})
        return messages

    def _format_tool_descriptions(self) -> str:
        lines = []
        for tool in self.registry._tools.values():
            params = tool.parameters.get("properties", {})
            required = tool.parameters.get("required", [])
            param_parts = []
            for pname, pschema in params.items():
                req = " (required)" if pname in required else ""
                param_parts.append(f"    - {pname}: {pschema.get('description', pschema.get('type', ''))}{req}")
            param_text = "\n".join(param_parts) if param_parts else "    (no params)"
            lines.append(f"### {tool.name}\n{tool.description}\n  Params:\n{param_text}")
        return "\n\n".join(lines)

    @staticmethod
    def format_tool_result(tool_call_id: str, tool_name: str, result: str) -> Dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result,
        }

    @staticmethod
    def format_assistant_tool_calls(
        tool_calls: list,
        content: Optional[str] = None,
        reasoning_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        message = {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ],
        }
        if reasoning_content is not None:
            message["reasoning_content"] = reasoning_content
        return message
