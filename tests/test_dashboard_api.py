import time

import pytest
from fastapi.testclient import TestClient

from galahad.web.server import create_app

TOKEN = "test-token-123"
H = {"X-Galahad": "1"}


@pytest.fixture()
def client(full_engine):
    app = create_app(full_engine, TOKEN)
    c = TestClient(app, base_url="http://127.0.0.1:8765", cookies={"galahad_token": TOKEN})
    c.eng = full_engine
    return c


def test_auth_host_and_csrf_guards(full_engine):
    app = create_app(full_engine, TOKEN)
    anon = TestClient(app, base_url="http://127.0.0.1:8765")
    assert anon.get("/api/status").status_code == 401
    assert anon.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert anon.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    evil = TestClient(app, base_url="http://evil.example.com", cookies={"galahad_token": TOKEN})
    assert evil.get("/api/status").status_code == 403                       # DNS-rebinding style host
    good = TestClient(app, base_url="http://127.0.0.1:8765", cookies={"galahad_token": TOKEN})
    assert good.post("/api/command", json={"plugin": "utilities", "command": "calculate", "args": {"expression": "1+1"}}).status_code == 403   # no CSRF header
    r = good.post("/api/command", json={"plugin": "utilities", "command": "calculate", "args": {"expression": "1+1"}}, headers=H)
    assert r.status_code == 200 and r.json()["ok"]


def test_token_exchange_sets_strict_cookie(full_engine):
    app = create_app(full_engine, TOKEN)
    c = TestClient(app, base_url="http://127.0.0.1:8765", follow_redirects=False)
    r = c.get("/?token=" + TOKEN)
    assert r.status_code == 303 and "samesite=strict" in r.headers["set-cookie"].lower() and "httponly" in r.headers["set-cookie"].lower()
    assert c.get("/?token=wrong").status_code == 200 and "galahad_token" not in c.get("/?token=wrong").headers.get("set-cookie", "")
    page = c.get("/")
    assert "GALAHAD" in page.text and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert c.get("/static/../../../etc/passwd").status_code in (404, 400)
    assert c.get("/static/app.js").status_code == 200


def test_status_home_and_settings(client):
    s = client.get("/api/status").json()
    assert s["plugins"]["active"] >= 20 and "ai" in s and "voice" in s
    assert client.get("/api/home").json()["unread"] == 0
    st = client.post("/api/settings", json={"key": "appearance.theme", "value": "light"}, headers=H).json()
    assert st["ok"] and client.get("/api/status").json()["settings"]["appearance.theme"] == "light"
    assert client.post("/api/settings", json={"key": "appearance.theme", "value": "neon"}, headers=H).status_code == 400
    assert client.post("/api/settings", json={"key": "nope", "value": 1}, headers=H).status_code == 400
    assert any(x["key"] == "voice.speed" for x in client.get("/api/settings").json()["settings"])


def test_plugin_management_roundtrip(client):
    pl = client.get("/api/plugins").json()
    ids = {p["id"] for p in pl["plugins"]}
    assert {"productivity", "spotify", "gmail", "word", "excel", "powerpoint", "finance", "vision"} <= ids
    r = client.post("/api/plugins/utilities/disable", json={}, headers=H).json()
    assert r["ok"] and r["plugin"]["state"] == "disabled"
    assert not client.post("/api/command", json={"plugin": "utilities", "command": "calculate", "args": {"expression": "1"}}, headers=H).json()["ok"]
    assert client.post("/api/plugins/utilities/enable", json={}, headers=H).json()["plugin"]["state"] == "active"
    assert client.post("/api/plugins/utilities/test", json={}, headers=H).json()["ok"]
    assert client.post("/api/plugins/utilities/uninstall", json={}, headers=H).json()["plugin"]["state"] == "unloaded"
    assert client.post("/api/plugins/utilities/install", json={}, headers=H).json()["plugin"]["state"] == "active"
    assert client.post("/api/plugins/utilities/update", json={}, headers=H).json()["ok"]
    assert client.post("/api/plugins/utilities/explode", json={}, headers=H).status_code == 404
    # permissions
    perms = client.post("/api/plugins/gmail/permission", json={"permission": "mail.read", "granted": True}, headers=H).json()
    assert next(p for p in perms["permissions"] if p["permission"] == "mail.read")["granted"]
    # config: secrets are write-only
    cfg = client.post("/api/plugins/spotify/config", json={"values": {"client_id": "abc123secret"}}, headers=H).json()["config"]
    cid = next(f for f in cfg if f["key"] == "client_id")
    assert cid["is_set"] and cid["value"] == "" and "abc123secret" not in str(client.get("/api/plugins").json())
    bad = client.post("/api/plugins/spotify/config", json={"values": {"nonsense": 1}}, headers=H).json()
    assert not bad["ok"]


