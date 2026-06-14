# Prompt Caching 优化计划

> 状态：草案
> 起草日期：2026-06-13
> 关联模块：`src/agent/context.py`、`src/providers/chat.py`、`src/providers/llm.py`、`src/agent/loop.py`

## 1. 背景

主流 LLM provider（OpenAI、DeepSeek、Zhipu、Anthropic、Moonshot 等）都提供 prompt caching：当多次请求共享稳定的前缀时，缓存命中的部分按 0.1× ~ 0.5× 计价，同时降低首 token 延迟（TTFT）。

参考工业级 agent 框架 [hermes-agent](https://github.com/NousResearch/hermes-agent) 的设计哲学——"Per-conversation prompt caching is sacred"——它把"维持字节级稳定的 system prompt"作为整个架构的第一条不变量，所有动态内容必须注入到 user message 而非 system prompt。

mini-agent 当前实现违反了这条原则，导致几乎每一轮 LLM 调用的 system prompt 都不同，缓存无法命中。

## 2. 现状分析

### 2.1 mini-agent 当前 system prompt 的构成

`src/agent/context.py:19-70` 中的 `_SYSTEM_PROMPT` 包含以下动态字段：

| 字段 | 来源 | 变化频率 |
|---|---|---|
| `current_datetime` | `datetime.now()` | **每分钟变化** |
| `memory_summary` | `WorkspaceMemory.to_summary()` | **每次工具调用后变化**（含工具调用计数） |
| `memory_section`（PersistentMemory snapshot） | `persistent_memory.snapshot` | 每次 `remember` 工具调用后变化 |
| `tool_count` / `skill_count` | registry / skills_loader | 启动后稳定 |
| `tool_descriptions` / `skill_descriptions` | registry / skills_loader | 启动后稳定 |

前三项直接破坏了前缀缓存的命中条件。

### 2.2 已做对的部分

- `src/agent/context.py:117-127` 已经将 `recalled-memories` 注入到 user message 而非 system prompt，符合 hermes 的原则。
- `src/agent/context.py:148-179` 的 `format_assistant_tool_calls` / `format_tool_result` 使用稳定的 JSON 序列化，没有动态字段。

### 2.3 调用层现状

`src/providers/llm.py:143-163` 的 `build_llm` 基于 `langchain_openai.ChatOpenAI`，`extra_body` 仅包含 `reasoning.effort`，未传递任何 cache 字段。这对 OpenAI / DeepSeek / Zhipu 这种**自动缓存**的 provider 不影响（自动命中），但对 Anthropic / Moonshot 这种**需要显式 `cache_control` 标记**的 provider 完全无法享受缓存。

### 2.4 问题清单

1. `current_datetime` 嵌入 system prompt 末尾 → 整个 system prompt 每分钟失效。
2. `WorkspaceMemory.to_summary()` 含工具调用计数器 → 每次 `increment()` 后 system prompt 变化。
3. `PersistentMemory.snapshot` 嵌入 system prompt → 每次 `remember` 后失效。
4. 无缓存命中率观测 → silent miss 无法发现。
5. 不支持显式 `cache_control` → Anthropic / Moonshot 用户无法享受缓存。

## 3. 必要性

### 3.1 成本

OpenAI / DeepSeek / Zhipu 对 cached prefix 计价为标准价的 0.1× ~ 0.5×。mini-agent 一次 ReAct 任务平均 5–15 turn，每 turn 重发 system prompt（含 tool 描述、skill 描述、guidelines），保守估计 3–8K tokens。

以 15 turn × 5K tokens × 10 次任务估算：
- 优化前：750K prompt tokens 全额计费。
- 优化后：约 90% 命中缓存，按 0.2× 计价，等效 165K tokens 价格。
- 节省：约 78%。

### 3.2 延迟

缓存命中可降低 50–80% TTFT，对 mini-agent 的交互式 CLI 体验有直接感知价值。

### 3.3 架构对齐

hermes-agent 的设计警告过："silent prefix-cache misses were a real production bug that was eventually traced"——即如果不做可观测性，缓存失效在生产中难以察觉，成本在不知不觉中累积。本计划除了修复缓存，还引入命中率观测，避免 silent miss。

### 3.4 改造成本低

阶段 1 改动仅 10–20 行 diff，零新依赖，立即对 OpenAI / DeepSeek / Zhipu 生效。

## 4. 优化计划

### 阶段 1：稳定 system prompt（P0，零依赖）

**目标**：让 system prompt 在一个 session 内字节级稳定。

修改清单：

- [ ] `src/agent/context.py`：移除 `_SYSTEM_PROMPT` 中的 `current_datetime` 段落，或改为不带分钟粒度的日期。
- [ ] `src/agent/context.py`：移除 `_SYSTEM_PROMPT` 中的 `{memory_summary}`，将 `WorkspaceMemory.to_summary()` 移入 user message 的 `<workspace-state>` 块。
- [ ] `src/agent/context.py`：评估 `_MEMORY_SECTION` 的位置。建议同样移入 user message 的 `<persistent-memory>` 块，因为 `remember` 工具调用会触发其变化。
- [ ] `src/agent/context.py`：在 `ContextBuilder` 中缓存 system prompt（用 hash 比对），仅在 tool / skill 列表变化时重建。
- [ ] `src/agent/context.py`：`build_messages` 改为构造如下 user message：
  ```
  <workspace-state>
  {memory_summary}
  </workspace-state>

  <persistent-memory>
  {snapshot}
  </persistent-memory>

  <recalled-memories>
  {recalls}
  </recalled-memories>

  {user_message}
  ```

### 阶段 2：缓存命中可观测性（P0）

**目标**：让用户能看到 cache 是否生效，避免 silent miss。

修改清单：

- [ ] `src/providers/chat.py:_parse_response`：解析 token 用量字段：
  - OpenAI / DeepSeek：`response_metadata.token_usage.prompt_tokens_details.cached_tokens`
  - Anthropic（经 OpenRouter）：`response_metadata.cache_read_input_tokens` / `cache_creation_input_tokens`
  - Zhipu：`response_metadata.usage.cached_tokens`
- [ ] `src/providers/chat.py`：在 `LLMResponse` 增加 `cache_stats: CacheStats`（字段：`prompt_tokens`、`cached_tokens`、`cache_hit_ratio`）。
- [ ] `src/agent/loop.py`：在每次 `stream_chat` 后将 cache_stats 写入 trace，并在 CLI 输出命中率（例如 `[cache: 4.2K/5.1K cached, 82%]`）。
- [ ] `src/agent/loop.py`：连续 3 turn 命中率低于 50% 时输出 warning，提示可能 system prompt 不稳定。
- [ ] `src/providers/chat.py`：`stream_chat` 异常 fallback 到 `chat` 的路径同样解析 cache_stats。

### 阶段 3：显式 `cache_control` 支持（P1）

**目标**：覆盖非自动缓存的 provider。

修改清单：

- [ ] `src/providers/llm.py`：在 `_PROVIDER_MAP` 增加 `anthropic`，检测 `provider == "anthropic"` 或模型名前缀（OpenRouter 的 `anthropic/`）。
- [ ] `src/providers/chat.py`：在 Anthropic 路径下，为 system prompt 和历史 messages 倒数第二条注入 `cache_control: {"type": "ephemeral"}`。
- [ ] `src/providers/chat.py`：Moonshot 路径下注入 `cache: {"type": "kwai"}`。
- [ ] `src/providers/chat.py`：OpenRouter Anthropic 路径识别 `model.startswith("anthropic/")` 并切换到显式 cache_control 注入。

### 阶段 4：辅助模型独立 client（P2）

**目标**：为后续 curator / 压缩 / 标题生成等辅助任务铺路，避免污染主 session 的 token 统计与缓存。

修改清单：

- [ ] 新增 `src/providers/auxiliary.py`，提供 `AuxiliaryLLM` 类，使用便宜模型、独立 system prompt、不复用主 session 的 messages。
- [ ] 配置项：`LANGCHAIN_AUX_MODEL_NAME`（如 `gpt-4o-mini` / `deepseek-chat`），缺省时回退到主模型。
- [ ] `src/agent/loop.py:_auto_compact`：改为使用 `AuxiliaryLLM` 执行压缩，避免占用主 session 的上下文。

### 阶段 5：字节稳定性自检（P2，可选）

**目标**：开发期防止 system prompt 不稳定回归。

修改清单：

- [ ] `src/agent/context.py`：开发模式下（`MINI_AGENT_DEBUG=1`）连续构造两次 `build_messages`，对比 system prompt 的 SHA256，不一致时 raise。
- [ ] `cli.py`：增加 `--debug-cache` 参数，打印当前 system prompt 的前 200 字节、SHA256 与最近一次命中率。

### 阶段 6：5 层压缩的联动改造（P0，必须与阶段 1/2 配套）

**目标**：消除 Layer 1/2（microcompact / context_collapse）对 prefix caching 的持续破坏，保留 Layer 3/4/5 作为容量超限的最后防线。

#### 6.1 角色重新划分

prompt caching 与 5 层压缩解决的问题维度不同，二者并存：

| 机制 | 解决的问题 | 何时触发 | 与 caching 的关系 |
|---|---|---|---|
| Prompt caching | 重复 tokens 按折扣计费 | 前缀字节稳定时自动生效 | — |
| Layer 1 microcompact | 每 turn 清除旧 tool 结果 | 当前每轮都跑（`loop.py:291`） | **破坏 prefix** |
| Layer 2 context_collapse | 折叠中间长文本块 | raw tokens > COLLAPSE_THRESHOLD | **破坏 prefix** |
| Layer 3 auto_compact | LLM 结构化 summary | raw tokens > TOKEN_THRESHOLD | 压缩即重建，"唯一允许的 cache-break" |
| Layer 4 compact tool | 模型主动触发 L3 | 模型自行决定 | 同 L3 |
| Layer 5 iterative update | L3 的增量优化 | L3 触发时 | 与 caching 无关，仅省压缩本身的成本 |

cached_tokens 不会让 messages 变短，因此"防撑爆 context window"的根本作用依然必要。冲突点集中在 Layer 1/2。

#### 6.2 冲突的本质

Layer 1/2 在 **messages 列表中间**修改 content：

- `_microcompact` 把旧 tool 消息的 content 改成 `"[cleared]"`
- `_context_collapse` 把中间长文本改成 `head + ...[collapsed N chars]... + tail`

OpenAI / DeepSeek 的缓存是前缀连续匹配，任何一处变化都会让从该位置往后的所有内容（包括最新 user message）失效。

改前的逻辑是"清除旧 tool 结果以免重复计费"，边际收益为正；改后旧 tool 结果已可缓存命中（0.1× 计费），清掉反而让缓存失效、从该位置起全额计费，边际收益可能为负。更严重的是 microcompact **每轮都跑**，会持续打断缓存。

#### 6.3 改造方案

推荐方案 A（懒触发），改动小、效果直接：

- [ ] `src/agent/loop.py`：新增 `MICROCOMPACT_THRESHOLD = int(TOKEN_THRESHOLD * 0.85)`，把 `_microcompact(messages)` 从无条件每轮执行改为 `if tokens > MICROCOMPACT_THRESHOLD:`。
- [ ] `src/agent/loop.py`：调整 `KEEP_RECENT = 6`（原值 3），保留更多近期 tool 结果以延长缓存命中段。
- [ ] `src/agent/loop.py`：调整 `COLLAPSE_THRESHOLD = int(TOKEN_THRESHOLD * 0.85)`（原值 0.7），给 caching 留出更大生效空间。
- [ ] `src/agent/loop.py`：在触发 Layer 1/2 前输出 trace 事件 `{"type": "silent_compress", "layer": "microcompact|collapse", "tokens": tokens}`，便于观测触发频率。
- [ ] 保留 Layer 3/4/5 不变，作为容量超限的最后防线。

可选方案 B（cache 命中率二次判断），依赖阶段 2 可观测性，精度更高：

```python
if tokens > THRESHOLD and cache_hit_ratio < 0.5:
    _microcompact(messages)  # 缓存已经不好，压缩有意义
elif tokens > TOKEN_THRESHOLD:
    self._auto_compact(...)  # 缓存还好但容量快满，优先 LLM 压缩
```

可选方案 C（对齐 hermes，删除 Layer 1/2），激进，会让 raw tokens 处于 30K–40K 区间时成本上升。除非主用 DeepSeek 这类缓存折扣极大的 provider，否则不推荐。

#### 6.4 配套要求

阶段 6 **必须与阶段 1/2 同批上线**：

- 若只做阶段 1/2 不做阶段 6：system prompt 稳定了，但 microcompact 每轮仍会破坏 messages 中段，缓存命中率会持续被打断，效果大打折扣。
- 若只做阶段 6 不做阶段 1/2：messages 中段稳定了，但 system prompt 仍带时间戳和工具计数，前缀本身就失效，缓存依然不命中。

## 5. 实施顺序与预期收益

| 阶段 | 工时 | 收益 |
|---|---|---|
| 阶段 1（稳定 system prompt） | 1–2h | 省 50–70% prompt token（OpenAI / DeepSeek / Zhipu 立即生效） |
| 阶段 2（可观测性） | 1h | 防止 silent miss，长期保住收益 |
| 阶段 6（5 层压缩联动改造） | 1–2h | 防止 microcompact / collapse 持续打断缓存，**必须与阶段 1/2 同批上线** |
| 阶段 3（显式 cache_control） | 2–3h | Anthropic / Moonshot 用户额外 30%+ 收益 |
| 阶段 4（auxiliary client） | 1h | 配合 learning loop，进一步节省 |
| 阶段 5（自检工具） | 30min | 开发期防回归 |

合计 6–9 小时。阶段 1、2、6 互为前提且 ROI 最高，应作为同一批 PR 上线；阶段 3 可独立跟进；阶段 4、5 视后续需要安排。

## 6. 风险与注意点

1. **`memory_summary` 移走后，模型是否仍能看到状态？** 能。移入 user message 后模型同样能读到，且符合 hermes 的"ephemeral context goes to user message"原则。
2. **历史 messages 的字节稳定性**：阶段 1 只解决 system prompt。OpenAI / DeepSeek 的缓存是前缀连续匹配，messages 列表也需稳定。当前 `format_assistant_tool_calls` 用 `json.dumps(ensure_ascii=False)`，已是稳定序列化，重点是不再动态插入 system reminder 类消息。
3. **`stream_chat` 异常 fallback**：阶段 2 必须覆盖 `stream_chat` 与 `chat` 两条路径，否则统计会丢失。
4. **审查清单**：禁止在 system prompt 中放入以下内容：
   - 时间戳（带分钟或更高粒度）
   - 随机 ID / UUID
   - 自增计数器
   - 单次任务相关的临时数据

## 7. 验收标准

- [ ] 同一 session 内连续 3 轮 `build_messages`，system prompt 的 SHA256 完全一致。
- [ ] OpenAI / DeepSeek 路径下，trace 显示 cache 命中率 ≥ 80%（从第 2 轮起）。
- [ ] Anthropic / Moonshot 路径下，trace 显示 `cache_read_input_tokens` > 0。
- [ ] CLI 输出包含 cache 命中率指示。
- [ ] 连续 3 turn 命中率低于 50% 时触发 warning。
- [ ] raw tokens < TOKEN_THRESHOLD × 0.85 时，trace 中不出现 `silent_compress` 事件（即 Layer 1/2 不应触发）。
- [ ] raw tokens 超过 TOKEN_THRESHOLD × 0.85 时，Layer 1/2 正常触发并写入 trace 事件。
- [ ] 长对话场景下（≥ 20 turn），命中率仍能维持在 ≥ 70%。

## 8. 参考资料

- [NousResearch/hermes-agent — GitHub](https://github.com/NousResearch/hermes-agent)
- [Hermes Agent: Architecture Overview](https://hermes-agent.nousresearch.com/docs/1-overview)
- [Hermes Agent: Conversation Loop Internals](https://hermes-agent.nousresearch.com/docs/10-conversation-loop-internals)
- [Hermes Agent: Context Engine and Compression](https://hermes-agent.nousresearch.com/docs/12-context-engine-and-compression)
- [OpenAI: Prompt Caching](https://platform.openai.com/docs/guides/prompt-caching)
- [DeepSeek: API Pricing (cache discount)](https://api-docs.deepseek.com/quick_start/pricing)
- [Anthropic: Prompt Caching](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching)
