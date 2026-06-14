# Agent Core：ReAct 主循环与上下文管理

> 模块路径：`src/agent/`
> 核心文件：`loop.py` · `context.py` · `trace.py` · `frontmatter.py`
> 适用读者：希望理解 mini-agent 运行时机制的开发者

---

## 1. 模块概览

Agent Core 是整个 mini-agent 的「大脑」。它接收一条用户消息，驱动 LLM 与工具反复交互，直到模型给出最终答案或达到迭代上限。模块要同时解决三类问题：

1. **决策循环**：什么时候让模型调用工具，什么时候强制收尾？
2. **上下文膨胀**：长对话会撑爆 token 窗口，如何在不丢失关键信息的前提下压缩？
3. **可观测性**：每次循环都留下 trace，便于复盘和调试。

在整体架构中的位置（箭头表示数据流）：

```
                  +-----------------------+
   user_message ->|   ContextBuilder      |-> messages[]
                  |  (system + history +  |
                  |   user + recalled     |
                  |   memories)           |
                  +-----------+-----------+
                              |
                              v
   +--------------------------+--------------------------+
   |                     AgentLoop                       |
   |                                                     |
   |  +---------+    +-------------+    +-------------+  |
   |  | Layer1  | -> |   LLM call  | -> | tool batch  |  |
   |  | micro   |    | (streaming) |    | (parallel)  |  |
   |  +---------+    +-------------+    +------+------+  |
   |       ^              |                    |         |
   |       |              v                    v         |
   |  +----+----+   +-----------+      +---------------+ |
   |  | Layer2  |   | answer?   | yes  | TraceWriter   | |
   |  | collapse|   | no tool?  |--->  | (trace.jsonl) | |
   |  +---------+   +-----+-----+      +---------------+ |
   |       ^              | no                            |
   |       |              v                               |
   |  +----+----+   +-----------+                         |
   |  | Layer3-5|   | append    |                         |
   |  | compact |<- | tool_call |<------------------------+
   |  +---------+   | + result  |
   |                +-----------+
   +---------------------+-------------------------------+
                         |
                         v
                final answer + run_dir
```

---

## 2. 核心概念

### 2.1 ReAct（Reasoning + Acting）

ReAct 是一种把「思考」和「行动」交替进行的 agent 范式。每一轮：

1. LLM 先输出推理（thinking）和/或工具调用意图；
2. 框架执行工具，把结果回填到上下文；
3. LLM 基于新上下文继续推理。

mini-agent 的 ReAct 没有显式 `Thought:` 文本约定，而是利用 LLM 的 **tool calling** 能力——模型要么返回最终文本，要么返回 `tool_calls`，由 `AgentLoop` 决定下一步。

### 2.2 为什么需要工具批处理

模型常常一次性要求调用多个工具（例如「先读 A，再读 B，再写 C」）。如果串行执行，5 个 read 工具就要等 5 倍延迟。mini-agent 把连续的**只读工具**打包成一批，用线程池并行执行；写入工具则保持串行，避免竞态。

### 2.3 为什么需要 5 层上下文压缩

单层压缩无法兼顾「成本」和「保真」：

- 完全用 LLM 摘要：每次调用都花钱，且可能丢细节；
- 完全用规则裁剪：便宜但会丢关键参数和路径。

mini-agent 用 **渐进式策略**：先做最便宜的清理，再做零成本的折叠，最后才请 LLM 摘要；并且在多次压缩时复用上一次的摘要（迭代更新），而不是每次从头再来。

---

## 3. 代码精读

### 3.1 ReAct 主循环（`loop.py:247-380`）

一次循环的完整步骤伪代码：

```
run(user_message, history):
    init run_dir, trace, messages
    while iteration < max_iterations:
        1. 检查 cancel / 接近迭代上限时插入收尾提示
        2. Layer1 microcompact:  清理老旧 tool 结果
        3. 估算 token，超 COLLAPSE 阈值 -> Layer2 折叠
        4. 超 TOKEN_THRESHOLD   -> Layer3 auto_compact
        5. stream_chat(流式接收 thinking)
        6. if 无 tool_calls: 记录 answer，跳出
        7. 把 assistant 的 tool_calls 加入 messages
        8. _process_tool_calls:
           - 处理 compact 工具
           - 拦截重复调用
           - 批量 / 单个执行
        9. 若模型显式请求 compact -> 再触发 Layer3
    finalize status + 返回结果
```

