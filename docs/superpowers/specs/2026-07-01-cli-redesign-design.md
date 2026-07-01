# mini-agent CLI 改造设计（美化 + 优雅安装）

- 日期：2026-07-01
- 状态：待实现（spec 已批准）
- 参照工程：Vibe-Trading（`E:/01_大模型经典学习项目/Vibe-Trading/agent/cli/`）

## 1. 背景与目标

当前 mini-agent 的交互 CLI（根目录 `cli.py`）用原始 `print()` + 手写 `====` 分隔线渲染，无
`rich`/`prompt_toolkit`；且 `pyproject.toml` 只注册了 `mini-agent-mcp` / `mini-agent-gateway`
两个命令，**主 CLI 没有入口点**，只能 `python cli.py` 启动（[docs/learning/06-entrypoints.md](../../learning/06-entrypoints.md)
自己已标注这是"小遗留点"）。

目标：**全面对齐 Vibe-Trading 的 CLI 分层**，让 mini-agent 拥有一个美观、可安装的交互终端。

两条主线：

1. **美观**：基于 `rich` + `prompt_toolkit`，做品牌 banner、Markdown 回复渲染、瞬态思考
   spinner、dexter 风格工具事件行、斜杠命令补全、持久历史、底部工具栏。
2. **优雅安装**：新增 `mini-agent = "cli:main"` console script；uv 优先的安装/启动工作流；
   首次运行 onboarding 向导自动写 `.env`。

### 设计原则

- **薄入口厚内核**：保留 mini-agent 哲学。UI 逻辑全部进 `src/cli/` 包，根 `cli.py` 退化为薄 shim。
- **全面对齐、按体量裁剪**：结构对齐 Vibe-Trading，但不照搬其 54KB `main.py` 与 8 文件命令包
  （mini-agent 只有 5 个斜杠命令）。
- **复用现有基础设施**：`AgentLoop` 的 `event_callback` 流式事件、`PersistentMemory`、
  `SkillsLoader`、`ChatLLM`、`registry`、`DelegateTool/TeamTool`，以及
  `src/providers/llm.py` 的 `_PROVIDER_MAP`（provider 唯一真源）。

## 2. 目录结构

```
mini-agent/
├── cli.py                    # 薄 shim：from src.cli.app import main（保留 `python cli.py`）
└── src/cli/
    ├── __init__.py           # 导出 main + 向后兼容再导出
    ├── app.py                # main() 前门：UTF-8 修复 → 装配 → onboarding → banner → REPL
    ├── theme.py              # Rich 样式表 + 品牌紫 + 深浅色检测 + 单例 Console
    ├── banner.py             # ASCII 字标 "mini-agent" 渐变 + 元信息行
    ├── stream.py             # StreamRenderer + ThinkingSpinner（对接 AgentLoop 事件）
    ├── input.py              # prompt_toolkit 输入：持久历史 + 补全 + 键位 + 底部工具栏
    ├── completer.py          # SlashCompleter 斜杠命令模糊补全
    ├── commands.py           # 斜杠命令注册表 + 处理器（单文件）
    └── onboard.py            # 首次运行向导（选 provider → 填 model → 粘 key → 写 .env）
```

**为何放 `src/cli/` + 根 shim，而非根 `cli/` 包**：

1. 保留 `python cli.py` 向后兼容，学习文档不失效；
2. UI 属于"厚内核"，放 `src/` 更自洽；
3. `cli:main` 入口点仍能解析（根 shim 暴露 `main`），与 `mini-agent = "cli:main"` 对齐；
4. 根 `cli.py`（模块）与 `src.cli`（包）名字不同，无冲突。

## 3. 各模块职责

### 3.1 theme.py

移植自 Vibe-Trading `cli/theme.py`，替换品牌色为紫色：

| 场景 | dark 终端 | light 终端 |
|------|-----------|-----------|
| `primary`（品牌字标 / agent 身份） | `#a78bfa` | `#7c3aed` |
| `primary_dim`（次要/思考中） | `#8b5cf6` | `#6d28d9` |
| `success` | `#16a34a` | 同 |
| `danger` | `#dc2626` | 同 |
| `warning`（工具运行中） | `#d97706` | 同 |
| `info` | `#0891b2` | 同 |
| `muted` | `#9ca3af` | `#737373` |

