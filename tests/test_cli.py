from __future__ import annotations

from typer.testing import CliRunner

from minidex.cli import app


def test_help_lists_commands():
    r = CliRunner().invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in ("init", "universe", "filter", "fetch", "shortlist", "score", "qc", "build"):
        assert cmd in r.stdout


def test_init_invokes_db_init(monkeypatch):
    """CLI `init` dispatches to _init_db without side-effects on the real DB."""
    from minidex import cli as _cli

    called = {"n": 0}

    def _fake():
        called["n"] += 1

    monkeypatch.setattr(_cli, "_init_db", _fake)
    r = CliRunner().invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    assert called["n"] == 1
    assert "initialized" in r.output.lower()
