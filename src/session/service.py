"""Session lifecycle orchestration for message flow, attempt creation, and execution scheduling."""

from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

_AGENT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")

from src.session.events import EventBus
from src.session.models import Attempt, AttemptStatus, Message, Session
from src.session.search import get_shared_index
from src.session.store import SessionStore


class SessionService:
    """Session lifecycle service."""

    def __init__(self, store: SessionStore, event_bus: EventBus, runs_dir: Path) -> None:
        self.store = store
        self.event_bus = event_bus
        self.runs_dir = runs_dir
        self._active_loops: Dict[str, Any] = {}
        self._search_index = get_shared_index()

    def create_session(self, title: str = "", config: Optional[Dict[str, Any]] = None) -> Session:
        session = Session(title=title, config=config or {})
        self.store.create_session(session)
        self._search_index.index_session(session.session_id, title)
        self.event_bus.emit(session.session_id, "session.created", {"session_id": session.session_id, "title": title})
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        return self.store.get_session(session_id)

    def list_sessions(self, limit: int = 50) -> list[Session]:
        return self.store.list_sessions(limit)

    def delete_session(self, session_id: str) -> bool:
        self.event_bus.clear(session_id)
        return self.store.delete_session(session_id)

    async def send_message(self, session_id: str, content: str, role: str = "user") -> Dict[str, Any]:
        session = self.store.get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        message = Message(session_id=session_id, role=role, content=content)
        self.store.append_message(message)
        self._search_index.index_message(session_id, role, content)
        self.event_bus.emit(session_id, "message.received", {"message_id": message.message_id, "role": role, "content": content})

        if role != "user":
            return {"message_id": message.message_id}

        attempt = Attempt(session_id=session_id, parent_attempt_id=session.last_attempt_id, prompt=content)
        self.store.create_attempt(attempt)
        session.last_attempt_id = attempt.attempt_id
        session.updated_at = datetime.now().isoformat()
        self.store.update_session(session)
        self.event_bus.emit(session_id, "attempt.created", {"attempt_id": attempt.attempt_id, "prompt": content})

        asyncio.create_task(self._run_attempt(session, attempt))
        return {"message_id": message.message_id, "attempt_id": attempt.attempt_id}

    def get_messages(self, session_id: str, limit: int = 100) -> list[Message]:
        return self.store.get_messages(session_id, limit)

    def get_attempts(self, session_id: str) -> list[Attempt]:
        return self.store.list_attempts(session_id)

    def cancel_current(self, session_id: str) -> bool:
        loop = self._active_loops.get(session_id)
        if loop is None:
            return False
        loop.cancel()
        return True

    async def _run_attempt(self, session: Session, attempt: Attempt) -> None:
        attempt.mark_running()
        self.store.update_attempt(attempt)
        self.event_bus.emit(session.session_id, "attempt.started", {"attempt_id": attempt.attempt_id})

        try:
            messages = self.store.get_messages(session.session_id)
            result = await self._run_with_agent(attempt, messages=messages)
            if result.get("status") == "success":
                attempt.mark_completed(summary=result.get("content", ""))
            else:
                attempt.mark_failed(error=result.get("reason", "unknown"))
            attempt.run_dir = result.get("run_dir")
            self.store.update_attempt(attempt)

            reply = Message(session_id=session.session_id, role="assistant",
                           content=attempt.summary or "Execution completed.", linked_attempt_id=attempt.attempt_id)
            self.store.append_message(reply)
            self._search_index.index_message(session.session_id, "assistant", reply.content)

            if attempt.status == AttemptStatus.COMPLETED:
                self.event_bus.emit(
                    session.session_id,
                    "attempt.completed",
                    {
                        "attempt_id": attempt.attempt_id,
                        "status": attempt.status.value,
                        "content": reply.content,
                        "run_dir": attempt.run_dir,
                    },
                )
            else:
                self.event_bus.emit(
                    session.session_id,
                    "attempt.failed",
                    {
                        "attempt_id": attempt.attempt_id,
                        "status": attempt.status.value,
                        "error": attempt.error or "",
                        "run_dir": attempt.run_dir,
                    },
                )

        except Exception as exc:
            attempt.mark_failed(error=str(exc))
            self.store.update_attempt(attempt)
            self.event_bus.emit(
                session.session_id,
                "attempt.failed",
                {
                    "attempt_id": attempt.attempt_id,
                    "status": attempt.status.value,
                    "error": str(exc),
                    "run_dir": attempt.run_dir,
                },
            )

    async def _run_with_agent(self, attempt: Attempt, messages: list = None) -> Dict[str, Any]:
        from src.tools import build_registry
        from src.providers.chat import ChatLLM
        from src.agent.loop import AgentLoop
        from src.agent.subagent import SubAgentContext
        from src.tools.delegate_tool import DelegateTool
        from src.tools.team_tool import TeamTool
        from src.memory.persistent import PersistentMemory

        llm = ChatLLM()
        pm = PersistentMemory()
        session_id = attempt.session_id
        attempt_id = attempt.attempt_id

        def event_callback(event_type: str, data: Dict[str, Any]) -> None:
            data["attempt_id"] = attempt_id
            self.event_bus.emit(session_id, event_type, data)

        registry = build_registry(persistent_memory=pm)
        run_dir = self.runs_dir / attempt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        parent_ctx = SubAgentContext(depth=0, parent_run_dir=run_dir, parent_session_id=session_id)
        registry.register(DelegateTool(llm, registry, run_dir, parent_ctx, event_callback))
        registry.register(TeamTool(llm, registry, run_dir, parent_ctx, event_callback))

        agent = AgentLoop(
            registry=registry,
            llm=llm,
            event_callback=event_callback,
            max_iterations=50,
            persistent_memory=pm,
        )
        self._active_loops[session_id] = agent

        history = self._convert_messages_to_history(messages) if messages else None

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                _AGENT_EXECUTOR,
                lambda: agent.run(user_message=attempt.prompt, history=history, session_id=session_id),
            )
        finally:
            self._active_loops.pop(session_id, None)

        return result

    @staticmethod
    def _convert_messages_to_history(messages: list) -> list[Dict[str, Any]]:
        history = []
        for msg in messages[:-1]:
            role = msg.role if hasattr(msg, "role") else msg.get("role", "user")
            content = msg.content if hasattr(msg, "content") else msg.get("content", "")
            if not content.strip() or role not in ("user", "assistant"):
                continue
            history.append({"role": role, "content": content})

        MAX_HISTORY_CHARS = 12000
        total_chars = 0
        trimmed: list = []
        for msg in reversed(history):
            msg_len = len(msg.get("content", ""))
            if total_chars + msg_len > MAX_HISTORY_CHARS:
                break
            trimmed.append(msg)
            total_chars += msg_len
        return list(reversed(trimmed))
