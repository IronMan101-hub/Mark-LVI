import functools
import http.server
import socket
import threading

import pytest

from tests.conftest import call

PAGE1 = """<html><head><title>Alpha Report</title></head><body><nav>menu</nav><article>
<h1>Quarterly results</h1>
<p>The company reported record revenue of 5 billion dollars in the third quarter, driven by strong cloud demand across regions.</p>
<p>Operating margin expanded to 31 percent as the firm reduced costs and improved data centre efficiency this year.</p>
<p>Analysts expect continued growth next quarter, although currency headwinds remain a risk for international sales.</p>
<p>The chief executive said investment in artificial intelligence infrastructure would accelerate through the next year.</p>
</article><script>alert(1)</script></body></html>"""
PAGE2 = """<html><head><title>Beta Analysis</title></head><body><main>
<p>Independent analysts disagree on the outlook, arguing that cloud demand is slowing and margins will compress next year.</p>
<p>Currency headwinds remain a risk for international sales according to several economists surveyed this month.</p>
</main></body></html>"""
BLOCKED = "<html><body>Please complete the captcha to verify you are human.</body></html>"


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        routes = {"/a": (200, PAGE1), "/b": (200, PAGE2), "/captcha": (200, BLOCKED), "/forbidden": (403, "no")}
        code, body = routes.get(self.path, (404, "nf"))
        self.send_response(code); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a): pass


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture()
def eng(full_engine, monkeypatch):
    import galahad.textutil as tu
    import galahad.plugins.browser as b
    real = tu.check_url
    monkeypatch.setattr(b, "check_url", lambda u: real(u, allow_private=True) if "127.0.0.1" in u else real(u))
    return full_engine


def test_ssrf_blocks_private_addresses(full_engine):
    for url in ("http://127.0.0.1:1/", "http://localhost/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd", "ftp://x.com"):
        r = call(full_engine, "browser", "read_page", url=url)
        assert not r.ok, url


def test_read_and_local_summary(eng, site):
    r = call(eng, "browser", "read_page", url=site + "/a")
    assert r.ok and r.data["title"] == "Alpha Report" and "alert(1)" not in r.data["text"] and "menu" not in r.data["text"]
    s = call(eng, "browser", "summarise_page", url=site + "/a", sentences=2)
    assert s.ok and s.data["method"].startswith("local") and "revenue" in s.data["summary"].lower() or "margin" in s.data["summary"].lower()


def test_blocked_sites_are_reported_not_bypassed(eng, site):
    for path in ("/captcha", "/forbidden", "/missing"):
        r = call(eng, "browser", "read_page", url=site + path)
        assert not r.ok
    assert "bypass" in call(eng, "browser", "read_page", url=site + "/forbidden").message.lower()


def test_compare_and_find(eng, site):
    c = call(eng, "browser", "compare_pages", urls=[site + "/a", site + "/b"], focus="outlook")
    assert c.ok and len(c.data["sources"]) == 2
    assert not call(eng, "browser", "compare_pages", urls=[site + "/a"]).ok
    f = call(eng, "browser", "find_in_page", term="currency", url=site + "/a")
    assert f.data["count"] == 1 and "currency" in f.data["snippets"][0].lower()


def test_ai_path_marks_untrusted_content(eng, site, monkeypatch):
    from galahad.ai import AIResponse
    seen = {}
    monkeypatch.setattr(eng.ai, "available", lambda: True)
    def fake(prompt, **kw):
        seen["p"] = prompt
        return AIResponse("AI summary", "fake", "m")
    monkeypatch.setattr(eng.ai, "complete", fake)
    r = call(eng, "browser", "summarise_page", url=site + "/a")
    assert r.data["summary"] == "AI summary" and "untrusted" in seen["p"] and "<page>" in seen["p"]
    assert call(eng, "browser", "ask_page", question="what was revenue?", url=site + "/a").message == "AI summary"


def test_ask_page_requires_ai(eng, site):
    assert not call(eng, "browser", "ask_page", question="?", url=site + "/a").ok


def test_open_url_validation(full_engine):
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://a.b"):
        assert not call(full_engine, "browser", "open_url", url=bad).ok


def test_web_results_degrades_gracefully(eng, monkeypatch):
    monkeypatch.setattr(eng.registry.entries["browser"].plugin, "search_results", lambda q, n=6: [])
    r = call(eng, "browser", "web_results", query="x")
    assert not r.ok and "No results" in r.message
    monkeypatch.setattr(eng.registry.entries["browser"].plugin, "search_results",
                        lambda q, n=6: [{"title": "T", "url": "https://x.org", "snippet": "s"}])
    assert call(eng, "browser", "web_results", query="x").data["results"][0]["url"] == "https://x.org"


@pytest.fixture()
def controlled(eng, site):
    import os
    chrome = "/opt/pw-browsers/chromium"
    if not os.path.exists(chrome):
        pytest.skip("no chromium available")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    eng.registry.set_config("browser", {"executable": chrome, "debug_port": port})
    r = call(eng, "browser", "start_controlled_browser", headless=True)
    assert r.ok, r.message
    yield eng, site
    import requests
    try:
        for t in requests.get(f"http://127.0.0.1:{port}/json/list", timeout=3).json():
            requests.get(f"http://127.0.0.1:{port}/json/close/{t['id']}", timeout=3)
    except Exception:
        pass
    import psutil
    for p in psutil.process_iter(["cmdline"]):
        try:
            if any(f"--remote-debugging-port={port}" in a for a in (p.info["cmdline"] or [])):
                p.kill()
        except Exception:
            pass


def test_real_browser_tab_control(controlled):
    e, site = controlled
    assert call(e, "browser", "new_tab", url=site + "/a").ok
    assert call(e, "browser", "new_tab", url=site + "/b").ok
    import time; time.sleep(1.0)
    tabs = call(e, "browser", "tabs")
    assert tabs.ok and len(tabs.data["tabs"]) >= 3
    titles = " ".join(t["title"] for t in tabs.data["tabs"])
    assert "Alpha Report" in titles and "Beta Analysis" in titles
    assert call(e, "browser", "switch_tab", title="Alpha").ok
    info = call(e, "browser", "page_info", tab=None)
    assert info.ok
    s = call(e, "browser", "find_in_tabs", query="slowing")
    assert s.ok and s.data["tabs"] and "Beta" in s.data["tabs"][0]["title"]
    assert call(e, "browser", "summarise_page", tab=1).ok
    before = len(call(e, "browser", "tabs").data["tabs"])
    assert call(e, "browser", "close_tab", title="Beta").ok
    assert len(call(e, "browser", "tabs").data["tabs"]) == before - 1
    assert call(e, "browser", "navigate", action="reload").ok
