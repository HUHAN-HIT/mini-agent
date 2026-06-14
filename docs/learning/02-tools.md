# 02 - Tool System（工具系统）

> 模块定位：`src/agent/tools.py` + `src/tools/` 全部文件
> 阅读对象：希望理解 mini-agent 工具系统设计、并有能力新增工具的学习者
> 前置阅读：建议先了解 ReAct Loop（详见 [03-loop.md](./03-loop.md)）

---

## 1. 模块概览

mini-agent 的工具系统遵循三个设计原则：

1. **自动发现（auto-discovery）** —— 放进 `src/tools/` 目录的任意 `BaseTool` 子类会被自动注册，零配置。
2. **声明式 schema** —— 每个工具用 JSON Schema 静态声明参数，注册时直接喂给 LLM 的 function calling。
3. **统一返回字符串** —— 所有 `execute()` 都返回 JSON 字符串，错误也包成 JSON，loop 层无需关心类型差异。

### 生命周期总览

```
                  +-----------------------------+
                  | 启动时：build_registry()     |
                  +-----------------------------+
                              |
                              v
        +-------------------------------------------+
        | 1. pkgutil.iter_modules 遍历 src/tools/   |
        | 2. importlib.import_module 触发每个模块   |
        |    —— 模块顶层执行 class XxxTool(BaseTool) |
        |       Python 元类记录到 BaseTool.__subclasses__|
        +-------------------------------------------+
                              |
                              v
        +-------------------------------------------+
        | 3. BFS 遍历 BaseTool.__subclasses__()     |
        |    收集所有带 name 的子类                 |
        +-------------------------------------------+
                              |
                              v
        +-------------------------------------------+
        | 4. cls.check_available() 过滤依赖缺失工具 |
        | 5. registry.register(cls())               |
        +-------------------------------------------+
                              |
                              v
                   +-----------------------+
                   | ToolRegistry 实例     |
                   | {name -> BaseTool}    |
                   +-----------------------+
                              |
              +---------------+----------------+
              v                                v
   loop.py: 把 schema 推给 LLM        LLM 决定调用 ->
   get_definitions()                  registry.execute(name, params)
                                          |
                                          v
                                  tool.execute(**params)
                                          |
                                          v
                                  JSON 字符串 -> 写回 memory
```

---

## 2. 核心抽象

### 2.1 `BaseTool` —— 工具接口契约

文件 `src/agent/tools.py:13-47`。

```python
class BaseTool(ABC):
    name: str = ""           # 唯一标识，必填且非空才会被注册
    description: str = ""    # 给 LLM 看的工具说明
    parameters: Dict[str, Any] = {}  # JSON Schema 描述参数
    repeatable: bool = False         # 是否允许在一次 loop 中多次调用
    is_readonly: bool = True         # 是否只读（影响审计/UI）

    @classmethod
    def check_available(cls) -> bool:
        """依赖检查，默认 True。子类可重写，例如 web_search 检查 ddgs 包。"""
        return True

    @abstractmethod
    def execute(self, **kwargs: Any) -> str:
        """执行工具，返回 JSON 字符串。"""

    def to_openai_schema(self) -> Dict[str, Any]:
        """转 OpenAI function calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}, "required": []},
            },
        }
```

**接口契约要点**：

