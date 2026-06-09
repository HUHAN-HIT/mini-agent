"""Load skill tool: load full skill documentation by name."""

from __future__ import annotations

import json
from typing import Any

from src.agent.skills import SkillsLoader
from src.agent.tools import BaseTool


class LoadSkillTool(BaseTool):
    name = "load_skill"
    description = "Load full documentation for a named skill. Use this to learn about unfamiliar patterns or workflows before starting."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name"},
        },
        "required": ["name"],
    }
    repeatable = True

    def __init__(self, skills_loader: SkillsLoader | None = None) -> None:
        self._loader = skills_loader or SkillsLoader()

    def execute(self, **kwargs: Any) -> str:
        name = kwargs["name"]
        content = self._loader.get_content(name)
        return json.dumps({
            "status": "ok" if not content.startswith("Error:") else "error",
            "content": content,
        }, ensure_ascii=False)
