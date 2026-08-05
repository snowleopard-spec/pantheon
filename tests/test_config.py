from __future__ import annotations

import json

from minidex import config


def test_settings_loads_from_env():
    s = config.get_settings()
    assert s.anthropic_api_key.startswith("sk-ant-")
    assert "test@example.com" in s.sec_user_agent
    assert s.similarity_threshold == 0.60
    # score_floor is user-tunable in config.json — assert it resolved to a
    # sane value, not a pinned business number.
    assert 0.0 <= s.score_floor <= 1.0
    assert s.embedding_model.startswith("BAAI/bge")
    assert s.batch_model.startswith("claude-")


def test_prompt_version_parsed():
    s = config.get_settings()
    assert s.prompt_version == "1.6"


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


def _clear_tuning_env(monkeypatch):
    for key in (
        "MINIDEX_EMBED_MODEL",
        "MINIDEX_SIM_THRESHOLD",
        "MINIDEX_SCORE_FLOOR",
        "MINIDEX_BATCH_MODEL",
        "MINIDEX_MAX_ITEM1_CHARS",
        "MINIDEX_ANCHOR_MIN_WEIGHT",
    ):
        monkeypatch.delenv(key, raising=False)


def test_missing_json_file_falls_back_to_defaults(monkeypatch, tmp_path):
    _clear_tuning_env(monkeypatch)
    monkeypatch.setattr(config, "CONFIG_JSON_PATH", tmp_path / "does_not_exist.json")
    config.get_settings.cache_clear()

    s = config.get_settings()
    assert s.embedding_model == "BAAI/bge-large-en-v1.5"
    assert s.similarity_threshold == 0.60
    assert s.score_floor == 0.10
    assert s.batch_model == "claude-haiku-4-5"
    assert s.max_item1_chars == 12_000
    assert s.anchor_min_weight == 0.05


def test_json_file_values_override_defaults(monkeypatch, tmp_path):
    _clear_tuning_env(monkeypatch)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "embedding_model": "test/embed-model",
                "similarity_threshold": 0.42,
                "score_floor": 0.07,
                "batch_model": "claude-sonnet-test",
                "max_item1_chars": 9999,
                "anchor_min_weight": 0.11,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_JSON_PATH", cfg_path)
    config.get_settings.cache_clear()

    s = config.get_settings()
    assert s.embedding_model == "test/embed-model"
    assert s.similarity_threshold == 0.42
    assert s.score_floor == 0.07
    assert s.batch_model == "claude-sonnet-test"
    assert s.max_item1_chars == 9999
    assert s.anchor_min_weight == 0.11


def test_env_var_beats_json_file(monkeypatch, tmp_path):
    _clear_tuning_env(monkeypatch)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "similarity_threshold": 0.42,
                "anchor_min_weight": 0.11,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_JSON_PATH", cfg_path)
    monkeypatch.setenv("MINIDEX_SIM_THRESHOLD", "0.77")
    monkeypatch.setenv("MINIDEX_ANCHOR_MIN_WEIGHT", "0.99")
    config.get_settings.cache_clear()

    s = config.get_settings()
    # env wins over JSON
    assert s.similarity_threshold == 0.77
    assert s.anchor_min_weight == 0.99


def test_partial_json_config_leaves_unspecified_at_defaults(monkeypatch, tmp_path):
    _clear_tuning_env(monkeypatch)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"anchor_min_weight": 0.25}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_JSON_PATH", cfg_path)
    config.get_settings.cache_clear()

    s = config.get_settings()
    assert s.anchor_min_weight == 0.25
    # everything else falls back to the hardcoded defaults
    assert s.embedding_model == "BAAI/bge-large-en-v1.5"
    assert s.similarity_threshold == 0.60
    assert s.score_floor == 0.10
    assert s.batch_model == "claude-haiku-4-5"
    assert s.max_item1_chars == 12_000


def test_malformed_json_raises(monkeypatch, tmp_path):
    _clear_tuning_env(monkeypatch)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_JSON_PATH", cfg_path)
    config.get_settings.cache_clear()

    import pytest

    with pytest.raises(RuntimeError, match="Failed to parse"):
        config.get_settings()


def test_non_object_json_raises(monkeypatch, tmp_path):
    _clear_tuning_env(monkeypatch)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_JSON_PATH", cfg_path)
    config.get_settings.cache_clear()

    import pytest

    with pytest.raises(RuntimeError, match="JSON object"):
        config.get_settings()
