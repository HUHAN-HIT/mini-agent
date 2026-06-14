# Session & Memory：会话管理与持久化记忆

> 学习目标：读完本文档后，你应当能回答：
> 1. mini-agent 为什么需要「三层」记忆？职责边界在哪里？
> 2. 一条用户消息从进入到生成回复，在三层记忆中各留下什么痕迹？
> 3. 如何在不引入向量数据库的前提下，做出可用的语义检索？

---

## 1. 模块概览

### 1.1 为什么是三层

一个 agent 框架面临三类「遗忘」问题：

| 问题 | 例子 | 解决层 |
| --- | --- | --- |
| 一次 run 内，工具之间需要共享临时状态 | read_file 之后 edit_file 复用同一个 run_dir | **WorkspaceMemory** |
| 跨次会话，agent 应记住用户偏好与项目知识 | "用户偏好 TypeScript"、"项目用 pnpm" | **PersistentMemory** |
| 历史会话可检索，能根据关键词找到以前讨论的内容 | "上周那个 SQLite 报错是哪一次？" | **Session Layer + FTS5** |

mini-agent 用三套不重叠的子系统分别承担，避免「一套数据结构搞定所有」的反模式——后者要么性能差，要么强制引入向量库。

### 1.2 三层关系图

```
+--------------------------------------------------------------------------+
|                         一次对话的完整生命周期                            |
+--------------------------------------------------------------------------+

 用户输入                      +--------------------------+
  │                            |   WorkspaceMemory        |  ① 单次 run 内
  ▼                            |   (src/agent/memory.py)  |     进程内对象
 SessionService.send_message   +-----------+--------------+     随 loop 结束 GC
  │                                        │
  ├─> Attempt ───> AgentLoop.run() ────────┘
  │                    │
  │                    ├─> ContextBuilder.build_messages()
  │                    │      ├─ 注入 WorkspaceMemory.to_summary()
  │                    │      ├─ 注入 PersistentMemory.snapshot
  │                    │      └─ find_relevant(user_msg) 命中条目 prepend
  │                    │
  │                    ├─> 工具调用（read/write/bash/remember…）
  │                    │         └─ remember_tool ──> PersistentMemory.add()
  │                    ▼                                ▼
  │              +-------------+              +---------------------+
  │              | Session     |              | PersistentMemory    |  ② 跨 session
  │              | Store       |              | (src/memory/        |     文件 + 索引
  │              | (JSON+JSONL)|              |  persistent.py)     |     ~/.mini-agent/memory
  │              +------+------+              +---------------------+
  │                     │
  ├─> EventBus.emit(...)│ index_message
  │     │               ▼
  │     ▼         +-------------------+
  │   SSE 推送    | SessionSearchIndex|  ③ 全文检索
  │               | (SQLite FTS5)     |     ~/.mini-agent/sessions.db
  │               +-------------------+
  └─> 回复消息写回 messages.jsonl
```

三层的物理位置：

| 层 | 存储介质 | 生命周期 | 代码位置 |
| --- | --- | --- | --- |
| WorkspaceMemory | 内存 Python 对象 | 单次 `AgentLoop.run()` | `src/agent/memory.py` |
| PersistentMemory | 本地 Markdown 文件 | 永久（直到 forget） | `src/memory/persistent.py` |
| Session Layer | 本地文件 + SQLite | 永久（直到 delete_session） | `src/session/` |

---

## 2. WorkspaceMemory（工作记忆）

### 2.1 设计意图

文件头注释（`memory.py:1-5`）一句话定调：

> *Lightweight runtime state — survives within one AgentLoop.run() invocation only.*

它只解决一件事：**让同一个 run 内的多个工具共享临时状态**，而无需把状态塞进 LLM 的对话上下文（既费 token 又不可靠）。

### 2.2 数据结构

整个类只有两个字段（`memory.py:13-23`）：

```python
@dataclass
class WorkspaceMemory:
    run_dir: Optional[str] = None
    counters: Dict[str, int] = field(default_factory=dict)
```

- `run_dir`：每次 run 创建独立目录（`runs/<timestamp>/`），所有写文件操作都落到这里，多次 run 互不覆盖。
- `counters`：按工具名统计调用次数，压缩时给 LLM 看「当前已调过哪些工具几次」。

### 2.3 关键方法

| 方法 | 作用 | 引用 |
| --- | --- | --- |
| `increment(key)` | 自增某个工具的计数 | `memory.py:25-35` |
| `to_summary()` | 生成给 LLM 看的状态摘要 | `memory.py:37-49` |

