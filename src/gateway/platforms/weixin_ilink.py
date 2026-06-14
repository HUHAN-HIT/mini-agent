"""WeChat personal (个人微信) iLink adapter.

The most stateful adapter in the gateway: long polling + per-peer
context_token + content debounce + rate-limit circuit + chunked send.

Why all this lives in one place: iLink isn't a public API. The behavior
described here is what hermes' weixin.py observes in production. Until we
have fixture coverage (P1a), some branches are best-effort and clearly
marked with TODO(ilink-fixture).

The adapter is structured so the protocol transport (``_api_post``) is
injectable — tests can pass a fake and exercise the full state machine
without hitting Tencent.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from src.gateway.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    PlatformCapabilities,
    SendResult,
    SessionSource,
)
from src.gateway.delivery import split_message
from src.gateway.platforms._utils import (
    MessageDeduplicator,
    RateLimitCircuit,
    TextDebouncer,
    TtlSet,
    hash_token,
)
from src.gateway.platforms.ilink_protocol import (
    IlinkMessage,
    IlinkRateLimited,
    IlinkSessionExpired,
    check_errcode,
    content_dedup_key,
    extract_messages,
    get_updates_buf,
    normalize_message,
)
from src.gateway.router import atomic_write_json, load_json
from src.gateway.session_key import build_session_key

logger = logging.getLogger(__name__)


@dataclass
class IlinkCredentials:
    account_id: str
    bot_token: str
    base_url: str
    cdn_base_url: str
    bot_type: str
    extras: dict


class ContextTokenStore:
    """Per-peer context_token cache, persisted to JSON for restart resilience."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._tokens: dict[str, dict[str, str]] = load_json(path, default=dict) or {}

    def _key(self, account_id: str, peer_id: str) -> str:
        return account_id

    def get(self, account_id: str, peer_id: str) -> str:
        return self._tokens.get(account_id, {}).get(peer_id, "")

    def set(self, account_id: str, peer_id: str, token: str) -> None:
        if not token:
            return
        bucket = self._tokens.setdefault(account_id, {})
        if bucket.get(peer_id) == token:
            return
        bucket[peer_id] = token
        try:
            atomic_write_json(self._path, self._tokens)
        except OSError as exc:
            logger.warning("context token persist failed: %s", exc)

    def snapshot(self) -> dict[str, dict[str, str]]:
        return json.loads(json.dumps(self._tokens))


def _policy_allows(policy: str, allow: set[str], value: str) -> bool:
    if policy == "disabled":
        return False
    if policy == "open":
        return True
    # allowlist
    return value in allow


def _load_credentials(path: Path) -> Optional[IlinkCredentials]:
    data = load_json(path, default=None)
    if not isinstance(data, dict):
        return None
    if not data.get("account_id") or not data.get("bot_token"):
        return None
    return IlinkCredentials(
        account_id=str(data["account_id"]),
        bot_token=str(data["bot_token"]),
        base_url=str(data.get("base_url") or "https://ilinkai.weixin.qq.com"),
        cdn_base_url=str(data.get("cdn_base_url") or "https://novac2c.c2c.weixin.qq.com/c2c"),
        bot_type=str(data.get("bot_type") or "3"),
        extras=dict(data.get("extras") or {}),
    )


def _save_credentials(path: Path, creds: IlinkCredentials) -> None:
    payload = {
        "account_id": creds.account_id,
        "bot_token": creds.bot_token,
        "base_url": creds.base_url,
        "cdn_base_url": creds.cdn_base_url,
        "bot_type": creds.bot_type,
        "extras": creds.extras,
    }
    atomic_write_json(path, payload)


# --------------------------------------------------------------------------
# Interactive QR login (个人微信 iLink scan-to-connect)
#
# Mirrors hermes' weixin.py: fetch a bot QR, render it in the terminal, then
# poll get_qrcode_status until the user confirms in WeChat. On "confirmed" the
# server hands back ``ilink_bot_id`` + ``bot_token`` — that *is* the whole
# config, so the operator fills nothing by hand (this is the "scan and you're
# done" UX). The HTTP transport is injected so the state machine is unit
# testable without touching Tencent (see test_gateway_core).
# --------------------------------------------------------------------------

_ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
_ILINK_CDN_BASE_URL = "https://novac2c.c2c.weixin.qq.com/c2c"
_ILINK_APP_ID = "bot"
_CHANNEL_VERSION = "2.2.0"
_ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0  # iLink client "2.2.0"
_EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
_EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
_QR_TIMEOUT_MS = 35_000
_QR_LOGIN_DEADLINE_SECONDS = 480
_QR_MAX_REFRESH = 3
_ITEM_TEXT = 1
_MSG_TYPE_BOT = 2
_MSG_STATE_FINISH = 2

# (base_url, endpoint) -> parsed JSON dict
QrGet = Callable[[str, str], Awaitable[dict]]
# (qrcode_value, qrcode_url) -> None
QrRender = Callable[[str, str], None]


def _make_ssl_connector() -> Optional["aiohttp.TCPConnector"]:
    """certifi-backed TCPConnector, or None to fall back to aiohttp defaults.

    Tencent's iLink endpoint is not verifiable against every system CA store;
    when certifi is installed we pin its Mozilla bundle. Without certifi we
    rely on the system store (and SSL_CERT_FILE via ``trust_env=True``).
    """
    try:
        import ssl

        import certifi
    except ImportError:
        return None
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_ctx)


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _base_info() -> dict[str, str]:
    return {"channel_version": _CHANNEL_VERSION}


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _ilink_headers(token: str, body: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": _ILINK_APP_ID,
        "iLink-App-ClientVersion": str(_ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _api_get(session: aiohttp.ClientSession, *, base_url: str, endpoint: str) -> dict:
    """Bare iLink GET with the app-id headers the QR endpoints expect."""
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = {
        "iLink-App-Id": _ILINK_APP_ID,
        "iLink-App-ClientVersion": str(_ILINK_APP_CLIENT_VERSION),
    }

    async def _do() -> dict:
        async with session.get(url, headers=headers) as resp:
            raw = await resp.text()
            if not resp.ok:
                raise RuntimeError(f"iLink GET {endpoint} HTTP {resp.status}: {raw[:200]}")
            return json.loads(raw)

    return await asyncio.wait_for(_do(), timeout=_QR_TIMEOUT_MS / 1000)


def _render_qr_terminal(qrcode_value: str, qrcode_url: str) -> None:
    """Print the scannable URL and, if ``qrcode`` is installed, an ASCII QR.

    ``qrcode_url`` is the full scannable liteapp URL; ``qrcode_value`` is just
    the hex token. WeChat must scan the URL, so we prefer it as the QR payload.
    """
    scan_data = qrcode_url or qrcode_value
    print("\n请使用微信扫描以下二维码（也可在手机微信中直接打开下面的链接）：")
    if qrcode_url:
        print(qrcode_url)
    try:
        import qrcode  # type: ignore

        qr = qrcode.QRCode()
        qr.add_data(scan_data)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print(
            "（未安装 qrcode，无法在终端绘制二维码，请打开上面的链接扫码；"
            "可执行 `pip install qrcode` 启用终端二维码）"
        )
    except Exception as exc:  # rendering is best-effort
        print(f"（终端二维码渲染失败: {exc}，请直接打开上面的链接）")


async def _run_qr_login(
    *,
    get: QrGet,
    bot_type: str,
    render_qr: QrRender = _render_qr_terminal,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], float] = time.monotonic,
    deadline_seconds: int = _QR_LOGIN_DEADLINE_SECONDS,
) -> Optional[IlinkCredentials]:
    """Drive the iLink QR state machine to completion.

    ``get(base_url, endpoint)`` is the injected HTTP-GET transport returning a
    parsed JSON dict. Returns ready-to-persist :class:`IlinkCredentials` once
    the user confirms in WeChat, or ``None`` on timeout / unrecoverable error.
    Owns no network or wall clock of its own, so the whole flow is testable
    with a scripted ``get`` and fake ``sleep``/``now``.
    """
    try:
        qr_resp = await get(_ILINK_BASE_URL, f"{_EP_GET_BOT_QR}?bot_type={bot_type}")
    except Exception as exc:
        logger.error("weixin: failed to fetch QR code: %s", exc)
        return None

    qrcode_value = str(qr_resp.get("qrcode") or "")
    qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
    if not qrcode_value:
        logger.error("weixin: QR response missing 'qrcode'")
        return None
    render_qr(qrcode_value, qrcode_url)

    deadline = now() + deadline_seconds
    base_url = _ILINK_BASE_URL
    refresh_count = 0

    while now() < deadline:
        try:
            status_resp = await get(base_url, f"{_EP_GET_QR_STATUS}?qrcode={qrcode_value}")
        except asyncio.TimeoutError:
            await sleep(1)
            continue
        except Exception as exc:
            logger.warning("weixin: QR poll error: %s", exc)
            await sleep(1)
            continue

        status = str(status_resp.get("status") or "wait")
        if status == "wait":
            print(".", end="", flush=True)
        elif status == "scaned":
            print("\n已扫码，请在微信中点击确认……")
        elif status == "scaned_but_redirect":
            # The bot lives on a sharded host; follow it for subsequent polls.
            redirect_host = str(status_resp.get("redirect_host") or "")
            if redirect_host:
                base_url = f"https://{redirect_host}"
        elif status == "expired":
            refresh_count += 1
            if refresh_count > _QR_MAX_REFRESH:
                print("\n二维码多次过期，请重新执行 `gateway.py login weixin`。")
                return None
            print(f"\n二维码已过期，正在刷新……（{refresh_count}/{_QR_MAX_REFRESH}）")
            try:
                qr_resp = await get(_ILINK_BASE_URL, f"{_EP_GET_BOT_QR}?bot_type={bot_type}")
            except Exception as exc:
                logger.error("weixin: QR refresh failed: %s", exc)
                return None
            qrcode_value = str(qr_resp.get("qrcode") or "")
            qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
            if not qrcode_value:
                logger.error("weixin: refreshed QR response missing 'qrcode'")
                return None
            base_url = _ILINK_BASE_URL
            render_qr(qrcode_value, qrcode_url)
        elif status == "confirmed":
            account_id = str(status_resp.get("ilink_bot_id") or "")
            bot_token = str(status_resp.get("bot_token") or "")
            if not account_id or not bot_token:
                logger.error("weixin: QR confirmed but credential payload was incomplete")
                return None
            return IlinkCredentials(
                account_id=account_id,
                bot_token=bot_token,
                base_url=str(status_resp.get("baseurl") or base_url),
                cdn_base_url=_ILINK_CDN_BASE_URL,
                bot_type=str(bot_type),
                extras={"user_id": str(status_resp.get("ilink_user_id") or "")},
            )
        await sleep(1)

    print("\n微信登录超时，请重试。")
    return None


