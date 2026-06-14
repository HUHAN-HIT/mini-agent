# Subagent & Agent Team 模块设计

mini-agent 的多 agent 协作模块。融合 hermes-agent（delegate 工具）、Vibe-Trading（DAG 团队）、OpenHarness（工具驱动编排）三个参考工程的精华。

---

## 1. 设计目标

| 问题 | 解决方案 |
|---|---|
| 复杂任务让主 agent 上下文膨胀、频繁 compact | 子 agent 独立上下文，主 agent 只看 summary |
| 没有"专家分工"机制 | 角色 + 工具白名单，每个子 agent 专注一类任务 |
| 无法并行执行独立子任务 | DAG 调度，层内并行 |

## 2. 模块结构

```
src/agent/
  loop.py            # 现有，复用
  subagent.py        # SubAgentContext / SubAgentConfig / SubAgentRunner
  team.py            # AgentSpec / TeamPreset / TeamRunner (Kahn 拓扑排序)
  presets/
    loader.py        # YAML 加载 + 变量插值 + 加载时环检测
    research_team.yaml
    code_review_team.yaml
src/tools/
  delegate_tool.py   # 单子 agent 派发工具
  team_tool.py       # 团队派发工具
```

## 3. 核心抽象

### 3.1 SubAgentContext — 派发血统

跨层传递的上下文，防止递归失控：

```python
@dataclass
class SubAgentContext:
    depth: int = 0                  # 0=父, 1=子, 2=孙（上限）
    parent_run_dir: Optional[Path]
    parent_session_id: str = ""
```

### 3.2 SubAgentConfig — 单次派发配置

```python
@dataclass
class SubAgentConfig:
    role: str = "leaf"                       # leaf | specialist
    goal: str = ""                           # 必填
    context: str = ""                        # 父 agent 注入的背景
    tools_whitelist: Optional[List[str]]     # None=继承父工具除黑名单
    model_name: Optional[str]                # None=复用父 LLM 实例
    max_iterations: int = 15
    timeout_sec: int = 180
```

### 3.3 SubAgentRunner — 执行器

关键逻辑：
1. **深度检查**：`ctx.depth >= MAX_DEPTH(2)` 直接返回 failed
2. **隔离 run_dir**：`parent_run_dir / "subagents" / f"{role}_{ms}"`
3. **过滤 registry**：白名单 ∩ 父工具 − 黑名单（`delegate/spawn_team/compact`）
4. **复用 LLM**：model_name 为 None 或与父一致时复用，否则 `new ChatLLM(model_name)`
5. **独立 WorkspaceMemory**：`run_dir=sub_run_dir`
6. **event_callback 转发**：加 `subagent.` 前缀，附 depth/role
7. **超时保护**：`ThreadPoolExecutor(max_workers=1)` + `future.result(timeout)`
8. **错误降级**：失败返回 dict（status=failed），不抛异常

### 3.4 TeamPreset — YAML 团队定义

```yaml
name: research_team
description: "Parallel web research with synthesis"
variables:
  topic: {required: true}

agents:
  - id: tech_searcher
    role: leaf
    goal: "Find 5 technical blog sources about {{topic}}"
    tools: [web_search, read_url]
    max_iterations: 12

  - id: synthesizer
    role: specialist
    depends_on: [tech_searcher, official_searcher]
    input_from: [tech_searcher, official_searcher]
    goal: "Merge two research streams into a 400-word brief"
    tools: [read_file, write_file]

aggregator:
  id: report_writer
  role: specialist
  goal: "Write final markdown report to research.md"
  tools: [write_file]
```

### 3.5 TeamRunner — DAG 调度器

执行流程：
1. **变量插值**：Jinja 风格 `{{var}}` 替换
2. **拓扑排序**：Kahn 算法 + 分层（同层节点 depends_on 都已完成）
3. **环检测**：某轮无 indegree=0 节点时立即抛 `ValueError`
4. **层内并行**：`ThreadPoolExecutor(min(len, 4))` + 全局 `Semaphore(8)`
5. **upstream 注入**：上游 agent 的 content（截断 4000 字符）拼成 `<upstream-results>` 块作为下游的 user message 前缀
6. **可选 aggregator**：合成最终结果，截断 8000 字符返回

## 4. 安全机制

| 机制 | 实现 | 参考来源 |
|---|---|---|
| 深度限制 | `SubAgentContext.depth >= 2` 拒绝 | hermes-agent `max_spawn_depth` |
| 工具黑名单 | leaf 角色看不到 `delegate/spawn_team/compact` | hermes-agent 工具集动态限制 |
| DAG 环检测 | 加载时 Kahn 算法跑空校验，立即报错 | Vibe-Trading |
| 超时保护 | 单 agent 180s，`future.result(timeout)` 触发 `loop.cancel()` | hermes-agent |
| 并发限制 | 层内 `min(len, 4)`，全局 `Semaphore(8)` | hermes-agent `max_concurrent_children` |
| 错误降级 | 失败作为 tool_result 返回（status=error），不抛异常 | OpenHarness |

## 5. 关键设计决策

### 决策 1：子 agent 不共享 PersistentMemory

