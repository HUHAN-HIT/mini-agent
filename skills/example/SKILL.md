---
name: example
description: An example skill demonstrating the skill format and available tools
triggers:
  - example
  - demo
---

# Example Skill

## Overview

This is an example skill that demonstrates the skill file format. Skills are
Markdown documents with YAML frontmatter that get injected into the agent's
system prompt as concise summaries. When the agent needs details, it calls
`load_skill("example")` to retrieve the full content.

## Guidelines

1. **File format**: YAML frontmatter between `---` delimiters, followed by Markdown body.
2. **Frontmatter fields**:
   - `name`: unique skill identifier (required)
   - `description`: one-line summary shown in skill listings (required)
   - `triggers`: list of keywords that activate the skill (optional)
3. **Body**: Full documentation the agent reads on demand via `load_skill`.

## Available Tools

| Tool | Description |
|------|-------------|
| `read_file` | Read file contents |
| `write_file` | Write content to a file |
| `edit_file` | Edit files with find/replace |
| `bash` | Execute shell commands |
| `web_search` | Search the web via DuckDuckGo |
| `read_url` | Fetch and convert URL to Markdown |
| `remember` | Save/recall/forget persistent memories |
| `load_skill` | Load full skill documentation |
| `compact` | Summarize and compact context |

## Example Usage

```
User: Help me create a Python project
Agent: I'll create the project structure using write_file and bash tools.
```
