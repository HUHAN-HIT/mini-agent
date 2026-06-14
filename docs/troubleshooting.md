# mini-agent 故障排查与已知问题

记录工程中遇到的典型问题、根因分析与修复方案。新增问题请追加到末尾。

---

## 问题 1：Agent Loop 无限搜索循环

### 现象

用户提问"给我分析下当前就业市场 agent 开发的行情"，agent 持续调用 `web_search` 和 `read_url` 工具 50 次（达到 `max_iterations` 上限），最终因 `"loop did not produce output"` 失败。trace 显示相同查询被重复搜索 5+ 次。

### 根因

`src/agent/loop.py` 的 ReAct 循环缺少"停止搜索、开始回答"的机制：
1. **System prompt 没有指导模型何时收手** — 模型不知道迭代次数即将耗尽
2. **没有迭代倒计时提醒** — 模型无法感知剩余预算
3. **没有强制回答降级** — 达到上限直接失败，没有兜底

### 修复

三层防护（均在 `src/agent/loop.py` 和 `src/agent/context.py`）：

| 层 | 实现 | 位置 |
|---|---|---|
| **L1：搜索纪律** | system prompt 增加"Tool Usage Discipline"段，明确要求 3-5 次工具调用后必须合成回答 | `context.py` 的 `_SYSTEM_PROMPT` |
| **L2：迭代倒计时** | 迭代达到 60% (`WARN_ITERATION_RATIO`) 时注入系统消息提醒模型停止搜索 | `loop.py` 的 `run()` |
| **L3：强制回答** | 累计工具调用 ≥8 (`FORCE_ANSWER_THRESHOLD`) 或剩余迭代 ≤1 时，传 `tools=[]` 强制模型只能产出文本 | `loop.py` 的 `run()` |

### 验证

修复后跑相同 prompt，agent 在 5-8 次工具调用后主动产出最终回答，不再触发 `max_iterations` 上限。

---

## 问题 2：LangChain + glm-5.1 间歇性挂死

### 现象

`ChatLLM.chat()` / `stream_chat()` 偶发性挂死：90s+ 无响应，无异常抛出。但相同参数下一次调用可能 2.8s 正常返回。

### 诊断过程

| 测试 | 结果 |
|---|---|
| 直接 HTTP POST 智谱 API（urllib） | ✅ 1.3s 返回 200，正常 |
| OpenAI SDK 直连（绕过 LangChain） | ✅ 3.4s 返回 |
| LangChain `ChatOpenAI.invoke()` | ⚠️ 间歇性挂死 |

### 根因

**不是 LangChain bug，是 reasoning model 的特性**：
- glm-5.1 是 reasoning model（类似 OpenAI o1），输出前会先在 `reasoning_content` 字段里"思考"
- 思考阶段耗时不稳定，简单问题可能几秒，复杂问题可能 1-2 分钟
- HTTP probe 1.3s 返回的是简单问题；复杂问题或网络抖动时远超 90s

响应示例（注意 `content` 为空，思考在 `reasoning_content`）：
```json
{
  "choices": [{
    "finish_reason": "length",
    "message": {
      "content": "",
      "reasoning_content": "Let me consider how to respond to this greeting effectively..."
    }
  }]
}
```

### 应对方案

| 方案 | 操作 | 适用场景 |
|---|---|---|
| **A：增大超时** | `.env` 设 `TIMEOUT_SECONDS=300` | 需要高质量 reasoning，能等 |
| **B：关闭 reasoning** | `.env` 设 `LANGCHAIN_REASONING_EFFORT=low`（若 provider 支持） | 测试/调试，求速度 |
| **C：换非 reasoning 模型** | `LANGCHAIN_MODEL_NAME=glm-4` 或 `glm-4-flash` | 生产环境稳定输出 |
| **D：失败重试** | `MAX_RETRIES=3` + 较短超时 | 偶发抖动场景 |

### 注意事项

