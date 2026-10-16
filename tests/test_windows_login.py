from claude_fleet import cli


def test_windows_oauth_login_uses_ten_minute_callback_window(monkeypatch):
    captured = {}

    def fake_run(arguments, *, environment_defaults=None):
        captured["arguments"] = list(arguments)
        captured["defaults"] = dict(environment_defaults or {})

    monkeypatch.setattr(cli, "run_teamclaude_cli", fake_run)
    monkeypatch.setattr(cli.os, "name", "nt")

    cli.run_login("max-1")

    assert captured["arguments"] == ["login", "--oauth", "--name", "max-1"]
    assert captured["defaults"]["TEAMCLAUDE_LOGIN_TIMEOUT_MS"] == "600000"
