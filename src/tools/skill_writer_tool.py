"""Skill management tools: full CRUD + auxiliary file support."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from src.agent.skills import USER_SKILLS_DIR
from src.agent.tools import BaseTool

_ALLOWED_SUBDIRS = {"references", "templates", "examples", "assets"}


def _sanitize_skill_name(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", name.lower().strip())[:60]


class SaveSkillTool(BaseTool):
    name = "save_skill"
    description = "Save a successful workflow as a reusable skill. Available in future sessions via load_skill."
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name (lowercase, a-z0-9 and hyphens)"},
            "content": {"type": "string", "description": "Full SKILL.md content including frontmatter"},
            "category": {"type": "string", "description": "Skill category. Default: user"},
        },
        "required": ["name", "content"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        name = kwargs.get("name", "")
        content = kwargs.get("content", "")
        category = kwargs.get("category", "user")
        if not name or not content:
            return json.dumps({"status": "error", "error": "name and content required"})
        slug = _sanitize_skill_name(name)
        skill_dir = USER_SKILLS_DIR / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"
        if not content.strip().startswith("---"):
            content = f"---\nname: {slug}\ndescription: User-created skill\ncategory: {category}\n---\n\n{content}"
        skill_path.write_text(content, encoding="utf-8")
        return json.dumps({"status": "ok", "message": f"Skill '{slug}' saved.", "path": str(skill_path)})


class PatchSkillTool(BaseTool):
    name = "patch_skill"
    description = "Fix or update an existing skill by replacing specific text."
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name to patch"},
            "find": {"type": "string", "description": "Text to find (exact match)"},
            "replace": {"type": "string", "description": "Replacement text"},
        },
        "required": ["name", "find", "replace"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        name = kwargs.get("name", "")
        find_text = kwargs.get("find", "")
        replace_text = kwargs.get("replace", "")
        if not name or not find_text:
            return json.dumps({"status": "error", "error": "name and find required"})
        slug = _sanitize_skill_name(name)
        user_path = USER_SKILLS_DIR / slug / "SKILL.md"
        bundled_dir = Path(__file__).resolve().parents[1] / "skills"
        bundled_path = bundled_dir / slug / "SKILL.md"
        if user_path.exists():
            skill_path = user_path
        elif bundled_path.exists():
            user_path.parent.mkdir(parents=True, exist_ok=True)
            user_path.write_text(bundled_path.read_text(encoding="utf-8"), encoding="utf-8")
            skill_path = user_path
        else:
            return json.dumps({"status": "error", "error": f"Skill '{name}' not found"})
        content = skill_path.read_text(encoding="utf-8")
        if find_text not in content:
            return json.dumps({"status": "error", "error": f"Text not found in skill '{name}'"})
        patched = content.replace(find_text, replace_text, 1)
        skill_path.write_text(patched, encoding="utf-8")
        return json.dumps({"status": "ok", "message": f"Patched skill '{name}': replaced 1 occurrence.", "path": str(skill_path)})


class DeleteSkillTool(BaseTool):
    name = "delete_skill"
    description = "Delete a user-created skill and all its files."
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Skill name to delete"}},
        "required": ["name"],
    }

    def execute(self, **kwargs: Any) -> str:
        name = kwargs.get("name", "").strip()
        if not name:
            return json.dumps({"status": "error", "error": "name required"})
        slug = _sanitize_skill_name(name)
        skill_dir = USER_SKILLS_DIR / slug
        if not skill_dir.exists():
            return json.dumps({"status": "error", "error": f"User skill '{slug}' not found"})
        shutil.rmtree(skill_dir)
        return json.dumps({"status": "ok", "message": f"Deleted skill '{name}'."})


class SkillFileTool(BaseTool):
    name = "skill_file"
    description = "Manage auxiliary files in a skill directory (references, templates, examples, assets)."
    is_readonly = False
    repeatable = True
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["write", "remove", "list"], "description": "Action to perform"},
            "skill_name": {"type": "string", "description": "Skill name"},
            "path": {"type": "string", "description": "File path relative to skill dir"},
            "content": {"type": "string", "description": "File content for write action"},
        },
        "required": ["action", "skill_name"],
    }

    def execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action", "")
        skill_name = kwargs.get("skill_name", "").strip()
        if not skill_name:
            return json.dumps({"status": "error", "error": "skill_name required"})
        skill_dir = USER_SKILLS_DIR / _sanitize_skill_name(skill_name)
        if not skill_dir.exists():
            return json.dumps({"status": "error", "error": f"User skill '{skill_name}' not found."})
        if action == "list":
            files = [{"path": str(p.relative_to(skill_dir)), "size": p.stat().st_size} for p in sorted(skill_dir.rglob("*")) if p.is_file()]
            return json.dumps({"status": "ok", "skill": skill_name, "files": files}, ensure_ascii=False)
        elif action == "write":
            rel_path = kwargs.get("path", "").strip()
            content = kwargs.get("content", "")
            if not rel_path or not content:
                return json.dumps({"status": "error", "error": "path and content required for write"})
            parts = Path(rel_path).parts
            if len(parts) < 2 or parts[0] not in _ALLOWED_SUBDIRS:
                return json.dumps({"status": "error", "error": f"Path must start with one of: {', '.join(sorted(_ALLOWED_SUBDIRS))}"})
            target = skill_dir / rel_path
            try:
                target.resolve().relative_to(skill_dir.resolve())
            except ValueError:
                return json.dumps({"status": "error", "error": "Path escapes skill directory"})
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return json.dumps({"status": "ok", "message": f"Written {rel_path}", "path": str(target)})
        elif action == "remove":
            rel_path = kwargs.get("path", "").strip()
            if not rel_path:
                return json.dumps({"status": "error", "error": "path required for remove"})
            if Path(rel_path).name == "SKILL.md":
                return json.dumps({"status": "error", "error": "Cannot remove SKILL.md. Use delete_skill."})
            target = skill_dir / rel_path
            try:
                target.resolve().relative_to(skill_dir.resolve())
            except ValueError:
                return json.dumps({"status": "error", "error": "Path escapes skill directory"})
            if not target.exists():
                return json.dumps({"status": "error", "error": f"File not found: {rel_path}"})
            target.unlink()
            return json.dumps({"status": "ok", "message": f"Removed {rel_path}"})
        return json.dumps({"status": "error", "error": f"Unknown action '{action}'"})
