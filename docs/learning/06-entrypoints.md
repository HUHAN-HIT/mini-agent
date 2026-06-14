# 06 — Entry Points & Infrastructure（入口与基础设施）

> 本模块讲解 mini-agent 的两个对外入口（CLI、MCP Server）以及它们依赖的最底层基础设施（`RunStateStore`、`pyproject.toml`）。
> 阅读完本章，你将能够：独立启动 mini-agent 的两种用法；把它接入 Claude Desktop / Cursor；理解一次 agent run 是如何在磁盘上留下证据的。

---

## 1. 模块概览

mini-agent 对外暴露 **两套入口**，面向截然不同的两类用户：

| 入口 | 文件 | 面向谁 | 交互形式 | 调用栈深度 |
|------|------|--------|----------|------------|
| **CLI** | `cli.py` | 开发者（人） | 终端 REPL（read-eval-print loop） | 浅，直接 `AgentLoop.run()` |
| **MCP Server** | `mcp_server.py` | AI 客户端（Claude Desktop、Cursor 等） | JSON-RPC over stdio / SSE | 深，经 FastMCP 框架分发 |

两者的本质区别：

- **CLI** 是「**宿主即 agent**」——CLI 进程自己拥有 `AgentLoop`、自己驱动 ReAct 循环，用户只是在终端里和它对话。
- **MCP Server** 是「**宿主是别人，agent 只是工具箱**」——Claude Desktop 才是真正的 agent，mini-agent 只是把 `bash`、`web_search`、`read_file` 等工具一个个**暴露**出去，由客户端决定何时调用。

```
                         ┌───────────────────────────┐
   开发者 (人)            │   cli.py  (CLI 入口)       │
       │                 │   ─ 持有 AgentLoop          │
       └── 终端输入 ────► │   ─ 驱动 ReAct 循环         │
                         │   ─ 自己跑完整个对话        │
                         └────────────┬──────────────┘
                                      │
                                      ▼
                         ┌───────────────────────────┐
                         │     src/agent/loop.py      │
                         │     (AgentLoop — ReAct)    │
                         │     src/providers/chat.py  │
                         │     (ChatLLM — 调 LLM)     │
                         │     src/tools/* (10+ 工具) │
                         └───────────────────────────┘

   AI 客户端               ┌───────────────────────────┐
   (Claude Desktop /      │  mcp_server.py (MCP 入口)  │
    Cursor / 远程脚本)     │  ─ 不持有 AgentLoop         │
       │                  │  ─ 只把工具逐个 @mcp.tool   │
       └── JSON-RPC ────► │    暴露给客户端             │
            (stdio/SSE)   └────────────┬──────────────┘
                                      │ 每次工具调用
                                      ▼
                         ┌───────────────────────────┐
                         │  src/tools/* (复用同一批)   │
                         │  src/agent/skills.py        │
                         │  src/memory/persistent.py   │
                         └───────────────────────────┘
```

一句话总结：**CLI 把你变成 agent 的用户，MCP 把 mini-agent 变成别人 agent 的工具。**

---

## 2. CLI 入口精读（`cli.py`）

整个文件只有 **77 行**，是一个教科书式的「薄入口」。它做的事情可以拆成 4 段。

### 2.1 把自己加进 `sys.path`（`cli.py:10-12`）

```python
AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))
```

这一步保证 **无论从哪个工作目录启动** `python cli.py`，`src.*` 这种包内导入都能解析到。这是脚本式 Python 项目（不强制 `pip install`）的常见 trick。

### 2.2 装配组件（`cli.py:15-35`）

`main()` 函数里依次 new 出 5 个对象，组装成一个 `AgentLoop`：

```python
pm = PersistentMemory()                                   # 持久记忆（跨 session）
llm = ChatLLM()                                           # LLM 调用器（读 .env）
registry = build_registry(persistent_memory=pm)           # 工具注册表（自动发现）
skills_loader = SkillsLoader()                            # Skills 加载器

agent = AgentLoop(
    registry=registry,
    llm=llm,
    max_iterations=50,                                    # ReAct 循环上限
    persistent_memory=pm,
)
```

注意 `build_registry(persistent_memory=pm)`（`cli.py:27`）——`RememberTool` 需要拿到 `pm` 句柄才能写记忆库，所以这里要把 `pm` 传进去。`src/tools/__init__.py:42` 的 `build_registry` 内部对 `RememberTool` 做了特殊注入。

