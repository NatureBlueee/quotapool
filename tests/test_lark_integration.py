from __future__ import annotations

import json

import pytest

from claude_fleet import lark_integration as lark
from claude_fleet import dashboard


def _runtime(tmp_path):
    return {"db_path": str(tmp_path / "fleet.sqlite3")}


def test_settings_store_only_identity_and_redacted_recipients(tmp_path):
    runtime = _runtime(tmp_path)

    saved = lark.save_settings(runtime, {
        "identity": "user",
        "accessToken": "must-never-be-saved",
        "recipients": [
            {"type": "user", "id": "ou_alice", "label": "Alice", "default": True},
            {"type": "chat", "id": "oc_team", "label": "产品群", "default": False},
            {"type": "user", "id": "ou_alice", "label": "duplicate"},
        ],
    })

    raw = lark.settings_path(runtime).read_text(encoding="utf-8")
    assert saved["identity"] == "user"
    assert [item["key"] for item in saved["recipients"]] == ["user:ou_alice", "chat:oc_team"]
    assert "must-never-be-saved" not in raw
    assert set(json.loads(raw)) == {"version", "identity", "recipients"}


def test_public_status_redacts_cli_credentials(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(lark, "_cli_command", lambda: ["node", "lark.js"])
    monkeypatch.setattr(lark, "run_lark_cli", lambda *args, **kwargs: {
        "appId": "cli_test",
        "brand": "feishu",
        "identity": "user",
        "accessToken": "secret",
        "identities": {
            "user": {"available": True, "status": "ready", "userName": "范茂伟", "openId": "ou_me", "token": "secret"},
            "bot": {"available": False, "status": "not_configured", "appSecret": "secret"},
        },
    })

    result = lark.public_status(runtime, fresh=True)

    assert result["connected"] is True
    assert result["user"] == {
        "available": True,
        "status": "ready",
        "name": "范茂伟",
        "openId": "ou_me",
        "verified": False,
    }
    assert "secret" not in json.dumps(result)


def test_search_merges_users_and_groups(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] == "contact":
            return {"ok": True, "data": {"users": [{
                "open_id": "ou_alice",
                "localized_name": "Alice",
                "enterprise_email": "alice@example.com",
            }]}}
        return {"ok": True, "data": {"chats": [{
            "chat_id": "oc_product",
            "name": "产品群",
            "external": False,
        }]}}

    monkeypatch.setattr(lark, "run_lark_cli", fake_run)

    result = lark.search_recipients("产品", identity="user")

    assert [item["key"] for item in result["recipients"]] == ["user:ou_alice", "chat:oc_product"]
    assert any("+search-user" in call for call in calls)
    assert any("+chat-search" in call for call in calls)


def test_share_sends_to_multiple_defaults_with_stable_idempotency(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    lark.save_settings(runtime, {
        "identity": "user",
        "recipients": [
            {"type": "user", "id": "ou_alice", "label": "Alice", "default": True},
            {"type": "chat", "id": "oc_product", "label": "产品群", "default": True},
        ],
    })
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True, "data": {"message_id": f"om_{len(calls)}"}}

    monkeypatch.setattr(lark, "run_lark_cli", fake_run)

    result = lark.share(runtime, {
        "requestId": "share-request-1234",
        "title": "周报",
        "content": "- 已完成",
        "format": "markdown",
    })

    assert result["ok"] is True
    assert result["sent"] == 2
    assert len(calls) == 2
    assert "--user-id" in calls[0][0]
    assert "--chat-id" in calls[1][0]
    assert all("--as" in args and "user" in args for args, _ in calls)
    assert all("## 周报\n\n- 已完成" in args for args, _ in calls)
    keys = [args[args.index("--idempotency-key") + 1] for args, _ in calls]
    assert len(set(keys)) == 2


def test_share_rejects_missing_recipient_and_file(tmp_path):
    runtime = _runtime(tmp_path)
    with pytest.raises(ValueError, match="至少选择"):
        lark.share(runtime, {
            "requestId": "share-request-1234",
            "content": "hello",
        })


def test_dashboard_contains_lark_settings_and_confirmed_share_flow():
    html = (dashboard.Path(dashboard.__file__).with_name("dashboard.html")
            .read_text(encoding="utf-8"))

    for marker in (
        'data-nav="lark"',
        'id="lark-section"',
        'id="lark-connect"',
        'id="lark-auth-qr"',
        'id="lark-recipient-query"',
        'id="lark-recipient-list"',
        'id="lark-settings-save"',
        'id="open-lark-share"',
        'id="lark-share-modal"',
        'id="lark-share-recipients"',
        "window.openLarkShare",
        "/api/lark/recipients/search",
        "/api/lark/share",
        "share-lark",
    ):
        assert marker in html
    assert "accessToken" not in html
    assert "appSecret" not in html


def test_dashboard_lark_auth_state_never_exposes_device_code(tmp_path, monkeypatch):
    server = object.__new__(dashboard.DashboardServer)
    server.runtime = _runtime(tmp_path)
    server._lark_auth_lock = dashboard.threading.Lock()
    server._lark_auth = {
        "status": "waiting",
        "verificationUrl": "https://example.test/verify",
        "deviceCode": "secret-device-code",
    }
    monkeypatch.setattr(lark, "public_status", lambda runtime: {
        "installed": True,
        "configured": True,
        "connected": False,
        "settings": {"identity": "user", "recipients": []},
    })

    result = server.lark_state()

    assert result["auth"]["status"] == "waiting"
    assert result["auth"]["verificationUrl"] == "https://example.test/verify"
    assert "deviceCode" not in result["auth"]
    assert "secret-device-code" not in json.dumps(result)
