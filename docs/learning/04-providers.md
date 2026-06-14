# 04 · LLM Providers（模型提供方抽象）

> 模块路径：`src/providers/`
> 核心文件：`llm.py`（工厂）、`chat.py`（ChatLLM 客户端）
> 依赖：`langchain-openai>=0.3`、`openai>=1.30`、`python-dotenv>=1.0`（见 `pyproject.toml:13-26`）

---

## 1. 模块概览

### 1.1 为什么要抽象 LLM Provider？

mini-agent 是一个 ReAct Agent 框架。它的核心循环（`AgentLoop`）每轮都要调用一次 LLM，让它「想一步、做一步」。如果框架硬编码只支持 OpenAI，那它就只是「OpenAI Agent」而不是「Agent 框架」。

抽象出 Provider 层，带来三方面的好处：

| 维度 | 说明 |
|------|------|
| **成本** | 不同模型单价差 10 倍以上。推理任务用 DeepSeek-R1，日常对话用 Qwen-Turbo，长上下文用 Gemini 1.5，可以显著降低运行成本。 |
| **地域** | 国内访问 OpenAI 需要代理，而 DeepSeek、智谱、通义、Moonshot、MiniMax 均在国内有原生节点。 |
| **能力差异** | 不同模型在 tool calling、长上下文、推理、多模态上能力不同。Agent 跑得好不好，往往换一个模型就天差地别。 |

### 1.2 本模块的职责边界

`src/providers/` 只做一件事：**把"调用 LLM"这件事统一成一个干净的 Python 接口**。

它**不关心**：
- Agent 循环逻辑（在 `src/agent/loop.py`）
- 工具定义（在 `src/tools/`）
- 上下文构造（在 `src/agent/context.py`）
- 持久化记忆（在 `src/memory/`）

它**只暴露**一个类：`ChatLLM`，以及一个工厂函数：`build_llm()`。上层代码（`AgentLoop`、`SessionService`）只依赖 `ChatLLM`，不知道底层跑的是哪个模型。

```
┌─────────────────────────────────────────┐
│   AgentLoop / SessionService（调用方）    │
└──────────────────┬──────────────────────┘
                   │ 依赖
                   ▼
┌─────────────────────────────────────────┐
│           ChatLLM（chat.py）             │  ← 唯一对外接口
└──────────────────┬──────────────────────┘
                   │ 组合
                   ▼
┌─────────────────────────────────────────┐
│      build_llm()（llm.py 工厂）          │  ← 读 .env，选 provider
└──────────────────┬──────────────────────┘
                   │ 实例化
                   ▼
┌─────────────────────────────────────────┐
│  ChatOpenAIWithReasoning（langchain）    │  ← 实际 HTTP 客户端
└──────────────────┬──────────────────────┘
                   │ OpenAI-compatible HTTP/SSE
                   ▼
        OpenAI / DeepSeek / Zhipu / Qwen / ...
```

---

## 2. 核心设计：OpenAI-compatible 协议

### 2.1 为什么选 OpenAI 接口作为统一抽象？

mini-agent 的 Provider 层**没有**自己写 HTTP 客户端，而是直接复用了 `langchain_openai.ChatOpenAI`。这个决策背后的关键事实是：

> **OpenAI 的 Chat Completions API 已成为业界事实标准。**

市面上几乎所有主流模型厂商都提供"OpenAI 兼容端点"。这意味着只要把 `base_url` 指过去、`api_key` 换成厂商签发的，请求体和响应体格式几乎原样可用。这给 mini-agent 带来了一个巨大的红利：**接入一个新 provider 通常是 0 代码，只改配置**。

### 2.2 原生兼容 vs 需要适配

mini-agent 在 `llm.py:112-124` 维护了一张 `_PROVIDER_MAP`，把每个 provider 映射到它对应的「API Key 环境变量」和「Base URL 环境变量」：

