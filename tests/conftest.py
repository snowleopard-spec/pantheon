from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _fake_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    monkeypatch.setenv("SEC_USER_AGENT", "minidex tests <test@example.com>")
    from minidex import config as _config

    _config.get_settings.cache_clear()
    yield
    _config.get_settings.cache_clear()