`to_summary()` 输出样例：

```
- run_dir: E:\...\mini-agent\runs\20260613_103045
- counters: read_file=3, bash=2, edit_file=1
```

这段文字在两处被注入提示词：
1. `ContextBuilder.build_system_prompt` 的 `## State` 区块（`context.py:88`）；
2. `_auto_compact` 把摘要拼到压缩后的 handoff 文本（`loop.py:574-577`），让接力的 LLM 仍知道当前工作目录。

### 2.4 与 loop.py 的协作

`AgentLoop.__init__` 接收 `memory: WorkspaceMemory`（`loop.py:229`），整个 run 复用同一实例：

- **创建 run_dir**：`loop.py:255-259`。memory 上已有就复用，否则新建。
- **工具调用后更新计数**：`_finalize_tool_result` 调 `self._update_memory(tc.name)`（`loop.py:502, 594-595`），内部即 `memory.increment(tool_name)`。
- **run_dir 透传给工具**：`_normalize_tool_run_dir`（`loop.py:202-219`）自动把 `memory.run_dir` 注入工具参数——这就是为什么用户调 write_file 不用传路径前缀。

### 2.5 为什么不放进 Session 层

工作记忆本质是「这次 run 的临时变量」。Session 层负责「这次对话说了什么」，关注点不同。把 `run_dir` 写进 `session.json` 会造成：一次对话多个 attempt 互相覆盖 run_dir；持久化了不该持久化的临时状态。所以 WorkspaceMemory 严格保持「不落盘、不跨 run」。

---

## 3. PersistentMemory（跨会话记忆）—— 重点

### 3.1 设计意图

这是 mini-agent 最有「agent 味」的一层：让 agent **自己**决定哪些信息值得长期记住。文件头（`persistent.py:1`）：

> *PersistentMemory: file-based cross-session memory, **zero external dependencies**.*

「零外部依赖」——不依赖向量库、不依赖 embedding API、不依赖网络。所有记忆存在 `~/.mini-agent/memory/` 下的 Markdown 文件里。

### 3.2 文件存储格式

#### 目录结构

```
~/.mini-agent/memory/
├── MEMORY.md                       # 索引（前 200 行摘要）
├── user_prefer_typescript.md       # 用户偏好类
├── project_使用_pnpm.md             # 项目类（注：CJK 会被替换）
├── feedback_代码要加注释.md          # 反馈类
└── reference_react_hooks.md        # 参考类
```

#### 单条记忆的文件格式

由 `add()` 写入（`persistent.py:94-110`）：

```markdown
---
name: 用户偏好 TypeScript
description: 用户偏好 TypeScript
type: user
---

用户在所有前端代码生成任务中明确要求使用 TypeScript。
严格类型、避免 any、优先 interface 而非 type。
```

frontmatter 由 `parse_frontmatter`（`frontmatter.py:9-40`）解析。这是一个**类 YAML** 解析器（不是真 YAML），只支持字符串、列表 `[a, b]`、布尔——刻意保持极简，避免成为攻击面。

#### 文件命名规则（`persistent.py:96-98`）

```python
slug = re.sub(r"[^a-z0-9_-]", "_", name.lower().strip())[:60]
filename = f"{memory_type}_{slug}.md"
```

- 所有非 `[a-z0-9_-]` 字符替换为 `_`，CJK 也会被替换；
- 文件名前缀是 memory_type（`user` / `project` / `feedback` / `reference`）；
- slug 截断到 60 字符。

> 注意：因为 CJK 会被替换成 `_`，中文 title 会产生形如 `user_________.md` 的文件名。索引 `MEMORY.md` 里的链接文本仍是原 title，所以检索不受影响，但文件名可读性差——值得改进。

#### MEMORY.md 索引

每次 `add` 都会更新（`persistent.py:120-137`）：

```markdown
- [用户偏好 TypeScript](user_prefer_typescript.md) — 用户偏好 TypeScript
- [使用 pnpm](project_______.md) — 使用 pnpm
```

索引截断到 `MAX_INDEX_LINES = 200`（`persistent.py:13`）。这个索引会被完整塞进系统提示词，是 agent 的「目录页」。

### 3.3 关键词评分检索算法（核心）

`find_relevant` 是这一层的灵魂（`persistent.py:78-92`）：

```python
def find_relevant(self, query: str, max_results: int = MAX_RESULTS) -> List[MemoryEntry]:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []
    scored: list[tuple[float, MemoryEntry]] = []
    for entry in self._scan_entries():
        meta_tokens = _tokenize(f"{entry.title} {entry.description}")
        body_tokens = _tokenize(entry.body)
        score = len(query_tokens & meta_tokens) * METADATA_WEIGHT + len(query_tokens & body_tokens)
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda x: (-x[0], -x[1].modified_at))
    return [entry for _, entry in scored[:max_results]]
```

