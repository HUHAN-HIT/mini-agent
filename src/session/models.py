"""Session data models."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class SessionStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class AttemptStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = ""
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_attempt_id: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Session:
        data = dict(data)
        if "status" in data:
            data["status"] = SessionStatus(data["status"])
        return cls(**data)


@dataclass
class Message:
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_id: str = ""
    role: str = "user"
    content: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    linked_attempt_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Message:
        return cls(**data)


@dataclass
class Attempt:
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_id: str = ""
    parent_attempt_id: Optional[str] = None
    status: AttemptStatus = AttemptStatus.PENDING
    prompt: str = ""
    run_dir: Optional[str] = None
    summary: Optional[str] = None
    react_trace: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None
    error: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Attempt:
        data = dict(data)
        if "status" in data:
            data["status"] = AttemptStatus(data["status"])
        return cls(**data)

    def mark_running(self) -> None:
        self.status = AttemptStatus.RUNNING
        self.completed_at = None

    def mark_completed(self, summary: Optional[str] = None) -> None:
        self.status = AttemptStatus.COMPLETED
        self.completed_at = datetime.now().isoformat()
        if summary:
            self.summary = summary

    def mark_failed(self, error: str) -> None:
        self.status = AttemptStatus.FAILED
        self.completed_at = datetime.now().isoformat()
        self.error = error

    def mark_waiting_user(self) -> None:
        self.status = AttemptStatus.WAITING_USER
