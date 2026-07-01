"""onboard.py 向导测试（不触发真实交互）。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def test_providers_derived_from_llm_map() -> None:
    from src.cli.onboard import PROVIDERS
    from src.providers.llm import PROVIDER_ENV_MAP

    keys = {p.key for p in PROVIDERS}
    assert "openai" in keys and "deepseek" in keys and "ollama" in keys
    # 每个 provider 的 key_env 与 llm 映射一致
    for p in PROVIDERS:
        assert p.key_env == PROVIDER_ENV_MAP[p.key][0]


def test_build_env_updates_openai() -> None:
    from src.cli.onboard import build_env_updates

    upd = build_env_updates("openai", "gpt-4o-mini", "sk-abc", "")
    assert upd["LANGCHAIN_PROVIDER"] == "openai"
    assert upd["LANGCHAIN_MODEL_NAME"] == "gpt-4o-mini"
    assert upd["OPENAI_API_KEY"] == "sk-abc"


def test_build_env_updates_ollama_no_key() -> None:
    from src.cli.onboard import build_env_updates

    upd = build_env_updates("ollama", "llama3", "", "http://localhost:11434")
    assert "OLLAMA_BASE_URL" in upd
    assert not any(k.endswith("_API_KEY") for k in upd)


def test_merge_env_file_roundtrip(tmp_path) -> None:
    from src.cli.onboard import merge_env_file

    env = tmp_path / ".env"
    env.write_text("EXISTING=1\nLANGCHAIN_PROVIDER=old\n", encoding="utf-8")
    merge_env_file(env, {"LANGCHAIN_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"})
    text = env.read_text(encoding="utf-8")
    assert "EXISTING=1" in text
    assert "LANGCHAIN_PROVIDER=openai" in text
    assert "LANGCHAIN_PROVIDER=old" not in text
    assert "OPENAI_API_KEY=sk-x" in text


def test_run_onboarding_writes_env(tmp_path, monkeypatch) -> None:
    from rich.console import Console
    from src.cli import onboard

    answers = iter(["1", "gpt-4o-mini", "sk-test", ""])  # provider#、model、key、base_url

    def fake_prompt(label, *, is_password=False):
        return next(answers)

    env = tmp_path / ".env"
    path = onboard.run_onboarding(Console(no_color=True), prompt_fn=fake_prompt, env_path=env)
    assert path == env
    text = env.read_text(encoding="utf-8")
    assert "LANGCHAIN_MODEL_NAME=gpt-4o-mini" in text
    assert "sk-test" in text
    import os
    assert os.environ.get("LANGCHAIN_MODEL_NAME") == "gpt-4o-mini"