保留：`NO_COLOR` 支持、深浅色检测（`MINI_AGENT_THEME` 覆盖 + `COLORFGBG` + 兜底 dark）、
`emoji=False`（项目规则无 emoji）、Windows `legacy_windows=False`、单例 `get_console()`。

### 3.2 banner.py

渐变 ASCII 字标 "mini-agent"（紫色渐变），下方一行元信息：
`mini-agent v<version> · <provider> · <model> · skills:N`。非 TTY 时降级为单行纯文本。

### 3.3 stream.py

`ThinkingSpinner`（瞬态 `rich.Live`，`transient=True`，`pause()` 上下文管理器停/复防 ANSI 交错）
+ `StreamRenderer`，对接 AgentLoop **现有事件契约**：

| 事件 | payload 关键字段 | 渲染 |
|------|-----------------|------|
| `thinking_delta` | `delta` | 思考流（`primary_dim`）|
| `text_delta` | `delta` | 回复流 |
| `thinking_done` | — | 结束思考块 |
| `tool_call` | `tool`, `arguments` | 记录开始 |
| `tool_result` | `tool`, `status`, `elapsed_ms`, `preview` | `● Tool Name (args)  时长 · 摘要` |
| `compact` | `tokens_before` | `[compact] triggered at N tokens` |
| `cache_stats` | `ratio`, `cached`, `prompt` | `format_cache_stats_line` |

工具名美化（`beautify_tool_name`：`web_search` → `Web Search`，acronym 白名单大写）、参数摘要
（`summarize_args`：优先 query/url/prompt，截断）、最终回复用 `rich.markdown.Markdown` 渲染。
**保留** `format_cache_stats_line`，格式不变：`[cache: <cached>K/<prompt>K cached, <pct>%]`，
`ratio is None` 时返回 `None`（静默）。

### 3.4 input.py

`prompt_toolkit.PromptSession`：

- 持久历史 `FileHistory(~/.mini-agent/history)`；
- `SlashCompleter` 补全；
- 键位：Enter 提交 / Ctrl+C 取消当前轮（不退出）/ Ctrl+D 退出；
- 底部工具栏显示 `provider · model · skills:N`；
- 提示符 `mini-agent>`（品牌紫）。

**非 TTY 降级**：`sys.stdin.isatty()` 为假时退回内置 `input()` 循环，跳过 banner/工具栏，
保证 `echo hi | mini-agent` 与自动化测试可用。

### 3.5 completer.py

`SlashCompleter`（移植 Vibe `completer.py`）：消费 `commands.py` 注册表，**仅**在行首为 `/`
且命令 token 未含空格时给补全；`/help <args>` 进入参数区后不再弹菜单。补全项名用品牌紫、
描述用 muted。

### 3.6 commands.py

单文件注册表（结构对齐 Vibe 的 `slash_router`，但不拆 8 文件）。命令集**保持现有 5 个**：

| 命令 | 别名 | 行为 |
|------|------|------|
| `/help` | — | 列出命令 |
| `/clear` | — | 清屏 + 重置内存历史 |
| `/history` | — | 显示近期对话轮次 |
| `/skills` | — | 列出已加载 skills |
| `/quit` | `/exit` `/q` | 退出 |

可选新增 `/version`。斜杠命令本地拦截，不进 LLM。

### 3.7 onboard.py

首次运行向导：

- **触发**：`app.main()` 启动时解析 `LANGCHAIN_PROVIDER` → 对应 key env；若 model 或 key 缺失
  （或 `build_llm` 抛 `LANGCHAIN_MODEL_NAME is not set`）→ 进向导。
- **步骤**：交互式选 provider（枚举自 `_PROVIDER_MAP`）→ 填 model（每 provider 给默认值：
  openai→`gpt-4o-mini`、deepseek→`deepseek-chat`、moonshot→`moonshot-v1-8k`、ollama→`llama3` 等）
  → **用户自己**粘贴 API key（`is_password=True` 掩码）→ 可选 base_url。
- **写入** `~/.mini-agent/.env`（正是 `_ENV_CANDIDATES[0]`，全局安装的 `mini-agent` 命令也生效）；
  dev 检出若已有项目 `.env` 则尊重之，仅追加缺失键。写完重载 env。
