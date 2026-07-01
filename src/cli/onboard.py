"""首次运行 onboarding 向导：选 provider → 填 model → 粘 key → 写 .env。

API key 只由用户键入并写入本地 .env（0600），绝不外传。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from src.providers.llm import PROVIDER_ENV_MAP

DEFAULT_ENV_PATH: Path = Path.home() / ".mini-agent" / ".env"

# provider -> 默认 model（补齐 PROVIDER_ENV_MAP 的展示信息）
_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "openrouter": "openai/gpt-4o-mini",
    "deepseek": "deepseek-chat",
    "gemini": "gemini-1.5-flash",
    "groq": "llama-3.1-8b-instant",
    "dashscope": "qwen-plus",
    "qwen": "qwen-plus",
    "zhipu": "glm-4-flash",
    "moonshot": "moonshot-v1-8k",
    "minimax": "MiniMax-M3",
    "ollama": "llama3",
}


@dataclass(frozen=True)
class ProviderInfo:
    key: str
    label: str
    key_env: Optional[str]
    base_env: str
    default_model: str


def _build_providers() -> tuple[ProviderInfo, ...]:
    infos: list[ProviderInfo] = []
    for key, (key_env, base_env) in PROVIDER_ENV_MAP.items():
        infos.append(ProviderInfo(
            key=key, label=key, key_env=key_env, base_env=base_env,
            default_model=_DEFAULT_MODELS.get(key, ""),
        ))
    return tuple(infos)


PROVIDERS: tuple[ProviderInfo, ...] = _build_providers()


def build_env_updates(provider: str, model: str, api_key: str,
                      base_url: str = "") -> dict[str, str]:
    """按 provider 生成要写入 .env 的键值。"""
    spec = PROVIDER_ENV_MAP.get(provider, PROVIDER_ENV_MAP["openai"])
    key_env, base_env = spec
    updates: dict[str, str] = {
        "LANGCHAIN_PROVIDER": provider,
        "LANGCHAIN_MODEL_NAME": model,
    }
    if key_env and api_key:
        updates[key_env] = api_key
    if base_url:
        updates[base_env] = base_url
    return updates


def merge_env_file(path: Path, updates: dict[str, str]) -> None:
    """把 updates 合并进 .env：已有键覆盖，其余追加。尽力 chmod 600。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    seen: dict[str, int] = {}
    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k = s.split("=", 1)[0].strip()
        seen[k] = i
    for key, value in updates.items():
        newline = f"{key}={value}"
        if key in seen:
            lines[seen[key]] = newline
        else:
            lines.append(newline)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def needs_onboarding() -> bool:
    """判断是否缺少可用配置（model 或对应 key 缺失）。"""
    from src.providers import llm as _llm

    _llm._ensure_dotenv()
    provider = os.getenv("LANGCHAIN_PROVIDER", "").strip().lower()
    model = os.getenv("LANGCHAIN_MODEL_NAME", "").strip()
    if not provider or not model:
        return True
    key_env, _ = PROVIDER_ENV_MAP.get(provider, PROVIDER_ENV_MAP["openai"])
    if key_env is None:  # ollama 无需 key
        return False
    return not (os.getenv(key_env) or os.getenv("OPENAI_API_KEY"))


def _default_prompt(label: str, *, is_password: bool = False) -> str:
    """默认交互读取。TTY 用 prompt_toolkit（key 掩码），否则退回 input。"""
    import sys

    if sys.stdin.isatty():
        try:
            from prompt_toolkit import prompt as pt_prompt

            return pt_prompt(label, is_password=is_password).strip()
        except Exception:  # noqa: BLE001
            pass
    return input(label).strip()


def run_onboarding(console, *, prompt_fn: Optional[Callable[..., str]] = None,
                   env_path: Optional[Path] = None) -> Path:
    """运行向导，写入 .env 并同步 os.environ；返回写入路径。"""
    from rich.text import Text

    ask = prompt_fn or _default_prompt
    path = env_path or DEFAULT_ENV_PATH

    console.print(Text("首次运行配置向导", style="bold"))
    console.print("可用 provider：")
    for i, p in enumerate(PROVIDERS, start=1):
        console.print(f"  {i:>2}. {p.label}")

    raw = ask("选择 provider 序号 [1]: ") or "1"
    try:
        idx = max(1, min(len(PROVIDERS), int(raw)))
    except ValueError:
        idx = 1
    provider = PROVIDERS[idx - 1]

    model = ask(f"model 名称 [{provider.default_model}]: ") or provider.default_model

    api_key = ""
    if provider.key_env is not None:
        api_key = ask(f"{provider.key_env}（粘贴你的 API key）: ", is_password=True)

    base_url = ask("base_url（可选，回车跳过）: ")

    updates = build_env_updates(provider.key, model, api_key, base_url)
    merge_env_file(path, updates)
    for k, v in updates.items():
        os.environ[k] = v
    console.print(Text(f"已写入 {path}", style="bold"))
    return path


__all__ = [
    "ProviderInfo", "PROVIDERS", "DEFAULT_ENV_PATH",
    "build_env_updates", "merge_env_file", "needs_onboarding", "run_onboarding",
]