| Provider | API Key 变量 | Base URL 变量 | 兼容方式 |
|----------|--------------|---------------|----------|
| `openai` | `OPENAI_API_KEY` | `OPENAI_BASE_URL` | 原生 |
| `deepseek` | `DEEPSEEK_API_KEY` | `DEEPSEEK_BASE_URL` | OpenAI 兼容端点 |
| `zhipu`（智谱） | `ZHIPU_API_KEY` | `ZHIPU_BASE_URL` | OpenAI 兼容端点 |
| `moonshot`（月之暗面） | `MOONSHOT_API_KEY` | `MOONSHOT_BASE_URL` | OpenAI 兼容端点 |
| `qwen` / `dashscope`（通义） | `DASHSCOPE_API_KEY` | `DASHSCOPE_BASE_URL` | OpenAI 兼容端点 |
| `gemini` | `GEMINI_API_KEY` | `GEMINI_BASE_URL` | OpenAI 兼容端点 |
| `groq` | `GROQ_API_KEY` | `GROQ_BASE_URL` | OpenAI 兼容端点 |
| `minimax` | `MINIMAX_API_KEY` | `MINIMAX_BASE_URL` | OpenAI 兼容端点 |
| `openrouter` | `OPENROUTER_API_KEY` | `OPENROUTER_BASE_URL` | 聚合层，OpenAI 兼容 |
| `ollama` | （无需 key） | `OLLAMA_BASE_URL` | 本地，OpenAI 兼容端点 |

> 注意：`.env.example` 里还提到了 `azure`、`anthropic`、`siliconflow`，但 `_PROVIDER_MAP` 中没有显式条目——它们会落到默认分支（按 OpenAI 标准变量名取值），见第 3 节分析。

---

## 3. Provider 工厂精读（`llm.py`）

### 3.1 整体职责

`build_llm()` 是整个模块的入口工厂。它做四件事：

1. **加载 `.env`**（多路径搜索）
2. **同步 provider 环境变量**（把厂商变量翻译成 OpenAI 标准变量）
3. **读模型参数**（model、temperature、timeout、retries）
4. **实例化 `ChatOpenAIWithReasoning`** 并返回

### 3.2 `.env` 多路径搜索（`llm.py:72-105`）

mini-agent 不要求用户把 `.env` 固定放在某处，而是按优先级搜索三个位置：

```python
_ENV_CANDIDATES = [
    Path.home() / ".mini-agent" / ".env",   # 用户全局
    AGENT_DIR / ".env",                      # 项目根
    Path.cwd() / ".env",                     # 当前工作目录
]
```

`_ensure_dotenv()` 用 `_dotenv_loaded` 全局标志保证只加载一次（`llm.py:80-105`）。如果没有装 `python-dotenv`，会走手写的极简 KV 解析器（`llm.py:87-94`），把 `KEY=VALUE` 灌进 `os.environ.setdefault`。

> **学习要点**：`setdefault` vs `load_dotenv(override=False)` 都是「不覆盖已有环境变量」的语义。这很重要——它允许用真正的 shell 环境变量临时覆盖 `.env`，便于测试和临时切模型。

### 3.3 Provider 环境变量同步（`llm.py:108-140`，核心）

这是整个多 provider 支持的"魔法"所在。函数 `_sync_provider_env()` 干的事是：

**把厂商特定的环境变量，统一翻译成 `ChatOpenAI` 认识的两个变量：`OPENAI_API_KEY` 和 `OPENAI_API_BASE`。**

```python
provider = os.getenv("LANGCHAIN_PROVIDER", "openai").lower()   # llm.py:110

spec = _PROVIDER_MAP.get(provider, _PROVIDER_MAP["openai"])    # llm.py:126
key_env, base_env = spec

# 取 key（厂商变量 → 回退到 OPENAI_API_KEY；ollama 无 key 时用占位 "ollama"）
if key_env is not None:
    api_key = os.getenv(key_env, "") or os.getenv("OPENAI_API_KEY", "")
else:
    api_key = os.getenv("OPENAI_API_KEY", "") or "ollama"

# 取 base_url（厂商变量 → 回退到 OPENAI_BASE_URL → 再回退 OPENAI_API_BASE）
base_url = os.getenv(base_env, "") or os.getenv("OPENAI_BASE_URL", "") \
           or os.getenv("OPENAI_API_BASE", "")

# 写回标准变量
if api_key:  os.environ["OPENAI_API_KEY"] = api_key               # llm.py:137
if base_url:
    os.environ["OPENAI_API_BASE"] = base_url                       # llm.py:139
    os.environ.setdefault("OPENAI_BASE_URL", base_url)             # llm.py:140
```