- **幂等**：已有可用配置时不触发；可用 `mini-agent`（首次）自然进入，或未来加 `--reconfigure`。

**安全**：API key 仅由用户键入并写入本地 `.env`（`0600` 权限），绝不外传、绝不由 agent 代填。

## 4. 打包与安装（uv 优先）

### 4.1 pyproject.toml 变更

```toml
[project]
dependencies = [
    # ... 现有 ...
    "rich>=13.0",
    "prompt_toolkit>=3.0",
]

[project.scripts]
mini-agent = "cli:main"          # 新增
mini-agent-mcp = "mcp_server:main"
mini-agent-gateway = "gateway:main"
```

`[tool.setuptools.packages.find] include = ["src*"]` 已覆盖 `src.cli`，无需改；
`py-modules = ["cli", "mcp_server", "gateway"]` 保留（根 `cli.py` shim 仍是顶层模块）。

### 4.2 README Quick Start 重写（uv 优先）

```bash
# 开发
uv sync
uv run mini-agent            # 首次运行进入 onboarding 向导

# 全局安装（得到 mini-agent 命令）
uv tool install .
mini-agent

# hacker 直跑（免安装）
python cli.py
```

修正 README 中所有 `python cli.py` 表述；更新 [06-entrypoints.md](../../learning/06-entrypoints.md)
的"入口未注册"遗留说明。

## 5. 错误处理与降级

- 非 TTY / `NO_COLOR` → rich 自动降级 + prompt_toolkit 退回 `input()`。
- spinner `pause()`/复位包裹静态打印，防 ANSI 交错（nanobot 教训）。
- 运行期缺 model/key → 捕获 `RuntimeError` 并路由到 onboarding。
- Windows UTF-8 重配置（`sys.stdout/stderr/stdin.reconfigure`）保留在 `app.py` 顶部。
- prompt_toolkit / rich 导入失败（极端环境）→ 退回纯 `input()` + `print()` 最小回退路径。

## 6. 向后兼容与测试

### 6.1 向后兼容

根 `cli.py` shim 与 `src/cli/__init__.py` 再导出以下**行为不变**的纯函数，保证旧测试绿：

- `format_cache_stats_line`（行为不变）
- `handle_builtin_command` / `CommandResult`（命令集不变）
- `format_help` / `format_history_summary`

`format_banner`（旧 ASCII 字符串）是**有意替换**为 rich banner → 对应旧断言迁移为对新渲染器
捕获输出的断言（见 6.2）。

### 6.2 测试（pytest，无网络，TDD 先红后绿）

- **theme**：`NO_COLOR` 强制纯样式；`MINI_AGENT_THEME=dark/light` 覆盖生效。
- **stream**：`beautify_tool_name` / `summarize_args` / 时长格式 / 工具行渲染（捕获 console）
  / `format_cache_stats_line`（移植现有 `test_cli_cache_line.py` 的 4 个用例，含 AgentLoop 端到端）。
- **completer**：`/` 触发、prose 不触发、含空格后停。
- **commands**：`/help /skills /history /clear /quit` 分发。
- **onboard**：给定伪输入写出正确 `.env` key、掩码、幂等、有配置时不触发（`tmp_path` +
  `monkeypatch HOME`）。
- **app**：非 TTY 降级路径；装配不崩（mock `ChatLLM`/`AgentLoop`）。
- 保留并适配现有 `tests/test_cli_terminal_ui.py`、`tests/test_cli_cache_line.py`。

## 7. YAGNI（不做）

- 不做 swarm 多 agent 网格 dashboard（Vibe 也是 stub）。
- 不做 onboarding 之外的 web/session 浏览器 UI。
- 命令不膨胀，保持现有 5 个（可选 `/version`）。

## 8. 交付验收

- `uv run mini-agent` 首次运行进向导、写 `~/.mini-agent/.env`、随后进入美化 REPL。
- 已配置环境下 `mini-agent` / `python cli.py` 均正常启动并渲染 banner + 流式 + 工具行。
- `echo "hi" | mini-agent` 走非 TTY 降级路径不崩。
- 全部新旧测试通过；`ruff` 干净。