#### 关键代码：循环顶部三层防护（`loop.py:282-300`）

```python
iteration += 1
remaining = self.max_iterations - iteration
warn_at = int(self.max_iterations * WARN_ITERATION_RATIO)

if iteration >= warn_at and remaining > 2:
    messages.append({"role": "user", "content": f"[SYSTEM] You have {remaining} iterations remaining..."})
    messages.append({"role": "assistant", "content": "Understood, I will now synthesize my answer."})

_microcompact(messages)                                   # Layer 1

tokens = estimate_tokens(messages)
if tokens > COLLAPSE_THRESHOLD:                           # Layer 2
    _context_collapse(messages)
    tokens = estimate_tokens(messages)

if tokens > TOKEN_THRESHOLD:                              # Layer 3
    logger.info(f"Auto compact triggered: {tokens} tokens > {TOKEN_THRESHOLD}")
    self._auto_compact(messages, run_dir, trace)
```

`WARN_ITERATION_RATIO = 0.6` 表示迭代到 60% 时就开始「软提醒」模型收尾，而不是直接硬切。

#### 关键代码：是否强制关闭工具调用（`loop.py:310-316`）

```python
force_no_tools = tool_call_count >= FORCE_ANSWER_THRESHOLD or remaining <= 1

response = self.llm.stream_chat(
    messages,
    tools=[] if force_no_tools else self.registry.get_definitions(),
    on_text_chunk=_on_text_chunk,
)
```

当工具调用累计 ≥ `FORCE_ANSWER_THRESHOLD = 8` 次或只剩 1 次迭代时，直接传入 `tools=[]`，模型物理上无法再调用工具，被迫产出答案。这是防止 agent 无限调用工具的「硬刹车」。

#### 关键代码：判断是否结束循环（`loop.py:323-327`）

```python
if not response.has_tool_calls:
    final_content = response.content or ""
    trace.write({"type": "answer", "iter": iteration, "content": final_content[:2000]})
    react_trace.append({"type": "answer", "content": final_content[:500]})
    break
```

只要模型这一轮没要求工具，就视为「最终回答」，立即跳出。这是 ReAct 的唯一正常退出条件。

### 3.2 工具调用处理与批处理（`loop.py:382-514`）

#### 步骤 1：过滤（`loop.py:395-413`）

```python
for tc in tool_calls:
    if tc.name == "compact":               # 手动 compact 工具，特殊处理
        compact_requested = True
        focus_topic = tc.arguments.get("focus_topic", "")
        ...
        continue

    tool_def = self.registry.get(tc.name)
    is_repeatable = tool_def.repeatable if tool_def else False
    if tc.name in self._called_ok and not is_repeatable:   # 去重
        ...
        continue

    to_execute.append(tc)
```

`_called_ok` 是一个 set，记录「已经成功过」的非可重复工具名。第二次再被调用时直接返回 skip 消息，避免模型重复搜索、重复读同一个文件。这是省 token、省时间的关键。

#### 步骤 2：分批（`loop.py:425-447`）

```python
def _batch_execute(self, tool_calls, ...):
    batches = []
    current_ro = []
    for tc in tool_calls:
        tool_def = self.registry.get(tc.name)
        if tool_def and tool_def.is_readonly:
            current_ro.append(tc)
        else:
            if current_ro:
                batches.append(("parallel", current_ro))
                current_ro = []
            batches.append(("serial", [tc]))
    if current_ro:
        batches.append(("parallel", current_ro))

    for mode, batch in batches:
        if mode == "parallel" and len(batch) > 1:
            self._execute_parallel(batch, ...)
        else:
            for tc in batch:
                self._execute_single(tc, ...)
```

策略非常清晰：**连续的只读工具 → 一批并行；遇到写工具 → 切断并行、单独串行**。这既最大化吞吐，又避免写操作之间的竞态。

#### 步骤 3：并行执行与容错（`loop.py:449-483`）

```python
TOOL_TIMEOUT = 120
with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(runnable), 8)) as pool:
    futures = [pool.submit(_run, item) for item in runnable]
    results = []
    for i, f in enumerate(futures):
        try:
            results.append(f.result(timeout=TOOL_TIMEOUT))
        except concurrent.futures.TimeoutError:
            results.append((tc, json.dumps({"status": "error", "error": f"...timed out after {TOOL_TIMEOUT}s"}), 0))
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        except Exception as exc:
            results.append((tc, json.dumps({"status": "error", "error": str(exc)}), 0))
```