为什么这样设计？因为 `langchain_openai.ChatOpenAI` 在初始化时，如果不显式传参，会自动从 `OPENAI_API_KEY` / `OPENAI_BASE_URL` 读环境变量。所以**只要把环境变量摆对，剩下的实例化就是一行代码**。

> **关键洞察**：这里没有 `if provider == "deepseek": ...` 这种硬编码分支，全部是「查表 + 写环境变量」的数据驱动写法。新增一个 provider 等于在 `_PROVIDER_MAP` 里加一行。

### 3.4 `build_llm()` 实例化（`llm.py:143-163`）

```python
def build_llm(*, model_name=None, callbacks=None):
    _sync_provider_env()
    name = model_name or os.getenv("LANGCHAIN_MODEL_NAME", "").strip()
    if not name:
        raise RuntimeError("LANGCHAIN_MODEL_NAME is not set")     # llm.py:147
    temperature = float(os.getenv("LANGCHAIN_TEMPERATURE", "0.0"))
    provider = os.getenv("LANGCHAIN_PROVIDER", "openai").lower()

    if ChatOpenAI is None:
        raise RuntimeError("langchain-openai is not installed")   # llm.py:152

    # MiniMax 特殊兜底：temperature=0 会报错
    if provider == "minimax" and temperature <= 0.0:
        temperature = 0.01                                         # llm.py:153-154

    effort = os.getenv("LANGCHAIN_REASONING_EFFORT", "").strip().lower()
    return ChatOpenAIWithReasoning(
        model=name,
        temperature=temperature,
        timeout=int(os.getenv("TIMEOUT_SECONDS", "120")),          # llm.py:159
        max_retries=int(os.getenv("MAX_RETRIES", "2")),            # llm.py:160
        callbacks=callbacks,
        extra_body={"reasoning": {"effort": effort}} if effort else None,  # llm.py:162
    )
```

几个细节值得注意：

- **`temperature` 默认 0.0**：Agent 场景强调确定性，工具调用一旦随机就乱套。
- **MiniMax 兜底**（`llm.py:153-154`）：MiniMax 的 API 在 `temperature=0` 时会报错，所以强行抬到 0.01。这是少数需要在工厂里写死厂商特殊逻辑的地方。
- **`reasoning.effort`**（`llm.py:155, 162`）：通过 `extra_body` 透传给支持 reasoning control 的模型（如 OpenAI o 系列），控制推理深度。
- **重试与超时**：`MAX_RETRIES=2`、`TIMEOUT_SECONDS=120`，由 langchain 底层 SDK 实现，自动处理 429 / 5xx。

### 3.5 `ChatOpenAIWithReasoning` —— 一个对 reasoning 友好的子类（`llm.py:21-70`）

这是 mini-agent 自己定义的 `ChatOpenAI` 子类，目的只有一个：**保留模型返回的 `reasoning_content`（思考过程）字段**。

为什么需要它？因为标准的 `ChatOpenAI` 在解析响应时只关心 `content`、`tool_calls`，会把厂商扩展字段（如 DeepSeek-R1 的 `reasoning_content`、智谱的 `reasoning`）丢掉。该子类重写了三个内部方法：

| 方法 | 作用 | 代码位置 |
|------|------|----------|
| `_create_chat_result` | 非流式响应解析后，从原始 choice 里捞 `reasoning_content` 塞进 `additional_kwargs` | `llm.py:30-35` |
| `_convert_chunk_to_generation_chunk` | 流式 chunk 解析后，同样捞 reasoning | `llm.py:37-51` |
| `_get_request_payload` | 把 assistant 历史消息里的 `reasoning_content` 一起发回去（多轮对话时） | `llm.py:53-68` |

`_capture()`（`llm.py:25-28`）是个静态小工具，兼容两种字段名：

```python
if value := src.get("reasoning_content") or src.get("reasoning"):
    msg.additional_kwargs["reasoning_content"] = value
```

> **学习要点**：这种"继承官方类 + 重写私有方法"的做法是 langchain 生态里常见的扩展模式。它不侵入官方代码，但依赖了官方的私有方法名（`_create_chat_result` 等），SDK 升级时需要回归测试。

---

## 4. ChatLLM 精读（`chat.py`，重点）

