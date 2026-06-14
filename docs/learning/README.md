# Mini-Agent 学习文档

> 本目录由 6 个并行 agent 团队分模块调研生成，覆盖 mini-agent 框架的全部核心子系统。
> 文档面向**初次接触本项目的开发者**，目标是「读完即可动手扩展」。

---

## 项目一句话简介

mini-agent 是一个极简、可扩展的 **ReAct agent 框架**，内置 5 层上下文压缩、工具自动注册、Skills 渐进式披露、跨会话持久化记忆，并通过 OpenAI 兼容协议支持 10+ 个 LLM 提供方，同时可作为 MCP Server 被 Claude Desktop / Cursor 等客户端调用。

## 整体架构

```
                      ┌─────────────────────────────────────┐
                      │           Entry Points               │
                      │  ┌─────────┐      ┌──────────────┐  │
                      │  │  CLI    │      │  MCP Server  │  │
                      │  └────┬────┘      └──────┬───────┘  │
                      └───────┼──────────────────┼──────────┘
                              │                  │
                              ▼                  ▼
                      ┌─────────────────────────────────────┐
                      │         Agent Core (01)              │
                      │  ReAct Loop · Context · Trace        │
                      │  5 层上下文压缩                       │
                      └──┬──────────┬──────────┬─────────────┘
                         │          │          │
            ┌────────────▼┐  ┌──────▼─────┐  ┌─▼──────────────┐
            │ Tools (02)  │  │ Skills (03)│  │ Providers (04) │
            │ 自动注册     │  │ 渐进式披露  │  │ OpenAI 兼容    │
            └─────────────┘  └────────────┘  └────────────────┘
                         │          │          │
                         ▼          ▼          ▼
                      ┌─────────────────────────────────────┐
                      │      Session & Memory (05)           │
                      │  工作记忆 · 持久记忆 · FTS5 搜索      │
                      └─────────────────────────────────────┘
```

## 阅读路径

### 推荐顺序（按依赖关系）

| 顺序 | 文档 | 模块 | 核心问题 | 行数 |
|------|------|------|---------|------|
| 1️⃣ | [01-agent-core.md](./01-agent-core.md) | Agent Core | ReAct 循环如何跑？工具怎么批处理？5 层压缩何时触发？ | 627 |
| 2️⃣ | [02-tools.md](./02-tools.md) | Tool System | 工具如何零配置自动注册？如何加新工具？ | 580 |
| 3️⃣ | [03-skills.md](./03-skills.md) | Skills System | 什么是渐进式披露？Skill 文档怎么写？ | 651 |
| 4️⃣ | [04-providers.md](./04-providers.md) | LLM Providers | 如何抽象 10+ 个 LLM？怎么切换 provider？ | 472 |
| 5️⃣ | [05-session-memory.md](./05-session-memory.md) | Session & Memory | 三层记忆体系怎么协作？FTS5 全文检索怎么用？ | 817 |
| 6️⃣ | [06-entrypoints.md](./06-entrypoints.md) | Entry Points | CLI 和 MCP Server 怎么接入？RunStateStore 怎么取证？ | 536 |

### 按目标快速入口

| 你想做的事 | 直接看 |
|-----------|-------|
| 理解 agent 主循环 | [01-agent-core.md](./01-agent-core.md) |
| 加一个新工具 | [02-tools.md §5](./02-tools.md) |
| 写一个新 Skill | [03-skills.md §3](./03-skills.md) |
| 接入新的 LLM provider | [04-providers.md §5](./04-providers.md) |
| 理解持久化记忆检索 | [05-session-memory.md §3-4](./05-session-memory.md) |
| 把 mini-agent 接到 Claude Desktop | [06-entrypoints.md §4](./06-entrypoints.md) |
| 理解 5 层上下文压缩 | [01-agent-core.md §3.2](./01-agent-core.md) |

## 模块速览

### 1️⃣ Agent Core —— 框架心脏
- **入口**：`src/agent/loop.py`
- **核心机制**：ReAct 三层刹车（max_steps / 上下文上限 / 用户中断）、工具批处理（只读并行 / 写入串行）、5 层渐进式上下文压缩
- **亮点**：microcompact → context collapse → auto-compact → compact tool → iterative update，每层触发条件不同、代价递增