值得注意的三点容错：

- **超时不杀死整个批次**：单个工具超时只占位返回 error JSON，其他工具继续；
- **Ctrl+C 优雅退出**：`KeyboardInterrupt` 时 `cancel_futures=True`，避免后台线程悬挂；
- **错误 JSON 化**：异常被包成 `{"status":"error","error":...}`，`_is_tool_success()`（`loop.py:192-199`）据此判断成败，决定是否把工具名加入 `_called_ok`。

### 3.3 五层上下文压缩策略详解

| 层 | 名称 | 触发条件 | 做什么 | 代码位置 |
|----|------|----------|--------|----------|
| L1 | microcompact | 每次循环开头 | 把最近 3 条之外的 tool 消息内容清成 `[cleared]` | `loop.py:53-60` |
| L2 | context collapse | `tokens > COLLAPSE_THRESHOLD (≈28000)` | 头尾保留，中间折叠成 `...[N chars collapsed]...` | `loop.py:63-75` |
| L3 | auto compact | `tokens > TOKEN_THRESHOLD (40000)` | LLM 结构化摘要，保留尾部 20000 token 原文 | `loop.py:516-585` |
| L4 | compact tool | 模型显式调用 `compact` 工具 | 转入 L3，并可带 `focus_topic` | `loop.py:396-401, 343-345` |
| L5 | iterative update | L3 第 2 次及以上触发 | 用 `_ITERATIVE_UPDATE_PROMPT` 在旧摘要上增量更新 | `loop.py:556-561` |

#### Layer 1 microcompact（`loop.py:53-60`）

```python
def _microcompact(messages: list) -> None:
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    if len(tool_msgs) <= KEEP_RECENT:
        return
    for msg in tool_msgs[:-KEEP_RECENT]:
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > 100:
            msg["content"] = "[cleared]"
```

`KEEP_RECENT = 3`：只保留最近 3 个工具结果完整可见。超过 100 字符的旧结果被替换为 `[cleared]`。零成本、每轮执行。

> 为什么是「替换内容」而不是「删除消息」？因为 OpenAI tool calling 协议要求 assistant 的 `tool_calls` 与 tool 结果一一对应，删除会导致 API 报错。所以只清空内容、保留消息骨架。

#### Layer 2 context collapse（`loop.py:63-75`）

```python
def _context_collapse(messages: list) -> None:
    if len(messages) <= COLLAPSE_PRESERVE_RECENT + 1:
        return
    for msg in messages[1:-COLLAPSE_PRESERVE_RECENT]:
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= COLLAPSE_TEXT_MIN:
            continue
        if content == "[cleared]":
            continue
        head = content[:COLLAPSE_HEAD]
        tail = content[-COLLAPSE_TAIL:]
        trimmed = len(content) - COLLAPSE_HEAD - COLLAPSE_TAIL
        msg["content"] = f"{head}\n\n...[{trimmed} chars collapsed]...\n\n{tail}"
```

阈值常量（`loop.py:38-42`）：

- `COLLAPSE_THRESHOLD = TOKEN_THRESHOLD * 0.7 ≈ 28000`：在硬阈值之前先折叠；
- `COLLAPSE_PRESERVE_RECENT = 6`：保留最近 6 条消息不动；
- `COLLAPSE_TEXT_MIN = 2400`：短于 2400 字符的不动；
- `COLLAPSE_HEAD = 900` / `COLLAPSE_TAIL = 500`：保留头 900 尾 500。

折叠不调用 LLM，零 token 成本，但保留了关键的开头（通常包含路径、错误信息）和结尾（最新状态）。

#### Layer 3 auto compact（`loop.py:516-585`）

这是最复杂的一层。完整流程：