#### 算法步骤

**Step 1：Tokenize（`persistent.py:29-32`）**

```python
def _tokenize(text: str) -> set[str]:
    ascii_tokens = set(re.findall(r"[a-zA-Z0-9_]{3,}", text.lower()))  # 英文 ≥3 字符
    cjk_tokens = set(re.findall(r"[一-鿿㐀-䶿]", text))                # 中文单字
    return ascii_tokens | cjk_tokens
```

英文按单词分（≥3 字符，过滤 `is`、`of` 噪音）；中文按单字分——没内置分词器，单字是最稳妥的最小粒度；返回 `set` 天然去重。

**Step 2：打分**

```
score = |query ∩ metadata| × 2.0  +  |query ∩ body| × 1.0
```

- `metadata` = title + description；
- `body` = 正文；
- `METADATA_WEIGHT = 2.0`（`persistent.py:16`）——元数据命中权重翻倍，因为 title/description 是主动写的「关键词」，比正文偶然出现更有信息量。

**Step 3：排序**

```python
scored.sort(key=lambda x: (-x[0], -x[1].modified_at))
```

主键分数降序，次键修改时间降序（更新的优先）——同样相关时，最近的可能更有用。

**Step 4：截断到 max_results**

默认 `MAX_RESULTS = 5`（`persistent.py:15`），系统提示词注入处传 `max_results=3`（`context.py:103`）避免上下文膨胀。

#### 算法复杂度

设记忆总数 N，每条平均 token 数 T，查询 token 数 Q：
- 时间：`O(N × (T + Q))`，主要是 set 交集；
- 空间：`O(N × T)` 临时存所有 entry 的 token 集合。

个人 agent（N < 1000，T < 1000）整个检索在毫秒级完成，**不需要缓存或向量索引**。

#### 与向量检索对比（思考题预告）

| 维度 | 关键词评分 | 向量检索 |
| --- | --- | --- |
| 依赖 | 无 | embedding 模型 + 向量库 |
| 精确匹配 | 强 | 弱（可能被近义词稀释） |
| 语义泛化 | 无 | 强 |
| 可解释性 | 强（分数可分解到每个 token） | 弱（黑盒相似度） |
| 隐私 | 完全本地 | 需上传文本到 embedding API |

mini-agent 选关键词评分，本质是赌「个人 agent 场景下，用户更可能用原词回忆」。这是产品取舍，不是技术落后。

### 3.4 写入路径：remember_tool

agent 主动记忆的入口是 `RememberTool`（`remember_tool.py`），把 PersistentMemory 包装成 LLM 可调用的工具。

#### 工具签名（`remember_tool.py:21-31`）

```json
{
  "action": "save | recall | forget",
  "title": "...",
  "content": "...",
  "memory_type": "user | feedback | project | reference",
  "query": "..."
}
```

`memory_type` 四种类型对应不同语义：
- `user`：用户长期偏好；
- `feedback`：用户对 agent 行为的纠正；
- `project`：项目相关事实；
- `reference`：参考资料。

#### 三个 action（`remember_tool.py:37-70`）

| action | 调用 | 输出 |
| --- | --- | --- |
| `save` | `memory.add(title, content, type, description)` | `{"status":"ok","path":"..."}` |
| `recall` | `memory.find_relevant(query)` | `{"status":"ok","count":N,"memories":[...]}` |
| `forget` | `memory.remove(title)` | `{"status":"ok"}` 或 `not_found` |

`recall` 返回作为工具结果回到 LLM——这是「显式检索」路径，agent 自己判断需要回忆时调用。

### 3.5 读取路径：双通道注入

PersistentMemory 有两条互补的读取通道：

#### 通道 A：被动注入（系统提示词）

`ContextBuilder.build_system_prompt`（`context.py:74-91`）：

```python
memory_section = ""
if self._persistent_memory and self._persistent_memory.snapshot:
    memory_section = _MEMORY_SECTION.format(snapshot=self._persistent_memory.snapshot)
```

`snapshot` 是 `MEMORY.md` 的前 200 行（`persistent.py:45-56`），即**所有记忆的标题列表**。作为「目录页」放进系统提示词，LLM 看到感兴趣的标题时可主动 `recall` 拉详细内容。

#### 通道 B：主动召回（query 命中）