- `name` 是注册 key，空字符串会被发现逻辑过滤掉（见 `src/tools/__init__.py:34`）。
- `parameters` 是 [JSON Schema](https://json-schema.org/) 草稿 07 风格，LLM 直接消费。
- `execute()` **必须**返回字符串（约定为 JSON），loop 不做二次包装。
- `repeatable=False` 的工具在一次 loop 中只会被「标记成功」一次（见 `loop.py` 中 `_called_ok` 集合）。

### 2.2 `ToolRegistry` —— 工具表

文件 `src/agent/tools.py:50-86`，是个简单的 `name -> BaseTool` 字典封装：

```python
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool            # 后注册会覆盖前者

    def get(self, name: str) -> Optional[BaseTool]: ...

    def get_definitions(self) -> List[Dict[str, Any]]:
        return [t.to_openai_schema() for t in self._tools.values()]   # 一次性输出全部 schema

    def execute(self, name: str, params: Dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if not tool:
            return json.dumps({"status": "error", "error": f"Tool '{name}' not found"}, ensure_ascii=False)
        try:
            return tool.execute(**params)
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            return json.dumps({"status": "error", "tool": name, "error": str(exc)}, ensure_ascii=False)
```

**关键设计**：

- `execute()` 做了一层兜底 try/except，**任何工具异常都不会穿透到 loop**，而是被包成 `{"status": "error", ...}` 字符串。这让 loop 极其健壮。
- `get_definitions()` 一次性把所有工具的 schema 输出，loop 把它直接喂给 LLM。
- `__contains__`、`__len__` 让 registry 支持自然语法 `if "bash" in registry`。

### 2.3 JSON Schema 怎么生成？

不是「生成」，而是**手工声明**为类属性。看 `bash_tool.py:18-22`：

```python
parameters = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "Shell command to execute"}
    },
    "required": ["command"],
}
```

`to_openai_schema()` 直接把它原样塞进 `function.parameters` 字段（见 `tools.py:38-47`）。LLM 收到后会按 schema 决定传什么参数。

---

## 3. 自动发现机制详解（重点）

这是 mini-agent 工具系统最巧妙的部分。核心代码在 `src/tools/__init__.py:13-39`。

### 3.1 为什么 `__subclasses__()` 能工作？

Python 的元类机制：**当一个类被定义时，解释器会自动把它加入父类的 `__subclasses__()` 列表**。这是 CPython 在 `type.__call__` 内部完成的，无需手动维护。

```python
class Base: pass
class A(Base): pass
class B(Base): pass
print(Base.__subclasses__())  # [<class 'A'>, <class 'B'>]
```

**但是有一个前提**：父类和子类都已经被 import 加载到内存。如果子类的模块从未被 import，CPython 自然不会知道它的存在。

这就引出下一个问题：怎么保证 `src/tools/` 下所有模块都被 import？

### 3.2 import 触发：`pkgutil + importlib`

`src/tools/__init__.py:21-28`：

```python
pkg_dir = str(Path(__file__).parent)
for _, module_name, _ in pkgutil.iter_modules([pkg_dir]):
    if module_name.startswith("_"):
        continue   # 跳过私有模块（如 path_utils.py 现在叫 path_utils，
                   # 如果以后改成 _path_utils 就不会被当工具模块）
    try:
        importlib.import_module(f"src.tools.{module_name}")
    except Exception as exc:
        logger.warning("Skipped src.tools.%s: %s", module_name, exc)
```

逻辑：
1. 用 `pkgutil.iter_modules` 枚举当前目录（`src/tools/`）下的所有模块名。
2. 跳过下划线开头的「私有」模块。
3. `importlib.import_module` 显式 import 每个模块 —— **模块被 import 的瞬间**，顶层 `class XxxTool(BaseTool)` 语句被执行，子类自动登记到 `BaseTool.__subclasses__()`。
4. 任何模块 import 失败都被捕获，记 warning，不影响其他工具。

### 3.3 BFS 收集所有子类（含孙类）

`src/tools/__init__.py:30-39`：

```python
classes: list[type[BaseTool]] = []
queue = deque(BaseTool.__subclasses__())   # 直接子类入队
while queue:
    cls = queue.popleft()
    if cls.name:                            # 跳过 name 为空的（例如中间抽象基类）
        classes.append(cls)
    queue.extend(cls.__subclasses__())      # 继续往孙类走
```

为何要 BFS？因为可能存在「工具基类的中分类」——比如有人写 `class FileToolBase(BaseTool)` 然后 `class ReadFileTool(FileToolBase)`，这种间接子类 `BaseTool.__subclasses__()` 不会直接列出，需要递归。

### 3.4 完整注册流程：`build_registry()`

`src/tools/__init__.py:42-57`：

```python
def build_registry(*, persistent_memory: "PersistentMemory | None" = None) -> ToolRegistry:
    from src.tools.remember_tool import RememberTool

    registry = ToolRegistry()
    for cls in _discover_subclasses():
        try:
            if not cls.check_available():                 # 依赖检查
                logger.info("Tool %s unavailable, skipping", cls.name)
                continue
            if cls is RememberTool and persistent_memory is not None:
                registry.register(cls(memory=persistent_memory))  # 注入外部依赖
            else:
                registry.register(cls())                  # 默认无参构造
        except Exception as exc:
            logger.warning("Failed to register tool %s: %s", cls.name, exc)
    return registry
```

两个亮点：
1. **`check_available()` 软过滤**：`web_search` 没装 `ddgs` 包就跳过，不会让整个 registry 启动失败。
2. **构造参数注入**：`RememberTool` 需要外部 `PersistentMemory` 实例，这里通过 `cls(memory=...)` 传入；其他工具默认无参构造。这是 mini-agent 中**唯一**一处对具体工具类的硬编码引用（`src/tools/__init__.py:43`）。

### 3.5 缓存机制

`_SUBCLASSES_CACHE`（`src/tools/__init__.py:13`）缓存发现结果，整个进程只发现一次。重复调用 `build_registry()` 也不会重新扫目录。

### 3.6 import 顺序与循环引用风险

⚠️ 注意 `_discover_subclasses()` 在 `src/tools/__init__.py:16` 中定义，但 **`build_registry()` 才真正触发发现**。这意味着：

- 仅 `import src.tools` 不会执行模块扫描，惰性触发。
- `src/tools/__init__.py:9` 自己只 import `BaseTool` 和 `ToolRegistry`，不 import 具体工具，避免循环。
- 具体工具模块（如 `remember_tool.py`）只能 import `src.agent.tools.BaseTool`，**不能** import `src.tools` 自身，否则会触发循环 import。

如果新增的工具 A 需要 import 工具 B 的某个常量，且 B 也间接 import A —— 会爆 `ImportError`。规避办法：把共享常量抽到 `path_utils.py` 这种 `_` 不参与发现的辅助模块。

---

## 4. 内置工具逐一精读

下面每个工具给一节，按职能分组。

### 4.1 `read_file` —— 读文件

文件：`src/tools/read_file_tool.py`。

- **用途**：读取工作目录或 skills 目录下的文件内容。
- **关键参数**：`path`（必填）、`limit`（可选，限制行数）。
- **返回**：`{"status": "ok", "path": "...", "content": "..."}`。
- **实现亮点**：
  - **双根搜索**：先在 `run_dir` 找，再在 `src/skills/` 找（`read_file_tool.py:33-38`），允许 LLM 通过 `skills/foo` 路径读取 skill 文档。
  - **路径沙箱**：通过 `safe_path()`（见 4.10）防止逃逸。
  - **输出截断**：单次最多返回 50 000 字符（`_OUTPUT_LIMIT`，`read_file_tool.py:12`），防止把超大文件灌进 context。

```json
{"path": "README.md", "limit": 100}
```

### 4.2 `write_file` —— 写文件

文件：`src/tools/write_file_tool.py`。

- **用途**：创建或覆盖工作目录中的文件。
- **关键参数**：`path`、`content` 均必填。
- **返回**：`{"status": "ok", "path": "...", "bytes_written": N}`。
- **实现亮点**：
  - `is_readonly = False`（写工具的标记，便于审计）。
  - `resolved.parent.mkdir(parents=True, exist_ok=True)` 自动建父目录（`write_file_tool.py:41`），LLM 不需要先调 `bash mkdir`。
  - 同样走 `safe_path()` 沙箱。

```json
{"path": "notes/todo.md", "content": "# TODO\n- [ ] demo"}
```

### 4.3 `edit_file` —— 精确替换

文件：`src/tools/edit_file_tool.py`。

- **用途**：在文件中找到 `old_text` 第一次出现，替换为 `new_text`。
- **关键参数**：`path`、`old_text`、`new_text` 全部必填。
- **返回**：`{"status": "ok", "message": "Edit applied"}`。
- **实现亮点**：
  - **精确匹配**：`content.replace(old_text, new_text, 1)`（`edit_file_tool.py:49`），第三个参数 `1` 表示只替换第一处，避免误伤重复片段。
  - **找不到原文本直接报错**（`edit_file_tool.py:47-48`），不让 LLM 假装成功 —— 这是「严格 replace」哲学，比模糊匹配更可控。

```json
{"path": "src/main.py", "old_text": "print('hi')", "new_text": "print('hello')"}
```

### 4.4 `bash` —— 执行 shell

文件：`src/tools/bash_tool.py`。

- **用途**：在 `run_dir` 下执行任意 shell 命令。
- **关键参数**：`command`（必填）。
- **返回**：`{"status": "ok"|"error", "exit_code": N, "stdout": "...", "stderr": "..."}`。
- **实现亮点**：
  - **超时保护**：默认 120 秒（`_DEFAULT_TIMEOUT`，`bash_tool.py:12`），超时单独返回 `status=error`，不会让 loop 卡死。
  - **输出截断**：stdout/stderr 各 50 000 字符上限（`bash_tool.py:36-37`），防止 `cat` 大文件撑爆 context。
  - **cwd 隔离**：`subprocess.run(..., cwd=run_dir)`（`bash_tool.py:31`），命令默认在工作目录执行，与系统其他位置隔离。
  - ⚠️ `shell=True` 是有意为之 —— 牺牲一点安全性换取「LLM 可以写管道、重定向」的能力。生产环境如果担心，可以继承后覆写。

```json
{"command": "pytest tests/ -x | tail -n 20"}
```

### 4.5 `web_search` —— 联网搜索

文件：`src/tools/web_search_tool.py`。

- **用途**：用 DuckDuckGo 搜索关键词。
- **关键参数**：`query`（必填）、`max_results`（可选，默认 5，硬上限 10）。
- **返回**：`{"status": "ok", "query": "...", "results": [{"title", "url", "snippet"}]}`。
- **实现亮点**：
  - **依赖软检测**：`check_available()`（`web_search_tool.py:14-23`）尝试 import `ddgs` 或旧版 `duckduckgo_search`，没有就告诉 registry「跳过我」。
  - **双库名兼容**：先试 `ddgs` 再试 `duckduckgo_search`，兼容新旧版包名。
  - **硬上限**：`min(max_results, 10)`（`web_search_tool.py:38`）防止 LLM 失控请求 1000 条结果。

```json
{"query": "OpenAI function calling best practices", "max_results": 5}
```

### 4.6 `read_url` —— 抓网页为 Markdown

文件：`src/tools/web_reader_tool.py`。

- **用途**：把任意 URL 抓回来转成 Markdown 文本。
- **关键参数**：`url`（必填）。
- **返回**：`{"status": "ok", "title": "...", "url": "...", "content": "..."}`。
- **实现亮点**：
  - **借力 Jina Reader**：实际请求 `https://r.jina.ai/{url}`（`web_reader_tool.py:11`），由 Jina 把 HTML 转成 Markdown，免维护 HTML 解析器。
  - **超时**：30 秒（`web_reader_tool.py:12`）。
  - **截断**：8 000 字符上限（`web_reader_tool.py:13`），网页文章普遍很长，更激进。
  - **提取 Title**：扫描前几行 `Title:` 前缀（`web_reader_tool.py:23-25`），符合 Jina 输出格式。

```json
{"url": "https://example.com/article"}
```

### 4.7 `compact` —— 上下文压缩（占位工具）

文件：`src/tools/compact_tool.py`。

- **用途**：让 LLM 主动触发对话历史压缩。
- **关键参数**：`focus_topic`（可选）。
- **返回**：`{"status": "ok", "message": "Compression triggered"}`。
- **实现亮点**：
  - **它本身不做压缩**！execute 只是返回一个 OK 标记（`compact_tool.py:23-24`）。
  - **真正的压缩发生在 loop 层**：loop 监听到这个工具被调用，会调用 `_compress_history()`（详见 loop 模块）对 memory 做摘要。这是工具系统与 loop 的**协议式协作** —— 工具只是信号，loop 才是执行者。
  - `is_readonly = False`，因为压缩会改 memory。

```json
{"focus_topic": "用户的部署配置需求"}
```

### 4.8 `load_skill` —— 加载 skill 文档

文件：`src/tools/load_skill_tool.py`。

- **用途**：按名称加载某个 skill 的完整 `SKILL.md` 内容。
- **关键参数**：`name`（必填）。
- **返回**：`{"status": "ok", "content": "..."}`。
- **实现亮点**：
  - **依赖注入**：构造函数接收外部 `SkillsLoader`（`load_skill_tool.py:24-25`），便于测试和复用。
  - **错误协议**：`SkillsLoader.get_content()` 失败时返回 `"Error: ..."` 字符串（约定），工具层据此设置 `status`（`load_skill_tool.py:31`）。注意这是「字符串约定」而非异常，反映了 mini-agent 倾向于「失败也用字符串表达」的设计选择。
  - `repeatable = True`：一次 loop 内可以加载多个 skill。

```json
{"name": "git-workflow"}
```

### 4.9 `remember` —— 持久化记忆

文件：`src/tools/remember_tool.py`。

- **用途**：跨 session 的长期记忆，支持 `save / recall / forget` 三种动作。
- **关键参数**：`action`（必填，enum），其余按动作变化。
- **返回**：每种动作不同，统一包 `{"status": "ok", ...}`。
- **实现亮点**：
  - **三合一 dispatch**：用 `action` 字段在一个工具内分发到 `_save / _recall / _forget`（`remember_tool.py:37-45`），而不是拆成三个工具 —— 减少 schema 数量，LLM 决策更轻松。
  - **依赖注入**：构造函数接 `PersistentMemory`（`remember_tool.py:34-35`），由 `build_registry()` 在 `src/tools/__init__.py:51-52` 特殊注入。
  - **recall 截断**：每条 memory body 只取前 2 000 字符（`remember_tool.py:61`），避免灌爆。

```json
{"action": "save", "title": "用户偏好", "content": "喜欢 TypeScript 而非 JavaScript", "memory_type": "user"}
```

```json
{"action": "recall", "query": "用户喜欢的语言"}
```

### 4.10 `safe_path` —— 路径沙箱（辅助函数，非工具）

文件：`src/tools/path_utils.py`。

它**不是**工具（不带 `BaseTool`，名字也不带 tool 后缀），是文件类工具共用的辅助函数：

```python
def safe_path(p: str, workdir: Path) -> Path:
    _rejects_unc(p)                                  # 拒绝 UNC 路径（\\server\share）
    base = Path(workdir).resolve()
    resolved = (base / p).resolve()
    try:
        resolved.relative_to(base)                   # 必须在 base 之内
    except ValueError as exc:
        raise ValueError(f"Path {p!r} escapes workspace {base}") from exc
    return resolved
```

**防御三类攻击**：

1. **路径穿越**：`../../../etc/passwd` 经 `resolve()` 后会跳出 `base`，`relative_to(base)` 抛错。
2. **UNC 路径**：`\\server\share` 在 Windows 上可能挂载外部资源，直接拒绝（`path_utils.py:8-10`）。
3. **符号链接**：`resolve()` 会解开 symlink，再 `relative_to` 检查 —— 即使攻击者构造 symlink 也跑不出去。

被 `read_file_tool.py:48`、`write_file_tool.py:36`、`edit_file_tool.py:38` 共同使用。

### 4.11 skill_writer_tool —— 四个 skill 管理工具

文件：`src/tools/skill_writer_tool.py`，**一个文件定义了 4 个工具**：`SaveSkillTool`、`PatchSkillTool`、`DeleteSkillTool`、`SkillFileTool`。

| 工具 | 用途 | 关键参数 |
|------|------|----------|
| `save_skill` | 把一次成功的工作流存成 skill | `name`, `content`, `category?` |
| `patch_skill` | 用 find/replace 修改已有 skill | `name`, `find`, `replace` |
| `delete_skill` | 删除整个 skill 目录 | `name` |
| `skill_file` | 管理 skill 的辅助文件（references/templates/examples/assets） | `action ∈ {write,remove,list}`, `skill_name`, `path?`, `content?` |

**实现亮点**：

- **slug 清洗**：`_sanitize_skill_name()` 用正则把任意名称压成 `[a-z0-9-]`（`skill_writer_tool.py:17-18`），防止文件名注入。
- **bundled -> user 复制**：`PatchSkillTool` 修改内置 skill 时，先把它复制到 user 目录再改（`skill_writer_tool.py:77-82`），保护内置 skill 只读。
- **白名单子目录**：`SkillFileTool.write` 限制路径首段必须在 `{references, templates, examples, assets}`（`skill_writer_tool.py:14, 147-149`），防止乱写。
- **SKILL.md 保护**：`skill_file` 的 remove 动作拒绝删除 `SKILL.md`（`skill_writer_tool.py:162-163`），那是 skill 的入口文件。

```json
{"name": "deploy-vercel", "content": "---\nname: deploy-vercel\n---\n步骤..."}
```

---

## 5. 如何添加自定义工具

只需 **3 步**，无需改任何注册代码。

### 步骤 1：新建文件 `src/tools/my_tool.py`

```python
"""My custom tool: greet the user."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool


class GreetTool(BaseTool):
    name = "greet"                          # 必填，全局唯一
    description = "Greet someone by name."  # 给 LLM 看的说明
    parameters = {
        "type": "object",
        "properties": {
            "who": {"type": "string", "description": "Person to greet"},
        },
        "required": ["who"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        who = kwargs["who"]
        return json.dumps({"status": "ok", "message": f"Hello, {who}!"}, ensure_ascii=False)
```

### 步骤 2：确保文件名**不**以下划线开头

`_discover_subclasses()` 会跳过 `_xxx.py`（见 `src/tools/__init__.py:23`）。

### 步骤 3：完成。

下次启动 mini-agent 时：
1. `pkgutil.iter_modules` 会发现 `my_tool.py`。
2. `importlib.import_module("src.tools.my_tool")` 触发类定义。
3. `GreetTool` 出现在 `BaseTool.__subclasses__()` 中。
4. `build_registry()` 自动注册它。

LLM 立刻就能在工具列表里看到 `greet` 并调用。

### 进阶：依赖外部对象的工具

如果工具需要外部依赖（像 `RememberTool` 需要 `PersistentMemory`），有两种模式：

- **构造参数注入**：参考 `src/tools/__init__.py:51-52` 的硬编码 if 分支。简单但需要改 registry 构建逻辑。
- **延迟初始化**：构造函数留空，`execute` 时通过 `kwargs` 接收（如 `run_dir` 就是这样传给 `bash_tool` 的，见 `bash_tool.py:28`）。

---

## 6. 关键类与方法清单

| 文件 | 类/函数 | 作用 |
|------|---------|------|
| `src/agent/tools.py:13` | `BaseTool` | 所有工具的 ABC 基类 |
| `src/agent/tools.py:29` | `BaseTool.check_available()` | 类方法，依赖检查 |
| `src/agent/tools.py:34` | `BaseTool.execute()` | 抽象方法，执行工具 |
| `src/agent/tools.py:38` | `BaseTool.to_openai_schema()` | 转 OpenAI function schema |
| `src/agent/tools.py:50` | `ToolRegistry` | 工具表，name -> BaseTool |
| `src/agent/tools.py:56` | `ToolRegistry.register()` | 注册一个工具实例 |
| `src/agent/tools.py:62` | `ToolRegistry.get_definitions()` | 输出全部工具 schema |
| `src/agent/tools.py:65` | `ToolRegistry.execute()` | 按名调用 + 异常兜底 |
| `src/tools/__init__.py:16` | `_discover_subclasses()` | 自动发现所有 BaseTool 子类 |
| `src/tools/__init__.py:42` | `build_registry()` | 构建并返回 ToolRegistry |
| `src/tools/path_utils.py:13` | `safe_path()` | 路径沙箱检查 |
| `src/tools/read_file_tool.py:15` | `ReadFileTool` | 读文件 |
| `src/tools/write_file_tool.py:13` | `WriteFileTool` | 写文件 |
| `src/tools/edit_file_tool.py:13` | `EditFileTool` | 精确替换 |
| `src/tools/bash_tool.py:15` | `BashTool` | 执行 shell |
| `src/tools/web_search_tool.py:11` | `WebSearchTool` | DuckDuckGo 搜索 |
| `src/tools/web_reader_tool.py:36` | `WebReaderTool` | URL 转 Markdown |
| `src/tools/compact_tool.py:11` | `CompactTool` | 触发上下文压缩（占位） |
| `src/tools/load_skill_tool.py:12` | `LoadSkillTool` | 加载 skill 文档 |
| `src/tools/remember_tool.py:12` | `RememberTool` | 持久记忆 CRUD |
| `src/tools/skill_writer_tool.py:21` | `SaveSkillTool` | 新建 skill |
| `src/tools/skill_writer_tool.py:52` | `PatchSkillTool` | 修改 skill |
| `src/tools/skill_writer_tool.py:93` | `DeleteSkillTool` | 删除 skill |
| `src/tools/skill_writer_tool.py:115` | `SkillFileTool` | skill 辅助文件管理 |

---

## 7. 学习要点

1. **`__subclasses__()` 的局限**：它只能列出「已被 import 的」子类。所以自动发现必须搭配 `pkgutil.iter_modules` + `importlib.import_module` 主动触发模块加载。这是 Python 元编程的经典组合拳。
2. **声明式 schema 优于反射**：mini-agent 没有用类型注解反射生成 schema，而是手工写 JSON Schema。代价是要维护两份（注解 + schema），收益是 schema 完全可控（可以写 description、enum、default），LLM 看到的提示更丰富。
3. **错误也用 JSON 字符串**：`registry.execute()` 的 try/except 把所有异常包成 JSON，意味着 loop 可以无差别地把工具结果塞回 `messages`，不用类型判断。这是一种「统一返回类型」的简化策略。
4. **`compact` 工具是协议占位**：它揭示了「工具可以只是信号」的设计模式 —— 真正的执行交给 loop。这种解耦让工具系统保持简单。
5. **构造参数注入的例外**：`RememberTool` 是唯一在 `build_registry()` 里被特殊处理的工具（`src/tools/__init__.py:51-52`）。这违反了「零配置」原则，是务实妥协 —— 为了不引入更复杂的依赖注入框架。
6. **路径沙箱的重要性**：`safe_path` 体现了 LLM agent 的安全底线 —— 模型可能输出任意路径，工具层必须有边界检查。

---

## 8. 思考题

1. **`__subclasses__()` 的顺序问题**：`build_registry()` 用 BFS 收集子类，如果两个工具 `name` 相同会发生什么？查看 `ToolRegistry.register()`（`tools.py:56-57`），思考这种「后者覆盖前者」的语义是否合理？如何改进（启动时检测重复并 warning）？

2. **`compact` 工具的设计**：如果让你重新设计，会不会把「触发压缩」做成 loop 内置的判断（比如 token 数超阈值自动压缩）而不是工具？两种方案各有什么优劣？提示：从「LLM 是否有自主决定权」角度思考。

3. **`bash` 的安全边界**：`shell=True` 让 LLM 可以执行任意命令，包括 `rm -rf`。如果你要在生产环境部署 mini-agent，会怎么加固？考虑：命令白名单、seccomp 沙箱、用户确认 hook、独立容器。

4. **`remember` 的三合一设计**：把 save/recall/forget 合并到一个工具，相比拆成三个独立工具，对 LLM 的决策难度有什么影响？schema 复杂度 vs 工具数量如何权衡？查阅 OpenAI function calling 文档，思考 LLM 在面对 enum 参数时的表现。

5. **自动发现的成本**：`_SUBCLASSES_CACHE` 缓存了发现结果（`src/tools/__init__.py:13`）。如果运行时新增了一个工具文件，需要重启进程才能生效。这个限制你能接受吗？如何实现「热加载」工具（提示：清空 cache + 重新 import 模块）？热加载会带来什么新风险？

---

## 9. 延伸阅读

- **[03-loop.md](./03-loop.md)** —— 看 `AgentLoop` 如何把 `registry.get_definitions()` 推给 LLM、如何解析 LLM 返回的 tool_call、如何调 `registry.execute()` 并把结果塞回 messages。重点理解 ReAct 循环里工具调用的位置。
- **[04-skills.md](./04-skills.md)** —— `load_skill_tool` 和 `skill_writer_tool` 都依赖 skills 系统。理解 `SkillsLoader.get_content()` 如何从 `src/skills/` 和用户目录加载 skill 文档，以及 skill 的 frontmatter 格式。
- **[01-context.md](./01-context.md)** —— 工具 schema 是怎么被组装进 system prompt 的？`ContextBuilder` 决定了 LLM 第一次看到工具列表时的格式。
- **OpenAI Function Calling Guide** —— https://platform.openai.com/docs/guides/function-calling ，理解 `to_openai_schema()` 输出的格式标准。
- **Python Data Model: `__subclasses__`** —— https://docs.python.org/3/reference/datamodel.html ，理解元类如何维护子类列表。
