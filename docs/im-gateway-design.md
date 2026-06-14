# Mini-Agent IM Gateway 设计文档

> **文档版本**：v1.4
> **创建日期**：2026-06-14
> **作者**：baymax
> **状态**：按 hermes-agent 网关边界全面修订，补充安装、自启动与账号单所有者锁设计，待实现

## 目录

1. [背景与目标](#1-背景与目标)
2. [Hermes 经验提炼](#2-hermes-经验提炼)
3. [Mini-Agent 现状与约束](#3-mini-agent-现状与约束)
4. [整体架构](#4-整体架构)
5. [核心抽象设计](#5-核心抽象设计)
6. [Session 与 Turn 编排](#6-session-与-turn-编排)
7. [企业微信适配器](#7-企业微信适配器)
8. [个人微信适配器 iLink](#8-个人微信适配器-ilink)
9. [Delivery 与消息格式化](#9-delivery-与消息格式化)
10. [平台账号单所有者锁](#10-平台账号单所有者锁)
11. [配置设计](#11-配置设计)
12. [安装与自启动设计](#12-安装与自启动设计)
13. [合规与风险](#13-合规与风险)
14. [实施路线](#14-实施路线)
15. [文件清单与代码量估算](#15-文件清单与代码量估算)
16. [验收标准](#16-验收标准)
17. [未解决问题](#17-未解决问题)
18. [附录：与 hermes 的对应关系](#18-附录与-hermes-的对应关系)

---

## 1. 背景与目标

### 1.1 背景

Mini-Agent 当前主要通过 `cli.py` 进行交互，入口是阻塞式 `input()`。用户希望把 agent 接入即时通讯软件，使其能在企业微信和个人微信中直接对话使用。

参考项目 `hermes-agent` 位于：

```text
E:\01_大模型经典学习项目\hermes-agent
```

hermes 已实现大量 IM/协作平台接入，核心价值不是“代码可以照搬”，而是它已经证明了几个边界：

- 平台协议状态应放在 adapter 内部，而不是泄漏到 runner。
- 入站消息要先标准化为 `MessageEvent`，并携带 `SessionSource`。
- session key 规则必须是独立契约，不能散落在各平台。
- 出站发送要有 `SendResult` 和平台能力标记，不能只假设“发一段文本”。
- webhook/长轮询平台都需要内部队列、排重、限流和关闭时的任务清理。

### 1.2 目标

- 首期接入两个平台：企业微信 + 个人微信 iLink。
- 借鉴 hermes 的网关边界，但不复制 hermes 的业务体量。
- 复用 mini-agent 现有 `SessionService`、`AgentLoop`、`ChatLLM`、`ToolRegistry`。
- P0 只做稳定的文本私聊；P1 接入个人微信文本；P2 以后再考虑流式、媒体、群策略。
- 为第三个平台，例如 Telegram，保留低成本扩展路径。

### 1.3 非目标

- 不实现 hermes 中的 kanban、sticker cache、memory monitor、slash command、pairing 等完整业务能力。
- P0/P1 不支持复杂群策略、@提及、富媒体卡片。
- P0/P1 不实现消息编辑式流式输出。个人微信 iLink 不支持编辑已发消息，P0/P1 统一走 final-only。
- 不接入 itchat、wechaty、iPad 协议等逆向方案。
- 普通安装动作不静默注册系统自启动。自启动必须通过显式 `service install` 命令启用。
- 同一个平台账号、应用或 token 不允许被 hermes 和 mini-agent 同时直接占用；共存必须通过分账号或上游桥接。

---

## 2. Hermes 经验提炼

### 2.1 值得吸收的边界

| hermes 组件 | 位置 | 对 mini-agent 的设计启发 |
|---|---|---|
| `MessageEvent` | `gateway/platforms/base.py:1416` | 入站消息不能只有 text/chat_id，还要带 source、message_id、media、raw_message |
| `SendResult` | `gateway/platforms/base.py:1545` | 出站必须返回 success/message_id/error/retryable，方便降级和诊断 |
| `BasePlatformAdapter` | `gateway/platforms/base.py:1796` | adapter 负责连接、接收、发送、平台能力和忙碌状态 |
| `SessionSource` | `gateway/session.py:70` | session、投递、权限、日志都应围绕 source，而不是散落字符串 |
| `build_session_key()` | `gateway/session.py:617` | DM、群、线程、按用户隔离的规则必须集中定义 |
| `WecomCallbackAdapter` | `gateway/platforms/wecom_callback.py` | webhook 应先入内部队列并立即 ACK，再异步交给 gateway |
| `WeixinAdapter` | `gateway/platforms/weixin.py` | iLink 的 token、sync buf、context token、文本 debounce、限流都属于 adapter |
| `GatewayStreamConsumer` | `gateway/stream_consumer.py` | P2 可借鉴，但 P0/P1 不应引入流式复杂度 |
| `DeliveryRouter` | `gateway/delivery.py` | 定时任务/跨平台主动投递未来可扩展，P0 只需最小 delivery contract |

### 2.2 不应照搬的内容

hermes 的 `gateway/run.py` 很强，但也非常重。mini-agent 不应复制：

- slash command 体系
- pairing/access group 完整流程
- background process watcher
- runtime footer/display_config
- multi-platform delivery router 的完整能力
- stream consumer 的 rich edit/draft 逻辑
- TTS/voice mode/sticker/media cache

mini-agent 应该只保留“薄网关控制面”：adapter 标准化消息，runner 串行执行 agent turn，delivery 把 final answer 发回原会话。

### 2.3 本文相对 v1.1 的核心变化

v1.1 修掉了几个会导致 P0 失败的硬错误，例如 `EventBus.subscribe` 用法、完成事件正文、同 session 串行。v1.2 进一步按 hermes 经验调整边界：

- 用 `SessionSource + MessageEvent` 替代 `IncomingMessage`。
- 用 `connect/disconnect/send/set_message_handler` 替代 `start/stop/send/set_handler`。
- 引入 `SendResult` 和 `PlatformCapabilities`。
- 把消息排重、access policy、协议状态优先放回 adapter。
- 企业微信使用 adapter 内部 queue + drain task。
- 企业微信配置支持多 app，session 用 `corp_id:user_id` 避免跨企业冲突。
- 个人微信 iLink 增加文本 debounce、send gate、rate-limit circuit、chunk delivery。
- gateway 层保留全局兜底去重和 per-session turn serial queue。
- 增加 platform account/token single-owner lock，防止 hermes 与 mini-agent 同时 poll 同一个 iLink bot。

---

## 3. Mini-Agent 现状与约束

### 3.1 当前可复用能力

| 模块 | 当前事实 | Gateway 设计影响 |
|---|---|---|
| `AgentLoop` | 同步阻塞，支持 `event_callback` | 继续用线程池运行，不在 IM 事件循环里阻塞 |
| `SessionService.send_message()` | async，创建 attempt 后后台执行 `_run_attempt` | 可作为 agent turn 入口，但需补事件 payload |
| `EventBus` | `subscribe()` 是 async iterator，`emit()` 可被线程池回调触发 | gateway 要 `event_bus.set_loop()`，并用 async iterator 等待指定 attempt |
| `SessionStore` | 文件系统持久化 session/message/attempt | 可复用 transcript/history |
| `ChatLLM` | 支持流式 delta、工具调用、cache stats | P0/P1 只消费最终结果，P2 再接 delta |

### 3.2 必须补齐的运行时契约

| 契约 | 当前事实 | P0 处理方式 |
|---|---|---|
| 跨线程事件投递 | agent 在线程池中执行，event callback 可能从 worker thread 调用 `EventBus.emit()` | gateway 启动时调用 `event_bus.set_loop(asyncio.get_running_loop())` |
| 完成事件正文 | 当前 `attempt.completed` 只带 `attempt_id/status` | 修改 `SessionService`，完成事件携带 `content/run_dir`，失败事件携带 `error/run_dir` |
| 等待完成 | `EventBus.subscribe()` 是 async iterator，不是 callback 注册 | runner 增加 `wait_for_attempt(session_id, attempt_id)` |
| 极短回复 race | attempt 可能在 subscribe 建立前完成 | `wait_for_attempt()` 先查 store，再订阅；heartbeat 时再次查 store |
| 同会话并发 | `send_message()` 会立即创建后台 task | gateway 为每个 session 加 `SessionTurnQueue`，保证同 session 串行 |

---

## 4. 整体架构

### 4.1 架构图

```text
IM 平台
  │
  ├─ 企业微信 HTTPS callback
  │
  └─ 个人微信 iLink long polling
        │
        ▼
┌─────────────────────────────────────────────┐
│ Platform Adapter                             │
│ - connect / disconnect                       │
│ - protocol auth/state                        │
│ - inbound dedupe                             │
│ - access policy                              │
│ - queue / debounce / rate limit              │
│ - normalize raw payload -> MessageEvent      │
└─────────────────────┬───────────────────────┘
                      │ set_message_handler
                      ▼
┌─────────────────────────────────────────────┐
│ GatewayRunner / TurnOrchestrator             │
│ - build_session_key(SessionSource)           │
│ - SessionTurnQueue per session_id            │
│ - service.send_message()                     │
│ - wait_for_attempt(session_id, attempt_id)   │
│ - delivery final answer through adapter      │
└─────────────────────┬───────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────┐
│ SessionService                               │
│ - persist user/assistant messages            │
│ - run AgentLoop in ThreadPoolExecutor        │
│ - emit attempt.completed(content)            │
└─────────────────────┬───────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────┐
│ AgentLoop / ChatLLM / Tools                  │
└─────────────────────────────────────────────┘
```

### 4.2 设计原则

1. **adapter owning protocol state**  
   `sync_buf`、`context_token`、access token、扫码状态、限流、平台排重都属于 adapter，不属于 runner。

2. **runner owning turn lifecycle**  
   runner 不关心平台协议，只关心 `MessageEvent -> session_id -> attempt_id -> final answer -> adapter.send()`。

3. **session source as single source of truth**  
   session key、出站 metadata、日志、权限判断都基于 `SessionSource`。

4. **final-only first**  
   P0/P1 只发最终答案。流式 delta 先保留在 `SessionService/EventBus`，不推给 IM。

5. **capability-driven delivery**  
   不在 runner 里写平台分支；通过 adapter capability 判断能不能编辑、最大长度、是否支持 code block、是否需要拆分。

---

## 5. 核心抽象设计

### 5.1 `src/gateway/base.py`

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Optional


class MessageType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    FILE = "file"
    MIXED = "mixed"


@dataclass(frozen=True)
class SessionSource:
    """Where an inbound message came from."""

    platform: str                 # "wecom" | "weixin" | future "telegram"
    chat_id: str                  # conversation target; may be scoped
    chat_type: str = "dm"         # "dm" | "group" | "thread"
    user_id: str = ""
    user_name: str = ""
    account_id: str = ""          # bot/account/app id; important for multi app/account
    thread_id: str = ""
    message_id: str = ""
    raw_chat_id: str = ""         # platform original chat id when chat_id is scoped


@dataclass
class MessageEvent:
    """Normalized inbound message from a platform adapter."""

    text: str
    source: SessionSource
    message_type: MessageType = MessageType.TEXT
    message_id: str = ""
    raw_message: Any = None
    media_paths: list[str] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)
    reply_to_message_id: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    internal: bool = False


@dataclass
class SendResult:
    """Result of sending a message to a platform."""

    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None
    retryable: bool = False
    continuation_message_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlatformCapabilities:
    max_message_length: int = 4000
    supports_message_editing: bool = False
    supports_code_blocks: bool = False
    supports_typing: bool = False
    supports_media_upload: bool = False
    requires_context_token: bool = False
    final_only: bool = True


MessageHandler = Callable[[MessageEvent], Awaitable[None]]


class BasePlatformAdapter(ABC):
    """Minimal hermes-style platform adapter contract."""

    platform_name: str
    capabilities = PlatformCapabilities()

    def __init__(self) -> None:
        self._message_handler: Optional[MessageHandler] = None
        self._fatal_error_code: Optional[str] = None
        self._fatal_error_message: Optional[str] = None
        self._running = False

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._message_handler = handler

    @property
    def has_fatal_error(self) -> bool:
        return self._fatal_error_message is not None

    @property
    def fatal_error_message(self) -> Optional[str]:
        return self._fatal_error_message

    @abstractmethod
    async def connect(self) -> bool:
        """Connect/authenticate and start receiving messages."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Stop receiving messages and release resources."""

    @abstractmethod
    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send content to a platform chat."""

    async def send_typing(self, chat_id: str, metadata: Optional[dict[str, Any]] = None) -> None:
        """Optional platform typing indicator."""
```

### 5.2 为什么不用 v1.1 的 `IncomingMessage`

`IncomingMessage(platform, chat_id, user_id)` 对 P0 看起来够用，但会很快遇到边界：

- 企业微信多 app 时，`UserID` 在不同 corp/app 下可能冲突。
- 个人微信 iLink 的出站不仅需要 `chat_id`，还需要 account + peer 的 `context_token`。
- 群聊需要区分“群会话”和“发言人”。
- 未来 Telegram/Slack/Discord 会有 thread/topic。
- 日志和持久化需要 raw id 与 scoped id 同时存在。

因此 v1.2 使用 `SessionSource` 作为所有路由规则的输入。

### 5.3 adapter 与 runner 的职责分界

| 能力 | 归属 | 理由 |
|---|---|---|
| 协议鉴权/token | adapter | 平台差异大 |
| `sync_buf/context_token` | adapter | iLink 私有协议状态 |
| 入站 `msg_id` 排重 | adapter 优先，runner 可兜底 | 重试语义来自平台 |
| 文本 debounce | adapter | 微信用户连发/转发是平台体验问题 |
| session key | router | 必须跨平台一致 |
| 同 session 串行 | runner | agent history 一致性问题 |
| agent 执行 | `SessionService` | 已有能力 |
| 出站 chunk/format | delivery + adapter capabilities | 平台长度/格式不同 |

---

## 6. Session 与 Turn 编排

### 6.1 `src/gateway/session_key.py`

```python
def build_session_key(
    source: SessionSource,
    *,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
) -> str:
    """Build deterministic mini-agent session key from source."""

    parts = ["agent", "main", source.platform, source.chat_type]

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
```

P0 可只启用 DM，但函数必须一开始就承载群/线程扩展规则。这样 P5 加 Telegram 时不会推翻 session 存储。

### 6.2 `src/gateway/router.py`

```python
class SessionRouter:
    """Map stable gateway session keys to mini-agent session ids."""

    def __init__(self, service: SessionService, path: Path, config: dict):
        self._service = service
        self._path = path
        self._config = config
        self._map: dict[str, str] = load_json(path, default={})

    def get_or_create(self, source: SessionSource) -> tuple[str, str]:
        session_key = build_session_key(
            source,
            group_sessions_per_user=self._config.get("group_sessions_per_user", True),
            thread_sessions_per_user=self._config.get("thread_sessions_per_user", False),
        )
        if session_key not in self._map:
            session = self._service.create_session(title=session_key)
            self._map[session_key] = session.session_id
            atomic_write_json(self._path, self._map)
        return self._map[session_key], session_key
```

### 6.3 `src/gateway/turn_queue.py`

```python
class SessionTurnQueue:
    """Serialize agent turns per mini-agent session."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    async def run(self, session_id: str, fn: Callable[[], Awaitable[None]]) -> None:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            await fn()
```

P0 用 lock 足够。后续如要展示排队状态，可以升级为显式 queue。

### 6.4 `src/gateway/runner.py`

```python
async def run_gateway(config_path: Path | None = None) -> None:
    config = load_config(config_path)
    data_dir = expand_path(config["data_dir"])

    store = SessionStore(...)
    event_bus = EventBus()
    event_bus.set_loop(asyncio.get_running_loop())
    service = SessionService(store, event_bus, RUNS_DIR)

    router = SessionRouter(service, data_dir / "sessions_map.json", config["session"])
    turn_queue = SessionTurnQueue()
    adapters = build_adapters(config)

    async def wait_for_attempt(session_id: str, attempt_id: str) -> tuple[str, dict]:
        def from_store() -> tuple[str, dict] | None:
            attempt = next(
                (a for a in service.get_attempts(session_id) if a.attempt_id == attempt_id),
                None,
            )
            if not attempt or attempt.status.value not in ("completed", "failed"):
                return None
            event_type = "attempt.completed" if attempt.status.value == "completed" else "attempt.failed"
            return event_type, {
                "attempt_id": attempt_id,
                "status": attempt.status.value,
                "content": attempt.summary,
                "error": attempt.error,
                "run_dir": attempt.run_dir,
            }

        if stored := from_store():
            return stored

        async for event in event_bus.subscribe(session_id):
            if event.event_type == "heartbeat":
                if stored := from_store():
                    return stored
                continue
            if event.event_type not in ("attempt.completed", "attempt.failed"):
                continue
            if event.data.get("attempt_id") != attempt_id:
                continue
            return event.event_type, event.data

    async def handle_event(event: MessageEvent) -> None:
        adapter = adapters[event.source.platform]
        session_id, session_key = router.get_or_create(event.source)

        async def run_turn() -> None:
            result = await service.send_message(session_id, event.text)
            attempt_id = result["attempt_id"]
            event_type, data = await wait_for_attempt(session_id, attempt_id)

            if event_type == "attempt.completed":
                content = data.get("content") or "(无回复)"
            else:
                content = f"执行失败：{data.get('error') or 'unknown'}"

            await deliver_final_response(
                adapter=adapter,
                source=event.source,
                content=content,
                reply_to=event.message_id or event.source.message_id,
            )

        await turn_queue.run(session_id, run_turn)

    for adapter in adapters.values():
        adapter.set_message_handler(handle_event)
        ok = await adapter.connect()
        if not ok:
            logger.warning("adapter failed to connect: %s %s", adapter.platform_name, adapter.fatal_error_message)

    await serve_until_shutdown(adapters)
```

### 6.5 `SessionService` 最小契约修订

P0 必须对 `src/session/service.py` 做向后兼容的小改动：

```python
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

self.event_bus.emit(
    session.session_id,
    "attempt.failed",
    {
        "attempt_id": attempt.attempt_id,
        "status": attempt.status.value,
        "error": attempt.error,
        "run_dir": attempt.run_dir,
    },
)
```

已有 CLI/MCP 消费者仍可只读取 `attempt_id/status`，因此这是兼容扩展。

---

## 7. 企业微信适配器

### 7.1 设计目标

企业微信采用标准 callback 模式：

- GET `/wecom/callback` 用于 URL 验证。
- POST `/wecom/callback` 接收加密 XML。
- webhook 处理函数只做验签、解密、排重、入队、ACK。
- agent 回复通过 `message/send` 主动发送。

### 7.2 与 hermes 对齐的关键点

hermes 的 `WecomCallbackAdapter` 有几个设计必须吸收：

- 支持多个 self-built app。
- 使用 `corp_id:user_id` 作为 scoped chat id，避免不同企业/应用的 `UserID` 冲突。
- webhook 里只 `queue.put(event)` 并立即返回 `success`。
- adapter 内部维护短 TTL `_seen_messages` 处理企微重试。
- access token 按 app 缓存。
- XML 使用安全解析器，避免 untrusted XML 风险。

### 7.3 文件：`src/gateway/platforms/wecom_webhook.py`

```python
class WecomWebhookAdapter(BasePlatformAdapter):
    platform_name = "wecom"
    capabilities = PlatformCapabilities(
        max_message_length=4000,
        supports_message_editing=False,
        supports_code_blocks=False,
        supports_typing=False,
        final_only=True,
    )

    def __init__(self, config: dict, app: FastAPI):
        super().__init__()
        self._app = app
        self._apps = normalize_wecom_apps(config)
        self._message_queue: asyncio.Queue[MessageEvent] = asyncio.Queue()
        self._seen_messages = TtlSet(ttl_seconds=300, max_size=2000)
        self._access_tokens: dict[str, AccessToken] = {}
        self._user_app_map: dict[str, str] = {}
        self._http: httpx.AsyncClient | None = None
        self._drain_task: asyncio.Task | None = None

    async def connect(self) -> bool:
        if not self._apps:
            self._fatal_error_message = "No WeCom app configured"
            return False
        self._http = httpx.AsyncClient()
        self._register_routes()
        self._drain_task = asyncio.create_task(self._drain_loop(), name="wecom-drain")
        self._running = True
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._drain_task:
            self._drain_task.cancel()
        if self._http:
            await self._http.aclose()

    def _register_routes(self) -> None:
        @self._app.get("/wecom/callback")
        async def verify_url(msg_signature: str, timestamp: str, nonce: str, echostr: str):
            for app in self._apps:
                try:
                    plain = self._crypt_for_app(app).verify_url(
                        msg_signature, timestamp, nonce, echostr
                    )
                    return PlainTextResponse(plain)
                except WeComCryptoError:
                    continue
            return PlainTextResponse("signature verification failed", status_code=403)

        @self._app.post("/wecom/callback")
        async def callback(req: Request, msg_signature: str, timestamp: str, nonce: str):
            body = await req.body()
            for app in self._apps:
                try:
                    xml_text = self._decrypt_request(app, body, msg_signature, timestamp, nonce)
                    event = self._build_event(app, xml_text)
                    if event is not None:
                        if event.message_id and self._seen_messages.seen_or_mark(event.message_id):
                            return PlainTextResponse("success")
                        await self._message_queue.put(event)
                    return PlainTextResponse("success")
                except WeComCryptoError:
                    continue
                except Exception:
                    logger.exception("wecom callback error")
                    break
            return PlainTextResponse("invalid callback payload", status_code=400)

    async def _drain_loop(self) -> None:
        while True:
            event = await self._message_queue.get()
            if self._message_handler is None:
                continue
            task = asyncio.create_task(self._message_handler(event))
            # track task if later shutdown needs cancellation

    def _build_event(self, app: dict, xml_text: str) -> MessageEvent | None:
        msg = parse_wecom_xml_safely(xml_text)
        if msg.msg_type == "event" and msg.event in {"enter_agent", "subscribe"}:
            return None
        if msg.msg_type not in {"text", "event"}:
            return None

        user_id = msg.from_user
        corp_id = msg.to_user or app["corp_id"]
        scoped_chat_id = f"{corp_id}:{user_id}"

        source = SessionSource(
            platform=self.platform_name,
            account_id=app["name"],
            chat_id=scoped_chat_id,
            raw_chat_id=user_id,
            chat_type="dm",
            user_id=user_id,
            user_name=user_id,
            message_id=msg.msg_id,
        )
        return MessageEvent(
            text=msg.content or "/start",
            source=source,
            message_id=msg.msg_id,
            raw_message=xml_text,
        )

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        metadata: dict | None = None,
    ) -> SendResult:
        app_name = (metadata or {}).get("account_id")
        app = self._app_by_name(app_name) or self._app_for_scoped_chat(chat_id)
        if app is None:
            return SendResult(success=False, error=f"unknown wecom app for {chat_id}")

        user_id = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        token = await self._get_access_token(app)
        resp = await self._http.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
            json={
                "touser": user_id,
                "agentid": app["agent_id"],
                "msgtype": "text",
                "text": {"content": content},
            },
        )
        data = resp.json()
        if data.get("errcode", 0) != 0:
            return SendResult(success=False, error=str(data), raw_response=data)
        return SendResult(success=True, raw_response=data)
```

### 7.4 P0 范围

P0 只做：

- GET URL 验证
- POST 文本/简单 event 入站
- 私聊主动文本回复
- app-level access token cache
- app-level allowlist
- 300 秒 TTL 入站排重

不做：

- 群聊发送策略
- 图片/语音/文件
- 被动回复 XML
- 复杂成员/部门/标签路由

---

## 8. 个人微信适配器 iLink

### 8.1 设计目标

个人微信 iLink 是状态型协议，adapter 必须拥有这些状态：

- account credential：`account_id`、`bot_token`、`base_url`
- long polling cursor：`get_updates_buf` 或 `sync_buf`
- peer context token：`account_id:user_id -> context_token`
- typing ticket cache
- message dedupe
- rate limit circuit
- send lock / chunk delay
- text debounce buffer

runner 不应知道这些字段。

### 8.2 文件：`src/gateway/platforms/weixin_ilink.py`

```python
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

    def __init__(self, config: dict, data_dir: Path):
        super().__init__()
        self._config = config
        self._data_dir = data_dir
        self._account: dict | None = None
        self._poll_session: aiohttp.ClientSession | None = None
        self._send_session: aiohttp.ClientSession | None = None
        self._poll_task: asyncio.Task | None = None
        self._token_store = ContextTokenStore(data_dir / "context_tokens")
        self._typing_cache = TypingTicketCache(ttl_seconds=600)
        self._dedup = MessageDeduplicator(ttl_seconds=300)
        self._send_gate = asyncio.Lock()
        self._rate_limit = RateLimitCircuit(threshold=1, window_seconds=30, open_seconds=30)
        self._text_batcher = TextDebouncer(
            delay_seconds=config.get("text_batch_delay_seconds", 3.0),
            split_delay_seconds=config.get("text_batch_split_delay_seconds", 5.0),
            flush=self._flush_text_batch,
        )

    async def connect(self) -> bool:
        self._account = await self._load_or_login()
        self._token_store.restore(self._account["account_id"])
        self._poll_session = aiohttp.ClientSession()
        self._send_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        self._poll_task = asyncio.create_task(self._poll_loop(), name="weixin-poll")
        self._running = True
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
        await self._text_batcher.close()
        if self._poll_session:
            await self._poll_session.close()
        if self._send_session:
            await self._send_session.close()
```

### 8.3 长轮询

```python
async def _poll_loop(self) -> None:
    assert self._account is not None
    account_id = self._account["account_id"]
    updates_buf = load_updates_buf(self._data_dir, account_id)
    failures = 0

    while self._running:
        try:
            response = await self._api_post(
                "ilink/bot/getupdates",
                {"get_updates_buf": updates_buf},
                timeout_ms=35_000,
            )
            failures = 0
            new_buf = response.get("get_updates_buf") or response.get("sync_buf") or ""
            if new_buf:
                updates_buf = new_buf
                save_updates_buf(self._data_dir, account_id, updates_buf)

            for raw in normalize_ilink_messages(response):
                asyncio.create_task(self._process_message_safe(raw))

        except IlinkRateLimited:
            self._rate_limit.record()
            await asyncio.sleep(30)
        except IlinkSessionExpired:
            self._account = await self._qr_login()
            updates_buf = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            logger.warning("weixin poll error %s", exc)
            await asyncio.sleep(30 if failures >= 3 else 2)
```

### 8.4 入站处理

```python
async def _process_message(self, raw: dict) -> None:
    account_id = self._account["account_id"]
    sender_id = str(raw.get("from_user_id") or "").strip()
    if not sender_id or sender_id == account_id:
        return

    message_id = str(raw.get("message_id") or raw.get("msg_id") or "").strip()
    if message_id and self._dedup.is_duplicate(message_id):
        return

    item_list = raw.get("item_list") or []
    text = extract_text_from_ilink_items(item_list)
    if text:
        content_key = f"content:{sender_id}:{md5(text)}"
        if self._dedup.is_duplicate(content_key):
            return

    chat_type, effective_chat_id = guess_ilink_chat_type(raw, account_id)
    if chat_type == "group":
        if not self._is_group_allowed(effective_chat_id):
            return
    elif not self._is_dm_allowed(sender_id):
        return

    context_token = str(raw.get("context_token") or "").strip()
    if context_token:
        self._token_store.set(account_id, sender_id, context_token)
        asyncio.create_task(self._maybe_fetch_typing_ticket(sender_id, context_token))

    media_paths, media_types = await self._collect_media(item_list)
    if not text and not media_paths:
        return

    source = SessionSource(
        platform=self.platform_name,
        account_id=account_id,
        chat_id=effective_chat_id,
        raw_chat_id=effective_chat_id,
        chat_type=chat_type,
        user_id=sender_id,
        user_name=sender_id,
        message_id=message_id,
    )
    event = MessageEvent(
        text=text,
        source=source,
        message_id=message_id,
        raw_message=raw,
        media_paths=media_paths,
        media_types=media_types,
        message_type=message_type_from_media(media_types, text),
    )

    if event.message_type == MessageType.TEXT:
        self._text_batcher.enqueue(build_session_key(source), event)
    elif self._message_handler:
        await self._message_handler(event)
```

### 8.5 文本 debounce

微信用户很容易连续发送多条短消息，或者转发一组消息。hermes 的 iLink adapter 会把短时间内同一 session 的文本聚合后再触发 agent。mini-agent 应保留这个能力，否则用户连发三句就会触发三个 agent attempt。

P1 默认：

```yaml
text_batch_delay_seconds: 3.0
text_batch_split_delay_seconds: 5.0
```

规则：

- 同一 session key 的纯文本消息进入 debounce buffer。
- 新文本到达时重置 flush timer。
- flush 时将文本用换行合并，media path 合并。
- 非文本媒体消息不 debounce，直接投递。

### 8.6 出站发送

iLink 出站不能只发送 `content`，必须带最新 `context_token`。发送也要限速和拆分。

```python
async def send(
    self,
    chat_id: str,
    content: str,
    *,
    reply_to: str | None = None,
    metadata: dict | None = None,
) -> SendResult:
    account_id = (metadata or {}).get("account_id") or self._account["account_id"]
    peer_id = (metadata or {}).get("user_id") or chat_id
    context_token = self._token_store.get(account_id, peer_id)
    if not context_token:
        return SendResult(success=False, error=f"missing context_token for {account_id}:{peer_id}")

    if self._rate_limit.is_open():
        return SendResult(success=False, error="weixin rate limit circuit open", retryable=True)

    chunks = split_for_weixin(content, max_length=self.capabilities.max_message_length)
    sent_ids: list[str] = []

    async with self._send_gate:
        for index, chunk in enumerate(chunks):
            if index:
                await asyncio.sleep(self._config.get("send_chunk_delay_seconds", 1.5))
            data = await self._send_one_text(
                to=peer_id,
                text=chunk,
                context_token=context_token,
            )
            if data.get("errcode") == -2:
                self._rate_limit.record()
                return SendResult(success=False, error=str(data), raw_response=data, retryable=True)
            if data.get("errcode") == -14:
                return SendResult(success=False, error="weixin session expired", raw_response=data)
            if data.get("errcode", 0) != 0:
                return SendResult(success=False, error=str(data), raw_response=data)
            if data.get("message_id"):
                sent_ids.append(str(data["message_id"]))

    return SendResult(
        success=True,
        message_id=sent_ids[-1] if sent_ids else None,
        continuation_message_ids=tuple(sent_ids[:-1]),
    )
```

### 8.7 iLink fixture 要求

P1 实现前先落 `tests/fixtures/ilink/`：

- QR 登录：`wait`、`scaned`、`confirmed`、`expired`
- `getupdates` 空返回
- 单条文本消息
- 连续多条文本消息
- `get_updates_buf`/`sync_buf` 字段差异
- `msgs`/`messages` 字段差异
- `item_list/text_item` 文本提取
- `context_token` 缺失
- `errcode=-2` 频率限制
- `errcode=-14` session 过期
- `sendmessage` payload 校验：`AuthorizationType`、`Authorization`、`X-WECHAT-UIN`、`base_info`、`msg.context_token`

---

## 9. Delivery 与消息格式化

### 9.1 P0 delivery contract

```python
async def deliver_final_response(
    *,
    adapter: BasePlatformAdapter,
    source: SessionSource,
    content: str,
    reply_to: str | None,
) -> SendResult:
    chunks = split_message(
        content,
        max_length=adapter.capabilities.max_message_length,
        len_fn=len,
    )

    last_result = SendResult(success=True)
    for chunk in chunks:
        last_result = await adapter.send(
            source.chat_id,
            chunk,
            reply_to=reply_to,
            metadata={
                "account_id": source.account_id,
                "user_id": source.user_id,
                "chat_type": source.chat_type,
                "raw_chat_id": source.raw_chat_id,
            },
        )
        if not last_result.success:
            return last_result
    return last_result
```

### 9.2 P0/P1 格式策略

| 平台 | 最大长度 | code block | 编辑 | 出站策略 |
|---|---:|---|---|---|
| 企业微信 | 4000 | 不依赖 | 不支持 | final-only，超长拆分 |
| 个人微信 iLink | 2000 | 可保留 fenced code | 不支持 | final-only，按块拆分并加发送间隔 |

P2 若要流式：

- 支持编辑的平台使用 edit/draft stream。
- 不支持编辑的平台继续 final-only，最多发送 typing indicator。
- 个人微信不应发送“半截答案 + 最终答案”两份内容，避免刷屏。

---

## 10. 平台账号单所有者锁

### 10.1 结论

同一个微信 AI bot 账号不应同时被 hermes 和 mini-agent 直接使用。这里的“直接使用”指两个进程都拿同一个 iLink `bot_token` 做长轮询、推进 `get_updates_buf/sync_buf`，并用各自缓存的 `context_token` 发消息。

允许的共存方式只有三类：

| 模式 | 说明 | 适用场景 |
|---|---|---|
| 分账号/分 app | hermes 和 mini-agent 使用不同 iLink bot token、不同企微 agent_id 或不同平台账号 | 两套系统都要独立对外服务 |
| 上游桥接 | hermes 独占微信/企微入口，把事件转发给 mini-agent 的 backend endpoint，mini-agent 不直接 poll 平台 | 已经用 hermes 管理 IM 入口，希望 mini-agent 只作为 agent runtime |
| mini-agent 独占 | mini-agent 直接占用平台账号，hermes 禁用对应平台 adapter | 轻量部署、只需要 mini-agent |

不允许两个系统同时直接 poll 同一个 iLink bot token，也不允许两个 gateway 同时暴露同一个企微 `corp_id + agent_id` callback。前者会互相推进同步游标和上下文 token，后者会造成平台回调入口不可预测。

### 10.2 锁粒度

新增 `src/gateway/locks.py`，在 adapter 真正连接平台前获取平台资源锁。

| 平台 | lock scope | identity | 说明 |
|---|---|---|---|
| 个人微信 iLink | `weixin-bot-token` | `sha256(bot_token)`，没有 token 时用 `account_id` | 防止两个进程同时长轮询同一个 bot |
| 企业微信 | `wecom-app` | `corp_id:agent_id` | 防止两个 callback/service 同时声明同一个企微应用 |
| 后续 Telegram | `telegram-bot-token` | `sha256(bot_token)` | 验证抽象时复用 |

锁文件默认写入：

```text
~/.mini-agent/gateway/locks/<scope>/<identity>.json
```

锁文件只保存可诊断元数据，不保存明文 token：

```json
{
  "owner": "mini-agent",
  "platform": "weixin",
  "scope": "weixin-bot-token",
  "identity": "sha256:7c9c...",
  "pid": 12345,
  "cwd": "E:/03_个人项目归档/mini-agent",
  "command": "python gateway.py run --config ...",
  "config_path": "E:/.../gateway.yaml",
  "started_at": "2026-06-14T09:30:00-07:00"
}
```

写锁必须使用原子创建语义：

- 锁文件存在且 `pid` 仍存活时，当前 adapter 进入 fatal 状态，不启动对应平台。
- 锁文件存在但 `pid` 不存在时，判定为 stale lock；默认由 `doctor` 提示，`run/service start` 可在安全确认后清理。
- 不提供“抢占活锁”的默认行为。`--force-stale-lock` 只允许清理 stale lock，不允许杀进程或抢占仍存活的 hermes/mini-agent。

### 10.3 生命周期

获取锁的位置：

1. `gateway.py run` 解析配置后创建 adapter。
2. adapter `connect()` 在发起 webhook 监听、iLink poll 或 token 刷新前调用 `PlatformLock.acquire()`。
3. 成功后启动平台主循环；失败时记录 `last_error`，该平台标记为 disabled/fatal。
4. `disconnect()`、`service stop` 和进程正常退出时释放锁。

如果启用了多个平台，一个平台锁失败不应拖垮整个 gateway；runner 应继续启动未冲突的平台。但如果所有 enabled platform 都因为锁失败而不可用，进程应以非 0 状态退出，避免自启动任务反复“看似成功”。

### 10.4 与 hermes 共存

设计目标不是阻止 hermes 和 mini-agent 同机运行，而是阻止它们同时直接占用同一个平台资源。

mini-agent 的 `doctor` 和 `service install` 需要做两层检查：

| 检查 | 行为 |
|---|---|
| mini-agent 自身锁目录 | 同 token/app 已被另一个 mini-agent 进程占用时 fatal |
| hermes 兼容锁或状态目录 | 配置了 `locks.hermes_home` 时，发现同一 `weixin-bot-token` 或 `corp_id:agent_id` 已由 hermes 占用则 fatal |

P0 不要求 mini-agent 完整解析 hermes 内部状态库；只要求提供可配置的 `hermes_home`/`hermes_lock_dir`，并在能识别到同 scope identity 时拒绝直接启动。识别不到 hermes 锁时，文档和 `doctor` 输出必须明确提醒：同一 iLink token 只能由一个系统直接 poll。

如果希望同一个微信 aibot 同时服务 hermes 和 mini-agent，应采用桥接模式：

```text
WeChat/WeCom -> hermes gateway -> mini-agent backend endpoint -> mini-agent session/runner
```

此时 mini-agent 的 `platforms.weixin.enabled=false`，不获取微信平台锁，只暴露后端调用入口。桥接协议可以放到后续 P2/P3，不进入当前 P0/P1 的直接 IM gateway 范围。

### 10.5 配置开关

```yaml
locks:
  enabled: true
  dir: "~/.mini-agent/gateway/locks"
  stale_after_seconds: 86400
  check_hermes: true
  hermes_home: ""
  hermes_lock_dir: ""
```

- `locks.enabled=false` 只允许测试环境使用；生产 `doctor` 应给出 warning。
- `hermes_home` 和 `hermes_lock_dir` 为空时，不做 hermes 跨系统检测，但仍做 mini-agent 自身资源锁。
- token 类 identity 一律 hash 后落盘，日志也只输出短 hash。

---

## 11. 配置设计

### 11.1 `gateway.yaml`

```yaml
server:
  host: "0.0.0.0"
  port: 8645

data_dir: "~/.mini-agent/gateway"

locks:
  enabled: true
  dir: "~/.mini-agent/gateway/locks"
  stale_after_seconds: 86400
  check_hermes: true
  hermes_home: ""
  hermes_lock_dir: ""

session:
  router_path: "~/.mini-agent/gateway/sessions_map.json"
  group_sessions_per_user: true
  thread_sessions_per_user: false
  per_session_serial: true
  history_max_chars: 12000

platforms:
  wecom:
    enabled: true
    host: "0.0.0.0"
    port: 8645
    path: "/wecom/callback"
    # P0 可以只配置一个 app；结构上支持多个 app，避免后续重构。
    apps:
      - name: "default"
        corp_id: ${WECOM_CORP_ID}
        agent_id: ${WECOM_AGENT_ID}
        corp_secret: ${WECOM_SECRET}
        token: ${WECOM_TOKEN}
        encoding_aes_key: ${WECOM_AES_KEY}
        allow_from: []
    message_dedup_ttl_seconds: 300

  weixin:
    enabled: false
    bot_type: "3"
    base_url: "https://ilinkai.weixin.qq.com"
    cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c"
    account_id: ${WEIXIN_ACCOUNT_ID:-}
    bot_token: ${WEIXIN_TOKEN:-}
    dm_policy: "allowlist"      # "disabled" | "allowlist" | "open"
    allow_from: []
    group_policy: "disabled"   # P1 默认不启用群
    group_allow_from: []
    text_batch_delay_seconds: 3.0
    text_batch_split_delay_seconds: 5.0
    send_chunk_delay_seconds: 1.5
    send_chunk_retries: 4
    rate_limit_circuit_threshold: 1
    rate_limit_circuit_window_seconds: 30
    rate_limit_circuit_open_seconds: 30

logging:
  level: "INFO"
  file: "~/.mini-agent/gateway/gateway.log"
```

### 11.2 环境变量

```bash
WECOM_CORP_ID=ww1234567890abcdef
WECOM_AGENT_ID=1000002
WECOM_SECRET=xxx
WECOM_TOKEN=xxx
WECOM_AES_KEY=xxx

WEIXIN_ACCOUNT_ID=xxx@im.bot
WEIXIN_TOKEN=ilinkbot_xxx
```

### 11.3 默认安全姿态

- 企业微信可以默认开放到已配置 app，但建议生产环境使用 `allow_from`。
- 个人微信默认 `dm_policy=allowlist`，避免任何好友都能消耗 token。
- 个人微信群默认 disabled，因为 iLink 群能力不稳定，且容易带来噪声。
- 平台账号锁默认开启；同一个 iLink token 或企微 app 被占用时，当前平台 adapter 不启动。

---

## 12. 安装与自启动设计

### 12.1 设计原则

gateway 可以支持系统自启动，但不能在普通安装时静默加入系统自启动。原因：

- gateway 会监听端口，可能涉及防火墙和反向代理。
- 企业微信需要 `gateway.yaml`、`.env`、回调 URL、证书/公网入口先准备好。
- 个人微信 iLink 首次登录可能需要扫码，不适合在后台服务启动时交互。
- 自启动会长期持有 IM 凭证，必须由用户显式确认。
- Windows、Linux、macOS 的服务机制不同，静默注册容易造成不可诊断的启动失败。

因此安装流程分成两层：

```bash
pip install -e ".[gateway]"       # 只安装代码和依赖，不注册服务
python gateway.py doctor          # 检查配置、端口、凭证、依赖
python gateway.py login weixin    # 可选：前台扫码，写入凭证
python gateway.py service install # 显式注册自启动
python gateway.py service start
python gateway.py service status
```

### 12.2 CLI 命令

新增 `gateway.py` 子命令：

```text
python gateway.py run [--config gateway.yaml]
python gateway.py doctor [--config gateway.yaml]
python gateway.py login weixin [--config gateway.yaml]
python gateway.py service install [--name mini-agent-gateway] [--config gateway.yaml]
python gateway.py service uninstall [--name mini-agent-gateway]
python gateway.py service start [--name mini-agent-gateway]
python gateway.py service stop [--name mini-agent-gateway]
python gateway.py service status [--name mini-agent-gateway]
```

行为约束：

- `run` 是前台启动，适合调试和首次验收。
- `doctor` 只检查，不修改系统服务。
- `login weixin` 只做扫码/凭证落盘，不启动 gateway 服务。
- `service install` 必须先运行 doctor，doctor 失败则拒绝注册。
- `service uninstall` 只删除自启动项，不删除 `data_dir`、凭证、session 和日志。
- `service start/stop/status` 只操作服务，不修改配置。

### 12.3 Doctor 检查项

`doctor` 应输出 JSON 和人类可读摘要。P0 至少检查：

| 检查 | 失败级别 | 说明 |
|---|---|---|
| Python 可执行路径存在 | fatal | service 需要固定 python 路径 |
| 项目工作目录存在 | fatal | service 需要固定 cwd |
| `gateway.yaml` 可解析 | fatal | 配置错误不能注册服务 |
| `data_dir` 可创建/可写 | fatal | 凭证、router、日志都依赖它 |
| 已启用平台至少一个 | fatal | 空服务无意义 |
| 端口未被占用 | fatal | webhook 服务无法启动 |
| 企业微信必填字段完整 | fatal | 启用 wecom 时检查 app 字段 |
| 个人微信凭证存在或可扫码 | warning/fatal | service install 时若启用 weixin 且无凭证，建议先 `login weixin` |
| 平台账号锁未被占用 | fatal for enabled platform | 防止与 hermes 或另一个 mini-agent 同时占用同一 token/app |
| 日志目录可写 | fatal | 后台服务必须可诊断 |
| Windows Task Scheduler 可用 | fatal for Windows service install | 无法注册自启动 |

### 12.4 Windows 自启动：首选 Task Scheduler

P0 优先支持 Windows 用户级计划任务，而不是 Windows Service：

- 不强制管理员权限。
- 能指定 working directory。
- 能在用户登录后启动。
- 适合 `.venv\Scripts\python.exe gateway.py run` 这种 Python 项目。
- 安装/卸载可通过 `schtasks.exe` 或 PowerShell `ScheduledTasks` 完成。

推荐任务参数：

```text
Name: mini-agent-gateway
Trigger: At logon of current user
Action: <project>\.venv\Scripts\python.exe gateway.py run --config <project>\gateway.yaml
Start in: <project>
Stdout/Stderr: ~/.mini-agent/gateway/logs/service.log
Restart: 失败后延迟重试（Task Scheduler 支持有限，P0 可由 wrapper 进程处理）
```

P0 实现可以采用 wrapper 脚本：

```python
python gateway.py service run-wrapper --config gateway.yaml
```

wrapper 负责：

- 设置 cwd。
- 追加写日志。
- 写 runtime status。
- 捕获异常并以非零 exit code 退出。
- 可选：简单重试，避免瞬时网络错误导致任务永久退出。

### 12.5 Linux/macOS 后续策略

P0 可以只实现 Windows Task Scheduler，因为当前工作环境是 Windows。跨平台设计保留接口：

```python
class ServiceManager(ABC):
    @abstractmethod
    def install(self, spec: ServiceSpec) -> None: ...
    @abstractmethod
    def uninstall(self, name: str) -> None: ...
    @abstractmethod
    def start(self, name: str) -> None: ...
    @abstractmethod
    def stop(self, name: str) -> None: ...
    @abstractmethod
    def status(self, name: str) -> ServiceStatus: ...
```

后续映射：

| OS | 推荐机制 | 阶段 |
|---|---|---|
| Windows | Task Scheduler user task | P0c |
| Windows | WinSW/NSSM/pywin32 Windows Service | P4+，需要更强守护能力时 |
| Linux | systemd user service | P4+ |
| macOS | launchd user agent | P4+ |

### 12.6 Runtime status

后台服务必须可诊断，新增 runtime status 文件：

```text
~/.mini-agent/gateway/status.json
```

示例：

```json
{
  "service_name": "mini-agent-gateway",
  "state": "running",
  "pid": 12345,
  "started_at": "2026-06-14T10:00:00",
  "config_path": "E:\\03_个人项目归档\\mini-agent\\gateway.yaml",
  "cwd": "E:\\03_个人项目归档\\mini-agent",
  "python": "E:\\03_个人项目归档\\mini-agent\\.venv\\Scripts\\python.exe",
  "host": "0.0.0.0",
  "port": 8645,
  "enabled_platforms": ["wecom"],
  "log_path": "C:\\Users\\14907\\.mini-agent\\gateway\\logs\\service.log",
  "last_error": null
}
```

`service status` 优先读取系统服务状态，再合并 `status.json`，展示：

- 是否已安装
- 是否正在运行
- pid
- 端口
- 配置路径
- 日志路径
- 最近错误
- 已启用平台

### 12.7 配置扩展

`gateway.yaml` 增加可选 service 段：

```yaml
service:
  name: "mini-agent-gateway"
  autostart: false        # 只表达期望状态；不会被普通 install 自动执行
  start_on: "logon"       # Windows P0: logon
  python: ".venv/Scripts/python.exe"
  cwd: "."
  args: ["gateway.py", "run", "--config", "gateway.yaml"]
  log_file: "~/.mini-agent/gateway/logs/service.log"
  status_file: "~/.mini-agent/gateway/status.json"
  restart:
    enabled: true
    max_attempts: 5
    delay_seconds: 10
```

`service.autostart` 不代表安装时自动注册，只用于 `doctor`/`status` 提醒“配置期望自启动，但尚未安装”。

---

## 13. 合规与风险

### 13.1 合规等级

| 平台 | 等级 | 说明 |
|---|---|---|
| 企业微信 | 官方 API，低风险 | 自建应用 webhook + 主动消息 |
| 个人微信 iLink | 官方 ClawBot 通道，但有约束 | 不属于 itchat/wechaty/iPad 逆向协议，但仍受 iLink 能力与频控限制 |

### 13.2 个人微信 iLink 已知约束

1. `context_token` 强绑定会话，缺失时不能可靠主动发消息。
2. `errcode=-2` 需要退避，且应打开短暂 circuit breaker。
3. `errcode=-14` 表示 session/凭证问题，需要重新扫码。
4. 普通微信群不应作为 P1 验收能力。
5. 高频推送会破坏账号体验，也可能触发平台限制。

### 13.3 严格避免

- 不使用逆向协议。
- webhook handler 不等待 agent 完成。
- 不让个人微信成为高频广播通道。
- 不在 runner 中硬编码平台协议字段。
- 不把不同企业/账号的同名用户合并到同一个 session。
- 不在普通依赖安装、`pip install` 或项目初始化时静默注册自启动。
- 不让 hermes 与 mini-agent 同时直接占用同一个 iLink bot token 或同一个企微 app。

---

## 14. 实施路线

| 阶段 | 内容 | 工作量 | 验证标准 |
|---|---|---:|---|
| P0a | `base.py`、`session_key.py`、`router.py`、`turn_queue.py`、delivery final-only、`SessionService` 事件 payload | 0.5 天 | 单元测试覆盖 session key、turn serial、attempt wait、delivery split |
| P0b | 企微 callback adapter：GET verify、POST decrypt、queue、dedupe、多 app、主动回复 | 1 天 | 企微私聊发“你好”，30 秒内收到回复；重放 msg_id 不重复执行 |
| P0c | `doctor` + Windows Task Scheduler service 管理 | 0.5 天 | doctor 阻止无效配置注册；登录后自启动；status 可诊断 |
| P0d | 平台账号单所有者锁 + hermes 占用检测 | 0.5 天 | 同 token/app 被占用时 adapter 拒绝启动；doctor 可显示占用者 |
| P1a | iLink fixture 与协议 client：QR、getupdates、sendmessage、字段归一化 | 0.5 天 | fixture 测试稳定通过 |
| P1b | 个人微信 adapter：扫码/凭证、poll、context token、文本 debounce、限流、final-only 回复 | 1.5 天 | 微信私聊发“你好”，30 秒内收到回复；连发文本合并为一个 turn |
| P2 | typing/progress/final-only streaming policy | 0.5 天 | 长任务期间 typing，不刷半截答案 |
| P3 | 入站媒体 | 1.5 天 | 图片/文件落盘并进入 `MessageEvent.media_paths` |
| P4 | 出站媒体 | 1 天 | adapter capability 下发送图片/文件 |
| P5 | 第三平台 Telegram 验证抽象 | 0.5 天 | 不修改 session/router/runner 核心即可接入 |

---

## 15. 文件清单与代码量估算

| 文件 | 估算行数 | 说明 |
|---|---:|---|
| `gateway.py` | 80 | 项目根入口，包含 `run/doctor/login/service` 子命令分发 |
| `src/gateway/__init__.py` | 5 | 包声明 |
| `src/gateway/base.py` | 170 | `SessionSource`、`MessageEvent`、`SendResult`、adapter contract |
| `src/gateway/config.py` | 120 | YAML + env expansion + path expansion |
| `src/gateway/session_key.py` | 80 | `build_session_key()` 与测试 |
| `src/gateway/router.py` | 90 | session key -> mini session id |
| `src/gateway/turn_queue.py` | 50 | per-session serial execution |
| `src/gateway/delivery.py` | 120 | final-only delivery, split, SendResult handling |
| `src/gateway/runner.py` | 260 | adapter lifecycle, handler, wait_for_attempt |
| `src/gateway/locks.py` | 160 | 平台资源锁、stale lock、hermes lock/status 兼容检查 |
| `src/gateway/doctor.py` | 210 | 配置、端口、凭证、路径、平台依赖、平台锁检查 |
| `src/gateway/service.py` | 260 | ServiceSpec、ServiceManager、Windows Task Scheduler manager |
| `src/gateway/status.py` | 80 | runtime status 写入与读取 |
| `src/gateway/platforms/__init__.py` | 5 | 包声明 |
| `src/gateway/platforms/wecom_webhook.py` | 300 | 多 app callback adapter |
| `src/gateway/platforms/wecom_crypto.py` | 220 | 从 hermes 精简复制 WXBizMsgCrypt |
| `src/gateway/platforms/weixin_ilink.py` | 650 | iLink client + adapter + debounce/limit/chunk |
| `src/gateway/platforms/ilink_protocol.py` | 180 | iLink payload normalization helpers |
| `src/session/service.py` | +20 | completed/failed event payload |
| **合计** | **约 3060 行** | 包含 P0c service 管理和 P0d 平台资源锁；比 v1.1 多，但边界更接近 hermes 实战 |

v1.1 的 1500 行估算偏乐观。按 hermes 边界补齐 adapter 状态、delivery、iLink debounce、显式自启动管理和平台账号锁后，首版更现实的范围是 3000-3100 行。仍然远小于 hermes 网关体量。

### 15.1 新增依赖

```toml
[project.optional-dependencies]
gateway = [
    "fastapi>=0.110",
    "uvicorn>=0.27",
    "httpx>=0.27",
    "aiohttp>=3.9",
    "pycryptodome>=3.20",
    "pyyaml>=6.0",
    "qrcode>=7.4",
    "defusedxml>=0.7",
]
```

---

## 16. 验收标准

### 16.1 P0a 基础层

- [ ] `build_session_key()` 覆盖 DM、group per-user、group shared、thread shared。
- [ ] `SessionService` 的 `attempt.completed` 事件携带 `content/run_dir`。
- [ ] `attempt.failed` 事件携带 `error/run_dir`。
- [ ] `wait_for_attempt()` 只响应目标 `attempt_id`。
- [ ] attempt 在 subscribe 建立前已完成时，能从 store 兜底返回。
- [ ] 同一 session 连续两条消息串行执行。
- [ ] `deliver_final_response()` 能按 adapter max length 拆分。

### 16.2 P0b 企业微信

- [ ] GET URL 验证成功。
- [ ] POST 加密 XML 能解密为 `MessageEvent`。
- [ ] webhook 5 秒内返回 `success`。
- [ ] 重放同一 `MsgId` 不产生第二次 agent run。
- [ ] 多 app 配置下，`corp_id:user_id` 不冲突。
- [ ] 企微私聊“你好”，30 秒内收到 agent 回复。
- [ ] access token 缓存命中，过期前刷新。

### 16.3 P0c 安装与自启动

- [ ] `python gateway.py doctor` 能输出配置、端口、路径、凭证、日志目录检查结果。
- [ ] doctor 发现 fatal 项时，`service install` 拒绝注册。
- [ ] `service install` 在 Windows 当前用户下创建 Task Scheduler 任务。
- [ ] 计划任务 action 使用固定 Python 路径、固定 cwd 和显式 config 路径。
- [ ] `service start` 能启动 gateway。
- [ ] `service status` 能显示 installed/running/pid/config/log/last_error/enabled_platforms。
- [ ] 用户重新登录后 gateway 自动启动。
- [ ] `service stop` 能停止 gateway。
- [ ] `service uninstall` 删除自启动项，但不删除 `data_dir`、凭证、session、日志。
- [ ] 普通 `pip install`、`python gateway.py run`、`python gateway.py doctor` 不会注册自启动。

### 16.4 P0d 平台账号锁

- [ ] 两个 mini-agent 进程使用同一个 iLink token 时，第二个进程不发起 poll，并报告锁占用者。
- [ ] 两个 mini-agent 进程配置同一个企微 `corp_id:agent_id` 时，第二个进程拒绝启动对应 adapter。
- [ ] 锁文件和日志不包含明文 `bot_token`、`corp_secret`、`encoding_aes_key`。
- [ ] stale lock 只在 pid 不存在时允许清理；活锁不被默认抢占。
- [ ] `doctor` 能显示 owner/pid/cwd/config_path/started_at。
- [ ] 配置 hermes lock/status 目录后，发现 hermes 已占用同一 token/app 时，mini-agent 直接启动被阻止。
- [ ] hermes 和 mini-agent 使用不同 token/app 时，可以同时运行。

### 16.5 P1a iLink 协议

- [ ] fixture 覆盖 QR 登录状态机。
- [ ] fixture 覆盖 `msgs/messages`、`get_updates_buf/sync_buf` 差异。
- [ ] `item_list/text_item` 能提取文本。
- [ ] `sendmessage` payload 包含 `AuthorizationType`、`Authorization`、`X-WECHAT-UIN`、`base_info`、`msg.context_token`。
- [ ] `errcode=-2` 映射为 rate limited。
- [ ] `errcode=-14` 映射为 session expired。

### 16.6 P1b 个人微信

- [ ] 首次启动能扫码登录并持久化凭证。
- [ ] 重启后使用缓存凭证。
- [ ] `get_updates_buf` 按账号持久化。
- [ ] 入站消息更新 peer `context_token`。
- [ ] 连续 3 秒内多条短文本合并为一个 agent turn。
- [ ] 出站长文本按 2000 字符左右拆分，并有发送间隔。
- [ ] 频率限制后 circuit breaker 生效。
- [ ] 微信里发“你好”，30 秒内收到 agent 回复。

### 16.7 P5 抽象验证

- [ ] 新增 Telegram adapter 不修改 `SessionSource`、`MessageEvent`、`build_session_key()`、`SessionService`。
- [ ] 只新增 adapter 实现、配置段和 adapter factory 注册。
- [ ] Telegram 与企微/微信走同一个 runner handler。

---

## 17. 未解决问题

| 编号 | 问题 | 当前倾向 |
|---|---|---|
| Q1 | P0 是否要支持企业微信群聊 | 不支持，只做私聊；群聊需要独立发送策略 |
| Q2 | 个人微信 QR 登录是否放在 adapter connect 还是单独 CLI | P1 可先放 connect；后续可拆 `python gateway.py login weixin` |
| Q3 | iLink 群能力是否可验收 | 不作为 P1 验收，默认 disabled |
| Q4 | 是否需要 SQLite 存 gateway routing state | P0 用 JSON 原子写；多账号/多平台增多后可迁移 SQLite |
| Q5 | P2 是否真的推流式 | 个人微信不编辑消息，P2 更适合 typing/progress；流式只给支持编辑的平台 |
| Q6 | access control 是否做 hermes pairing | 不做。P0 用 allowlist；pairing 是后续独立功能 |
| Q7 | DeliveryRouter 是否需要完整目标解析 | P0 不需要，只回原会话；cron/主动投递以后再扩 |
| Q8 | Windows 自启动是否升级为真正 Windows Service | P0 先用用户级 Task Scheduler；需要无登录运行时再评估 WinSW/NSSM/pywin32 |
| Q9 | hermes -> mini-agent 桥接协议是否做成 HTTP、MCP 还是本地队列 | 当前只定义共存边界；桥接模式放到后续独立设计 |

---

## 18. 附录：与 hermes 的对应关系

| hermes 路径 | mini-agent 对应路径 | 复用策略 |
|---|---|---|
| `gateway/platforms/base.py` | `src/gateway/base.py` | 吸收 `MessageEvent`、`SendResult`、adapter lifecycle/capabilities，删除复杂 UI/command hooks |
| `gateway/session.py` | `src/gateway/session_key.py` + `src/gateway/router.py` | 吸收 `SessionSource` 和 `build_session_key()` 思路，不复制 SQLite/expiry 全量实现 |
| `gateway/platforms/wecom_callback.py` | `src/gateway/platforms/wecom_webhook.py` | 保留多 app、queue、dedupe、GET verify、主动回复主线 |
| `gateway/platforms/wecom_crypto.py` | `src/gateway/platforms/wecom_crypto.py` | 协议固定，可精简复制 |
| `gateway/platforms/weixin.py` | `src/gateway/platforms/weixin_ilink.py` + `ilink_protocol.py` | 保留 token/context/sync/debounce/rate-limit/chunk 主线，砍掉媒体/sticker/voice 复杂度 |
| `gateway/delivery.py` | `src/gateway/delivery.py` | 只保留 final-only 回原会话与拆分 |
| `gateway/stream_consumer.py` | P2 参考 | P0/P1 不实现 |
| `gateway/run.py` | `src/gateway/runner.py` | 只保留 adapter lifecycle、session routing、turn orchestration |
| `gateway/platform_registry.py` | `src/gateway/adapters.py` 或 runner factory | P0 可手写，P5 前再抽象 |
