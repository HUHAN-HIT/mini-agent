# Mini-Agent

A minimal, extensible ReAct agent framework. Includes MCP server, Skills system, and persistent Memory.

## Features

- **ReAct Agent Loop** with 5-layer context compression (microcompact, context collapse, auto-compact, compact tool, iterative update)
- **Tool Registry** with auto-discovery via `BaseTool.__subclasses__()`
- **Progressive Disclosure Skills** — summaries in system prompt, full docs on demand
- **Persistent Memory** — file-based cross-session memory with keyword scoring
- **MCP Server** — expose tools to Claude Desktop, Cursor, and any MCP client
- **Session Layer** — filesystem persistence with SSE event bus and SQLite FTS5 search
- **IM Gateway** — bring the agent to WeCom (企业微信) and WeChat (个人微信 iLink) chats, with multi-app callback, long polling, per-session turn serialization, and platform-account single-owner locks
- **10+ LLM Providers via OpenAI-compatible endpoints** — OpenAI, DeepSeek, Zhipu, Moonshot, Qwen, Gemini, Ollama, OpenRouter, Groq, MiniMax, and custom

## Quick Start

```bash
# 1. Install dependencies
pip install -e .

# 2. Configure your LLM provider
cp .env.example .env
# Edit .env with your API key and provider

# 3a. Run interactive CLI
python cli.py

# 3b. Run MCP server (for Claude Desktop, Cursor, etc.)
python mcp_server.py

# 3c. Run MCP server with SSE transport
python mcp_server.py --transport sse --port 8900

# 3d. Run IM gateway (WeCom / WeChat)
pip install -e ".[gateway]"
# 在 .env 里填好平台凭据（见下方 IM Gateway 一节），然后：
python gateway.py init            # 从 .env 自动生成 gateway.yaml
python gateway.py doctor          # check config + env
python gateway.py run             # foreground
```

## IM Gateway

The gateway lets you chat with the agent from WeCom (企业微信) and WeChat (个人微信 via iLink). It is a thin control plane on top of the existing `SessionService` / `AgentLoop`: adapters normalize inbound messages, the runner serializes per-session agent turns, and the final answer is sent back through the originating adapter. See [`docs/im-gateway-design.md`](docs/im-gateway-design.md) for the full design.

### Install

```bash
pip install -e ".[gateway]"      # adds httpx, aiohttp, pycryptodome, defusedxml
```

FastAPI and uvicorn are already required by the base project. The gateway command line is exposed via `gateway.py` (or `mini-agent-gateway` after install).

### Configure（只需填 `.env`，无需手写 yaml）

凭据集中放在 **`.env`**（和 LLM 配置同一个文件），`gateway.py init` 会据此自动生成 `gateway.yaml`——**某平台的必填项全部填好后，该平台自动启用**，留空则保持关闭。

```bash
# 1) 在 .env 里填凭据（.env.example 已带 IM Gateway 段，取消注释填值即可）

# WeCom 企业微信 —— 自建应用，凭据来自 https://work.weixin.qq.com/
WECOM_CORP_ID=ww...
WECOM_AGENT_ID=1000002
WECOM_SECRET=...
WECOM_TOKEN=...
WECOM_AES_KEY=...            # WeCom 控制台 43 位
WECOM_ALLOW_FROM=            # 可选，逗号分隔的 UserID 白名单（留空=允许所有人）

# WeChat 个人微信（iLink / ClawBot）—— 来自你的 bot 控制台
WEIXIN_ACCOUNT_ID=xxx@im.bot
WEIXIN_TOKEN=ilinkbot_xxx
WEIXIN_DM_POLICY=allowlist   # disabled | allowlist | open
WEIXIN_ALLOW_FROM=peerA,peerB  # allowlist 下的私聊白名单（逗号分隔）

# 网关通用（均有默认值，可不填）
# GATEWAY_HOST=0.0.0.0
# GATEWAY_PORT=8645
# GATEWAY_DATA_DIR=~/.mini-agent/gateway
```

```bash
# 2) 生成 gateway.yaml（自动判断启用哪些平台）
python gateway.py init
# 改了 .env 想重新生成：覆盖旧文件
python gateway.py init --force

# 可选参数：
#   --output PATH   指定输出路径（默认 gateway.yaml）
#   --env PATH      指定 .env 路径（默认按 ~/.mini-agent/.env → 项目 .env → ./.env 顺序查找）
```

`init` 会打印检测到的启用平台，并提示下一步运行 `doctor`。