`ContextBuilder.build_messages`（`context.py:101-110`）：

```python
if self._persistent_memory:
    recalls = self._persistent_memory.find_relevant(user_message, max_results=3)
    if recalls:
        lines = [f"- **{r.title}** ({r.memory_type}): {r.body[:500]}" for r in recalls]
        recall_block = "\n".join(lines)
        enriched = f"<recalled-memories>\n{recall_block}\n</recalled-memories>\n\n{user_message}"
```

每次用户消息进来，先用消息本身做 query 跑 `find_relevant`，把命中的前 3 条记忆用 `<recalled-memories>` 标签包起，prepend 到用户消息前。LLM 在看到原文之前就已看到「可能相关」的历史记忆。

> 「被动索引 + 主动召回」的双通道设计很巧妙：索引保证 LLM 知道有什么可查，召回保证 LLM 不查也能用上最相关的。即使 LLM 忘记调 `recall`，关键信息也已被送达。

### 3.6 关键代码引用

| 功能 | 位置 |
| --- | --- |
| 目录与常量 | `persistent.py:12-16` |
| MemoryEntry 数据结构 | `persistent.py:19-26` |
| Tokenizer（中英混合） | `persistent.py:29-32` |
| 加载 MEMORY.md 快照 | `persistent.py:45-56` |
| 扫描所有记忆文件 | `persistent.py:58-76` |
| **关键词评分检索** | `persistent.py:78-92` |
| 写入新记忆 | `persistent.py:94-110` |
| 删除记忆 | `persistent.py:112-118` |
| 更新索引 | `persistent.py:120-137` |
| 重建索引 | `persistent.py:139-142` |

---

## 4. Session Layer（会话层）

Session 层是 mini-agent 的「数据库」，但不是真数据库，而是**文件系统 + SQLite FTS5** 的组合。负责四件事：数据建模、持久化、事件推送、全文检索。

### 4.1 数据模型（`models.py`）

#### Session（`models.py:27-47`）

```python
@dataclass
class Session:
    session_id: str            # 12 位 uuid hex
    title: str
    status: SessionStatus      # ACTIVE / COMPLETED / ARCHIVED
    created_at: str
    updated_at: str
    last_attempt_id: Optional[str]   # 链表：指向上一次 attempt
    config: Dict[str, Any]
```

`last_attempt_id` 构成隐式 attempt 链表，新 attempt 的 `parent_attempt_id` 总指向前一个（`service.py:59`），可追溯对话分支。

#### Message（`models.py:50-65`）

```python
@dataclass
class Message:
    message_id: str
    session_id: str
    role: str                  # user / assistant
    content: str
    created_at: str
    linked_attempt_id: Optional[str]  # 属于哪次 attempt
    metadata: Dict[str, Any]
```

`linked_attempt_id` 让 assistant 回复可追溯到触发它的执行——debug 与审计的关键。

#### Attempt（`models.py:68-111`）

Attempt 是「一次完整的 agent 执行」，包含 ReAct 循环的全部信息：

```python
@dataclass
class Attempt:
    attempt_id: str
    session_id: str
    parent_attempt_id: Optional[str]   # 链表
    status: AttemptStatus              # 6 种状态
    prompt: str                        # 触发本次执行的原始输入
    run_dir: Optional[str]             # 对应 WorkspaceMemory.run_dir
    summary: Optional[str]
    react_trace: List[Dict]            # ReAct 循环完整轨迹
    created_at: str
    completed_at: Optional[str]
    error: Optional[str]
    metrics: Optional[Dict]
```

`AttemptStatus` 有 6 个值（`models.py:18-24`）：`pending → running → waiting_user / completed / failed / cancelled`。状态机转移方法（`mark_running` 等）封装在模型里，调用方不直接改字段。

### 4.2 持久化（`store.py`）

#### 文件系统布局

`SessionStore` docstring（`store.py:13-22`）：

```
sessions/
├── {session_id}/
│   ├── session.json          # Session 元数据
│   ├── messages.jsonl        # 消息流（每行一条）
│   └── attempts/{attempt_id}/attempt.json
```

**为什么 messages 用 JSONL 而 session/attempt 用 JSON？**
- `messages.jsonl`：每条一行，append-only（`store.py:80-84`），写性能好，可流式读；
- `session.json` / `attempt.json`：整体读改写，适合频繁更新的小对象。

这是「事件流 vs 快照」的二分法，事件溯源（Event Sourcing）的简化形态。

**append_message 实现（`store.py:80-84`）**

```python
def append_message(self, message: Message) -> None:
    path = self._messages_file(message.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")
```