```python
def _auto_compact(self, messages, run_dir, trace, focus_topic=""):
    # 1. 转存完整 transcript 到磁盘，作为「保底证据」
    transcript_path = run_dir / f"transcript_{int(_time.time())}.jsonl"
    with open(transcript_path, "w", ...) as f:
        for msg in messages:
            f.write(json.dumps(msg, ...) + "\n")

    system_msg = messages[0]
    body = messages[1:]

    # 2. 从尾部反向累计，保留 TAIL_TOKEN_BUDGET (20000) token 的最近消息
    accumulated = 0
    cut_idx = len(body)
    for i in range(len(body) - 1, -1, -1):
        ...
        if accumulated + msg_tokens > TAIL_TOKEN_BUDGET:
            cut_idx = i + 1
            break
        accumulated += msg_tokens
        cut_idx = i

    # 3. 避免 cut 落在 tool 消息上（破坏配对）
    while 0 < cut_idx < len(body) and body[cut_idx].get("role") == "tool":
        cut_idx += 1

    head = body[:cut_idx]   # 要被压缩的部分
    tail = body[cut_idx:]   # 原文保留的部分

    # 4. 构造摘要 prompt（首次或迭代更新）
    if self._previous_summary:
        prompt = _ITERATIVE_UPDATE_PROMPT.format(previous_summary=..., new_turns=..., focus_section=...)
    else:
        prompt = _STRUCTURED_SUMMARY_PROMPT.format(focus_section=...) + conv_text

    # 5. 调 LLM 生成摘要
    summary_resp = self.llm.chat([{"role": "user", "content": prompt}])
    summary = summary_resp.content or ""
    self._previous_summary = summary   # 记下来供下一次迭代更新用

    # 6. 重建 messages：system + 摘要 + (agent state) + tail
    compressed = f"[Conversation compressed — handoff summary. Transcript: {transcript_path}]\n\n{summary}"
    ...
    messages.clear()
    messages.append(system_msg)
    messages.append({"role": "user", "content": compressed})
    messages.append({"role": "assistant", "content": "Understood. Continuing from the summary."})
    messages.extend(tail)

    _fix_tool_pairs(messages)   # 修复压缩后可能出现的 tool 配对错乱
```

三个关键设计：

**(a) 尾部保护（Tail Protection）**：不把整段对话都喂给 LLM 摘要，而是把最近约 20000 token 的消息原封不动保留在 `tail` 里。这样即使摘要丢了一些细节，最近的工具结果（往往是当下最相关的）仍然完整。

**(b) Tool 配对修复 `_fix_tool_pairs`（`loop.py:78-119`）**：压缩后可能出现两种破坏——

- 孤儿 tool result（assistant 的 tool_call 在 head 里被压缩掉了，但 tool result 还在）；
- 缺失 tool result（assistant 的 tool_call 在 tail 里，但对应 result 在 head 里被压缩了）。

`_fix_tool_pairs` 做两件事：删除孤儿 result，并为缺失的 result 插入占位 stub `"[Result from earlier context — see summary above]"`。

**(c) 结构化摘要 prompt（`loop.py:122-165`）**：摘要必须按固定结构输出（Goal / Constraints / Progress / Key Decisions / Resolved Questions / Pending / Files / Remaining Work / Critical Context / Tools & Patterns）。这种结构化输出便于下一次迭代更新时 LLM 精确定位段落。

#### Layer 4 compact tool（`loop.py:396-401, 343-345`）

模型可以在 `tool_calls` 里直接调用名为 `compact` 的工具，并传 `focus_topic` 参数：

```python
for tc in tool_calls:
    if tc.name == "compact":
        compact_requested = True
        focus_topic = tc.arguments.get("focus_topic", "")
        messages.append(context.format_tool_result(tc.id, "compact", '{"status":"ok","message":"Compressing..."}'))
        trace.write({"type": "compact_requested", "iter": iteration})
        continue
```

随后在主循环里（`loop.py:343-345`）触发 L3，并把 `focus_topic` 透传：

```python
if compact_requested:
    logger.info("Manual compact triggered by model")
    self._auto_compact(messages, run_dir, trace, focus_topic=focus_topic)
```

`focus_topic` 会注入 `_FOCUS_SECTION`（`loop.py:167-171`），让 LLM 把 60-70% 的摘要预算花在指定主题上。这是给模型的「主动压缩 + 聚焦」能力。

#### Layer 5 iterative update（`loop.py:556-561`）

```python
if self._previous_summary:
    prompt = _ITERATIVE_UPDATE_PROMPT.format(
        previous_summary=self._previous_summary,
        new_turns=conv_text,
        focus_section=focus_section,
    )
else:
    prompt = _STRUCTURED_SUMMARY_PROMPT.format(focus_section=focus_section) + conv_text
```

