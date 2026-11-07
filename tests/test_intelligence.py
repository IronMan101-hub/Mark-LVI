import json
import time

import pytest

from tests.conftest import call

RSS_A = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>A</title>
<item><title>New gene therapy approved for rare disease</title><link>https://example.org/a1</link>
<description>&lt;p&gt;Regulators approved a gene therapy today.&lt;/p&gt;</description><pubDate>%s</pubDate></item>
<item><title>Gene therapy approved for rare disease by regulators</title><link>https://example.org/dup</link>
<description>same story</description><pubDate>%s</pubDate></item>
<item><title>Old story</title><link>https://example.org/old</link><pubDate>Mon, 01 Jan 2018 10:00:00 GMT</pubDate></item>
</channel></rss>"""
EVIL = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]><rss><channel><item><title>&lol2;</title><link>https://x.org</link></item></channel></rss>"""


def rss():
    from email.utils import formatdate
    now = formatdate(time.time())
    return RSS_A % (now.encode(), now.encode())


class R:
    def __init__(self, content=b"", status=200, text=None, j=None):
        self.content, self.status_code, self._j = content, status, j
        self.text = text if text is not None else content.decode("utf-8", "ignore")
    def json(self): return self._j


@pytest.fixture()
def news(full_engine, monkeypatch):
    e = full_engine
    monkeypatch.setattr(e.http, "get", lambda url, **kw: R(rss()) if "bbci" in url or "npr" in url else R(b"", 500))
    return e


def test_feed_parsing_and_entity_bomb_rejected():
    from galahad.plugins.news import parse_feed
    items = parse_feed(rss())
    assert len(items) == 3 and items[0]["summary"] == "Regulators approved a gene therapy today."
    with pytest.raises(ValueError):
        parse_feed(EVIL)


def test_refresh_dedupes_and_ignores_old(news):
    r = call(news, "news", "refresh", category="medicine")
    assert r.ok and r.data["added"] == 2                     # 'Old story' skipped by age
    again = call(news, "news", "refresh", category="medicine")
    assert again.data["added"] == 0                          # same URLs not re-added
    h = call(news, "news", "headlines", category="medicine")
    assert h.ok and len(h.data["articles"]) == 1             # near-duplicate headline merged
    assert "fetched" in h.message and "BBC" in h.message or "NPR" in h.message


def test_offline_refresh_is_reported(full_engine, monkeypatch):
    def boom(url, **kw): raise ConnectionError("offline")
    monkeypatch.setattr(full_engine.http, "get", boom)
    r = call(full_engine, "news", "refresh", category="science")
    assert not r.ok and "online" in r.message
    assert full_engine.registry.health("news").status == "degraded"


def test_save_bookmark_history_and_search(news):
    call(news, "news", "refresh", category="medicine")
    a = call(news, "news", "headlines", category="medicine").data["articles"][0]
    assert call(news, "news", "save", id=a["id"]).ok
    assert call(news, "news", "save", id=a["id"], bookmark=True).ok
    assert call(news, "news", "saved").data["articles"][0]["id"] == a["id"]
    assert call(news, "news", "saved", bookmarked=True).data["articles"]
    assert call(news, "news", "mark_read", id=a["id"]).ok
    assert call(news, "news", "history").data["articles"][0]["id"] == a["id"]
    assert call(news, "news", "search", query="gene").data["articles"]
    assert not call(news, "news", "save", id=9999).ok


def test_briefing_has_date_sources_and_no_duplicates(news):
    b = call(news, "news", "briefing", kind="daily")
    assert b.ok and "Daily briefing" in b.message and "https://example.org/a1" in b.message
    assert b.message.count("gene therapy") + b.message.count("Gene therapy") <= 2     # duplicate story merged
    cat = call(news, "news", "briefing", kind="category", category="medicine")
    assert cat.ok and "Medicine" in cat.message
    assert not call(news, "news", "briefing", kind="category").ok


def test_custom_feed_validation(news, monkeypatch):
    assert not call(news, "news", "add_feed", category="india", name="Bad", url="https://bad.example/feed").ok
    monkeypatch.setattr(news.http, "get", lambda url, **kw: R(rss()))
    assert call(news, "news", "add_feed", category="india", name="Mine", url="https://mine.example/rss").ok
    assert any(f["name"] == "Mine" for f in call(news, "news", "sources").data["feeds"])


