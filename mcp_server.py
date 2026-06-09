#!/usr/bin/env python3
"""Mini-Agent MCP Server — expose agent tools to any MCP client.

Works with Claude Desktop, Cursor, and any MCP-compatible client.

Usage:
    python mcp_server.py                    # stdio transport (default)
    python mcp_server.py --transport sse    # SSE transport for web clients

Claude Desktop config:
    {
      "mcpServers": {
        "mini-agent": {
          "command": "python",
          "args": ["/path/to/mini-agent/mcp_server.py"]
        }
      }
    }
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from fastmcp import FastMCP

mcp = FastMCP("Mini-Agent")

_skills_loader = None
_registry = None


def _get_skills_loader():
    global _skills_loader
    if _skills_loader is None:
        from src.agent.skills import SkillsLoader
        _skills_loader = SkillsLoader()
    return _skills_loader


def _get_registry():
    global _registry
    if _registry is None:
        from src.tools import build_registry
        _registry = build_registry()
    return _registry


# ---------------------------------------------------------------------------
# Skill tools
# ---------------------------------------------------------------------------

@mcp.tool
def list_skills() -> str:
    """List all available skills with names and descriptions.

    Returns a JSON array of {name, description} for each skill.
    Use load_skill(name) to get the full documentation for any skill.
    """
    loader = _get_skills_loader()
    skills = [{"name": s.name, "description": s.description} for s in loader.skills]
    return json.dumps(skills, ensure_ascii=False, indent=2)


@mcp.tool
def load_skill(name: str) -> str:
    """Load full documentation for a named skill.

    Each skill is a comprehensive knowledge document. Use list_skills() first
    to discover available skills.

    Args:
        name: Skill name (e.g. 'example').
    """
    loader = _get_skills_loader()
    content = loader.get_content(name)
    if content.startswith("Error:"):
        return json.dumps({"status": "error", "error": content}, ensure_ascii=False)
    return json.dumps({"status": "ok", "skill": name, "content": content}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Web tools
# ---------------------------------------------------------------------------

@mcp.tool
def read_url(url: str) -> str:
    """Fetch a web page and convert it to clean Markdown text.

    Strips ads, navigation, and styling. Useful for reading docs,
    articles, and GitHub READMEs.

    Args:
        url: Target URL to read.
    """
    from src.tools.web_reader_tool import read_url as _read_url
    return _read_url(url)


@mcp.tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web via DuckDuckGo and return top results.

    Returns titles, URLs, and snippets. Use read_url() to fetch full content.
    Free, no API key required.

    Args:
        query: Search query string.
        max_results: Maximum results to return (default 5, max 10).
    """
    registry = _get_registry()
    return registry.execute("web_search", {
        "query": query, "max_results": min(max_results, 10),
    })


# ---------------------------------------------------------------------------
# File I/O tools
# ---------------------------------------------------------------------------

@mcp.tool
def write_file(path: str, content: str) -> str:
    """Write content to a file.

    Args:
        path: File path (relative to workspace or absolute).
        content: File content to write.
    """
    registry = _get_registry()
    return registry.execute("write_file", {"path": path, "content": content})


@mcp.tool
def read_file(path: str) -> str:
    """Read the contents of a file.

    Args:
        path: File path to read.
    """
    registry = _get_registry()
    return registry.execute("read_file", {"path": path})


@mcp.tool
def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Edit a file by replacing old_string with new_string.

    Args:
        path: File path to edit.
        old_string: Text to find and replace.
        new_string: Replacement text.
    """
    registry = _get_registry()
    return registry.execute("edit_file", {"path": path, "old_string": old_string, "new_string": new_string})


# ---------------------------------------------------------------------------
# Shell tool
# ---------------------------------------------------------------------------

@mcp.tool
def bash(command: str, timeout: int = 30) -> str:
    """Execute a shell command and return the output.

    Args:
        command: Shell command to execute.
        timeout: Timeout in seconds (default 30).
    """
    registry = _get_registry()
    return registry.execute("bash", {"command": command, "timeout": timeout})


# ---------------------------------------------------------------------------
# Memory tool
# ---------------------------------------------------------------------------

@mcp.tool
def remember(action: str, content: str = "", query: str = "") -> str:
    """Save, recall, or forget persistent memories across sessions.

    Args:
        action: One of 'save', 'recall', 'forget'.
        content: Memory content (for 'save').
        query: Search query (for 'recall' or 'forget').
    """
    registry = _get_registry()
    return registry.execute("remember", {"action": action, "content": content, "query": query})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Entry point for mini-agent MCP server."""
    import argparse

    parser = argparse.ArgumentParser(description="Mini-Agent MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio",
                        help="MCP transport (default: stdio)")
    parser.add_argument("--port", type=int, default=8900,
                        help="SSE port (only used with --transport sse)")
    args = parser.parse_args()

    if args.transport == "sse":
        mcp.run(transport="sse", port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
