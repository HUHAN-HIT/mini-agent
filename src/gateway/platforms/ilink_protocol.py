"""iLink (Tencent ClawBot) payload normalization helpers.

iLink responses have several field-name variants (``msgs`` vs ``messages``,
``get_updates_buf`` vs ``sync_buf``). hermes handles both — we replicate just
enough normalization to be robust against either shape, since iLink isn't a
publicly documented API and Tencent has shipped both spellings.

Real protocol contract is owned by the adapter (see ``weixin_ilink.py``).
This module only contains pure helpers that are easy to unit-test against
fixtures.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


ILINK_ERR_RATE_LIMITED = -2
ILINK_ERR_SESSION_EXPIRED = -14


@dataclass
class IlinkMessage:
    message_id: str
    from_user_id: str
    chat_type: str  # "dm" | "group"
    chat_id: str
    text: str
    raw: dict
    context_token: str = ""
    item_list: list[dict] = field(default_factory=list)


class IlinkRateLimited(Exception):
    """Raised when iLink returns ``errcode=-2`` (too frequent)."""


class IlinkSessionExpired(Exception):
    """Raised when iLink returns ``errcode=-14`` (account session lost)."""


class IlinkProtocolError(Exception):
    """Raised when iLink returns a non-success ``ret`` / ``errcode``."""

    def __init__(self, data: dict) -> None:
        self.data = data
        ret = data.get("ret")
        errcode = data.get("errcode") or data.get("err_code")
        errmsg = data.get("errmsg") or data.get("err_msg") or data.get("message") or ""
        super().__init__(f"iLink request failed ret={ret!r} errcode={errcode!r} errmsg={errmsg!r}")


def _get_first(data: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def get_updates_buf(response: dict) -> str:
    """Extract the next poll cursor from an iLink response.

    iLink has shipped both ``get_updates_buf`` and ``sync_buf``. Either should
    advance the cursor; we prefer the more specific name.
    """

    value = _get_first(response, "get_updates_buf", "sync_buf", default="")
    return str(value or "")


def extract_messages(response: dict) -> list[dict]:
    """Return the raw message objects from a getupdates response."""

    if not isinstance(response, dict):
        return []
    items = _get_first(response, "msgs", "messages", "items", default=[])
    if not isinstance(items, list):
        return []
    return [m for m in items if isinstance(m, dict)]


def extract_text(item_list: Iterable[Any]) -> str:
    """Pull text from ``item_list`` entries.

    iLink wraps each piece of content in an ``item``; the text variant has
    ``text_item`` / ``content`` / ``text`` depending on the version. We pick
    the first non-empty shape we find.
    """

    parts: list[str] = []
    for item in item_list or []:
        if not isinstance(item, dict):
            continue
        text = ""
        if "text_item" in item and isinstance(item["text_item"], dict):
            text = str(item["text_item"].get("content") or item["text_item"].get("text") or "")
        elif "content" in item:
            text = str(item.get("content") or "")
        elif "text" in item:
            text = str(item.get("text") or "")
        text = text.strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def guess_chat_type(raw: dict, account_id: str) -> tuple[str, str]:
    """Return ``(chat_type, effective_chat_id)`` for a raw iLink message.

    DM uses ``from_user_id`` as the chat id. Group messages carry a
    ``chat_room_id`` / ``group_id``; we use that for chat id and the sender
    for user_id (which feeds per-user session isolation).
    """

    group_id = str(_get_first(raw, "chat_room_id", "group_id", default="") or "").strip()
    if group_id and group_id != account_id:
        return "group", group_id
    sender = str(raw.get("from_user_id") or "").strip()
    return "dm", sender


def normalize_message(raw: dict, account_id: str) -> Optional[IlinkMessage]:
    if not isinstance(raw, dict):
        return None
    sender_id = str(raw.get("from_user_id") or "").strip()
    if not sender_id or sender_id == account_id:
        return None

    message_id = str(_get_first(raw, "message_id", "msg_id", "msgid", default="") or "").strip()
    item_list = raw.get("item_list") or []
    if not isinstance(item_list, list):
        item_list = []
    text = extract_text(item_list)
    chat_type, chat_id = guess_chat_type(raw, account_id)
    if not chat_id:
        return None

    context_token = str(_get_first(raw, "context_token", "contextToken", default="") or "").strip()
    return IlinkMessage(
        message_id=message_id,
        from_user_id=sender_id,
        chat_type=chat_type,
        chat_id=chat_id,
        text=text,
        raw=raw,
        context_token=context_token,
        item_list=[i for i in item_list if isinstance(i, dict)],
    )


def check_errcode(data: dict) -> None:
    """Raise the matching iLink exception for known error codes."""

    if not isinstance(data, dict):
        return
    ret = data.get("ret", 0)
    errcode = data.get("errcode") or data.get("err_code") or 0
    try:
        ret_value = int(ret)
    except (TypeError, ValueError):
        ret_value = 0
    try:
        errcode_value = int(errcode)
    except (TypeError, ValueError):
        return

    if ret_value in {0, None} and errcode_value in {0, None}:
        return
    errmsg = str(_get_first(data, "errmsg", "err_msg", "message", default="") or "")
    if (ret_value == ILINK_ERR_SESSION_EXPIRED or errcode_value == ILINK_ERR_SESSION_EXPIRED):
        raise IlinkSessionExpired(str(data))
    if (ret_value == ILINK_ERR_RATE_LIMITED or errcode_value == ILINK_ERR_RATE_LIMITED) and errmsg.lower() == "unknown error":
        raise IlinkSessionExpired(str(data))
    if errcode_value == ILINK_ERR_RATE_LIMITED or ret_value == ILINK_ERR_RATE_LIMITED:
        raise IlinkRateLimited(str(data))
    raise IlinkProtocolError(data)


def content_dedup_key(account_id: str, sender_id: str, text: str) -> str:
    joined = f"{account_id}\x00{sender_id}\x00{text}"
    return "content:" + hashlib.md5(joined.encode("utf-8")).hexdigest()