# ───────────────────────────── research ─────────────────────────────────
@pytest.fixture()
def research(full_engine, monkeypatch):
    e = full_engine
    bp = e.registry.entries["browser"].plugin
    pages = {
        "https://a.org/x": "Solar capacity grew 24 percent in 2024 according to the agency, driven by falling panel prices and policy support worldwide. " * 6,
        "https://b.com/y": "Analysts confirm solar capacity grew by roughly a quarter last year as panel prices fell sharply across all major markets. " * 6,
        "https://a.org/z": "Another page on the same domain talking about solar costs and installation numbers in great detail for readers. " * 6}
    monkeypatch.setattr(bp, "search_results", lambda q, n=6: [{"title": u.split("/")[-1], "url": u, "snippet": ""} for u in pages])
    monkeypatch.setattr(bp, "_fetch", lambda url: ("Title " + url, pages[url], []))
    return e


def test_research_offline_is_honest(research):
    r = call(research, "research", "research", question="How fast is solar growing?", depth="quick")
    assert r.ok, r.message
    assert r.data["method"].startswith("extraction only")
    assert all(c["status"] == "uncertain" for c in r.data["claims"])           # never claims verification it didn't do
    urls = {x["url"] for x in r.data["references"]}
    assert urls <= {"https://a.org/x", "https://b.com/y", "https://a.org/z"}
    assert "## References" in r.data["report"]


def test_research_ai_citations_are_enforced_by_code(research, monkeypatch):
    from galahad.ai import AIResponse
    e = research
    calls = []
    def fake(prompt, **kw):
        calls.append(prompt)
        if "distinct web search queries" in prompt:
            return AIResponse(json.dumps({"queries": ["solar growth", "solar prices"]}), "fake", "m")
        return AIResponse(json.dumps({
            "claims": [{"text": "Solar grew ~24%", "sources": [1, 2], "status": "established"},
                       {"text": "Single-domain claim", "sources": [1, 3], "status": "established"},
                       {"text": "Invented", "sources": [42], "status": "established"}],
            "disagreements": [], "summary": "Solar grew.", "report": "Solar grew about 24% [1][2]. Also see [99]."}), "fake", "m")
    monkeypatch.setattr(e.ai, "available", lambda: True)
    monkeypatch.setattr(e.ai, "complete", fake)
    r = call(e, "research", "research", question="solar?", format="brief", depth="normal")
    assert r.ok and "AI analysis" in r.data["method"]
    st = {c["text"]: c for c in r.data["claims"]}
    assert st["Solar grew ~24%"]["status"] == "established" and st["Solar grew ~24%"]["independent_sources"] == 2
    assert st["Single-domain claim"]["status"] == "evidence-based"              # same domain twice ≠ corroboration
    assert st["Invented"]["status"] == "uncertain" and st["Invented"]["sources"] == []
    assert "[99]" not in r.data["report"] and "[?]" in r.data["report"]
    assert any("untrusted" in c for c in calls)
    hist = call(e, "research", "history")
    assert hist.data["reports"][0]["id"] == r.data["id"]
    ex = call(e, "research", "export", id=r.data["id"])
    assert ex.ok and open(ex.data["path"]).read().startswith("# solar?")


def test_research_with_no_sources_does_not_fabricate(full_engine, monkeypatch):
    bp = full_engine.registry.entries["browser"].plugin
    monkeypatch.setattr(bp, "search_results", lambda q, n=6: [])
    r = call(full_engine, "research", "research", question="anything")
    assert not r.ok and "Nothing was fabricated" in r.message


# ───────────────────────────── finance ──────────────────────────────────
@pytest.fixture()
def fin(full_engine):
    full_engine.permissions.set("finance", "finance.data", True)
    return full_engine


def test_finance_is_private_by_default(full_engine):
    r = call(full_engine, "finance", "add_entry", kind="expense", amount=5)
    assert not r.ok and "Permission" in r.message