def run_login_flow(config: dict, *, creds_path: Path) -> bool:
    """Interactive QR login for 个人微信 (iLink): scan → auto-config → persist.

    Like hermes, the operator fills nothing by hand. We fetch a QR, render it
    in the terminal, and on confirm the server returns ``account_id`` +
    ``bot_token``, which we write to ``creds_path`` for ``connect()`` to pick
    up. Returns ``True`` on success so the CLI can enable the platform.
    """
    bot_type = str(config.get("bot_type") or "3")

    async def _main() -> Optional[IlinkCredentials]:
        connector = _make_ssl_connector()
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=40)
        async with aiohttp.ClientSession(
            trust_env=True, connector=connector, timeout=timeout
        ) as session:
            async def _get(base_url: str, endpoint: str) -> dict:
                return await _api_get(session, base_url=base_url, endpoint=endpoint)

            return await _run_qr_login(get=_get, bot_type=bot_type)

    creds = asyncio.run(_main())
    if creds is None:
        return False

    _save_credentials(creds_path, creds)
    # A fresh scan may be a different bot identity than last time — drop the old
    # long-poll cursor so getupdates starts clean. A stale cursor (from a prior
    # account) can silently swallow new messages for the new account.
    cursor_path = creds_path.parent / "weixin_cursor.json"
    try:
        cursor_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("could not clear weixin cursor: %s", exc)
    print(f"\n微信连接成功，account_id={creds.account_id}")
    print(f"bot_token={creds.bot_token}")
    print(
        f"凭据已保存到 {creds_path}（adapter 启动时自动读取，无需手动把 token 填到任何地方）",
        flush=True,
    )
    return True


