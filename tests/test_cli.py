from __future__ import annotations

from typer.testing import CliRunner

from minidex.cli import app


def test_help_lists_commands():
    r = CliRunner().invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in ("init", "universe", "filter", "fetch", "shortlist", "score", "qc", "build"):
        assert cmd in r.stdout


def test_init_creates_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from minidex import config

    config.get_settings.cache_clear()
    s = config.get_settings()
    if s.db_path.exists():
        s.db_path.unlink()
    r = CliRunner().invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    assert s.db_path.exists()