def test_ledger_summary_budgets_and_alerts(fin):
    e = fin
    assert call(e, "finance", "set_budget", category="food", monthly_limit=1000).ok
    assert call(e, "finance", "add_entry", kind="income", amount=50000, category="salary").ok
    assert call(e, "finance", "add_entry", kind="expense", amount=850, category="Food").ok
    assert call(e, "finance", "add_entry", kind="saving", amount=5000, category="rd").ok
    assert any("Budget alert" in n["title"] for n in e.notifications.list())
    s = call(e, "finance", "summary").data
    assert (s["income"], s["expenses"], s["savings_added"], s["net"]) == (50000, 850, 5000, 44150)
    assert s["budgets"][0]["percent"] == 85.0 and s["source"] == "manual entries"
    assert not call(e, "finance", "add_entry", kind="expense", amount=-5).ok
    assert not call(e, "finance", "add_entry", kind="expense", amount=5, date="31/12/2026").ok
    eid = call(e, "finance", "list_entries", kind="expense").data["entries"][0]["id"]
    assert call(e, "finance", "edit_entry", id=eid, amount=900).ok
    d = call(e, "finance", "delete_entry", id=eid)
    assert d.status == "awaiting_confirmation"


def test_goals_and_holdings_projections(fin):
    e = fin
    assert call(e, "finance", "goal", name="Laptop", target=80000).ok
    assert "25%" in call(e, "finance", "goal", name="laptop", add_saved=20000).message
    call(e, "finance", "add_holding", kind="fd", name="SBI FD", amount=100000, rate=7, start="2025-01-01", maturity="2027-01-01")
    call(e, "finance", "add_holding", kind="sip", name="Index fund", amount=50000, rate=12, monthly=5000)
    call(e, "finance", "add_holding", kind="bank_account", name="Savings", amount=25000)
    h = call(e, "finance", "holdings").data
    fd = next(x for x in h["holdings"] if x["kind"] == "fd")
    assert 114000 < fd["projection"]["maturity_value"] < 116000 and fd["projection"]["estimate"]
    sip = next(x for x in h["holdings"] if x["kind"] == "sip")
    assert sip["projection"]["future_value"] > 5000 * 120
    assert all(x["source"] == "manual" for x in h["holdings"]) and h["total"] == 175000
    assert call(e, "finance", "dashboard").ok


def test_finance_math_reference_values():
    from galahad.plugins.finance import fd_maturity, rd_maturity, sip_future_value
    assert round(fd_maturity(100000, 7, 2), 0) == 114888
    assert round(sip_future_value(1000, 12, 12), 0) == 12809
    assert 12000 < rd_maturity(1000, 6, 12) < 12500


def test_market_quote_parsing_and_errors(fin, monkeypatch):
    e = fin
    csvtext = "Symbol,Date,Time,Open,High,Low,Close,Volume\nAAPL.US,2026-11-06,22:00:09,200,205,199,204,1000\n"
    monkeypatch.setattr(e.http, "get", lambda url, **kw: R(text=csvtext))
    q = call(e, "finance", "quote", symbol="aapl.us")
    assert q.ok and q.data["change_pct"] == 2.0 and "delayed" in q.message
    monkeypatch.setattr(e.http, "get", lambda url, **kw: R(text="Symbol,Date,Time,Open,High,Low,Close,Volume\nNOPE,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n"))
    assert not call(e, "finance", "quote", symbol="nope").ok
    monkeypatch.setattr(e.http, "get", lambda url, **kw: R(j={"date": "2026-11-06", "rates": {"INR": 88.1}}))
    assert "88.1" in call(e, "finance", "fx", base="usd", targets=["inr"]).message


# ───────────────────────────── unified search ───────────────────────────
def test_unified_search_spans_plugins(full_engine, tmp_path):
    e = full_engine
    e.settings.set("privacy.search_roots", [str(tmp_path)])
    (tmp_path / "zebra plan.docx").write_text("x")
    call(e, "productivity", "add_task", title="Zebra crossing report")
    call(e, "productivity", "add_reminder", text="feed the zebra", when="tomorrow 9am")
    call(e, "assistant", "remember", key="zebra", value="favourite animal")
    e.memory.add_message("user", "tell me about zebra stripes")
    r = call(e, "search", "search", query="zebra")
    types = {x["type"] for x in r.data["results"]}
    assert {"task", "reminder", "memory", "conversation", "file"} <= types, types
    f = next(x for x in r.data["results"] if x["type"] == "file")
    assert f["action"]["plugin"] == "files"
    assert not e.registry.execute("search", "open_result", {"plugin": "computer", "command": "power", "args": {"action": "shutdown"}}).ok   # whitelist
    narrow = call(e, "search", "search", query="zebra", scopes=["tasks"])
    assert {x["type"] for x in narrow.data["results"]} == {"task"}