`_previous_summary` 是 AgentLoop 实例属性，首次压缩后保存下来。后续压缩不再从零摘要，而是基于上一次的摘要做增量更新（`_ITERATIVE_UPDATE_PROMPT`，`loop.py:173-189`），规则包括：

- 保留旧摘要的全部信息；
- 把完成的 "In Progress" 项移到 "Done"；
- 把已答的问题移到 "Resolved Questions"。

这避免了每次都重新读完整对话，显著降低压缩成本，且让摘要随对话推进而越来越精准。

### 3.4 系统提示词动态拼装（`context.py`）

`ContextBuilder` 的核心职责是把工具、Skills、记忆、持久化记忆、当前时间等信息拼成 system prompt。

#### 提示词模板（`context.py:19-53`）

```python
_SYSTEM_PROMPT = """You are an intelligent agent with {skill_count} skills, {tool_count} tools, and persistent cross-session memory.

## Tools
{tool_descriptions}

## Skills (use load_skill to read full docs)
{skill_descriptions}

## State
{memory_summary}
...
{memory_section}
## Tool Usage Discipline (CRITICAL)
- After **3-5 tool calls**, you MUST stop and synthesize an answer from what you have gathered.
...
- **NEVER** call more than 10 tool calls total for a single user request.

## Current Date & Time
Today is {current_datetime}.
"""
```

注意提示词里硬编码了「Tool Usage Discipline」——这是另一种「软刹车」，在 prompt 层面约束模型不要无限制调用工具，与代码里的 `FORCE_ANSWER_THRESHOLD` 形成「软硬双保险」。

#### 拼装逻辑（`context.py:74-91`）

```python
def build_system_prompt(self, user_message: str = "") -> str:
    now = datetime.now()
    memory_section = ""
    if self._persistent_memory and self._persistent_memory.snapshot:
        memory_section = _MEMORY_SECTION.format(snapshot=self._persistent_memory.snapshot)

    return _SYSTEM_PROMPT.format(
        tool_count=len(self.registry._tools),
        skill_count=len(self.skills_loader.skills),
        tool_descriptions=self._format_tool_descriptions(),
        skill_descriptions=self.skills_loader.get_descriptions(),
        memory_summary=self.memory.to_summary(),
        memory_section=memory_section,
        current_datetime=now.strftime("%A, %B %d, %Y %H:%M (local)"),
    )
```

每一次构建 system prompt 都会重新计算 tool/skill 数量、刷新 WorkspaceMemory 摘要、注入当前时间。这意味着运行期间动态注册的工具会在下一轮被模型看到。

#### 持久化记忆自动召回（`context.py:100-114`）

```python
enriched = user_message
if self._persistent_memory:
    try:
        recalls = self._persistent_memory.find_relevant(user_message, max_results=3)
        if recalls:
            lines = [f"- **{r.title}** ({r.memory_type}): {r.body[:500]}" for r in recalls]
            recall_block = "\n".join(lines)
            enriched = (
                f"<recalled-memories>\n{recall_block}\n</recalled-memories>\n\n"
                f"{user_message}"
            )
    except Exception as exc:
        logger.debug("Auto-recall failed: %s", exc)

messages.append({"role": "user", "content": enriched})
```

用户消息发出去之前，先用 `find_relevant` 在持久化记忆库里检索 top-3 相关条目，包成 `<recalled-memories>` 块插到用户消息前面。这样模型在第一轮就能「想起」跨会话的偏好和历史决策。失败时静默降级（只 debug log），不影响主流程。

#### 工具描述格式化（`context.py:117-128`）

```python
def _format_tool_descriptions(self) -> str:
    lines = []
    for tool in self.registry._tools.values():
        params = tool.parameters.get("properties", {})
        required = tool.parameters.get("required", [])
        param_parts = []
        for pname, pschema in params.items():
            req = " (required)" if pname in required else ""
            param_parts.append(f"    - {pname}: {pschema.get('description', ...)}{req}")
        param_text = "\n".join(param_parts) if param_parts else "    (no params)"
        lines.append(f"### {tool.name}\n{tool.description}\n  Params:\n{param_text}")
    return "\n\n".join(lines)
```

把 JSON Schema 转成人类/LLM 都易读的 markdown，并标注 required。这是「提示词工程」的细活。

### 3.5 Trace 写入（`trace.py`）

`TraceWriter` 极简：一个文件句柄，每次 `write` 一行 JSON 后 `flush()`，保证崩溃不丢数据。