`ChatLLM` 是 Provider 模块对外暴露的唯一类。它把底层 `langchain` 的 LLM 对象包装成「**消息进、`LLMResponse` 出**」的极简接口。

### 4.1 数据结构（`chat.py:19-35`）

```python
@dataclass
class ToolCallRequest:
    id: str                          # 工具调用 ID（用于回传 tool result）
    name: str                        # 工具名
    arguments: Dict[str, Any]        # 已解析的参数字典（不是 JSON 字符串）

@dataclass
class LLMResponse:
    content: Optional[str] = None                       # 文本回复
    tool_calls: List[ToolCallRequest] = field(...)      # 工具调用列表
    reasoning_content: Optional[str] = None             # 思考过程（如果有）
    finish_reason: str = "stop"                         # stop / tool_calls / length / ...

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0
```

> **设计要点**：`arguments` 直接是 `Dict`，而不是 JSON 字符串。这意味着上层代码不用再 `json.loads` 一次——`ChatLLM._parse_response` 已经处理过。这是降低调用方心智负担的好设计。

### 4.2 三种调用模式

`ChatLLM` 提供三种调用方式，对应不同场景：

| 方法 | 模式 | 用途 | 代码位置 |
|------|------|------|----------|
| `chat()` | 同步一次性 | 简单问答、不需要流式展示 | `chat.py:45-49` |
| `stream_chat()` | 同步流式 | Agent 主循环，边生成边推 UI | `chat.py:51-70` |
| `achat()` | 异步一次性 | 异步服务、并发调用 | `chat.py:72-76` |

#### 4.2.1 `chat()` —— 最简形式（`chat.py:45-49`）

```python
def chat(self, messages, tools=None, timeout=None):
    llm = self._llm.bind_tools(tools) if tools else self._llm   # 绑定工具 schema
    config = {"timeout": timeout} if timeout else {}
    ai_message = llm.invoke(messages, config=config)
    return self._parse_response(ai_message)
```

`bind_tools(tools)` 是 langchain 的 API：把 OpenAI function-calling 格式的工具定义"绑"到 LLM 上，之后每次调用都会带上 `tools` 字段。

#### 4.2.2 `stream_chat()` —— Agent 主战场（`chat.py:51-70`，重点）

这是 `AgentLoop` 实际用的方法（见 `src/agent/loop.py:312`）。

```python
def stream_chat(self, messages, tools=None, on_text_chunk=None, timeout=None):
    try:
        llm = self._llm.bind_tools(tools) if tools else self._llm
        config = {"timeout": timeout} if timeout else {}
        accumulated = None
        for chunk in llm.stream(messages, config=config):       # chat.py:62
            if chunk.content and on_text_chunk:
                on_text_chunk(chunk.content)                    # chat.py:64 流式回调
            accumulated = chunk if accumulated is None else accumulated + chunk  # chat.py:65
        if accumulated is None:
            return LLMResponse(content="", tool_calls=[], finish_reason="stop")
        return self._parse_response(accumulated)                # chat.py:68
    except Exception:
        return self.chat(messages, tools=tools, timeout=timeout)  # chat.py:70 兜底
```

**关键设计**：

1. **流式 chunk 累加**（`chat.py:65`）：langchain 的 chunk 对象支持 `+` 运算符，把多个 chunk 拼成一个完整的 message。最后只对累加结果调用一次 `_parse_response`，保证流式和非流式返回结构完全一致。
2. **文本增量回调**（`chat.py:63-64`）：每收到一个有内容的 chunk，立刻调用 `on_text_chunk(delta)`。`AgentLoop` 在 `src/agent/loop.py:306-308` 定义了这个回调，把 delta 转成 `text_delta` 事件推给前端，实现"打字机效果"。
3. **异常自动降级**（`chat.py:69-70`）：流式失败时，自动回退到非流式 `chat()`。这是一个朴素但有效的容错——某些 provider / 某些模型对 streaming 支持不稳定。

> **学习要点**：注意 `on_text_chunk` 只在 `chunk.content` 非空时触发。tool_calls 的增量不会走这个回调，它们最终在 `accumulated` 里被合并，再由 `_parse_response` 一次性提取。这意味着 UI 上看不到"工具调用正在生成"的逐字流，只能看到思考文本的逐字流。