`SubAgentRunner` 构造 AgentLoop 时传 `persistent_memory=None`。

**理由**：
- 主 agent 的 PersistentMemory 可能含跨会话偏好/历史，会让子 agent 上下文污染
- 子 agent 应聚焦当前 goal，不该被无关记忆干扰
- 节省 token 预算（auto-recall 会注入 3 条记忆到 user message）

### 决策 2：WorkspaceMemory.run_dir = sub_run_dir（非父 run_dir）

**理由**：
- 子 agent 的 trace 应写到独立子目录，便于排查
- 子 agent 调 `write_file` 写到子目录，避免与兄弟 agent 互相覆盖
- 父 agent 通过 `result["subagent_run_dir"]` 定位子产物

> ⚠️ 这与最初设计相反。最初打算 memory.run_dir=parent 让文件写到父目录，但导致 trace 合并 bug。详见 `troubleshooting.md` 问题 4。

### 决策 3：upstream_context 作为独立 user message

格式（非塞 system_prompt）：
```
<upstream-results>
[searcher_a]: {content}
[searcher_b]: {content}
</upstream-results>

Your task: {goal}
```

**理由**：
- system_prompt 来自 ContextBuilder 固定模板，改动会影响所有 agent
- 多上游合并时结构化块更清晰
- 每个上游截断 4000 字符，总预算可控

### 决策 4：model 复用优先

```python
def _resolve_llm(self, model_name):
    if model_name is None or model_name == self._parent_llm.model_name:
        return self._parent_llm              # 复用实例
    return ChatLLM(model_name=model_name)    # 新建
```

**理由**：
- ChatLLM 无状态，复用安全
- 节省底层 HTTP 连接池初始化成本
- 仅当 YAML 显式指定不同 model 时才新建

### 决策 5：trace 嵌套用链接条目

父 trace 不直接包含子的所有事件，只写两条链接：

```jsonl
{"type":"subagent_start","role":"leaf","sub_run_dir":"...","goal":"...","depth":1}
{"type":"subagent_end","role":"leaf","status":"success","sub_run_dir":"...","depth":1}
```

子 agent 的完整 trace 在 `sub_run_dir/trace.jsonl`。

**理由**：
- 父 trace 保持简洁，不被子事件淹没
- 跨层排查时按 `sub_run_dir` 跳转即可

## 6. 使用方式

### 6.1 单子 agent

```python
from src.agent.subagent import SubAgentConfig, SubAgentContext, SubAgentRunner

cfg = SubAgentConfig(
    role="leaf",
    goal="Read 5 files in /src and summarize key patterns",
    tools_whitelist=["read_file", "bash"],
    max_iterations=15,
)
ctx = SubAgentContext(depth=0, parent_run_dir=run_dir)
result = SubAgentRunner(llm, registry, run_dir, ctx).run(cfg)
print(result["content"])  # 子 agent 的 summary
```

### 6.2 团队派发

```python
from src.agent.presets import load_preset
from src.agent.team import TeamRunner

preset = load_preset("research_team")
result = TeamRunner(llm, registry, run_dir, ctx).run(preset, {"topic": "AI Agent frameworks"})
print(result["content"])  # aggregator 合成的最终报告
```

### 6.3 LLM 自动调用（生产用法）

主 agent 看到 system prompt 的 "Subagent & Team Delegation" 段后，会自主调用 `delegate` / `spawn_team` 工具：

```bash
python cli.py
You> 用 delegate 派一个子 agent 调研 Rust 2026 就业行情
You> 用 spawn_team 派 research_team 调研 "AI Agent 框架对比"
```

## 7. 扩展指南

### 7.1 添加新 preset

在 `src/agent/presets/` 新建 YAML：

```yaml
name: my_team
description: "..."
variables:
  input: {required: true}
agents:
  - id: worker_1
    goal: "Process {{input}}"
    tools: [...]
aggregator:
  id: merger
  goal: "Merge results"
```

加载时自动校验 DAG（环检测），无需额外代码。

### 7.2 添加新角色

修改 `SubAgentConfig.role` 的取值集合，并在 `_filter_registry` 中按角色定制黑名单。

### 7.3 跨 agent 共享状态

当前不支持。如果需要，可：
- 通过 `SubAgentConfig.context` 显式传递（推荐）
- 让多个 agent 写文件到同一个 shared 目录，下游通过 `read_file` 读取
- 引入 `WorkspaceState` 共享对象（需扩展 SubAgentRunner）

## 8. 测试

- **单元测试**：`tests/test_mock_team.py`（mock LLM，5 个测试覆盖全部核心逻辑）
- **真实 LLM 测试**：`tests/test_subagent_team.py`（依赖 glm-5.1 API，注意间歇性挂死，见 `troubleshooting.md` 问题 2）

## 9. 参考工程

| 工程 | 借鉴点 |
|---|---|
| hermes-agent | `delegate_task` 工具模式 + 角色系统 + 深度限制 + 同步并行 |
| Vibe-Trading | YAML preset + DAG 拓扑排序 + upstream_context 注入 + Aggregator 合成 |
| OpenHarness | 工具驱动编排（非框架驱动） + 错误降级 + 极简注册表 |