纯追加，不锁文件。单进程异步场景下安全；多进程并发需要外部协调。

**get_messages 的尾部截断（`store.py:86-94`）** 总是返回最近 N 条——天然适配 LLM 上下文窗口。

#### 原子性

`_write_json`（`store.py:123-126`）**不是原子的**：

```python
path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
```

写入过程中崩溃，文件可能损坏。这是已知 trade-off：用 `path.write_text` 换简单性。生产级实现应写到临时文件再 `os.rename`。

#### 并发安全

`SessionStore` **没有锁**。并发安全性来自上层 `SessionService` 用单线程 asyncio + ThreadPoolExecutor 调度 agent（`service.py:11, 138-141`）。同一 session 的 attempt 串行执行（`_active_loops` 字典追踪），不会并发写同一文件。

### 4.3 事件总线（`events.py`）

#### SSE 协议格式

`SSEEvent.to_sse`（`events.py:25-27`）：

```python
def to_sse(self) -> str:
    payload = json.dumps(self.data, ensure_ascii=False)
    return "\n".join([
        f"id: {self.event_id}",
        f"event: {self.event_type}",
        f"data: {payload}",
        "", ""
    ])
```

输出符合 [SSE 规范](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events)，每个事件用两个换行结尾（消息分隔符）。

#### 事件类型清单

通过追踪 `SessionService` 中的 `emit` 调用：

| 事件 | 触发时机 | 来源 |
| --- | --- | --- |
| `session.created` | 创建 session | `service.py:33` |
| `message.received` | 收到消息 | `service.py:54` |
| `attempt.created` | 创建 attempt | `service.py:64` |
| `attempt.started` | attempt 开始 | `service.py:85` |
| `attempt.completed` / `attempt.failed` | 执行结束 | `service.py:101-103, 108` |
| `tool_call` | agent 调用工具 | `loop.py:454, 489`（经 event_callback） |
| `tool_result` | 工具返回 | `loop.py:514` |
| `text_delta` | 流式输出 token | `loop.py:308` |
| `thinking_done` | 思考完成 | `loop.py:321` |
| `compact` | 上下文压缩 | `loop.py:572` |
| `heartbeat` | 心跳保活 | `events.py:103` |

#### 订阅机制（`events.py:88-108`）

```python
async def subscribe(self, session_id, last_event_id=None) -> AsyncIterator[SSEEvent]:
    queue: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=200)
    self._subscribers[session_id].append(queue)
    try:
        # 1. 重放 missed events
        for event in self.replay(session_id, last_event_id):
            yield event
        # 2. 长轮询
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=30.0)
            yield event
    finally:
        self._subscribers[session_id].remove(queue)
```

四个设计要点：
1. **每个订阅者一个 asyncio.Queue**（容量 200），简单可靠；
2. **last_event_id 恢复**：客户端断线重连传上次的 event id，服务端从 buffer 重放 missed events（`events.py:74-86`）；
3. **30 秒心跳**：超时没事件就发 `heartbeat`，保持 HTTP 连接不被代理掐断；
4. **跨线程发布**：`publish` 通过 `loop.call_soon_threadsafe` 把事件塞进队列（`events.py:54-55`），因为 agent 在 ThreadPoolExecutor 里跑，跟订阅者的 event loop 不同线程。

#### Buffer 容量与丢弃策略

每个 session 最多缓存 500 条事件（`events.py:33, 50-51`）。超过后**丢弃最旧的**：

```python
if len(buffer) > self.max_buffer_size:
    self._buffers[session_id] = buffer[-self.max_buffer_size:]
```

有损策略——客户端断线太久（>500 事件）重连后会丢事件。trade-off 是用内存换简单性。

### 4.4 全文搜索（`search.py`）

#### 为什么选 SQLite FTS5

- **零依赖**：SQLite 是 Python 标准库自带；
- **性能**：基于倒排索引，查询是 O(log N)；
- **功能**：内置 `snippet()`、`rank()`、BM25 评分、前缀查询；
- **学习曲线低**：SQL 语法，无需学专门 DSL。

对比：Elasticsearch 太重；Whoosh 维护停滞；向量库（Chroma/Faiss）需要 embedding，违反「零依赖」。

#### 索引结构（`search.py:48-67`）

```sql
-- 普通表
CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at REAL, message_count INTEGER);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL);

-- FTS5 虚拟表（外部内容表模式）
CREATE VIRTUAL TABLE messages_fts USING fts5(content, content=messages, content_rowid=id);

-- 同步触发器
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
```