### 2.3 REPL 循环 + 命令分发（`cli.py:39-59`）

进入 `while True` 死循环，先读一行输入，再用一连串 `if` 做**斜杠命令**分发：

| 输入 | 行为 | 代码位置 |
|------|------|----------|
| `EOF` / `Ctrl+C` | 打印 `Goodbye!` 后退出 | `cli.py:42-44` |
| 空行 | 忽略，重新读 | `cli.py:46-47` |
| `/quit` `/exit` `/q` | 退出 | `cli.py:48-50` |
| `/skills` | 列出所有 skill 的 name + description | `cli.py:51-54` |
| `/help` | 打印命令帮助 | `cli.py:55-59` |
| 其他 | 当作 user message 喂给 agent | `cli.py:61` |

斜杠命令是**本地拦截**的，不会进 LLM——这是 REPL 的典型设计。

### 2.4 调用 agent + 展示结果（`cli.py:61-73`）

```python
result = agent.run(user_message=user_input, history=history or None)
status = result.get("status", "unknown")          # "success" / "failed" / ...
content = result.get("content", "")               # 最终回复文本

history.append({"role": "user", "content": user_input})
history.append({"role": "assistant", "content": content})

print(f"\nAgent [{status}]:")
print(content)
print()

if run_dir := result.get("run_dir"):
    print(f"Run dir: {run_dir}\n")
```

**关键观察**：

1. **没有用流式输出**——`agent.run()` 是同步阻塞调用，返回时 ReAct 循环已经全部跑完。CLI 只看到最终结果。如果你想看中间的 tool call，得去 `run_dir/logs/trace.jsonl` 翻。
2. **`history` 是 in-memory 的**——一个 Python list，进程退出就丢。多轮上下文靠它，但跨 session 不持久（跨 session 用 `/remember` 工具）。
3. **`run_dir` 总会被打印**——这是 mini-agent 的「证据链」理念：每一次 run 都在 `runs/YYYYMMDD_HHMMSS_xxxxxx/` 留下完整快照（见第 5 节）。

> 思考：为什么 CLI 不做流式？因为 `AgentLoop.run` 内部已经是多轮 tool-calling，每轮都是一次 LLM 调用 + 一次工具执行，"流式"在 agent 层语义模糊（流式什么？token？tool call？）。CLI 选择最简单的「跑完再打印」。

---

## 3. MCP Server 入口精读（`mcp_server.py`）— 重点

### 3.1 MCP 协议简介

**MCP（Model Context Protocol）** 是 Anthropic 在 2024 年底开源的一个**开放协议**，解决的核心问题是：

> LLM 应用（如 Claude Desktop）想用外部工具（读文件、查数据库、调 API），但每个工具的接入方式都不一样。能不能有一个**统一标准**？

你可以把 MCP 类比为 **编辑器界的 LSP（Language Server Protocol）**：

| | LSP | MCP |
|---|---|---|
| 解决问题 | 编辑器 ↔ 语言服务（补全/跳转/诊断）统一接口 | LLM 客户端 ↔ 工具/数据源 统一接口 |
| 通信 | JSON-RPC over stdio / websocket | JSON-RPC over stdio / SSE / websocket |
| 服务端叫 | Language Server | MCP Server |
| 客户端叫 | Editor（VSCode、Vim…） | MCP Client（Claude Desktop、Cursor…） |

只要 mini-agent 实现成 MCP Server，**任何 MCP 客户端都能零成本接入**它的工具——这就是写 `mcp_server.py` 的全部价值。

### 3.2 FastMCP 框架

