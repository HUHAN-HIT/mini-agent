# Mini-Agent

A minimal, extensible ReAct agent framework extracted from [Vibe-Trading](https://github.com/MB-Ndhlovu/Vibe-Trading). Includes MCP server, Skills system, and persistent Memory.

## Features

- **ReAct Agent Loop** with 5-layer context compression (microcompact, context collapse, auto-compact, compact tool, iterative update)
- **Tool Registry** with auto-discovery via `BaseTool.__subclasses__()`
- **Progressive Disclosure Skills** — summaries in system prompt, full docs on demand
- **Persistent Memory** — file-based cross-session memory with keyword scoring
- **MCP Server** — expose tools to Claude Desktop, Cursor, and any MCP client
- **Session Layer** — filesystem persistence with SSE event bus and SQLite FTS5 search
- **13 LLM Providers** — OpenAI, DeepSeek, Zhipu, Moonshot, Qwen, Azure, Anthropic, Gemini, Ollama, OpenRouter, SiliconFlow, Groq, and custom

## Quick Start

```bash
# 1. Install dependencies
pip install -e .

# 2. Configure your LLM provider
cp .env.example .env
# Edit .env with your API key and provider

# 3a. Run interactive CLI
python cli.py

# 3b. Run MCP server (for Claude Desktop, Cursor, etc.)
python mcp_server.py

# 3c. Run MCP server with SSE transport
python mcp_server.py --transport sse --port 8900
```

## Project Structure

```
mini-agent/
├── src/
│   ├── agent/           # Core agent loop, context, tools, skills
│   │   ├── loop.py      # ReAct loop with tool batching
│   │   ├── context.py   # System prompt builder
│   │   ├── tools.py     # BaseTool ABC + ToolRegistry
│   │   ├── skills.py    # SkillsLoader with progressive disclosure
│   │   ├── memory.py    # WorkspaceMemory (single-run state)
│   │   ├── trace.py     # JSONL trace writer
│   │   └── frontmatter.py
│   ├── providers/       # LLM providers
│   │   ├── llm.py       # Factory for 13 providers
│   │   └── chat.py      # ChatLLM with stream/chat/async
│   ├── tools/           # Built-in tools (auto-discovered)
│   │   ├── read_file_tool.py
│   │   ├── write_file_tool.py
│   │   ├── edit_file_tool.py
│   │   ├── bash_tool.py
│   │   ├── web_search_tool.py
│   │   ├── web_reader_tool.py
│   │   ├── compact_tool.py
│   │   ├── load_skill_tool.py
│   │   ├── remember_tool.py
│   │   └── skill_writer_tool.py
│   ├── memory/          # Persistent cross-session memory
│   │   └── persistent.py
│   ├── core/            # Infrastructure
│   │   └── state.py     # RunStateStore
│   └── session/         # Session management (optional)
│       ├── models.py
│       ├── store.py
│       ├── service.py
│       ├── events.py
│       └── search.py
├── skills/              # Skill documents (Markdown + YAML frontmatter)
│   └── example/SKILL.md
├── runs/                # Run output directories
├── mcp_server.py        # MCP server entry point
├── cli.py               # Interactive CLI entry point
├── pyproject.toml
└── .env.example
```

## Adding Custom Tools

Create a new file in `src/tools/`:

```python
# src/tools/my_tool.py
from src.agent.tools import BaseTool

class MyTool(BaseTool):
    name = "my_tool"
    description = "Does something useful"

    def execute(self, **kwargs) -> str:
        return "result"
```

It will be auto-discovered and registered.

## Adding Custom Skills

Create a directory under `skills/`:

```markdown
<!-- skills/my-skill/SKILL.md -->
---
name: my-skill
description: What this skill does
triggers:
  - keyword
---

# My Skill
Full documentation here...
```

User skills can also go in `~/.mini-agent/skills/user/`.

## MCP Client Configuration

### Claude Desktop

```json
{
  "mcpServers": {
    "mini-agent": {
      "command": "python",
      "args": ["/path/to/mini-agent/mcp_server.py"]
    }
  }
}
```

### Cursor

Add to your Cursor MCP settings with the command `python /path/to/mini-agent/mcp_server.py`.

## Storage

All data is stored under `~/.mini-agent/`:

| Path | Contents |
|------|----------|
| `~/.mini-agent/.env` | API keys and provider config |
| `~/.mini-agent/memory/` | Persistent memories |
| `~/.mini-agent/skills/user/` | User-created skills |
| `~/.mini-agent/sessions.db` | Session search index |

## License

MIT