class WeixinIlinkAdapter(BasePlatformAdapter):
    platform_name = "weixin"
    capabilities = PlatformCapabilities(
        max_message_length=2000,
        supports_message_editing=False,
        supports_code_blocks=True,
        supports_typing=True,
        supports_media_upload=False,
        requires_context_token=True,
        final_only=True,
    )

    def __init__(self, config: dict, *, data_dir: Optional[Path] = None) -> None:
        super().__init__()
        self._config = config
        self._data_dir = data_dir or Path("~/.mini-agent/gateway").expanduser()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._creds_path = self._data_dir / "weixin_credentials.json"
        self._cursor_path = self._data_dir / "weixin_cursor.json"
        self._creds: Optional[IlinkCredentials] = None
        self._poll_session: Optional[aiohttp.ClientSession] = None
        self._send_session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None

        self._token_store = ContextTokenStore(self._data_dir / "weixin_context_tokens.json")
        self._seen_messages = TtlSet(ttl_seconds=300, max_size=5000)
        self._content_dedup = MessageDeduplicator(ttl_seconds=300)
        self._rate_limit = RateLimitCircuit(
            threshold=int(config.get("rate_limit_circuit_threshold", 1)),
            window_seconds=int(config.get("rate_limit_circuit_window_seconds", 30)),
            open_seconds=int(config.get("rate_limit_circuit_open_seconds", 30)),
        )
        self._send_gate = asyncio.Lock()
        self._text_batcher = TextDebouncer(
            delay_seconds=float(config.get("text_batch_delay_seconds", 3.0)),
            split_delay_seconds=float(config.get("text_batch_split_delay_seconds", 5.0)),
            flush=self._flush_text_batch,
        )

        self._dm_policy = str(config.get("dm_policy", "allowlist"))
        self._dm_allow = {str(x) for x in (config.get("allow_from") or []) if str(x).strip()}
        self._group_policy = str(config.get("group_policy", "disabled"))
        self._group_allow = {str(x) for x in (config.get("group_allow_from") or []) if str(x).strip()}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def connect(self) -> bool:
        self._creds = _load_credentials(self._creds_path)
        if self._creds is None:
            # Fall back to inline config credentials (env-expanded).
            self._creds = IlinkCredentials(
                account_id=str(self._config.get("account_id") or ""),
                bot_token=str(self._config.get("bot_token") or ""),
                base_url=str(self._config.get("base_url") or "https://ilinkai.weixin.qq.com"),
                cdn_base_url=str(self._config.get("cdn_base_url") or "https://novac2c.c2c.weixin.qq.com/c2c"),
                bot_type=str(self._config.get("bot_type") or "3"),
                extras={},
            )
        if not self._creds.account_id or not self._creds.bot_token:
            self.mark_fatal(
                code="missing_credentials",
                message=(
                    "weixin bot_token/account_id missing; run "
                    "`gateway.py login weixin` or set them in gateway.yaml"
                ),
            )
            return False

        timeout = aiohttp.ClientTimeout(total=None, sock_read=40, sock_connect=10)
        # trust_env=True so HTTP(S)_PROXY / NO_PROXY / SSL_CERT_FILE are honored —
        # users behind a proxy (e.g. Clash fake-ip) need the long-poll and send
        # traffic to route through it, just like the QR-login session does.
        self._poll_session = aiohttp.ClientSession(timeout=timeout, trust_env=True)
        self._send_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), trust_env=True
        )
        self._poll_task = asyncio.create_task(self._poll_loop(), name="weixin-poll")
        self._running = True
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self._text_batcher.close()
        except Exception:
            logger.exception("text batcher close failed")
        if self._poll_session:
            await self._poll_session.close()
        if self._send_session:
            await self._send_session.close()

    # ------------------------------------------------------------------
    # inbound: long polling
    # ------------------------------------------------------------------
    def _load_cursor(self) -> str:
        data = load_json(self._cursor_path, default=None)
        if not isinstance(data, dict):
            return ""
        return str(data.get("get_updates_buf") or data.get("sync_buf") or "")

    def _save_cursor(self, buf: str) -> None:
        if not buf:
            return
        try:
            atomic_write_json(self._cursor_path, {"get_updates_buf": buf, "ts": time.time()})
        except OSError as exc:
            logger.warning("cursor persist failed: %s", exc)

    async def _api_post(
        self,
        path: str,
        payload: dict,
        *,
        timeout_ms: int = 35_000,
    ) -> dict:
        assert self._creds is not None
        assert self._poll_session is not None
        data = await self._post_ilink(
            self._poll_session,
            path=path,
            payload=payload,
            timeout_ms=timeout_ms,
        )
        check_errcode(data)
        return data

    async def _post_ilink(
        self,
        session: aiohttp.ClientSession,
        *,
        path: str,
        payload: dict,
        timeout_ms: int,
    ) -> dict:
        assert self._creds is not None
        body = _json_dumps({**payload, "base_info": _base_info()})
        url = f"{self._creds.base_url.rstrip('/')}/{path.lstrip('/')}"

        async def _do() -> dict:
            async with session.post(url, data=body, headers=_ilink_headers(self._creds.bot_token, body)) as resp:
                text = await resp.text()
                if not getattr(resp, "ok", False):
                    raise RuntimeError(f"iLink POST {path} HTTP {resp.status}: {text[:200]}")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"ret": -1, "errcode": -1, "errmsg": text[:200]}

        return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)

    async def _poll_loop(self) -> None:
        assert self._creds is not None
        cursor = self._load_cursor()
        failures = 0
        while self._running:
            try:
                data = await self._api_post(
                    "ilink/bot/getupdates",
                    {"get_updates_buf": cursor},
                    timeout_ms=35_000,
                )
                failures = 0
                cursor = get_updates_buf(data) or cursor
                if cursor:
                    self._save_cursor(cursor)
                msgs = extract_messages(data)
                if msgs:
                    logger.info("weixin getupdates returned %d message(s)", len(msgs))
                for raw in msgs:
                    asyncio.create_task(self._process_message_safe(raw))
            except IlinkRateLimited:
                self._rate_limit.record()
                await asyncio.sleep(30)
            except IlinkSessionExpired:
                self.mark_fatal("session_expired", "weixin session expired; re-login required")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                logger.warning("weixin poll error (%d): %s", failures, exc)
                await asyncio.sleep(30 if failures >= 3 else 2)

    async def _process_message_safe(self, raw: dict) -> None:
        try:
            await self._process_message(raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("weixin process_message error")

    async def _process_message(self, raw: dict) -> None:
        assert self._creds is not None
        account_id = self._creds.account_id
        msg = normalize_message(raw, account_id)
        if msg is None:
            logger.debug("weixin inbound: skipped by normalize (self-message / unsupported)")
            return

        logger.info(
            "weixin inbound from=%s chat_type=%s text=%r",
            msg.from_user_id, msg.chat_type, (msg.text or "")[:80],
        )

        if msg.message_id and self._seen_messages.seen_or_mark(msg.message_id):
            return

        if msg.text:
            key = content_dedup_key(account_id, msg.from_user_id, msg.text)
            if self._content_dedup.is_duplicate(key):
                return

        if msg.chat_type == "group":
            if not _policy_allows(self._group_policy, self._group_allow, msg.chat_id):
                logger.info("weixin: group %s dropped by group_policy=%s", msg.chat_id, self._group_policy)
                return
        else:
            if not _policy_allows(self._dm_policy, self._dm_allow, msg.from_user_id):
                logger.info("weixin: DM from %s dropped by dm_policy=%s", msg.from_user_id, self._dm_policy)
                return

        if msg.context_token:
            token_peer_id = msg.chat_id if msg.chat_type == "group" else msg.from_user_id
            self._token_store.set(account_id, token_peer_id, msg.context_token)

        if not msg.text:
            return  # TODO(ilink-fixture): handle media

        source = SessionSource(
            platform=self.platform_name,
            account_id=account_id,
            chat_id=msg.chat_id,
            raw_chat_id=msg.chat_id,
            chat_type=msg.chat_type,
            user_id=msg.from_user_id,
            user_name=msg.from_user_id,
            message_id=msg.message_id,
        )
        event = MessageEvent(
            text=msg.text,
            source=source,
            message_type=MessageType.TEXT,
            message_id=msg.message_id,
            raw_message=raw,
        )

        # DM text -> debounce; group text -> deliver directly to avoid
        # merging different speakers.
        if msg.chat_type == "dm":
            session_key = build_session_key(source)
            self._text_batcher.enqueue(session_key, event)
        else:
            await self._dispatch(event)

    async def _flush_text_batch(self, session_key: str, events: list[MessageEvent]) -> None:
        if not events:
            return
        merged_text = "\n".join(e.text for e in events if e.text).strip()
        if not merged_text:
            return
        primary = events[0]
        merged = MessageEvent(
            text=merged_text,
            source=primary.source,
            message_type=MessageType.TEXT,
            message_id=primary.message_id,
            raw_message=[e.raw_message for e in events],
        )
        await self._dispatch(merged)

    # ------------------------------------------------------------------
    # outbound: send message
    # ------------------------------------------------------------------
    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        assert self._creds is not None
        meta = metadata or {}
        account_id = meta.get("account_id") or self._creds.account_id
        chat_type = meta.get("chat_type") or ""
        peer_id = chat_id if chat_type == "group" else (meta.get("user_id") or chat_id)
        context_token = self._token_store.get(account_id, peer_id)
        if not context_token:
            return SendResult(
                success=False,
                error=f"missing context_token for {account_id}:{peer_id}",
            )

        if self._rate_limit.is_open():
            return SendResult(success=False, error="weixin rate limit circuit open", retryable=True)

        chunks = split_message(content, max_length=self.capabilities.max_message_length)
        sent_ids: list[str] = []
        chunk_delay = float(self._config.get("send_chunk_delay_seconds", 1.5))

        async with self._send_gate:
            for index, chunk in enumerate(chunks):
                if index and chunk_delay:
                    await asyncio.sleep(chunk_delay)
                data = await self._send_one_text(
                    peer_id=peer_id,
                    text=chunk,
                    context_token=context_token,
                )
                errcode = data.get("errcode") or data.get("err_code") or 0
                try:
                    errcode = int(errcode)
                except (TypeError, ValueError):
                    errcode = 0

                if errcode == -2:
                    self._rate_limit.record()
                    return SendResult(
                        success=False,
                        error="weixin rate limited (-2)",
                        raw_response=data,
                        retryable=True,
                    )
                if errcode == -14:
                    self.mark_fatal("session_expired", "weixin session expired during send")
                    return SendResult(success=False, error="weixin session expired", raw_response=data)
                if errcode != 0:
                    return SendResult(success=False, error=str(data), raw_response=data)

                msg_id = str(data.get("message_id") or data.get("msgid") or "")
                if msg_id:
                    sent_ids.append(msg_id)

        return SendResult(
            success=True,
            message_id=sent_ids[-1] if sent_ids else None,
            continuation_message_ids=tuple(sent_ids[:-1]) if len(sent_ids) > 1 else (),
        )

    async def _send_one_text(self, *, peer_id: str, text: str, context_token: str) -> dict:
        assert self._creds is not None
        assert self._send_session is not None
        client_id = f"mini-agent-weixin-{uuid.uuid4().hex}"
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": peer_id,
                "client_id": client_id,
                "message_type": _MSG_TYPE_BOT,
                "message_state": _MSG_STATE_FINISH,
                "item_list": [{"type": _ITEM_TEXT, "text_item": {"text": text}}],
                "context_token": context_token,
            },
        }
        try:
            data = await self._post_ilink(
                self._send_session,
                path="ilink/bot/sendmessage",
                payload=payload,
                timeout_ms=15_000,
            )
            return data
        except aiohttp.ClientError as exc:
            return {"ret": -1, "errcode": -1, "errmsg": str(exc)}