mini-agent 用的是 [`fastmcp`](https://github.com/jlowin/fastmcp)（`pyproject.toml:16` 依赖），一个把 MCP 协议封装得极简的 Python 库。整个 server 的核心就两行（`mcp_server.py:31-33`）：

```python
from fastmcp import FastMCP
mcp = FastMCP("Mini-Agent")
```

然后每暴露一个工具，就写一个 `@mcp.tool` 装饰的函数。FastMCP 会自动：
- 把函数签名转成 JSON Schema（参数名、类型、描述）发给客户端；
- 接收客户端的 tool call 请求，反序列化参数，调用你的函数，把返回值序列化回去。

**你完全不用手写协议层代码。**

### 3.3 暴露的工具清单

`mcp_server.py` 一共暴露了 **9 个工具**，分成 5 组：

| 分组 | 工具名 | 行号 | 说明 |
|------|--------|------|------|
| **Skill** | `list_skills` | `mcp_server.py:59-68` | 列出所有 skill 的 name + description |
| | `load_skill` | `mcp_server.py:71-85` | 加载某个 skill 的完整 markdown |
| **Web** | `read_url` | `mcp_server.py:92-103` | 抓网页转 markdown |
| | `web_search` | `mcp_server.py:106-120` | DuckDuckGo 搜索 |
| **File I/O** | `write_file` | `mcp_server.py:127-136` | 写文件 |
| | `read_file` | `mcp_server.py:139-147` | 读文件 |
| | `edit_file` | `mcp_server.py:150-160` | 字符串替换式编辑 |
| **Shell** | `bash` | `mcp_server.py:167-176` | 执行 shell 命令 |
| **Memory** | `remember` | `mcp_server.py:183-193` | 持久记忆的 save / recall / forget |

#### 暴露的两种模式

仔细看代码，你会发现工具实现有两种风格：

**风格 A：直接调用底层模块**（如 `read_url`）

```python
@mcp.tool
def read_url(url: str) -> str:
    from src.tools.web_reader_tool import read_url as _read_url
    return _read_url(url)
```

直接 import 函数并调用，绕过 ToolRegistry。

**风格 B：通过 `registry.execute()` 走完整工具栈**（如 `web_search`、`bash`、`edit_file`、`remember`）

```python
@mcp.tool
def web_search(query: str, max_results: int = 5) -> str:
    registry = _get_registry()
    return registry.execute("web_search", {"query": query, "max_results": min(max_results, 10)})
```

走 `ToolRegistry.execute()`，**统一经过 trace 记录、错误处理、超时控制**。

> 实践建议：未来扩展 MCP 工具时优先用 **风格 B**，因为 registry 层的包装是 mini-agent 工具调用的「正规路径」，能自动获得 trace 日志。

### 3.4 懒加载（Lazy Initialization）

注意 `mcp_server.py:35-52` 的两个全局变量 + getter：

```python
_skills_loader = None
_registry = None

def _get_skills_loader():
    global _skills_loader
    if _skills_loader is None:
        from src.agent.skills import SkillsLoader
        _skills_loader = SkillsLoader()
    return _skills_loader
```

**为什么不用模块顶层 import？** 因为 MCP server 启动时（`mcp.run()`）会立刻进入 JSON-RPC 监听循环。如果顶层就 `import SkillsLoader`，会**在客户端还没调任何工具之前**就把 skill 全扫一遍、把 registry 全建一遍——启动变慢、内存白白占用。

懒加载的好处：**只有客户端真正调用某工具时，才初始化对应的重型对象**。这是 server 类代码的常见优化。

### 3.5 两种 Transport

`mcp_server.py:200-214` 的 `main()`：

```python
parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
parser.add_argument("--port", type=int, default=8900)

if args.transport == "sse":
    mcp.run(transport="sse", port=args.port)
else:
    mcp.run()              # 默认 stdio
```

| Transport | 适用场景 | 通信方式 | 启动命令 |
|-----------|----------|----------|----------|
| **stdio**（默认） | 本地客户端（Claude Desktop、Cursor） | 客户端 fork server 进程，通过 stdin/stdout 收发 JSON-RPC | `python mcp_server.py` |
| **SSE** | 远程客户端、Web 集成、多客户端共享一个 server | server 监听 HTTP 端口，客户端通过 Server-Sent Events 订阅 | `python mcp_server.py --transport sse --port 8900` |

**stdio 的优势**：
- 零网络配置，客户端启动即用；
- 进程隔离，每个客户端一个独立 server 实例；
- 适合「客户端在本机」的场景（典型如 Claude Desktop）。

**SSE 的优势**：
- 支持远程访问（server 在云端，客户端在本地）；
- 一个 server 服务多个客户端；
- 适合团队共享、Web 应用集成。

> 经验法则：**本地用 stdio，远程用 SSE**。

---

## 4. 客户端接入示例

### 4.1 Claude Desktop（stdio）

编辑 Claude Desktop 的配置文件（macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`；Windows: `%APPDATA%\Claude\claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "mini-agent": {
      "command": "python",
      "args": ["E:/03_个人项目归档/mini-agent/mcp_server.py"]
    }
  }
}
```

重启 Claude Desktop 后，对话框左下角的工具图标里会出现 mini-agent 的 9 个工具。

### 4.2 Cursor（stdio）

Cursor 的 MCP 配置在 `~/.cursor/mcp.json`，格式相同：

```json
{
  "mcpServers": {
    "mini-agent": {
      "command": "python",
      "args": ["E:/03_个人项目归档/mini-agent/mcp_server.py"]
    }
  }
}
```

或者用 Cursor 的 UI：`Settings → MCP → Add new MCP server`。

### 4.3 远程 SSE 接入

服务端启动：

```bash
python mcp_server.py --transport sse --port 8900
```

客户端配置（任何支持 SSE 的 MCP 客户端）：

```json
{
  "mcpServers": {
    "mini-agent-remote": {
      "url": "http://your-server-ip:8900/sse"
    }
  }
}
```

> 注意：SSE 模式目前**没有鉴权**，生产环境务必放在内网或加 reverse proxy + auth。

---

## 5. RunStateStore 基础设施（`src/core/state.py`）

这是 mini-agent 最底层的「**取证**」组件——每一次 agent run 都会在磁盘上留下一个完整的目录，方便事后复盘、debug、回放。

### 5.1 目录结构

`src/core/state.py:15-23` 的 `create_run_dir`：

```python
def create_run_dir(self, workspace: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:18]
    suffix = uuid.uuid4().hex[:6]
    run_dir = workspace / f"{timestamp}_{suffix}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "code").mkdir(exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    (run_dir / "artifacts").mkdir(exist_ok=True)
    return run_dir