#### 4.2.3 `achat()` —— 异步版本（`chat.py:72-76`）

```python
async def achat(self, messages, tools=None, timeout=None):
    llm = self._llm.bind_tools(tools) if tools else self._llm
    config = {"timeout": timeout} if timeout else {}
    ai_message = await llm.ainvoke(messages, config=config)
    return self._parse_response(ai_message)
```

逻辑和 `chat()` 一一对应，只是换成 `await ainvoke`。注意**没有 `astream_chat`**——如果 Agent 异步场景下需要流式，目前需要自己扩展。

### 4.3 响应解析（`chat.py:78-90`）

```python
@staticmethod
def _parse_response(ai_message) -> LLMResponse:
    return LLMResponse(
        content=ai_message.content,
        tool_calls=[
            ToolCallRequest(id=tc["id"], name=tc["name"], arguments=tc["args"])
            for tc in ai_message.tool_calls
        ],
        reasoning_content=ai_message.additional_kwargs.get("reasoning_content"),
        finish_reason=_dedupe_finish_reason(
            ai_message.response_metadata.get("finish_reason", "stop")
        ),
    )
```

`ai_message` 是 langchain 的 `AIMessage` 对象。这里把它的字段映射到 mini-agent 自己的 `LLMResponse` dataclass。三个细节：

1. `ai_message.tool_calls` 已经是 langchain 解析好的结构（`id` / `name` / `args`），直接取用。
2. `reasoning_content` 从 `additional_kwargs` 取——这正是 `ChatOpenAIWithReasoning` 辛苦保留的字段（见 3.5）。
3. `finish_reason` 经过 `_dedupe_finish_reason()` 清洗。

### 4.4 `_dedupe_finish_reason` —— 一个有意思的小工具（`chat.py:11-16`）

```python
def _dedupe_finish_reason(raw: str) -> str:
    return next(
        (m for m in ("tool_calls", "function_call", "content_filter",
                     "length", "stop")
         if raw.endswith(m)),
        raw,
    )
```

不同 provider 对 finish_reason 的命名略有差异：有的叫 `tool_calls`，有的叫 `finish_reason.tool_calls`，有的会带前缀。这个小函数用「后缀匹配」做了归一化：只要原始值以这五个标准词之一结尾，就归一成那个标准词；否则原样返回。

> **学习要点**：这是处理 provider 差异的一个典型模式——**在边界处归一化，让内部代码不用关心差异**。Agent 主循环只需要判断 `finish_reason == "tool_calls"` 就知道该执行工具了。

### 4.5 错误处理与重试策略总结

| 层级 | 机制 | 位置 |
|------|------|------|
| HTTP 层 | `max_retries=2`，由 openai SDK 自动重试 429/5xx | `llm.py:160` |
| 超时 | `timeout=120s`（默认），可被 `chat()` 的 `timeout` 参数覆盖 | `llm.py:159`，`chat.py:47` |
| 流式降级 | `stream_chat` 异常时自动 fallback 到 `chat()` | `chat.py:69-70` |
| Provider 缺失 | 未安装 langchain-openai 时抛 `RuntimeError` | `llm.py:151-152` |
| 配置缺失 | 未设 `LANGCHAIN_MODEL_NAME` 时抛 `RuntimeError` | `llm.py:146-147` |

> 注意：流式降级（`chat.py:70`）是「裸 `except Exception`」，这意味着**任何异常**（包括 KeyboardInterrupt 的近亲）都会被吞掉。在生产环境可能需要更细的异常分类，但对学习型项目来说，"先保证能跑"的优先级更高。

---

## 5. 配置与切换

### 5.1 `.env` 完整字段表