`content=messages` 是 FTS5「外部内容表」模式：FTS 表只存倒排索引，原文存 messages 表里。节省空间，但删除/更新需手动同步（用 trigger）。

#### 查询语法（`search.py:82-88`）

`_sanitize_fts_query` 把自然语言转 FTS5 表达式：

```python
@staticmethod
def _sanitize_fts_query(query: str) -> str:
    tokens = _re.findall(r"[a-zA-Z0-9_]{2,}|[一-鿿㐀-䶿]", query)
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)
```

用户输入「SQLite 报错怎么办」会被转成：

```
"SQLite" OR "报" OR "错" OR "怎" OR "么" OR "办"
```

`OR` 连接意味着「命中任一 token 即可」，召回率高但精度低。最终由 `rank`（BM25）排序，最相关的排前面。

#### 查询执行（`search.py:90-112`）

```sql
SELECT m.session_id, s.title, s.started_at, s.message_count,
       snippet(messages_fts, 0, '>>>', '<<<', '...', 64) AS snippet, rank
FROM messages_fts
JOIN messages m ON m.id = messages_fts.rowid
JOIN sessions s ON s.id = m.session_id
WHERE messages_fts MATCH ?
ORDER BY rank
LIMIT ?
```

要点：
- `snippet(...)` 自动提取命中词附近的 64 字符窗口，用 `>>>` 和 `<<<` 包围命中词，前端可高亮；
- `ORDER BY rank` 用 FTS5 内置 BM25 评分排序；
- 取 `max_sessions * 5` 条原始命中，在 Python 里去重到 `max_sessions` 个不同 session（`search.py:104-111`）。

#### SQLite 优化（`search.py:43-45`）

```python
self._conn.execute("PRAGMA journal_mode=WAL")
self._conn.execute("PRAGMA synchronous=NORMAL")
```

- `WAL`（Write-Ahead Logging）：读写不阻塞，适合一写多读；
- `synchronous=NORMAL`：放宽 fsync 频率，换写入吞吐，崩溃时可能丢最后几条事务——可接受。

#### 单例模式（`search.py:127-139`）

```python
_shared_index: Optional[SessionSearchIndex] = None
_shared_lock = _threading.Lock()

def get_shared_index() -> SessionSearchIndex:
    global _shared_index
    if _shared_index is None:
        with _shared_lock:
            if _shared_index is None:
                _shared_index = SessionSearchIndex()
    return _shared_index
```

双重检查锁定（DCL）单例，保证整个进程共享一个 SQLite 连接。`check_same_thread=False`（`search.py:43`）让连接可跨线程——配合 WAL 模式，多线程并发查询是安全的。

---

## 5. 三层协作：完整时序

下面是「用户问 agent 一个问题，agent 调用工具后回答」的完整时序：

```
用户              SessionService        AgentLoop          WS Mem    PM        Store     FTS5    EventBus
 │                     │                   │                 │        │          │         │        │
 │ POST /chat          │                   │                 │        │          │         │        │
 │────────────────────▶│                   │                 │        │          │         │        │
 │                     │ append_message(user)────────────────────────────▶         │         │        │
 │                     │ index_message()──────────────────────────────────────────────▶       │        │
 │                     │ emit message.received─────────────────────────────────────────────────▶       │
 │                     │                   │                 │        │          │         │        │
 │                     │ create Attempt    │                 │        │          │         │        │
 │                     │ update last_attempt_id─────────────────────────▶         │         │        │
 │                     │                   │                 │        │          │         │        │
 │                     │ asyncio.create_task(_run_attempt)  │        │          │         │        │
 │                     │  ┌────────────────│                 │        │          │         │        │
 │                     │  ▼                │                 │        │          │         │        │
 │                     │  AgentLoop(history + WorkspaceMem) │        │          │         │        │
 │                     │  build_messages:                    │        │          │         │        │
 │                     │    ├ snapshot 注入系统提示 ◀───────────────────│          │         │        │
 │                     │    └ find_relevant(user_msg) ◀────────────────│          │         │        │
 │                     │       命中 2 条 → prepend 到 user msg        │          │         │        │
 │                     │                   │                 │        │          │         │        │
 │                     │  LLM 调 remember(recall=...)         │        │          │         │        │
 │                     │                   │ find_relevant ◀───────────│          │         │        │
 │                     │                   │ tool_result 回 LLM       │          │         │        │
 │                     │                   │                 │        │          │         │        │
 │                     │  LLM 调 read_file                    │        │          │         │        │
 │                     │  WS.increment("read_file")─────────▶│        │          │         │        │
 │                     │  emit tool_call ───────────────────────────────────────────────────▶        │
 │                     │                   │                 │        │          │         │        │
 │                     │  LLM 调 remember(save="发现 X")     │        │          │         │        │
 │                     │  PersistentMemory.add() ────────────────────▶│ 写文件+更新索引       │        │
 │                     │                   │                 │        │          │         │        │
 │                     │  LLM 输出 final answer              │        │          │         │        │
 │                     │  ◀────────────────│                 │        │          │         │        │
 │                     │                   │                 │        │          │         │        │
 │                     │ mark_attempt_completed              │        │          │         │        │
 │                     │ append_message(assistant)─────────────────────────────▶           │        │
 │                     │ index_message(assistant)──────────────────────────────────▶       │        │
 │                     │ emit attempt.completed────────────────────────────────────────────▶        │
 │                     │                   │                 │        │          │         │        │
 │ SSE: tool_call, tool_result, answer deltas ◀────────────────────────────────────────────│        │
```

