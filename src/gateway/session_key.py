"""Deterministic mini-agent session key from SessionSource.

The key shape is stable across restarts and across platforms: same source
always maps to the same mini-agent session_id via the SessionRouter. This keeps
agent history consistent and lets future platforms reuse the same router
without rewriting the key format.

See docs/im-gateway-design.md §6.1 for the full rule set.
"""

from __future__ import annotations

from src.gateway.base import SessionSource


def build_session_key(
    source: SessionSource,
    *,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
) -> str:
    """Build deterministic mini-agent session key from source.

    Rule summary:

    - DM: ``agent:main:<platform>:dm:<account_id?>:<chat_id|user_id>``
    - Group: per-user isolation by default (``...:group:<chat_id>:<user_id>``);
      set ``group_sessions_per_user=False`` for shared group sessions.
    - Thread: shared by default; per-user under ``thread_sessions_per_user``.
    """

    parts: list[str] = ["agent", "main", source.platform, source.chat_type]

    if source.account_id:
        parts.append(source.account_id)

    if source.chat_id:
        parts.append(source.chat_id)

    if source.thread_id:
        parts.append(source.thread_id)

    if source.chat_type == "dm":
        if not source.chat_id and source.user_id:
            parts.append(source.user_id)
    elif source.thread_id:
        if thread_sessions_per_user and source.user_id:
            parts.append(source.user_id)
    elif group_sessions_per_user and source.user_id:
        parts.append(source.user_id)

    return ":".join(str(p) for p in parts if p)