| 变量名 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| `LANGCHAIN_PROVIDER` | 否 | `openai` | 选择 provider，见 `_PROVIDER_MAP` |
| `LANGCHAIN_MODEL_NAME` | **是** | — | 模型名，如 `gpt-4o-mini`、`deepseek-chat`、`glm-4` |
| `LANGCHAIN_TEMPERATURE` | 否 | `0.0` | 采样温度 |
| `LANGCHAIN_REASONING_EFFORT` | 否 | 空 | 推理强度（low/medium/high），通过 `extra_body` 透传 |
| `TIMEOUT_SECONDS` | 否 | `120` | 单次请求超时（秒） |
| `MAX_RETRIES` | 否 | `2` | 自动重试次数 |
| `OPENAI_API_KEY` | 视情况 | — | OpenAI 标准 key，也是所有 provider 的最终回退 |
| `OPENAI_BASE_URL` / `OPENAI_API_BASE` | 视情况 | — | OpenAI 标准 base，最终生效地址 |
| `<PROVIDER>_API_KEY` | 视情况 | — | 厂商 key，如 `DEEPSEEK_API_KEY` |
| `<PROVIDER>_BASE_URL` | 视情况 | — | 厂商 base，如 `DEEPSEEK_BASE_URL` |

### 5.2 切换 provider 的步骤

以从 OpenAI 切到 DeepSeek 为例，**只需要改 `.env`，不需要改任何代码**：

```dotenv
# 改前
LANGCHAIN_PROVIDER=openai
LANGCHAIN_MODEL_NAME=gpt-4o-mini
OPENAI_API_KEY=sk-xxx

# 改后
LANGCHAIN_PROVIDER=deepseek
LANGCHAIN_MODEL_NAME=deepseek-chat
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
```