```

`workspace` 在 `AgentLoop.run` 里被设为项目根的 `runs/`（`src/agent/loop.py:31`：`RUNS_DIR = Path(__file__).resolve().parents[2] / "runs"`）。每次 run 创建的目录形如：

```
runs/
└── 20260613_143052_a1b2c3/         ← 时间戳 + uuid 后缀（防同秒冲突）
    ├── code/                        ← 工具产生的代码片段
    ├── logs/                        ← trace.jsonl 在这里
    ├── artifacts/                   ← 工具输出的二进制产物
    ├── req.json                     ← 入参快照（prompt + context）
    └── state.json                   ← 终态（success / failed）
```

**时间戳格式** `%Y%m%d_%H%M%S_%f` 截到 18 位（`[:18]`），即「年月日_时分秒_微秒前3位」，再用 `uuid.uuid4().hex[:6]` 加 6 位随机后缀——**同秒并发也不会冲突**。

### 5.2 写入策略

| 方法 | 写什么 | 写到哪 | 代码 |
|------|--------|--------|------|
| `save_request` | 入参（prompt + context） | `req.json` | `state.py:25-28` |
| `mark_success` | `{"status": "success"}` | `state.json` | `state.py:30-31` |
| `mark_failure` | `{"status": "failed", "reason": ...}` | `state.json` | `state.py:33-34` |

底层统一走 `_write_json`（`state.py:36-38`）：`json.dumps(..., ensure_ascii=False, indent=2)`——**UTF-8 + 缩进 2 空格**，方便人眼直接读。

### 5.3 trace.jsonl 在哪写？

注意 `state.py` **没有**写 trace 的方法——trace 是 `src/agent/trace.py` 的 `TraceWriter` 直接往 `run_dir/logs/trace.jsonl` 追加写的（每行一个 JSON 事件：LLM 请求、tool call、tool result……）。`RunStateStore` 只管「目录创建 + 入参/终态」，分工清晰。

### 5.4 调用链

`AgentLoop.run`（`src/agent/loop.py:247-262`）开头会：

```python
state_store = RunStateStore()
RUNS_DIR.mkdir(parents=True, exist_ok=True)
run_dir = state_store.create_run_dir(RUNS_DIR)
state_store.save_request(run_dir, user_message, {"session_id": session_id})
```

收尾时（成功 / 失败分支）调用 `mark_success` 或 `mark_failure`，并把 `run_dir` 放进返回字典（`loop.py:355` / `loop.py:376`）——这就是 CLI 里 `result.get("run_dir")`（`cli.py:72`）的来源。

### 5.5 状态查询接口

当前 `RunStateStore` 是**写多读少**——只提供写方法，没有 `list_runs` / `get_state` 这类查询接口。如果要做一个「runs 浏览器」，需要自己加：

```python
def list_runs(self, workspace: Path) -> list[Path]:
    return sorted(
        [p for p in workspace.iterdir() if p.is_dir()],
        reverse=True,  # 最新的在前
    )