### 2️⃣ Tools —— 能力扩展点
- **入口**：`src/agent/tools.py` + `src/tools/`
- **核心机制**：`BaseTool.__subclasses__()` + `pkgutil` 自动发现，放入 `src/tools/` 即注册
- **亮点**：声明式 JSON Schema 描述参数；`execute()` 统一返回 JSON 字符串；ToolRegistry 兜底所有异常，使 loop 与工具层彻底解耦

### 3️⃣ Skills —— 知识渐进披露
- **入口**：`src/agent/skills.py` + `skills/`
- **核心机制**：系统提示词只放 skill 摘要，agent 显式调用 `load_skill` 才载入全文
- **亮点**：双目录扫描（user 覆盖 project）；`skill_writer_tool` 让 agent 把经验固化成 skill，形成自我进化闭环

### 4️⃣ Providers —— LLM 统一抽象
- **入口**：`src/providers/llm.py` + `chat.py`
- **核心机制**：基于 OpenAI-compatible 协议这一事实标准，用 `ChatOpenAI` 作统一客户端；`_PROVIDER_MAP` 做厂商环境变量翻译
- **亮点**：新增 provider 通常是 **0 代码、只改 `.env`**；`ChatLLM` 提供 `chat` / `stream_chat` / `achat` 三种模式

### 5️⃣ Session & Memory —— 状态与记忆
- **入口**：`src/agent/memory.py` + `src/memory/persistent.py` + `src/session/`
- **三层体系**：
  - **WorkspaceMemory**：单次 run 内的工作记忆
  - **PersistentMemory**：跨会话、文件 + frontmatter + 关键词评分检索
  - **Session Layer**：文件系统持久化 + SSE 事件总线 + SQLite FTS5 全文搜索
- **亮点**：关键词评分 vs 向量检索的工程取舍

### 6️⃣ Entry Points —— 对外接口
- **入口**：`cli.py` + `mcp_server.py`
- **双入口定位**：CLI 把你变成 agent 的用户；MCP 把 mini-agent 变成别人 agent 的工具
- **亮点**：MCP 支持 stdio（本地）和 SSE（远程）两种 transport；RunStateStore 在 `runs/YYYYMMDD_HHMMSS/` 留下完整取证链

## 关键设计决策一览

| 决策 | 选择 | 理由（见对应文档） |
|------|------|-------------------|
| Agent 范式 | ReAct（非 Plan-and-Execute） | 思考与行动交替，适配长任务，见 01 |
| 工具注册 | `__subclasses__()` 自动发现 | 零配置扩展，见 02 §3 |
| 知识载入 | Progressive Disclosure | 避免系统提示词爆炸，见 03 §2 |
| LLM 抽象 | OpenAI-compatible 协议 | 事实标准、生态最大，见 04 §2 |
| 记忆检索 | 关键词评分（非向量） | 轻量、可解释、无 embedding 依赖，见 05 §3 |
| 对外协议 | MCP（非自定义 RPC） | 复用 Claude / Cursor 生态，见 06 §3 |

## 学习检验

读完所有文档后，你应该能回答：

1. 一次 user message 进入系统后，经过了哪些组件、数据如何流动？
2. 5 层上下文压缩分别在什么条件下触发？为什么不全用最彻底的那层？
3. 工具系统为什么不需要手动注册？Python 元类机制是如何支撑的？
4. 渐进式披露相比「一次性塞满系统提示词」解决了什么问题？
5. 关键词评分检索相比向量检索的优劣？什么场景下应该升级？
6. CLI 与 MCP Server 共享多少代码？分叉点在哪里？

## 如何扩展 mini-agent

| 想加的东西 | 步骤 | 参考文档 |
|-----------|------|---------|
| 新工具 | 在 `src/tools/` 新建文件，继承 `BaseTool` | [02 §5](./02-tools.md) |
| 新 Skill | 在 `skills/` 或 `~/.mini-agent/skills/user/` 新建 `SKILL.md` | [03 §3](./03-skills.md) |
| 新 LLM Provider | 改 `.env`（多数情况）；非兼容协议才需写适配 | [04 §5](./04-providers.md) |
| 新 MCP 客户端 | 配置 `mcp_server.py` 的 command / transport | [06 §4](./06-entrypoints.md) |
| 新记忆类型 | 在 `src/memory/` 实现存储；接入 `remember_tool` | [05](./05-session-memory.md) |

---

*文档生成时间：2026-06-13 · 由 6 个并行 agent 协作产出*
