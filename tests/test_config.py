from __future__ import annotations

from minidex import config


def test_settings_loads_from_env():
    s = config.get_settings()
    assert s.anthropic_api_key.startswith("sk-ant-")
    assert "test@example.com" in s.sec_user_agent
    assert s.similarity_threshold == 0.60
    assert s.score_floor == 0.10
    assert s.embedding_model.startswith("BAAI/bge")
    assert s.batch_model.startswith("claude-")


def test_prompt_version_parsed():
    s = config.get_settings()
    assert s.prompt_version == "1.2"


def test_paths_exist():
    s = config.get_settings()
    assert s.definitions_path.exists()
    assert s.prompt_path.exists()
    assert s.data_dir.exists()
    assert s.outputs_dir.exists()


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config.get_settings.cache_clear()
    import pytest

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        config.get_settings()