```

这是一个很好的练手扩展点（见第 8 节思考题）。

---

## 6. 打包与发布（`pyproject.toml`）

### 6.1 构建系统（`pyproject.toml:1-3`）

```toml
[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.build_meta"
```

用最传统的 setuptools，没有上 Poetry / Hatch / PDM。够用就好。

### 6.2 依赖清单（`pyproject.toml:13-26`）

| 依赖 | 用途 |
|------|------|
| `langchain-openai` | `ChatLLM` 内部用 LangChain 的 OpenAI adapter |
| `openai` | 底层 SDK |
| `fastmcp` | MCP server 框架（第 3 节） |
| `fastapi` + `uvicorn` + `sse-starlette` | SSE transport 的底层 |
| `requests` | `web_reader_tool` 抓网页 |
| `python-dotenv` | 读 `.env` |
| `duckduckgo-search` | `web_search_tool`（免费、无 API key） |
| `pyyaml` | 解析 skill 的 YAML frontmatter |
| `beautifulsoup4` + `lxml` | 网页正文提取 |

开发依赖（`pyproject.toml:28-32`）只有 `pytest` 和 `ruff`。

### 6.3 entry_points（`pyproject.toml:34-35`）

```toml
[project.scripts]
mini-agent-mcp = "mcp_server:main"
```

这一行让 `pip install -e .` 后，shell 里会出现一个 `mini-agent-mcp` 命令，等价于 `python mcp_server.py`。注意 **CLI 入口没有注册** —— `cli.py` 必须用 `python cli.py` 启动。如果你想让 `mini-agent` 也变成命令，加一行：

```toml
mini-agent = "cli:main"
```

（这是个小遗留点，可能是作者故意只把 MCP 注册成命令。）

### 6.4 开发安装

```bash
pip install -e .          # editable 安装，改代码立即生效
pip install -e ".[dev]"   # 连开发依赖一起装
```

`-e`（editable）会在 site-packages 里放一个 `.pth` 文件指向你的源码目录，所以你改 `src/agent/loop.py` 后不需要重装。

### 6.5 包发现（`pyproject.toml:37-42`）

```toml
[tool.setuptools]
py-modules = ["cli", "mcp_server"]      # 顶层两个独立 .py 文件

[tool.setuptools.packages.find]
where = ["."]
include = ["src*"]                       # src 下所有子包
```

`cli.py` 和 `mcp_server.py` 在项目根，不在 `src/` 下，所以单独声明为 `py-modules`。

---

## 7. 关键类与方法清单

### CLI（`cli.py`）

| 符号 | 类型 | 行号 | 说明 |
|------|------|------|------|
| `main()` | func | `cli.py:15-73` | CLI 入口；装配组件 + REPL 循环 |

### MCP Server（`mcp_server.py`）

| 符号 | 类型 | 行号 | 说明 |
|------|------|------|------|
| `mcp` | `FastMCP` | `mcp_server.py:33` | server 实例 |
| `_get_skills_loader()` | func | `mcp_server.py:39-44` | 懒加载 SkillsLoader |
| `_get_registry()` | func | `mcp_server.py:47-52` | 懒加载 ToolRegistry |
| `list_skills()` | tool | `mcp_server.py:59-68` | MCP tool |
| `load_skill(name)` | tool | `mcp_server.py:71-85` | MCP tool |
| `read_url(url)` | tool | `mcp_server.py:92-103` | MCP tool |
| `web_search(query, max_results)` | tool | `mcp_server.py:106-120` | MCP tool |
| `write_file / read_file / edit_file` | tool | `mcp_server.py:127-160` | MCP tool × 3 |
| `bash(command, timeout)` | tool | `mcp_server.py:167-176` | MCP tool |
| `remember(action, content, query)` | tool | `mcp_server.py:183-193` | MCP tool |
| `main()` | func | `mcp_server.py:200-214` | 入口；解析 transport 参数 |

### RunStateStore（`src/core/state.py`）

| 符号 | 类型 | 行号 | 说明 |
|------|------|------|------|
| `create_run_dir(workspace)` | method | `state.py:15-23` | 创建带时间戳的 run 目录 |
| `save_request(run_dir, prompt, context)` | method | `state.py:25-28` | 写 `req.json` |
| `mark_success(run_dir)` | method | `state.py:30-31` | 写成功状态 |
| `mark_failure(run_dir, reason)` | method | `state.py:33-34` | 写失败状态 + 原因 |
| `_write_json(path, data)` | staticmethod | `state.py:36-38` | UTF-8 + indent=2 写 JSON |

---

## 8. 学习要点 + 思考题

### 学习要点

1. **「薄入口，厚内核」**——`cli.py` 只有 77 行，所有逻辑都在 `src/`。入口层只负责装配和 UI。这是易于测试、易于替换的设计。
2. **MCP ≠ 另一个 agent**——mini-agent 的 MCP server **不跑 ReAct 循环**，只暴露工具。客户端（Claude Desktop）才是 agent。理解这点能避免对架构的误解。
3. **懒加载是 server 的基本修养**——`mcp_server.py:39-52` 的双重检查 null 模式值得背下来。
4. **「每次 run 留痕」**——`RunStateStore` 强制每次 agent 调用都有磁盘证据。这是 debug agent 系统的关键武器。
5. **stdio vs SSE 的选型**——本地默认 stdio，远程才上 SSE，不要无脑开 SSE。
6. **`pyproject.toml` 的 `[project.scripts]`** 决定了哪些 Python 函数会变成 shell 命令——理解 entry_points 对发布 Python 工具至关重要。

### 思考题

1. **（架构）** 如果你只想让 Claude Desktop 用 mini-agent 的 `web_search` 工具，但**不想**让它能跑 `bash`，你会怎么改 `mcp_server.py`？提示：考虑装饰器是否执行、考虑把工具列表做成配置。
2. **（取证）** 一次 agent run 失败了，你只有 shell 访问权限。请描述你会按什么顺序看哪些文件，定位失败原因。（提示：`state.json` → `req.json` → `logs/trace.jsonl`）
3. **（懒加载）** `mcp_server.py:39-52` 的懒加载不是线程安全的——如果 FastMCP 在多线程里并发调用工具，`_registry is None` 检查可能竞争。你会如何修复？（提示：`threading.Lock` 或 `functools.lru_cache`）
4. **（扩展）** `RunStateStore` 没有 `list_runs()`。请设计一个方法，能列出最近 N 次 run，并按状态过滤。考虑：目录命名能反解出时间吗？怎么读 `state.json` 而不打开整个文件？
5. **（对比）** 比较 CLI 入口和 MCP 入口对 `history` 的处理：CLI 把 history 存在内存 list（`cli.py:37,65-66`），MCP 入口完全不维护 history。为什么？这种差异对「多轮对话」语义有什么影响？

---

## 9. 延伸阅读

本模块只讲了「入口与基础设施」。要理解入口背后真正干活的东西，继续读：

- **`src/agent/loop.py`** —— AgentLoop 是 mini-agent 的心脏。ReAct 循环、工具批处理、5 层上下文压缩都在这里。建议结合 `runs/*/logs/trace.jsonl` 一起读，对照「代码」和「真实事件流」。
- **`src/providers/chat.py` + `src/providers/llm.py`** —— ChatLLM 如何把 10+ 个 OpenAI 兼容 provider（OpenAI、DeepSeek、Zhipu、Moonshot、Qwen、Gemini、Ollama、OpenRouter、Groq、MiniMax）统一成一个接口。factory 模式的优秀范例。
- **`src/agent/tools.py`** —— `BaseTool` ABC + `ToolRegistry`。理解了它，你就理解了为什么 `mcp_server.py` 能用 `registry.execute("bash", {...})` 一行调用任意工具。
- **`src/agent/skills.py`** —— Progressive Disclosure 的实现。`list_skills` + `load_skill` 这两个 MCP 工具背后的引擎。

读完这四个，你就掌握了 mini-agent 的 80%。