> 进阶：需要多企业微信 app、自定义限流等 yaml 才能表达的细粒度配置时，可在 `init` 生成后手动编辑 `gateway.yaml`，或参考 `gateway.yaml.example`。

### Run in foreground

```bash
python gateway.py doctor              # 先校验 config + env
python gateway.py run --config gateway.yaml
```

WeCom uses an HTTPS callback (`POST /wecom/callback`). Point your WeCom app's "接收消息" URL to `https://<your-host>:8645/wecom/callback`. WeChat iLink runs long-polling from inside the gateway — no inbound port needed.

### Validate before going live

```bash
python gateway.py doctor              # human-readable
python gateway.py doctor --json       # for CI / scripts
```

Doctor checks Python path, working dir, data dir, port availability, logging dir, WeCom app fields, WeChat credentials, platform account locks, and Windows Task Scheduler availability. Any `fatal` check blocks `service install`.

### Login WeChat (iLink) — 扫码即配置

```bash
python gateway.py login weixin
```

无需在 `.env` 里填任何微信账号。命令会拉取一个登录二维码并在终端渲染（同时打印可点击的链接），用微信扫码并在手机上确认后，服务端**自动回传** `account_id` + `bot_token`：

1. 凭据写入 `~/.mini-agent/gateway/weixin_credentials.json`；
2. `gateway.yaml` 中 `platforms.weixin.enabled` 自动置为 `true`；
3. 直接 `python gateway.py run` 即可启动，无需手动编辑配置。

终端二维码需要 `qrcode` 包（含在 `pip install -e ".[gateway]"` 里）；若未安装，仍可打开打印出的链接扫码。整个扫码状态机（wait → scaned → confirmed）的 transport 是可注入的，已有单测覆盖（`tests/test_gateway_core.py`），无需真连 Tencent。

### System autostart (Windows Task Scheduler)

```bash
python gateway.py service install     # registers a user logon task (no admin)
python gateway.py service start
python gateway.py service status
python gateway.py service stop
python gateway.py service uninstall   # keeps data_dir, creds, sessions, logs
```

- `service install` requires a passing `doctor` first.
- The task uses a fixed Python path, working dir, and config path so it survives project moves.
- `pip install`, `gateway.py run`, and `gateway.py doctor` **never** register autostart — install is explicit only.
- Runtime state is mirrored to `~/.mini-agent/gateway/status.json` so `service status` works even if the OS service layer is unresponsive.

### Single-owner platform locks

WeCom `corp_id:agent_id` and WeChat iLink `bot_token` cannot be polled by two processes at once (they'd advance each other's sync cursor and corrupt context_token caches). The gateway writes a lock file under `~/.mini-agent/gateway/locks/<scope>/<identity>.json` before each adapter connects:

- **WeChat token** → identity is the short SHA-256 of the token (raw token never written down).
- **WeCom app** → identity is `corp_id:agent_id` (not secret).
- A held lock whose `pid` is still alive blocks the second adapter with a `fatal` error.
- A stale lock (pid dead) can be cleaned with explicit `force_stale_lock=True` (no live-lock stealing).
- To detect overlap with hermes, set `locks.check_hermes: true` and `locks.hermes_lock_dir` to where hermes stores its locks.

### What's stored where

| Path | Contents |
|------|----------|
| `~/.mini-agent/gateway/sessions/` | Mini-agent sessions created per chat (one per `corp_id:user_id` / peer id) |
| `~/.mini-agent/gateway/sessions_map.json` | Stable gateway session key → mini-agent session_id map |
| `~/.mini-agent/gateway/locks/` | Platform account single-owner locks |
| `~/.mini-agent/gateway/weixin_credentials.json` | WeChat iLink bot credentials |
| `~/.mini-agent/gateway/weixin_context_tokens.json` | Per-peer context_token cache (restart-resilient) |
| `~/.mini-agent/gateway/weixin_cursor.json` | Long-poll cursor (get_updates_buf) |
| `~/.mini-agent/gateway/gateway.log` | Foreground log |
| `~/.mini-agent/gateway/logs/service.log` | Service wrapper log |
| `~/.mini-agent/gateway/status.json` | Runtime status (pid / host / port / last_error) |

### Limits (P0 / P1)

- Text DM only. Group routing exists at the session-key level but groups default off for WeChat (`group_policy: "disabled"`).
- Final-only delivery: long answers are chunked to the platform's max length (WeCom 4000, WeChat 2000). No streaming edit, no half-message.
- No media inbound/outbound yet (P3 / P4 in the design doc).
- The same WeCom app or WeChat token cannot be used directly by both hermes and this gateway at the same time — use separate accounts or the upstream bridge pattern.