### 关键观察

1. **WorkspaceMemory 是一次性的**：从 `AgentLoop.run()` 开始到结束，结束后 GC。下次对话创建新实例。
2. **PersistentMemory 是双向的**：agent 既能读（snapshot + find_relevant），也能写（remember save）。读写都用文件系统，进程间也安全。
3. **Session 层是单向的**：agent 只往里写（append_message、index_message），不会主动读——读是由前端 UI 在用户查看历史时发起。
4. **FTS5 是异步入索引的**：每次 `index_message` 同步写一次 SQLite，但 WAL 模式不阻塞主流程。

---

## 6. 关键类与方法清单

### WorkspaceMemory（`src/agent/memory.py`）

| 成员 | 类型 | 说明 |
| --- | --- | --- |
| `run_dir` | 属性 | 本次 run 的工作目录 |
| `counters` | 属性 | 工具调用计数 dict |
| `increment(key)` | 方法 | 自增计数器 |
| `to_summary()` | 方法 | 生成 LLM 可读的状态摘要 |

### PersistentMemory（`src/memory/persistent.py`）

| 成员 | 类型 | 说明 |
| --- | --- | --- |
| `MEMORY_BASE` | 常量 | 默认 `~/.mini-agent/memory/` |
| `MAX_INDEX_LINES` / `MAX_ENTRY_CHARS` / `MAX_RESULTS` | 常量 | 200 / 8000 / 5 |
| `METADATA_WEIGHT` | 常量 | 2.0，元数据命中权重 |
| `snapshot` | 属性 | MEMORY.md 前 200 行 |
| `find_relevant(query, max_results)` | 方法 | 关键词评分检索 |
| `add(name, content, type, description)` | 方法 | 写入新记忆 |
| `remove(name)` | 方法 | 按 title 删除 |

### Session 模型（`src/session/models.py`）

| 成员 | 说明 |
| --- | --- |
| `Session` / `Message` / `Attempt` | 三个核心 dataclass |
| `SessionStatus` | ACTIVE / COMPLETED / ARCHIVED |
| `AttemptStatus` | 6 种执行状态 |
| `Attempt.mark_running/completed/failed/waiting_user` | 状态转移 |

### SessionStore（`src/session/store.py`）

| 方法 | 说明 |
| --- | --- |
| `create/get/update/delete/list_sessions` | Session CRUD |
| `append_message / get_messages` | Message 追加/读取（JSONL） |
| `create/get/update/list_attempts` | Attempt CRUD |

### SessionService（`src/session/service.py`）

| 方法 | 说明 |
| --- | --- |
| `create_session` | 创建会话并触发 `session.created` |
| `send_message` | 接收消息、创建 attempt、调度 agent |
| `_run_attempt` | 执行 attempt 并推送事件 |
| `_run_with_agent` | 构造 AgentLoop 并跑（线程池里） |
| `_convert_messages_to_history` | Message 列表转 LLM history，12000 字符截断 |
| `cancel_current` | 取消正在执行的 attempt |

### EventBus（`src/session/events.py`）

| 方法 | 说明 |
| --- | --- |
| `emit(session_id, event_type, data)` | 发布事件 |
| `publish(event)` | 底层发布（带 buffer + 跨线程） |
| `subscribe(session_id, last_event_id)` | SSE 订阅（async generator） |
| `replay(session_id, last_event_id)` | 重放 missed events |
| `clear(session_id)` | 清理 buffer |

### SessionSearchIndex（`src/session/search.py`）