```python
class TraceWriter:
    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / "trace.jsonl"
        self._file = open(self.path, "a", encoding="utf-8")

    def write(self, entry: Dict[str, Any]) -> None:
        if "ts" not in entry:
            entry["ts"] = time.time()
        self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._file.flush()
```

主循环里写入的 trace 事件类型汇总：

| type | 触发时机 | 关键字段 | 代码位置 |
|------|----------|----------|----------|
| `start` | run 开始 | `prompt` (前 500 字) | `loop.py:269` |
| `cancelled` | 用户取消 | `iter` | `loop.py:278` |
| `thinking` | 每轮 thinking 结束 | `iter`, `content` (前 2000 字) | `loop.py:320` |
| `answer` | 模型给出最终答案 | `iter`, `content` (前 2000 字) | `loop.py:325` |
| `tool_call` | 工具调用前 | `iter`, `tool`, `args` | `loop.py:455, 490` |
| `tool_result` | 工具执行后 | `iter`, `tool`, `status`, `elapsed_ms`, `preview` | `loop.py:512` |
| `tool_skipped` | 重复调用被拦截 | `iter`, `tool` | `loop.py:409` |
| `compact_requested` | 模型调用 compact 工具 | `iter` | `loop.py:400` |
| `compact` | auto_compact 完成 | `tokens_before`, `summary`, `focus_topic` | `loop.py:570` |
| `end` | run 结束 | `status`, `iterations` 或 `reason` | `loop.py:349, 371` |

用途：

- **调试**：`TraceWriter.read(run_dir)` 可回放整次 run；
- **性能分析**：`elapsed_ms` 字段可用于找慢工具；
- **成本审计**：`compact` 事件记录了每次压缩前 token 数，可统计压缩频率；
- **行为审计**：`tool_skipped` 用于发现模型是否有重复调用倾向。

---

## 4. 关键类与方法清单

### `AgentLoop`（`loop.py:222`）

| 方法 | 职责 | 关键参数 |
|------|------|----------|
| `__init__` | 注入 registry / llm / memory / persistent_memory | `max_iterations=50` |
| `run(user_message, history, session_id)` | 执行一次完整 ReAct 会话 | history 可选，用于续聊 |
| `cancel()` | 异步取消（设置 `_cancelled` flag） | — |
| `_process_tool_calls` | 过滤 + 分发工具调用 | 返回 `(compact_requested, focus_topic)` |
| `_batch_execute` | 按只读/写入分批 | — |
| `_execute_parallel` | 线程池并行执行 | `max_workers=min(n,8)`, `TOOL_TIMEOUT=120s` |
| `_execute_single` | 串行执行单个工具 | — |
| `_finalize_tool_result` | 写入 messages + trace + 更新 `_called_ok` | — |
| `_auto_compact` | L3+L5 压缩 | `focus_topic` 可选 |
| `_update_memory` | 工具调用计数 +1 | — |

### `ContextBuilder`（`context.py:63`）

| 方法 | 职责 | 关键参数 |
|------|------|----------|
| `__init__` | 注入 registry / memory / skills / persistent_memory | — |
| `build_system_prompt` | 动态拼装 system prompt | `user_message` 占位 |
| `build_messages` | 构造完整 messages 列表（含记忆召回） | history 可选 |
| `_format_tool_descriptions` | JSON Schema → markdown | — |
| `format_tool_result` (静态) | 构造 tool role 消息 | `tool_call_id`, `result` |
| `format_assistant_tool_calls` (静态) | 构造带 tool_calls 的 assistant 消息 | `reasoning_content` 可选 |

### `TraceWriter`（`trace.py:14`）

| 方法 | 职责 | 关键参数 |
|------|------|----------|
| `__init__` | 打开 trace.jsonl（append 模式） | `run_dir` |
| `write(entry)` | 写一行 JSON + flush | 自动补 `ts` |
| `close()` | 关闭文件 | — |
| `read(run_dir)` (静态) | 读回整份 trace | 容错跳过坏行 |

### `parse_frontmatter`（`frontmatter.py:9`）

| 函数 | 职责 | 返回 |
|------|------|------|
| `parse_frontmatter(text)` | 解析 `---` 分隔的 YAML-like 元数据 | `(meta_dict, body_str)` |

支持 string / list (`[a, b]`) / boolean 三种值类型。无 frontmatter 时返回 `({}, text)`。

---

