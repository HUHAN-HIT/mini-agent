"""Preset loader: YAML-based team definitions with variable interpolation.

Presets live as .yaml files alongside this loader. Loading parses them into
TeamPreset objects and validates the DAG (cycles fail fast at load time).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from src.agent.team import TeamPreset, TeamRunner

logger = logging.getLogger(__name__)

_PRESETS_DIR = Path(__file__).resolve().parent
_CACHE: Dict[str, TeamPreset] = {}


def _load_yaml(path: Path) -> Dict:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyYAML not installed. Run: pip install pyyaml") from exc
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"preset {path} must be a YAML mapping at top level")
    return data


def load_preset(name: str) -> TeamPreset:
    """Load a preset by name. Cached after first load."""
    if name in _CACHE:
        return _CACHE[name]

    candidate = _PRESETS_DIR / f"{name}.yaml"
    if not candidate.exists():
        available = sorted(p.stem for p in _PRESETS_DIR.glob("*.yaml"))
        raise FileNotFoundError(
            f"preset '{name}' not found at {candidate}. Available: {available}"
        )

    raw = _load_yaml(candidate)
    preset = TeamPreset.from_dict(raw)

    TeamRunner._topo_sort(preset.agents)
    if preset.aggregator:
        all_agents = list(preset.agents)
        all_ids = {a.id for a in preset.agents}
        if preset.aggregator.id in all_ids:
            raise ValueError(f"aggregator id '{preset.aggregator.id}' conflicts with agent id")

    _CACHE[name] = preset
    logger.info("Loaded preset '%s' with %d agents", name, len(preset.agents))
    return preset


def list_presets() -> Dict[str, str]:
    """Return {preset_name: description} for all available presets."""
    out: Dict[str, str] = {}
    for path in sorted(_PRESETS_DIR.glob("*.yaml")):
        try:
            raw = _load_yaml(path)
            out[path.stem] = str(raw.get("description", "")).strip()
        except Exception as exc:
            logger.warning("Failed to scan preset %s: %s", path, exc)
    return out


def reload_preset(name: str) -> TeamPreset:
    """Force reload a preset (bypass cache)."""
    _CACHE.pop(name, None)
    return load_preset(name)


__all__ = ["load_preset", "list_presets", "reload_preset"]