| 方法 | 说明 |
| --- | --- |
| `index_session(session_id, title)` | 索引会话元数据 |
| `index_message(session_id, role, content)` | 索引消息正文 |
| `search(query, max_sessions)` | FTS5 全文搜索 |
| `_sanitize_fts_query(query)` | 自然语言转 FTS5 表达式 |
| `get_shared_index()` | 模块级单例 |

### RememberTool（`src/tools/remember_tool.py`）

| action | 行为 |
| --- | --- |
| `save` | `PersistentMemory.add` |
| `recall` | `PersistentMemory.find_relevant` |
| `forget` | `PersistentMemory.remove` |

---

## 7. 学习要点

### 7.1 设计模式

1. **职责分离**：三层记忆各管一段，互不耦合。WorkspaceMemory 不持久化，PersistentMemory 不进 prompt 上下文（除了 snapshot），Session 层不被 agent 直接读写。
2. **零依赖原则**：除 SQLite（Python 自带），无任何外部存储依赖。mini-agent 在任何装 Python 的机器上都能跑。
3. **被动 + 主动**：PersistentMemory 同时支持被动注入（snapshot）和主动召回（find_relevant），双保险。

### 7.2 工程取舍

1. **关键词评分 vs 向量检索**：选了前者，换零依赖和可解释性，牺牲语义泛化。对个人 agent 场景是正确的赌注。
2. **JSONL vs 数据库**：消息用 JSONL 简化部署，但放弃事务和并发安全。
3. **追加 vs 原子写**：`append_message` 是追加（安全），`_write_json` 是覆盖（不安全）。trade-off 在「事件流」与「快照」的本质差异。

### 7.3 可改进点

1. **文件名 slug 对 CJK 不友好**：可保留 CJK 或用 hash。
2. **`_write_json` 非原子**：应用临时文件 + rename。
3. **EventBus buffer 有损**：溢出时可考虑落盘。
4. **FTS5 中文分词粗糙**：单字分词会引入噪音（"怎么样" 拆三字），可集成 jieba。

---

## 8. 思考题

1. **关键词评分 vs 向量检索的取舍**：如果 mini-agent 要支持「用户说 '上次那个数据库的问题'，agent 能找到关于 SQLite 的记忆」，当前关键词评分够用吗？需要怎么改造？引入向量检索会带来哪些副作用（依赖、隐私、延迟）？

2. **WorkspaceMemory 为什么要随 run 销毁**：如果把它持久化到 Session 层，会出现什么问题？提示：考虑多 attempt 共享状态的副作用。

3. **MEMORY.md 索引截断到 200 行的风险**：如果用户记忆超过 200 条，第 201 条之后会怎样？snapshot 还能反映它们的存在吗？如何让 agent 知道「还有更多记忆没列出」？

4. **FTS5 删除消息的同步**：当前只创建了 `AFTER INSERT` 和 `AFTER DELETE` 触发器，没有 `AFTER UPDATE`。如果消息被编辑（虽然 mini-agent 目前不支持），索引会出现什么问题？

5. **EventBus 的跨进程局限**：当前 EventBus 是进程内的，如果部署多个 mini-agent 实例（横向扩展），SSE 订阅会失效。该如何改造为分布式？Redis Pub/Sub？NATS？

---

## 9. 延伸阅读

本文档聚焦「记忆系统」本身。要完整理解记忆如何参与 agent 的运行，还需阅读：

- **Context 模块文档**：详细讲解 `ContextBuilder` 如何把 `WorkspaceMemory.to_summary()` 和 `PersistentMemory.snapshot` 拼装成系统提示词，以及 `<recalled-memories>` 标签的设计动机。重点看 `src/agent/context.py:74-115`。
- **Tools 模块文档**：讲解 `RememberTool` 如何作为 LLM 工具暴露给 agent，以及工具注册机制 `build_registry(persistent_memory=pm)` 如何把同一个 PersistentMemory 实例同时注入 ContextBuilder 和 RememberTool——这是「读通道」和「写通道」共享同一份记忆的关键。重点看 `src/tools/__init__.py` 和 `src/tools/remember_tool.py`。

另外推荐对照阅读源码：
- `src/agent/loop.py:255-259` —— WorkspaceMemory.run_dir 如何决定 AgentLoop 的工作目录；
- `src/session/service.py:110-145` —— PersistentMemory 如何被同时传给 ContextBuilder 和 AgentLoop；
- `src/session/search.py:48-67` —— FTS5 外部内容表模式的完整 DDL，全文检索的根基。

---

> **文档版本**：v1.0 ｜ **覆盖源码版本**：master 分支 commit `80227d1` ｜ **最后更新**：2026-06-13