## 5. 学习要点

读完本模块，你应该掌握：

1. **ReAct 循环的本质**：模型要么输出答案（结束），要么输出 tool_calls（继续）。框架据此驱动循环，没有第三种分支。
2. **三层「刹车」机制**：prompt 层的 Tool Usage Discipline（软提醒）、迭代比例 warn（中提醒）、`FORCE_ANSWER_THRESHOLD` + `tools=[]`（硬切断）。它们从不同维度防止 agent 无限循环。
3. **工具批处理的只读判定**：通过 `is_readonly` 属性区分可并行与必须串行的工具，平衡吞吐和安全。
4. **重复调用拦截**：`_called_ok` 集合 + `repeatable` 标志，对非可重复工具的二次调用直接 skip，是省 token 的关键。
5. **五层压缩的层次设计**：从最便宜（清字符串）到最贵（调 LLM），每一层有独立阈值，避免一刀切。L1/L2 零 LLM 成本，L3 才花钱。
6. **Tail Protection**：auto_compact 不会把整段对话都喂给 LLM 摘要，而是保留最近 20000 token 原文，兼顾压缩率和保真。
7. **Tool 配对修复**：`_fix_tool_pairs` 是 OpenAI tool calling 协议的「兜底」——压缩后必然出现配对错乱，必须主动修复。
8. **Trace 是可观测性的基石**：每个关键事件都落盘，且 `flush()` 保证崩溃安全，是事后复盘和性能调优的唯一依据。

---

## 6. 思考题

1. **为什么压缩要分 5 层而不是 1 层？** 如果只用 L3（LLM 摘要）会发生什么？提示：从成本、延迟、保真三个角度分析，并考虑「对话只有 30000 token 时是否值得调 LLM」。

2. **工具批处理失败时如何「回滚」？** 阅读 `_execute_parallel`（`loop.py:449-483`），思考：如果批次中 3 个工具里 1 个超时、1 个抛异常，另外 1 个成功，messages 里会出现什么？模型下一次会看到什么？是否存在部分失败导致状态不一致的风险？

3. **`_called_ok` 集合会不会误伤？** 假设模型第一次调用 `read_file` 读 A 文件成功，第二次想读 B 文件，会被拦截吗？阅读 `loop.py:404-411`，思考 `repeatable` 标志的设计意图，以及哪些工具应该设 `repeatable=True`。

4. **iterative update（L5）丢失信息的风险？** 如果第一次摘要遗漏了某个关键文件路径，第二次 iterative update 会把它补回来吗？阅读 `_ITERATIVE_UPDATE_PROMPT` 的规则，思考「摘要的摘要」是否会随轮次增加而失真，以及如何缓解。

5. **`force_no_tools` 的时机选择是否合理？** 当前是 `tool_call_count >= 8` 或 `remaining <= 1` 时清空 tools。如果某个任务确实需要 12 次工具调用才能完成（例如多步搜索 + 多文件编辑），这个阈值会不会过早触发？你会如何改进？提示：考虑让阈值可配置，或基于任务复杂度动态判断。

---

## 7. 延伸阅读

本模块是 mini-agent 的运行时核心，但要完整理解整个系统，还需要阅读：

- **`src/tools/`**：每个工具的实现，重点看 `BaseTool` 的 `is_readonly` 和 `repeatable` 属性如何被批处理逻辑消费。`compact_tool.py` 是 L4 的工具入口。
- **`src/agent/skills.py` + `src/agent/frontmatter.py`**：Skills 渐进式披露机制，理解 `load_skill` 工具如何按需加载详细文档，避免 system prompt 膨胀。
- **`src/providers/`**：`ChatLLM.stream_chat` 的 streaming 协议，以及 `has_tool_calls`、`reasoning_content` 等字段如何被主循环消费。多 provider 适配（OpenAI / Anthropic / 本地模型）也在这里。
- **`src/memory/persistent.py`**：跨会话持久化记忆，理解 `find_relevant` 的检索逻辑，以及 `snapshot` 如何被注入 system prompt。
- **`src/core/state.py`**：`RunStateStore` 如何管理 run_dir 的创建和状态标记（success / failure / cancelled）。

> 下游模块文档：
> - `02-tools-and-skills.md`：工具注册、Skills 渐进式披露
> - `03-context-compression.md`：五层压缩策略的深度对比
> - `04-providers.md`：多 LLM provider 适配