## Project Structure

```
mini-agent/
├── src/
│   ├── agent/           # Core agent loop, context, tools, skills
│   │   ├── loop.py      # ReAct loop with tool batching
│   │   ├── context.py   # System prompt builder
│   │   ├── tools.py     # BaseTool ABC + ToolRegistry
│   │   ├── skills.py    # SkillsLoader with progressive disclosure
│   │   ├── memory.py    # WorkspaceMemory (single-run state)
│   │   ├── trace.py     # JSONL trace writer
│   │   └── frontmatter.py
│   ├── providers/       # LLM providers
│   │   ├── llm.py       # Factory for OpenAI-compatible providers
│   │   └── chat.py      # ChatLLM with stream/chat/async
│   ├── tools/           # Built-in tools (auto-discovered)
│   │   ├── read_file_tool.py
│   │   ├── write_file_tool.py
│   │   ├── edit_file_tool.py
│   │   ├── bash_tool.py
│   │   ├── web_search_tool.py
│   │   ├── web_reader_tool.py
│   │   ├── compact_tool.py
│   │   ├── load_skill_tool.py
│   │   ├── remember_tool.py
│   │   └── skill_writer_tool.py
│   ├── memory/          # Persistent cross-session memory
│   │   └── persistent.py
│   ├── core/            # Infrastructure
│   │   └── state.py     # RunStateStore
│   ├── session/         # Session management (optional)
│   │   ├── models.py
│   │   ├── store.py
│   │   ├── service.py
│   │   ├── events.py
│   │   └── search.py
│   └── gateway/         # IM gateway (WeCom / WeChat)
│       ├── base.py              # SessionSource / MessageEvent / adapter contract
│       ├── session_key.py       # build_session_key() routing rules
│       ├── router.py            # session_key -> mini session_id
│       ├── turn_queue.py        # per-session serialization
│       ├── delivery.py          # final-only chunked delivery
│       ├── config.py            # YAML + ${VAR} + ~/ expansion
│       ├── adapters.py          # adapter factory + lock acquire
│       ├── runner.py            # GatewayRunner lifecycle
│       ├── locks.py             # platform-account single-owner locks
│       ├── doctor.py            # pre-install validation
│       ├── service.py           # Windows Task Scheduler manager
│       ├── status.py            # runtime status file
│       └── platforms/
│           ├── wecom_webhook.py # WeCom callback adapter
│           ├── wecom_crypto.py  # WeCom AES-CBC + signature
│           ├── weixin_ilink.py  # WeChat iLink adapter
│           ├── ilink_protocol.py# iLink payload normalization
│           └── _utils.py        # TTL dedup / rate-limit / debouncer
├── skills/              # Skill documents (Markdown + YAML frontmatter)
│   └── example/SKILL.md
├── runs/                # Run output directories
├── mcp_server.py        # MCP server entry point
├── cli.py               # Interactive CLI entry point
├── gateway.py           # IM gateway entry point (run/doctor/login/service)
├── gateway.yaml.example # gateway config template
├── pyproject.toml
└── .env.example
```

## Adding Custom Tools

Create a new file in `src/tools/`:

```python
# src/tools/my_tool.py
from src.agent.tools import BaseTool

class MyTool(BaseTool):
    name = "my_tool"
    description = "Does something useful"

    def execute(self, **kwargs) -> str:
        return "result"
```

It will be auto-discovered and registered.

## Adding Custom Skills

Create a directory under `skills/`:

```markdown
<!-- skills/my-skill/SKILL.md -->
---
name: my-skill
description: What this skill does
triggers:
  - keyword
---

# My Skill
Full documentation here...
```

User skills can also go in `~/.mini-agent/skills/user/`.

## MCP Client Configuration

### Claude Desktop

```json
{
  "mcpServers": {
    "mini-agent": {
      "command": "python",
      "args": ["/path/to/mini-agent/mcp_server.py"]
    }
  }
}
```

### Cursor

Add to your Cursor MCP settings with the command `python /path/to/mini-agent/mcp_server.py`.

## Storage

All data is stored under `~/.mini-agent/`:

| Path | Contents |
|------|----------|
| `~/.mini-agent/.env` | API keys and provider config |
| `~/.mini-agent/memory/` | Persistent memories |
| `~/.mini-agent/skills/user/` | User-created skills |
| `~/.mini-agent/sessions.db` | Session search index |
| `~/.mini-agent/gateway/` | IM gateway state, credentials, locks, logs |

## License

MIT
