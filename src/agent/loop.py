"""AgentLoop: ReAct core loop.

Five-layer context management:
  Layer 1 (microcompact)     — silently prunes old tool results each iteration
  Layer 2 (context_collapse) — folds long text blocks without LLM call (zero cost)
  Layer 3 (auto_compact)     — LLM structured summary with token-budget tail protection
  Layer 4 (compact tool)     — model explicitly calls the compact tool to trigger L3
  Layer 5 (iterative update) — Nth compression updates previous summary instead of starting fresh

Tool execution:
  - Read/write batching: consecutive readonly tools run in parallel via threads
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import time as _time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.agent.context import ContextBuilder
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry
from src.agent.trace import TraceWriter
from src.core.state import RunStateStore
from src.providers.chat import ChatLLM

RUNS_DIR = Path(__file__).resolve().parents[2] / "runs"
TOKEN_THRESHOLD = int(os.getenv("TOKEN_THRESHOLD", "40000"))
KEEP_RECENT = 3
TOOL_RESULT_LIMIT = 10_000

COLLAPSE_THRESHOLD = int(TOKEN_THRESHOLD * 0.7)
COLLAPSE_PRESERVE_RECENT = 6
COLLAPSE_TEXT_MIN = 2400
COLLAPSE_HEAD = 900
COLLAPSE_TAIL = 500

TAIL_TOKEN_BUDGET = 20_000

logger = logging.getLogger(__name__)


def estimate_tokens(messages: list) -> int:
    return len(json.dumps(messages, default=str, ensure_ascii=False)) // 4


def _microcompact(messages: list) -> None:
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    if len(tool_msgs) <= KEEP_RECENT:
        return
    for msg in tool_msgs[:-KEEP_RECENT]:
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > 100:
            msg["content"] = "[cleared]"


def _context_collapse(messages: list) -> None:
    if len(messages) <= COLLAPSE_PRESERVE_RECENT + 1:
        return
    for msg in messages[1:-COLLAPSE_PRESERVE_RECENT]:
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= COLLAPSE_TEXT_MIN:
            continue
        if content == "[cleared]":
            continue
        head = content[:COLLAPSE_HEAD]
        tail = content[-COLLAPSE_TAIL:]
        trimmed = len(content) - COLLAPSE_HEAD - COLLAPSE_TAIL
        msg["content"] = f"{head}\n\n...[{trimmed} chars collapsed]...\n\n{tail}"


def _fix_tool_pairs(messages: list) -> None:
    call_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                tc_id = tc.get("id", "")
                if tc_id:
                    call_ids.add(tc_id)

    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "tool" and msg.get("tool_call_id") not in call_ids:
            messages.pop(i)
        else:
            i += 1

    result_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "tool":
            tcid = msg.get("tool_call_id", "")
            if tcid:
                result_ids.add(tcid)

    inserts: list[tuple[int, dict]] = []
    for idx, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []):
            tc_id = tc.get("id", "")
            if tc_id and tc_id not in result_ids:
                stub = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": tc.get("function", {}).get("name", "unknown"),
                    "content": "[Result from earlier context — see summary above]",
                }
                inserts.append((idx + 1, stub))
                result_ids.add(tc_id)

    for pos, stub in reversed(inserts):
        messages.insert(pos, stub)


_STRUCTURED_SUMMARY_PROMPT = """\
Summarize this conversation for handoff to a fresh context window.
This summary is the ONLY context available — omitted information is lost.

Use EXACTLY this structure:

## Goal
What the user is trying to accomplish.

## Constraints & Preferences
User-stated requirements and preferences.

## Progress
### Done
- Completed steps with key results and specific numbers.
### In Progress
- Current work when compression triggered.

## Key Decisions
Choices made and rationale.

## Resolved Questions
Questions already answered — do NOT re-answer these.

## Pending User Asks
Unfinished requests still needing action.

## Relevant Files
File paths, run_dir, artifact locations.

## Remaining Work
What still needs to be done (background reference, NOT active instructions).

## Critical Context
Specific numbers, parameters, error messages, configuration values.

## Tools & Patterns
Which tools worked, what failed, effective approaches.

IMPORTANT: This is a handoff — background reference, NOT active instructions.
Preserve ALL specific numbers, file paths, and parameter values.
{focus_section}
Conversation to summarize:
"""

_FOCUS_SECTION = """
FOCUS TOPIC: {topic}
Allocate 60-70% of the summary budget to content related to this topic.
Aggressively compress unrelated content to make room.
"""

_ITERATIVE_UPDATE_PROMPT = """\
Update the existing summary with new conversation turns.

PREVIOUS SUMMARY:
{previous_summary}

NEW TURNS TO INCORPORATE:
{new_turns}

