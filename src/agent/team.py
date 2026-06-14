"""Agent team: DAG-based multi-agent orchestration via YAML presets.

Design:
  - Agents declared in YAML with depends_on / input_from edges.
  - Kahn topological sort groups agents into layers; layer-internal agents run in parallel.
  - Upstream outputs are injected as a structured user-message prefix into downstream agents.
  - Optional aggregator produces the final team result.

References:
  - Vibe-Trading: DAG scheduling + upstream_context injection
  - hermes-agent: bounded concurrency via ThreadPoolExecutor
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.agent.subagent import SubAgentConfig, SubAgentContext, SubAgentRunner
from src.agent.tools import ToolRegistry
from src.providers.chat import ChatLLM

logger = logging.getLogger(__name__)


@dataclass
class AgentSpec:
    """Single agent declaration within a team preset."""
    id: str
    role: str = "leaf"
    goal: str = ""
    depends_on: List[str] = field(default_factory=list)
    input_from: List[str] = field(default_factory=list)
    tools: Optional[List[str]] = None
    model: Optional[str] = None
    max_iterations: int = 15

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AgentSpec":
        return cls(
            id=str(raw["id"]),
            role=str(raw.get("role", "leaf")),
            goal=str(raw.get("goal", "")),
            depends_on=list(raw.get("depends_on", []) or []),
            input_from=list(raw.get("input_from", []) or []),
            tools=list(raw["tools"]) if raw.get("tools") else None,
            model=raw.get("model"),
            max_iterations=int(raw.get("max_iterations", 15)),
        )


@dataclass
class TeamPreset:
    """Full team definition loaded from YAML."""
    name: str
    description: str = ""
    variables: Dict[str, Any] = field(default_factory=dict)
    agents: List[AgentSpec] = field(default_factory=list)
    aggregator: Optional[AgentSpec] = None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "TeamPreset":
        agents_raw = raw.get("agents", []) or []
        agg_raw = raw.get("aggregator")
        return cls(
            name=str(raw["name"]),
            description=str(raw.get("description", "")),
            variables=dict(raw.get("variables", {}) or {}),
            agents=[AgentSpec.from_dict(a) for a in agents_raw],
            aggregator=AgentSpec.from_dict(agg_raw) if agg_raw else None,
        )


class TeamRunner:
    """Executes a TeamPreset as a DAG with layer-internal parallelism."""

    MAX_CONCURRENT = 4
    UPSTREAM_CHAR_BUDGET = 4000
    SUMMARY_CHAR_LIMIT = 8000
    _global_semaphore = threading.Semaphore(8)

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

    def run(self, preset: TeamPreset, variables: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        variables = variables or {}
        missing = [k for k, v in preset.variables.items() if isinstance(v, dict) and v.get("required") and k not in variables]
        if missing:
            return {
                "status": "failed",
                "reason": f"missing required variables: {missing}",
                "content": "",
                "agents": {},
            }

        interpolated = self._interpolate_preset(preset, variables)

        try:
            layers = self._topo_sort(interpolated.agents)
        except ValueError as exc:
            return {"status": "failed", "reason": str(exc), "content": "", "agents": {}}

        results: Dict[str, Dict[str, Any]] = {}
        for layer_idx, layer in enumerate(layers):
            self._emit("team.layer_start", {"layer": layer_idx, "agents": [a.id for a in layer]})
            layer_results = self._run_layer(layer, results)
            results.update(layer_results)
            self._emit("team.layer_end", {"layer": layer_idx, "completed": list(layer_results.keys())})

        final: Dict[str, Any]
        if interpolated.aggregator:
            final = self._run_aggregator(interpolated.aggregator, results)
        else:
            final = {
                "status": "success",
                "content": self._format_team_summary(results),
                "agents": results,
            }

        final["agents"] = final.get("agents", results)
        final["content"] = (final.get("content") or "")[: self.SUMMARY_CHAR_LIMIT]
        return final

    def _interpolate_preset(self, preset: TeamPreset, variables: Dict[str, str]) -> TeamPreset:
        def _interp(text: str) -> str:
            if not text:
                return text
            out = text
            for k, v in variables.items():
                out = out.replace("{{" + k + "}}", str(v)).replace("{{ " + k + " }}", str(v))
            return out

        new_agents: List[AgentSpec] = []
        for a in preset.agents:
            new_agents.append(AgentSpec(
                id=a.id,
                role=a.role,
                goal=_interp(a.goal),
                depends_on=list(a.depends_on),
                input_from=list(a.input_from),
                tools=list(a.tools) if a.tools else None,
                model=a.model,
                max_iterations=a.max_iterations,
            ))
        new_agg: Optional[AgentSpec] = None
        if preset.aggregator:
            new_agg = AgentSpec(
                id=preset.aggregator.id,
                role=preset.aggregator.role,
                goal=_interp(preset.aggregator.goal),
                depends_on=list(preset.aggregator.depends_on),
                input_from=list(preset.aggregator.input_from),
                tools=list(preset.aggregator.tools) if preset.aggregator.tools else None,
                model=preset.aggregator.model,
                max_iterations=preset.aggregator.max_iterations,
            )
        return TeamPreset(
            name=preset.name,
            description=_interp(preset.description),
            variables=preset.variables,
            agents=new_agents,
            aggregator=new_agg,
        )

    @staticmethod
    def _topo_sort(agents: List[AgentSpec]) -> List[List[AgentSpec]]:
        """Kahn algorithm — returns layers; raises ValueError on cycle or unknown dep."""
        by_id = {a.id: a for a in agents}
        if len(by_id) != len(agents):
            seen: set[str] = set()
            for a in agents:
                if a.id in seen:
                    raise ValueError(f"duplicate agent id: {a.id}")
                seen.add(a.id)

        indeg: Dict[str, int] = {a.id: 0 for a in agents}
        adj: Dict[str, List[str]] = {a.id: [] for a in agents}
        for a in agents:
            for dep in a.depends_on:
                if dep not in by_id:
                    raise ValueError(f"agent '{a.id}' depends on unknown agent '{dep}'")
                adj[dep].append(a.id)
                indeg[a.id] += 1

        layers: List[List[AgentSpec]] = []
        remaining = dict(indeg)
        while remaining:
            ready = [aid for aid, d in remaining.items() if d == 0]
            if not ready:
                raise ValueError(f"DAG cycle detected among agents: {sorted(remaining)}")
            layer = [by_id[aid] for aid in ready]
            layers.append(layer)
            for aid in ready:
                del remaining[aid]
                for nxt in adj[aid]:
                    if nxt in remaining:
                        remaining[nxt] -= 1
        return layers

    def _run_layer(self, layer: List[AgentSpec], prev_results: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        if len(layer) == 1:
            return {layer[0].id: self._run_one(layer[0], prev_results)}

        out: Dict[str, Dict[str, Any]] = {}
        workers = min(len(layer), self.MAX_CONCURRENT)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="team") as pool:
            future_to_id = {
                pool.submit(self._run_one_safe, spec, prev_results): spec.id
                for spec in layer
            }
            for fut in concurrent.futures.as_completed(future_to_id):
                aid = future_to_id[fut]
                try:
                    out[aid] = fut.result()
                except Exception as exc:
                    out[aid] = {"status": "failed", "reason": str(exc), "content": ""}
        return out

    def _run_one_safe(self, spec: AgentSpec, prev_results: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        with self._global_semaphore:
            try:
                return self._run_one(spec, prev_results)
            except Exception as exc:
                logger.exception("agent %s failed", spec.id)
                return {"status": "failed", "reason": str(exc), "content": ""}

    def _run_one(self, spec: AgentSpec, prev_results: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        upstream_context = self._build_upstream_context(spec, prev_results)
        config = SubAgentConfig(
            role=spec.role,
            goal=spec.goal,
            context=upstream_context,
            tools_whitelist=spec.tools,
            model_name=spec.model,
            max_iterations=spec.max_iterations,
            timeout_sec=180,
        )
        runner = SubAgentRunner(
            parent_llm=self._parent_llm,
            parent_registry=self._parent_registry,
            parent_run_dir=self._parent_run_dir,
            ctx=self._ctx,
            event_cb=self._event_cb,
        )
        result = runner.run(config)
        result["agent_id"] = spec.id
        return result

    def _build_upstream_context(self, spec: AgentSpec, results: Dict[str, Dict[str, Any]]) -> str:
        if not spec.input_from:
            return ""
        chunks: List[str] = []
        for uid in spec.input_from:
            upstream = results.get(uid)
            if not upstream:
                continue
            content = (upstream.get("content") or "").strip()
            if not content:
                continue
            truncated = content[: self.UPSTREAM_CHAR_BUDGET]
            if len(content) > self.UPSTREAM_CHAR_BUDGET:
                truncated += f"\n...(truncated, {len(content) - self.UPSTREAM_CHAR_BUDGET} chars omitted)"
            chunks.append(f"[{uid}]:\n{truncated}")
        if not chunks:
            return ""
        return "<upstream-results>\n" + "\n\n".join(chunks) + "\n</upstream-results>"

    def _run_aggregator(self, spec: AgentSpec, results: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        all_ids = list(results.keys())
        spec_with_all_inputs = AgentSpec(
            id=spec.id,
            role=spec.role,
            goal=spec.goal,
            depends_on=spec.depends_on,
            input_from=spec.input_from or all_ids,
            tools=spec.tools,
            model=spec.model,
            max_iterations=spec.max_iterations,
        )
        return self._run_one(spec_with_all_inputs, results)

    @staticmethod
    def _format_team_summary(results: Dict[str, Dict[str, Any]]) -> str:
        lines: List[str] = []
        for aid, res in results.items():
            status = res.get("status", "unknown")
            content = (res.get("content") or "").strip()
            header = f"## [{aid}] ({status})"
            lines.append(f"{header}\n\n{content}\n")
        return "\n".join(lines)

    def _emit(self, event_type: str, data: Dict[str, Any]) -> None:
        if not self._event_cb:
            return
        try:
            self._event_cb(event_type, data)
        except Exception:
            pass


__all__ = ["AgentSpec", "TeamPreset", "TeamRunner"]