重启程序即可。`_sync_provider_env()` 会把 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` 翻译成 `OPENAI_API_KEY` / `OPENAI_API_BASE`。

### 5.3 多 provider 共存策略

mini-agent **当前不直接支持同时配置多个 provider 实例**——`_dotenv_loaded` 标志保证全局只加载一次配置，`build_llm()` 每次都读同一组环境变量。

但有两种实际可行的"多 provider 共存"用法：

1. **会话级覆盖**：因为 `_sync_provider_env()` 用 `setdefault` 语义，可以在调用 `ChatLLM()` 之前手动 `os.environ["LANGCHAIN_PROVIDER"] = "..."` 临时切换。但这是全局副作用，不线程安全。
2. **多个 ChatLLM 实例 + 显式 model_name**：`ChatLLM(model_name="...")` 会覆盖 `LANGCHAIN_MODEL_NAME`，但 provider 仍由全局 `LANGCHAIN_PROVIDER` 决定。要切 provider 必须改环境变量。

如果未来要支持真正的多 provider 并存（比如一个 Agent 同时调用一个便宜模型做路由、一个贵模型做推理），需要重构 `_sync_provider_env()`，让它接受显式参数而不是读全局环境变量。

### 5.4 一个真实的配置样例（脱敏）

下面的配置把「智谱 GLM」当作 OpenAI 兼容服务接入——这正是 mini-agent 作者本地实际跑的配置（密钥已脱敏）：

```dotenv
LANGCHAIN_PROVIDER=openai
LANGCHAIN_MODEL_NAME=glm-5.1
OPENAI_API_KEY=sk-xxx                              # 智谱签发的 key
OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4   # 智谱的 OpenAI 兼容端点
```

> **学习要点**：这个例子很有教学价值——它展示了"OpenAI 兼容"的最纯粹形态：连 `LANGCHAIN_PROVIDER` 都不用改，只要把 `OPENAI_BASE_URL` 指向厂商端点、`OPENAI_API_KEY` 填厂商签发的 key，就接通了。`_PROVIDER_MAP` 里的 `zhipu` 条目只是"更明确的写法"，本质和这个等价。

---

## 6. 关键类与方法清单

### `src/providers/llm.py`

| 符号 | 类型 | 说明 |
|------|------|------|
| `build_llm(*, model_name, callbacks)` | 函数 | 工厂入口，返回 langchain LLM 实例 |
| `_sync_provider_env()` | 函数（私有） | 把厂商环境变量翻译成 OpenAI 标准变量 |
| `_ensure_dotenv()` | 函数（私有） | 多路径加载 `.env`，只加载一次 |
| `_PROVIDER_MAP` | dict（局部） | provider → (key_env, base_env) 映射表 |
| `ChatOpenAIWithReasoning` | 类 | `ChatOpenAI` 子类，保留 reasoning_content |

### `src/providers/chat.py`

| 符号 | 类型 | 说明 |
|------|------|------|
| `ChatLLM` | 类 | 对外暴露的唯一客户端 |
| `ChatLLM.__init__(model_name)` | 方法 | 构造时调用 `build_llm()` |
| `ChatLLM.chat(messages, tools, timeout)` | 方法 | 同步一次性调用 |
| `ChatLLM.stream_chat(messages, tools, on_text_chunk, timeout)` | 方法 | 同步流式，Agent 主用 |
| `ChatLLM.achat(messages, tools, timeout)` | 协程 | 异步一次性调用 |
| `ChatLLM._parse_response(ai_message)` | 静态方法 | 把 langchain AIMessage 转成 LLMResponse |
| `LLMResponse` | dataclass | 统一响应结构 |
| `ToolCallRequest` | dataclass | 工具调用结构 |
| `_dedupe_finish_reason(raw)` | 函数（私有） | finish_reason 归一化 |

---

## 7. 学习要点

1. **数据驱动胜过条件分支**：`_PROVIDER_MAP` 用一张表替代了一堆 `if/elif`。新增 provider 是「加一行字典」而不是「加一段分支」。这是用对了抽象层次。
2. **边界归一化模式**：`_dedupe_finish_reason` 和 `_parse_response` 都是在「外部世界进入内部世界」的边界上把差异抹平，让 `AgentLoop` 不用关心是哪个 provider。
3. **流式累加模式**：`accumulated = chunk if accumulated is None else accumulated + chunk`（`chat.py:65`）——用 langchain chunk 的 `__add__` 把流式拼成非流式，复用同一个解析路径。
4. **环境变量即配置**：整个 Provider 层没有 YAML、没有 JSON 配置文件，全部用 `.env` + `os.getenv`。这是 12-factor app 的典型实践，便于容器化部署。
5. **继承官方类做扩展**：`ChatOpenAIWithReasoning` 不重写公开 API，只重写私有解析方法，既扩展了能力（保留 reasoning），又保留了 langchain 的所有原生功能（重试、超时、callbacks）。
6. **优雅降级**：`stream_chat` 异常自动 fallback 到 `chat`（`chat.py:69-70`），是"先可用、再优化"的工程哲学体现。

---

## 8. 思考题

1. **【新增 provider】** 假设要接入一个全新的、OpenAI 兼容的国产模型厂商「FooLLM」，需要修改哪些文件？如果 FooLLM 的 API 不完全兼容（比如 tool_calls 字段名不同），又该怎么办？
2. **【流式降级的副作用】** `stream_chat` 的 `except Exception: return self.chat(...)`（`chat.py:69-70`）会吞掉所有异常。如果 provider 返回了 401（key 错误），Agent 会陷入什么行为？如何改进？
3. **【reasoning 的多轮处理】** `ChatOpenAIWithReasoning._get_request_payload`（`llm.py:53-68`）会把 assistant 历史消息的 `reasoning_content` 一起发回给模型。这样做的好处和风险分别是什么？如果不发回会怎样？
4. **【多 provider 并存】** 当前架构一次只能用一个 provider。如果要让一个 Agent 同时调用两个不同 provider 的模型（比如便宜模型做意图分类、贵模型做工具调用），你会怎么重构 `_sync_provider_env()` 和 `build_llm()`？需要引入显式参数还是配置对象？
5. **【temperature=0 的代价】** Agent 默认 `temperature=0.0` 以保证工具调用稳定。但在「写代码」「写文案」这类创意任务上，0 温度会让输出干瘪。你会如何在不动 `AgentLoop` 的前提下，让某些步骤用更高的温度？（提示：`ChatLLM` 的方法签名已经留了口子吗？）

---

## 9. 延伸阅读

本文聚焦 Provider 层本身。想知道 `ChatLLM` 是怎么被消费的，请看：

- **`src/agent/loop.py`** —— `AgentLoop.run()` 在主循环里调用 `self.llm.stream_chat()`（`loop.py:312`），把 `on_text_chunk` 回调挂上去实现打字机效果，根据 `response.has_tool_calls` 决定是执行工具还是结束循环。
- **`src/session/service.py`** —— `SessionService._run_with_agent()`（`service.py:110`）展示了一个最小化的 ChatLLM 使用样例：`llm = ChatLLM()`，无参构造，完全靠 `.env` 驱动。
- 配套学习文档：`docs/learning/` 目录下的其他章节，特别是 Agent Loop 一章（ReAct 循环如何与 Provider 层配合）。