- `providers/llm.py` 的 `ChatOpenAIWithReasoning` 类已正确处理 `reasoning_content` 字段，无需改动
- `ChatLLM.stream_chat()` 内部 try/except 失败时 fallback 到 `chat()`，会导致单次调用耗时翻倍

---

## 问题 3：build_registry 自动发现失败

### 现象

启动时 stderr 出现：
```
Failed to register tool delegate: DelegateTool.__init__() missing 4 required positional arguments
Failed to register tool spawn_team: TeamTool.__init__() missing 4 required positional arguments
```

虽然后续手动注册成功（功能不受影响），但启动有噪声。

### 根因

`src/tools/__init__.py:54` 的 `build_registry()` 用 `cls()` 无参实例化所有 BaseTool 子类。但 `DelegateTool` / `TeamTool` 需要 parent 上下文（llm、registry、run_dir、ctx），无法走自动注册路径。

### 修复

在两个工具类中覆盖 `check_available()` 返回 False，让自动发现跳过：

```python
class DelegateTool(BaseTool):
    @classmethod
    def check_available(cls) -> bool:
        return False
```

然后在 `cli.py` 和 `src/session/service.py` 中手动注册：

```python
registry = build_registry(persistent_memory=pm)
parent_ctx = SubAgentContext(depth=0, parent_run_dir=RUNS_DIR, parent_session_id="cli")
registry.register(DelegateTool(llm, registry, RUNS_DIR, parent_ctx))
registry.register(TeamTool(llm, registry, RUNS_DIR, parent_ctx))
```

### 通用规则

**何时走自动注册**：工具是无状态的、构造函数无必需参数 → `check_available()` 默认 True。
**何时走手动注册**：工具需要外部依赖（LLM、registry、parent context） → 覆盖 `check_available()` 返回 False + 在入口手动注入。

---

## 问题 4：子 agent trace 与父 trace 合并

### 现象

测试 `SubAgentRunner.run()` 时发现：
- 父 `trace.jsonl` 包含所有事件（包括子 agent 的）
- 子 agent 的 `sub_run_dir` 目录是空的，没有 `trace.jsonl`

### 根因

`SubAgentRunner` 创建子 `WorkspaceMemory` 时把 `run_dir` 设为 `parent_run_dir`：

```python
sub_memory = WorkspaceMemory(run_dir=str(self._parent_run_dir))  # 错误
```

而 `AgentLoop.run()` 内部用 `memory.run_dir` 初始化 `TraceWriter`：

```python
trace = TraceWriter(run_dir)  # run_dir 来自 memory.run_dir
```

导致子 AgentLoop 的 TraceWriter 写到了父目录。

### 修复

把 `WorkspaceMemory.run_dir` 设为子专属目录：

```python
sub_run_dir = self._parent_run_dir / "subagents" / f"{config.role}_{int(time.time() * 1000)}"
sub_run_dir.mkdir(parents=True, exist_ok=True)
sub_memory = WorkspaceMemory(run_dir=str(sub_run_dir))  # 修正
```

### 副作用权衡

修复后子 agent 调用 `write_file` 等工具时，文件默认写到 `sub_run_dir`（而非父目录）。这其实是**正确行为**：每个子 agent 有独立的工作空间，避免互相覆盖。父 agent 通过 `result["subagent_run_dir"]` 可以定位子 agent 的产物。

如果需要让子 agent 写到父目录，子 agent 显式传绝对路径即可。

---

## 排查工具清单

遇到新问题时，按以下顺序诊断：

| 工具 | 用途 |
|---|---|
| `trace.jsonl` 检查 | 看 `runs/{ts}/trace.jsonl` 的 `type=error` 和 `type=end` 记录 |
| `faulthandler.dump_traceback()` | 捕获挂死时的全线程栈 |
| 直接 HTTP probe | 绕过 SDK，确认 provider 是否可用 |
| OpenAI SDK 直连 | 确认问题在 LangChain 层还是更底层 |
| `python -m py_compile` | 语法快速校验 |
| mock LLM 测试 | 不依赖真实 API 验证逻辑（见 `tests/test_mock_team.py`） |