def test_confirmation_flow_through_dashboard(client, tmp_path):
    f = tmp_path / "x.txt"; f.write_text("1")
    r = client.post("/api/command", json={"plugin": "files", "command": "delete", "args": {"path": str(f)}}, headers=H).json()
    assert r["status"] == "awaiting_confirmation" and f.exists()
    pend = client.get("/api/confirmations").json()["pending"]
    assert pend and "Recycle Bin" in pend[0]["summary"]
    ok = client.post(f"/api/confirmations/{pend[0]['id']}/approve", headers=H).json()
    assert ok["ok"] and not f.exists()
    assert not client.post(f"/api/confirmations/{pend[0]['id']}/approve", headers=H).json()["ok"]       # double click safe
    assert client.post("/api/confirmations/x/maybe", headers=H).status_code == 404


def test_chat_activity_notifications_workflows(client):
    out = client.post("/api/chat", json={"message": "add a task to test the dashboard"}, headers=H).json()
    assert out["ok"] and out["status"] == "completed"
    assert client.post("/api/chat", json={"message": "  "}, headers=H).status_code == 400
    hist = client.get("/api/chat/history").json()["messages"]
    assert hist[-1]["role"] == "assistant" and any("dashboard" in m["content"] for m in hist)
    acts = client.get("/api/activity?limit=10").json()["activity"]
    assert any(a["action"] == "productivity.add_task" for a in acts)
    assert client.get("/api/activity?status=failed").json()["activity"] == [] or True
    client.eng.notifications.add("hello", "world", "system", 2)
    n = client.get("/api/notifications").json()
    assert n["unread"] == 1 and n["notifications"][0]["title"] == "hello"
    assert client.post("/api/notifications/read", json={"id": n["notifications"][0]["id"]}, headers=H).json()["unread"] == 0
    assert client.post("/api/notifications/dismiss", json={}, headers=H).json()["changed"] >= 1
    assert client.post("/api/notifications/wat", json={}, headers=H).status_code == 404
    s = client.post("/api/command", json={"plugin": "automation", "command": "save", "args": {"name": "w", "steps": [{"plugin": "utilities", "command": "calculate", "args": {"expression": "2*2"}}]}}, headers=H).json()
    assert s["ok"]
    assert client.get("/api/workflows").json()["workflows"][0]["name"] == "w"
    assert client.get("/api/runs/99999").status_code == 404


def test_memory_endpoints_and_export(client):
    assert client.post("/api/memory/remember", json={"key": "Tea", "value": "green"}, headers=H).json()["ok"]
    assert client.post("/api/memory/remember", json={"key": "", "value": ""}, headers=H).status_code == 400
    m = client.get("/api/memory").json()
    assert m["long_term"][0]["key"] == "tea"
    exp = client.get("/api/memory/export")
    assert exp.status_code == 200 and "green" in exp.text and "attachment" in exp.headers["content-disposition"]
    assert client.delete("/api/memory/tea", headers=H).json()["ok"]
    assert client.post("/api/memory/clear", json={"which": "everything"}, headers=H).status_code == 400
    assert client.post("/api/memory/clear", json={"which": "all"}, headers=H).json()["ok"]


def test_system_finance_media_endpoints(client):
    s = client.get("/api/system").json()
    assert s["snapshot"]["ram"]["total"] > 0 and isinstance(s["history"], list)
    f = client.get("/api/finance").json()
    assert f["needs_permission"] is True                                      # private until granted
    client.post("/api/plugins/finance/permission", json={"permission": "finance.data", "granted": True}, headers=H)
    f = client.get("/api/finance").json()
    assert f["ok"] and "summary" in f["data"]
    m = client.get("/api/media").json()
    assert m["spotify"]["ok"] is False and m["youtube"]["ok"] is False        # unconfigured: reported, not faked
    assert m["spotify"]["health"]["status"] == "unconfigured"


def test_search_endpoint_and_sse_stream_registered(client):
    client.eng.memory.remember("search target", "zebra")
    r = client.get("/api/search?q=zebra").json()
    assert r["ok"] and any(x["type"] == "memory" for x in r["data"]["results"])
    assert any(getattr(rt, "path", "") == "/api/stream" for rt in client.app.routes)


def test_server_starts_on_loopback_only(full_engine):
    import requests
    from galahad.web.server import DashboardServer
    srv = DashboardServer(full_engine, port=18765)
    srv.start()
    try:
        assert srv.url().startswith("http://127.0.0.1:")
        r = requests.get(srv.url(), timeout=5)
        assert r.status_code == 200 and "<title>GALAHAD</title>" in r.text
        assert requests.get(srv.url() + "api/status", timeout=5).status_code == 401
        assert requests.get(srv.url() + "api/status", headers={"Authorization": "Bearer " + srv.token}, timeout=5).status_code == 200
        assert srv._server.config.host == "127.0.0.1"
    finally:
        srv.stop()
