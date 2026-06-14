"""WeCom callback adapter: webhook verify + decrypt + queue + active reply.

Design (see docs/im-gateway-design.md §7):

- One FastAPI app instance, multiple WeCom apps register the same callback
  path; the adapter tries each app's cryptor until one succeeds.
- The webhook handler only verifies, decrypts, dedups, enqueues, and returns
  ``success``. Agent turn happens on a separate drain task — never block the
  webhook request, or WeCom will retry.
- Active replies go through ``message/send`` with per-app access_token cache.
- ``corp_id:user_id`` is the scoped chat id so different corps/apps with the
  same internal UserID never collide in the session router.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from src.gateway.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    PlatformCapabilities,
    SendResult,
    SessionSource,
)
from src.gateway.platforms._utils import TtlSet
from src.gateway.platforms.wecom_crypto import (
    WeComCryptoError,
    WeComCryptor,
    WeComMessage,
    parse_wecom_xml,
)

logger = logging.getLogger(__name__)


@dataclass
class WeComApp:
    name: str
    corp_id: str
    agent_id: str
    corp_secret: str
    token: str
    encoding_aes_key: str
    allow_from: set[str]
    cryptor: WeComCryptor


@dataclass
class AccessToken:
    value: str
    expires_at: float


def _normalize_apps(config: dict) -> list[WeComApp]:
    apps_cfg = config.get("apps") or []
    out: list[WeComApp] = []
    for raw in apps_cfg:
        if not isinstance(raw, dict):
            continue
        if not raw.get("corp_id") or not raw.get("corp_secret"):
            logger.warning("wecom app %r missing corp_id/corp_secret, skipped", raw.get("name"))
            continue
        name = str(raw.get("name") or "default")
        try:
            cryptor = WeComCryptor(
                corp_id=raw["corp_id"],
                token=raw["token"],
                encoding_aes_key=raw["encoding_aes_key"],
            )
        except WeComCryptoError as exc:
            logger.warning("wecom app %s cryptor init failed: %s", name, exc)
            continue
        allow = {str(x).strip() for x in (raw.get("allow_from") or []) if str(x).strip()}
        out.append(
            WeComApp(
                name=name,
                corp_id=raw["corp_id"],
                agent_id=str(raw.get("agent_id") or ""),
                corp_secret=raw["corp_secret"],
                token=raw["token"],
                encoding_aes_key=raw["encoding_aes_key"],
                allow_from=allow,
                cryptor=cryptor,
            )
        )
    return out


class WecomWebhookAdapter(BasePlatformAdapter):
    platform_name = "wecom"
    capabilities = PlatformCapabilities(
        max_message_length=4000,
        supports_message_editing=False,
        supports_code_blocks=False,
        supports_typing=False,
        final_only=True,
    )

    def __init__(self, config: dict, app: Any) -> None:
        super().__init__()
        self._app = app
        self._apps = _normalize_apps(config)
        self._by_name = {a.name: a for a in self._apps}
        self._by_corp = {a.corp_id: a for a in self._apps}
        self._message_queue: asyncio.Queue[MessageEvent] = asyncio.Queue()
        ttl = int(config.get("message_dedup_ttl_seconds", 300))
        self._seen_messages = TtlSet(ttl_seconds=ttl, max_size=5000)
        self._access_tokens: dict[str, AccessToken] = {}
        self._token_locks: dict[str, asyncio.Lock] = {}
        self._http: httpx.AsyncClient | None = None
        self._drain_task: asyncio.Task | None = None
        self._route_installed = False
        self._allow_any = not any(a.allow_from for a in self._apps)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def connect(self) -> bool:
        if not self._apps:
            self.mark_fatal("no_apps", "no WeCom app configured with full credentials")
            return False
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self._register_routes()
        self._drain_task = asyncio.create_task(self._drain_loop(), name="wecom-drain")
        self._running = True
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._drain_task and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._http:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------
    # inbound: webhook routes
    # ------------------------------------------------------------------
    def _register_routes(self) -> None:
        if self._route_installed:
            return
        self._route_installed = True

        @self._app.get("/wecom/callback")
        async def verify_url(  # noqa: ANN202 - FastAPI handler
            msg_signature: str,
            timestamp: str,
            nonce: str,
            echostr: str,
        ):
            from starlette.responses import PlainTextResponse

            for app in self._apps:
                try:
                    plain = app.cryptor.verify_url(
                        signature=msg_signature,
                        timestamp=timestamp,
                        nonce=nonce,
                        echostr=echostr,
                    )
                    return PlainTextResponse(plain)
                except WeComCryptoError:
                    continue

            return PlainTextResponse("signature verification failed", status_code=403)

        @self._app.post("/wecom/callback")
        async def callback(  # noqa: ANN202
            request,
            msg_signature: str,
            timestamp: str,
            nonce: str,
        ):
            from starlette.responses import PlainTextResponse

            body = await request.body()
            for app in self._apps:
                try:
                    xml_text = app.cryptor.decrypt_payload(
                        signature=msg_signature,
                        timestamp=timestamp,
                        nonce=nonce,
                        body=body,
                    )
                except WeComCryptoError:
                    continue
                event = self._build_event(app, xml_text)
                if event is None:
                    return PlainTextResponse("success")
                if event.message_id and self._seen_messages.seen_or_mark(event.message_id):
                    return PlainTextResponse("success")
                await self._message_queue.put(event)
                return PlainTextResponse("success")
            return PlainTextResponse("invalid callback payload", status_code=400)

    def _build_event(self, app: WeComApp, xml_text: str) -> MessageEvent | None:
        msg = parse_wecom_xml(xml_text)
        if msg is None:
            return None
        if msg.msg_type not in {"text", "event"}:
            return None
        if not msg.from_user:
            return None

        if not self._allow_any and app.allow_from and msg.from_user not in app.allow_from:
            logger.info("wecom user %s not in allowlist for app %s, dropped", msg.from_user, app.name)
            return None

        scoped_chat_id = f"{app.corp_id}:{msg.from_user}"
        source = SessionSource(
            platform=self.platform_name,
            account_id=app.name,
            chat_id=scoped_chat_id,
            raw_chat_id=msg.from_user,
            chat_type="dm",
            user_id=msg.from_user,
            user_name=msg.from_user,
            message_id=msg.msg_id,
        )

        if msg.msg_type == "event":
            text = "/start"
        else:
            text = msg.content or "/start"

        return MessageEvent(
            text=text,
            source=source,
            message_type=MessageType.TEXT,
            message_id=msg.msg_id,
            raw_message=xml_text,
        )

    async def _drain_loop(self) -> None:
        while True:
            event = await self._message_queue.get()
            try:
                await self._dispatch(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("wecom drain handler error")

    # ------------------------------------------------------------------
    # outbound: message/send
    # ------------------------------------------------------------------
    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        if self._http is None:
            return SendResult(success=False, error="adapter not connected")

        meta = metadata or {}
        app_name = meta.get("account_id")
        app = self._by_name.get(app_name) if app_name else None
        if app is None:
            app = self._app_for_scoped_chat(chat_id)
        if app is None:
            return SendResult(success=False, error=f"unknown wecom app for {chat_id}")

        user_id = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        try:
            token = await self._get_access_token(app)
        except Exception as exc:
            return SendResult(success=False, error=f"access_token: {exc}", retryable=True)

        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
        payload = {
            "touser": user_id,
            "agentid": app.agent_id,
            "msgtype": "text",
            "text": {"content": content},
        }
        try:
            resp = await self._http.post(url, json=payload)
            data = resp.json()
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

        errcode = data.get("errcode", 0)
        if errcode == 0:
            return SendResult(success=True, message_id=str(data.get("msgid") or ""), raw_response=data)
        return SendResult(
            success=False,
            error=str(data),
            raw_response=data,
            retryable=errcode in {42001, 40014, 40001},  # token issues
        )

    def _app_for_scoped_chat(self, chat_id: str) -> WeComApp | None:
        if not chat_id or ":" not in chat_id:
            return self._apps[0] if self._apps else None
        corp_id = chat_id.split(":", 1)[0]
        return self._by_corp.get(corp_id)

    async def _get_access_token(self, app: WeComApp) -> str:
        cached = self._access_tokens.get(app.name)
        if cached and cached.expires_at - time.time() > 60:
            return cached.value
        lock = self._token_locks.setdefault(app.name, asyncio.Lock())
        async with lock:
            cached = self._access_tokens.get(app.name)
            if cached and cached.expires_at - time.time() > 60:
                return cached.value
            assert self._http is not None
            url = (
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
                f"?corpid={app.corp_id}&corpsecret={app.corp_secret}"
            )
            resp = await self._http.get(url)
            data = resp.json()
            if data.get("errcode", 0) != 0:
                raise RuntimeError(f"gettoken failed: {data}")
            token = data["access_token"]
            ttl = int(data.get("expires_in", 7200))
            self._access_tokens[app.name] = AccessToken(value=token, expires_at=time.time() + ttl)
            return token