Rules:
- PRESERVE all existing information from the previous summary.
- ADD new progress, decisions, and findings.
- Move "In Progress" items to "Done" when completed.
- Move answered questions to "Resolved Questions".
- Keep the same section structure.
- Do NOT drop any critical context from the previous summary.
{focus_section}"""


def _is_tool_success(result: str) -> bool:
    try:
        data = json.loads(result)
        if isinstance(data, dict) and data.get("status") == "error":
            return False
    except (json.JSONDecodeError, TypeError):
        pass
    return True


def _normalize_tool_run_dir(args: dict[str, Any], memory_run_dir: str | None) -> dict[str, Any]:
    normalized = dict(args)
    if not memory_run_dir:
        return normalized

    if "run_dir" not in normalized:
        normalized["run_dir"] = memory_run_dir
        return normalized

    run_dir_value = str(normalized["run_dir"]).strip()
    if not run_dir_value:
        normalized["run_dir"] = memory_run_dir
        return normalized

    candidate = Path(run_dir_value)
    if not candidate.is_absolute():
        normalized["run_dir"] = str((Path(memory_run_dir) / candidate).resolve())
    return normalized


class AgentLoop:
    """ReAct Agent core loop."""

    def __init__(
        self,
        registry: ToolRegistry,
        llm: ChatLLM,
        memory: Optional[WorkspaceMemory] = None,
        event_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        max_iterations: int = 50,
        persistent_memory: Optional[Any] = None,
    ) -> None:
        self.registry = registry
        self.llm = llm
        self.memory = memory or WorkspaceMemory()
        self._event_callback = event_callback
        self.max_iterations = max_iterations
        self._called_ok: set[str] = set()
        self._cancelled: bool = False
        self._previous_summary: str = ""
        self._persistent_memory = persistent_memory

    def cancel(self) -> None:
        self._cancelled = True

    def run(self, user_message: str, history: Optional[List[Dict[str, Any]]] = None, session_id: str = "") -> Dict[str, Any]:
        self._cancelled = False
        self._called_ok = set()
        self._previous_summary = ""

        state_store = RunStateStore()
        RUNS_DIR.mkdir(parents=True, exist_ok=True)

        if self.memory.run_dir and Path(self.memory.run_dir).exists():
            run_dir = Path(self.memory.run_dir)
        else:
            run_dir = state_store.create_run_dir(RUNS_DIR)
            self.memory.run_dir = str(run_dir)

        state_store.save_request(run_dir, user_message, {"session_id": session_id})

        context = ContextBuilder(self.registry, self.memory,
                                  persistent_memory=self._persistent_memory)
        messages = context.build_messages(user_message, history)
        react_trace: List[Dict[str, Any]] = []

        trace = TraceWriter(run_dir)
        trace.write({"type": "start", "prompt": user_message[:500]})

        iteration = 0
        final_content = ""

        try:
            while iteration < self.max_iterations:
                if self._cancelled:
                    trace.write({"type": "cancelled", "iter": iteration})
                    logger.info("AgentLoop cancelled by user")
                    break

                iteration += 1

                _microcompact(messages)

                tokens = estimate_tokens(messages)
                if tokens > COLLAPSE_THRESHOLD:
                    _context_collapse(messages)
                    tokens = estimate_tokens(messages)

                if tokens > TOKEN_THRESHOLD:
                    logger.info(f"Auto compact triggered: {tokens} tokens > {TOKEN_THRESHOLD}")
                    self._auto_compact(messages, run_dir, trace)

                logger.info(f"ReAct iteration {iteration}/{self.max_iterations}")

                thinking_chunks: List[str] = []

                def _on_text_chunk(delta: str) -> None:
                    thinking_chunks.append(delta)
                    self._emit("text_delta", {"delta": delta, "iter": iteration})

                response = self.llm.stream_chat(
                    messages,
                    tools=self.registry.get_definitions(),
                    on_text_chunk=_on_text_chunk,
                )

                thinking_text = "".join(thinking_chunks)
                if thinking_text:
                    trace.write({"type": "thinking", "iter": iteration, "content": thinking_text[:2000]})
                    self._emit("thinking_done", {"iter": iteration, "content": thinking_text[:500]})

                if not response.has_tool_calls:
                    final_content = response.content or ""
                    trace.write({"type": "answer", "iter": iteration, "content": final_content[:2000]})
                    react_trace.append({"type": "answer", "content": final_content[:500]})
                    break

                messages.append(
                    context.format_assistant_tool_calls(
                        response.tool_calls,
                        content=response.content,
                        reasoning_content=response.reasoning_content or thinking_text or None,
                    )
                )

                compact_requested, focus_topic = self._process_tool_calls(
                    response.tool_calls, context, messages, trace, react_trace, iteration,
                )

                if compact_requested:
                    logger.info("Manual compact triggered by model")
                    self._auto_compact(messages, run_dir, trace, focus_topic=focus_topic)

        except Exception as exc:
            logger.exception(f"AgentLoop error: {exc}")
            trace.write({"type": "end", "status": "error", "reason": str(exc), "iterations": iteration})
            trace.close()
            state_store.mark_failure(run_dir, str(exc))
            return {
                "status": "failed",
                "reason": str(exc),
                "run_dir": str(run_dir),
                "run_id": run_dir.name,
                "content": "",
                "react_trace": react_trace,
            }

        if self._cancelled:
            state_store.mark_failure(run_dir, "cancelled by user")
            final_status = "cancelled"
        elif final_content:
            state_store.mark_success(run_dir)
            final_status = "success"
        else:
            state_store.mark_failure(run_dir, "loop did not produce output")
            final_status = "failed"

        trace.write({"type": "end", "status": final_status, "iterations": iteration})
        trace.close()

        return {
            "status": final_status,
            "run_dir": str(run_dir),
            "run_id": run_dir.name,
            "content": final_content,
            "react_trace": react_trace,
        }

    def _process_tool_calls(
        self,
        tool_calls: list,
        context: ContextBuilder,
        messages: list,
        trace: TraceWriter,
        react_trace: list,
        iteration: int,
    ) -> tuple[bool, str]:
        compact_requested = False
        focus_topic = ""
        to_execute = []

        for tc in tool_calls:
            if tc.name == "compact":
                compact_requested = True
                focus_topic = tc.arguments.get("focus_topic", "")
                messages.append(context.format_tool_result(tc.id, "compact", '{"status":"ok","message":"Compressing..."}'))
                trace.write({"type": "compact_requested", "iter": iteration})
                continue

            tool_def = self.registry.get(tc.name)
            is_repeatable = tool_def.repeatable if tool_def else False
            if tc.name in self._called_ok and not is_repeatable:
                logger.warning(f"Blocked duplicate call: {tc.name} (already succeeded)")
                skip_msg = json.dumps({"skipped": True, "reason": f"{tc.name} already completed successfully. Use the previous result."})
                messages.append(context.format_tool_result(tc.id, tc.name, skip_msg))
                trace.write({"type": "tool_skipped", "iter": iteration, "tool": tc.name})
                react_trace.append({"type": "tool_skipped", "tool": tc.name})
                continue

            to_execute.append(tc)

        if not to_execute:
            return compact_requested, focus_topic

        if len(to_execute) == 1:
            self._execute_single(to_execute[0], context, messages, trace, react_trace, iteration)
        else:
            self._batch_execute(to_execute, context, messages, trace, react_trace, iteration)

        return compact_requested, focus_topic

    def _batch_execute(self, tool_calls: list, context: ContextBuilder,
                       messages: list, trace: TraceWriter, react_trace: list, iteration: int) -> None:
        batches: list[tuple[str, list]] = []
        current_ro: list = []

        for tc in tool_calls:
            tool_def = self.registry.get(tc.name)
            if tool_def and tool_def.is_readonly:
                current_ro.append(tc)
            else:
                if current_ro:
                    batches.append(("parallel", current_ro))
                    current_ro = []
                batches.append(("serial", [tc]))
        if current_ro:
            batches.append(("parallel", current_ro))

        for mode, batch in batches:
            if mode == "parallel" and len(batch) > 1:
                self._execute_parallel(batch, context, messages, trace, react_trace, iteration)
            else:
                for tc in batch:
                    self._execute_single(tc, context, messages, trace, react_trace, iteration)

    def _execute_parallel(self, tool_calls: list, context: ContextBuilder,
                          messages: list, trace: TraceWriter, react_trace: list, iteration: int) -> None:
        runnable: list[tuple] = []
        for tc in tool_calls:
            args = _normalize_tool_run_dir(tc.arguments, self.memory.run_dir)
            self._emit("tool_call", {"tool": tc.name, "arguments": {k: str(v)[:200] for k, v in args.items()}, "iter": iteration})
            trace.write({"type": "tool_call", "iter": iteration, "tool": tc.name, "args": {k: str(v)[:200] for k, v in args.items()}})
            runnable.append((tc, args))

        def _run(tc_args: tuple) -> tuple:
            tc, args = tc_args
            t0 = _time.perf_counter()
            result = self.registry.execute(tc.name, args)
            elapsed_ms = int((_time.perf_counter() - t0) * 1000)
            return tc, result, elapsed_ms

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(runnable), 8)) as pool:
            futures = [pool.submit(_run, item) for item in runnable]
            results = []
            for i, f in enumerate(futures):
                try:
                    results.append(f.result())
                except Exception as exc:
                    tc = runnable[i][0]
                    results.append((tc, json.dumps({"status": "error", "error": str(exc)}), 0))

        for tc, result, elapsed_ms in results:
            self._finalize_tool_result(tc, result, elapsed_ms, context, messages, trace, react_trace, iteration)

    def _execute_single(self, tc: Any, context: ContextBuilder,
                        messages: list, trace: TraceWriter, react_trace: list, iteration: int) -> None:
        args = _normalize_tool_run_dir(tc.arguments, self.memory.run_dir)

        self._emit("tool_call", {"tool": tc.name, "arguments": {k: str(v)[:200] for k, v in args.items()}, "iter": iteration})
        trace.write({"type": "tool_call", "iter": iteration, "tool": tc.name, "args": {k: str(v)[:200] for k, v in args.items()}})
        logger.info(f"Tool call: {tc.name}({list(args.keys())})")

        t0 = _time.perf_counter()
        result = self.registry.execute(tc.name, args)
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)

        self._finalize_tool_result(tc, result, elapsed_ms, context, messages, trace, react_trace, iteration)

    def _finalize_tool_result(self, tc: Any, result: str, elapsed_ms: int,
                              context: ContextBuilder, messages: list,
                              trace: TraceWriter, react_trace: list, iteration: int) -> None:
        self._update_memory(tc.name)

        success = _is_tool_success(result)
        if success:
            self._called_ok.add(tc.name)

        status = "ok" if success else "error"
        truncated = result[:TOOL_RESULT_LIMIT]
        messages.append(context.format_tool_result(tc.id, tc.name, truncated))

        trace.write({"type": "tool_result", "iter": iteration, "tool": tc.name, "status": status, "elapsed_ms": elapsed_ms, "preview": result[:200]})
        react_trace.append({"type": "tool_call", "tool": tc.name, "result_preview": result[:200]})
        self._emit("tool_result", {"tool": tc.name, "status": status, "elapsed_ms": elapsed_ms, "preview": result[:200]})

    def _auto_compact(self, messages: list, run_dir: Path, trace: TraceWriter,
                      focus_topic: str = "") -> None:
        transcript_path = run_dir / f"transcript_{int(_time.time())}.jsonl"
        with open(transcript_path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")

        system_msg = messages[0]
        body = messages[1:]

        accumulated = 0
        cut_idx = len(body)
        for i in range(len(body) - 1, -1, -1):
            content = body[i].get("content", "")
            msg_tokens = (len(str(content)) // 4) + 10
            if accumulated + msg_tokens > TAIL_TOKEN_BUDGET:
                cut_idx = i + 1
                break
            accumulated += msg_tokens
            cut_idx = i

        while 0 < cut_idx < len(body) and body[cut_idx].get("role") == "tool":
            cut_idx += 1

        head = body[:cut_idx]
        tail = body[cut_idx:]

        if not head:
            if len(body) > 2:
                cut_idx = max(1, len(body) // 2)
                head = body[:cut_idx]
                tail = body[cut_idx:]
            else:
                logger.warning("Auto compact: nothing to compress (body too small)")
                return

        focus_section = _FOCUS_SECTION.format(topic=focus_topic) if focus_topic else ""

        conv_text = json.dumps(head, default=str, ensure_ascii=False)[:80000]

        if self._previous_summary:
            prompt = _ITERATIVE_UPDATE_PROMPT.format(
                previous_summary=self._previous_summary,
                new_turns=conv_text,
                focus_section=focus_section,
            )
        else:
            prompt = _STRUCTURED_SUMMARY_PROMPT.format(focus_section=focus_section) + conv_text

        summary_resp = self.llm.chat([{"role": "user", "content": prompt}])
        summary = summary_resp.content or ""
        self._previous_summary = summary

        tokens_before = estimate_tokens(messages)
        trace.write({"type": "compact", "tokens_before": tokens_before, "summary": summary[:500],
                      "focus_topic": focus_topic or "(none)"})
        self._emit("compact", {"tokens_before": tokens_before, "summary": summary[:200]})

        state_summary = self.memory.to_summary()
        compressed = f"[Conversation compressed — handoff summary. Transcript: {transcript_path}]\n\n{summary}"
        if state_summary and state_summary != "(empty state)":
            compressed += f"\n\nCurrent agent state:\n{state_summary}"

        messages.clear()
        messages.append(system_msg)
        messages.append({"role": "user", "content": compressed})
        messages.append({"role": "assistant", "content": "Understood. Continuing from the summary."})
        messages.extend(tail)

        _fix_tool_pairs(messages)

    def _emit(self, event_type: str, data: Dict[str, Any]) -> None:
        if self._event_callback:
            try:
                self._event_callback(event_type, data)
            except Exception:
                pass

    def _update_memory(self, tool_name: str) -> None:
        self.memory.increment(tool_name)
