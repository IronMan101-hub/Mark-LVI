"""Intelligence centre: RSS-based news in seven categories, saved/bookmarked articles,
reading history, de-duplicated briefings. Feed list is editable; every feed reports its own health."""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from ..plugin_api import Health, Manifest, Plugin, PluginError, command
from ..textutil import clip, similar

CATEGORIES = {
    "medicine": "Medicine & healthcare", "science": "Scientific discoveries", "ai_tech": "AI & technology",
    "finance": "Finance & banking", "economics": "Economics & business", "india": "India", "international": "International affairs"}

DEFAULT_FEEDS = {
    "medicine": [("BBC Health", "https://feeds.bbci.co.uk/news/health/rss.xml"), ("NPR Health", "https://feeds.npr.org/1128/rss.xml"),
                 ("WHO News", "https://www.who.int/rss-feeds/news-english.xml")],
    "science": [("BBC Science", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
                ("ScienceDaily", "https://www.sciencedaily.com/rss/all.xml"), ("Nature", "https://www.nature.com/nature.rss")],
    "ai_tech": [("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
                ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
                ("MIT Technology Review", "https://www.technologyreview.com/feed/")],
    "finance": [("CNBC Finance", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
                ("Economic Times Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
                ("Livemint Markets", "https://www.livemint.com/rss/markets")],
    "economics": [("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
                  ("Economic Times Economy", "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms"),
                  ("The Guardian Business", "https://www.theguardian.com/uk/business/rss")],
    "india": [("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss"),
              ("Times of India", "https://timesofindia.indiatimes.com/rssfeeds/-2128936835.cms"),
              ("NDTV India", "https://feeds.feedburner.com/ndtvnews-india-news")],
    "international": [("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"), ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
                      ("NPR World", "https://feeds.npr.org/1004/rss.xml")]}


def _txt(el, *names) -> str:
    for n in names:
        for child in el:
            tag = child.tag.split("}")[-1]
            if tag == n and (child.text or "").strip():
                return child.text.strip()
    return ""


def parse_feed(xml_bytes: bytes) -> list[dict]:
    head = xml_bytes[:4096].lower()
    if b"<!doctype" in head and b"<!entity" in xml_bytes[:20000].lower() or b"<!entity" in head:
        raise ValueError("feed rejected (XML entity declarations are not allowed)")
    root = ET.fromstring(xml_bytes)
    items = []
    entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for it in entries:
        title = re.sub(r"\s+", " ", _txt(it, "title"))
        link = _txt(it, "link")
        if not link:
            for child in it:
                if child.tag.split("}")[-1] == "link" and child.get("href"):
                    link = child.get("href")
                    break
        desc = _txt(it, "description", "summary", "encoded", "content")
        desc = re.sub(r"<[^>]+>", " ", desc)
        desc = re.sub(r"\s+", " ", re.sub(r"&[a-z]+;", " ", desc)).strip()
        raw = _txt(it, "pubDate", "published", "updated", "date")
        ts = None
        if raw:
            try:
                ts = parsedate_to_datetime(raw).timestamp()
            except Exception:
                try:
                    ts = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
                except Exception:
                    ts = None
        if title and link.startswith("http"):
            items.append({"title": title, "url": link, "summary": clip(desc, 400), "published": ts})
    return items


class News(Plugin):
    manifest = Manifest(
        id="news", name="News & Intelligence", category="Intelligence", version="1.0.0",
        description="Headlines in 7 categories from reputable RSS feeds, search, saved articles, reading history, and briefings.",
        permissions=["network", "ai.use", "plugins.call", "notify"], background=True,
        config_schema=[{"key": "refresh_minutes", "label": "Refresh every (minutes)", "type": "integer", "default": 30},
                       {"key": "max_age_days", "label": "Ignore items older than (days)", "type": "integer", "default": 14}])

    def initialize(self, ctx):
        super().initialize(ctx)
        self.status: dict = {}

    def health(self):
        bad = [k for k, v in self.status.items() if not v.get("ok")]
        total = len(self.status)
        if total and len(bad) == total:
            return Health("degraded", "No feed could be fetched (offline?)")
        return Health("ok", f"{total - len(bad)}/{total} feeds healthy" if total else "feeds not fetched yet")

    def ticks(self):
        return [("refresh", max(5, int(self.ctx.config.get("refresh_minutes") or 30)) * 60, self._auto_refresh)]

    def _auto_refresh(self):
        self.refresh()

    # ── feeds ─────────────────────────────────────────────────────────
    def feeds(self) -> dict[str, list[tuple[str, str]]]:
        custom = self.ctx.kv_get("custom_feeds", {}) or {}
        removed = set(self.ctx.kv_get("removed_feeds", []) or [])
        out = {}
        for cat, lst in DEFAULT_FEEDS.items():
            out[cat] = [(n, u) for n, u in lst if u not in removed] + [tuple(x) for x in custom.get(cat, [])]
        return out

    @command(description="Fetch the latest items for one category (or all) now.", permissions=("network",), timeout=120,
             params={"category": {"type": "string", "enum": list(CATEGORIES)}})
    def refresh(self, category=""):
        cats = [category] if category else list(CATEGORIES)
        feeds = self.feeds()
        added = 0
        max_age = time.time() - int(self.ctx.config.get("max_age_days") or 14) * 86400
        for cat in cats:
            for name, url in feeds.get(cat, []):
                self.ctx.check_cancel()
                try:
                    r = self.ctx.http_get(url, timeout=12, headers={"Accept": "application/rss+xml, application/xml, text/xml"})
                    if r.status_code != 200:
                        raise ValueError(f"HTTP {r.status_code}")
                    items = parse_feed(r.content)
                    self.status[f"{cat}/{name}"] = {"ok": True, "items": len(items), "at": time.time(), "url": url}
                except Exception as e:  # noqa: BLE001
                    self.status[f"{cat}/{name}"] = {"ok": False, "error": str(e)[:120], "at": time.time(), "url": url}
                    continue
                for it in items[:40]:
                    if it["published"] and it["published"] < max_age:
                        continue
                    if self.ctx.db.one("SELECT id FROM articles WHERE url=?", (it["url"],)):
                        continue
                    self.ctx.db.execute(
                        "INSERT INTO articles(url,title,publisher,category,published,summary,fetched) VALUES(?,?,?,?,?,?,?)",
                        (it["url"], it["title"], name, cat, it["published"], it["summary"], time.time()))
                    added += 1
        ok = sum(1 for v in self.status.values() if v.get("ok"))
        if not ok and self.status:
            return {"ok": False, "message": "Couldn't fetch any feed — are you online?", "data": {"feeds": self.status}}
        return {"message": f"{added} new article(s). {ok}/{len(self.status)} feeds responded.", "data": {"added": added, "feeds": self.status}}

    @command(description="Status of every news feed.", readonly=True)
    def sources(self):
        rows = [{"category": c, "name": n, "url": u, **self.status.get(f"{c}/{n}", {"ok": None})} for c, lst in self.feeds().items() for n, u in lst]
        return {"message": f"{len(rows)} feeds in {len(CATEGORIES)} categories.", "data": {"feeds": rows}}

    @command(description="Add a custom RSS/Atom feed to a category.", permissions=("network",),
             params={"category": {"type": "string", "enum": list(CATEGORIES), "required": True},
                     "name": {"type": "string", "required": True}, "url": {"type": "string", "required": True}})
    def add_feed(self, category, name, url):
        if not url.startswith(("http://", "https://")):
            raise PluginError("Feed address must be http(s).")
        try:
            r = self.ctx.http_get(url, timeout=12)
            n = len(parse_feed(r.content))
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"That doesn't look like a working RSS/Atom feed ({str(e)[:80]}).")
        custom = self.ctx.kv_get("custom_feeds", {}) or {}
        custom.setdefault(category, []).append([name, url])
        self.ctx.kv_set("custom_feeds", custom)
        return f"Added '{name}' ({n} items) to {CATEGORIES[category]}."

    @command(description="Remove a feed by URL.", params={"url": {"type": "string", "required": True}}, confirm="Remove feed {url}?")
    def remove_feed(self, url):
        custom = self.ctx.kv_get("custom_feeds", {}) or {}
        found = False
        for cat in custom:
            before = len(custom[cat])
            custom[cat] = [x for x in custom[cat] if x[1] != url]
            found |= len(custom[cat]) != before
        self.ctx.kv_set("custom_feeds", custom)
        if not found:
            removed = self.ctx.kv_get("removed_feeds", []) or []
            if url not in removed and any(url == u for lst in DEFAULT_FEEDS.values() for _n, u in lst):
                removed.append(url); self.ctx.kv_set("removed_feeds", removed); found = True
        if not found:
            raise PluginError("No such feed.")
        return "Feed removed."

    # ── reading ───────────────────────────────────────────────────────
    def _query(self, category="", query="", saved=False, bookmarked=False, limit=10, unique=True) -> list[dict]:
        sql, p = "SELECT * FROM articles WHERE 1=1", []
        if category:
            sql += " AND category=?"; p.append(category)
        if query:
            sql += " AND (title LIKE ? OR summary LIKE ?)"; p += [f"%{query}%"] * 2
        if saved:
            sql += " AND saved=1"
        if bookmarked:
            sql += " AND bookmarked=1"
        sql += " ORDER BY COALESCE(published, fetched) DESC LIMIT ?"; p.append(limit * 6 if unique else limit)
        rows = self.ctx.db.query(sql, p)
        if not unique:
            return rows[:limit]
        out = []
        for r in rows:                                 # drop near-duplicate stories from different outlets
            if not any(similar(r["title"], o["title"]) > 0.6 for o in out):
                out.append(r)
        return out[:limit]

    @staticmethod
    def _line(a: dict) -> str:
        d = datetime.fromtimestamp(a["published"]).strftime("%d %b %H:%M") if a.get("published") else "undated"
        return f"{a['title']} — {a['publisher']}, {d}"

    @command(description="Latest headlines, optionally for one category. Refreshes first if nothing is stored yet.",
             permissions=("network",), readonly=True, timeout=120,
             params={"category": {"type": "string", "enum": list(CATEGORIES)}, "limit": {"type": "integer", "default": 8, "minimum": 1, "maximum": 50}})
    def headlines(self, category="", limit=8):
        rows = self._query(category, limit=limit)
        note = ""
        if not rows:
            self.refresh(category)
            rows = self._query(category, limit=limit)
        if not rows:
            return {"ok": False, "message": "No headlines available — I couldn't reach the news feeds."}
        newest = max((r["fetched"] for r in rows), default=time.time())
        return {"message": f"{CATEGORIES.get(category, 'Top stories')} (fetched {datetime.fromtimestamp(newest):%d %b %H:%M}): " +
                " | ".join(self._line(r) for r in rows[:limit]) + note, "data": {"articles": rows, "fetched": newest}}

    @command(description="Search stored news by keyword.", readonly=True,
             params={"query": {"type": "string", "required": True}, "category": {"type": "string", "enum": list(CATEGORIES)},
                     "limit": {"type": "integer", "default": 10, "maximum": 50}})
    def search(self, query, category="", limit=10):
        rows = self._query(category, query, limit=limit)
        return {"message": f"{len(rows)} match(es): " + " | ".join(self._line(r) for r in rows[:5]) if rows else f"Nothing stored about '{query}'.",
                "data": {"articles": rows}}

    def _article(self, ident) -> dict:
        row = self.ctx.db.one("SELECT * FROM articles WHERE id=? OR url=?", (ident if str(ident).isdigit() else -1, str(ident)))
        if not row:
            raise PluginError(f"No stored article '{ident}'.")
        return row

    @command(description="Summarise a stored article (by id) or any article URL.", permissions=("network", "ai.use", "plugins.call"),
             timeout=90, params={"article": {"type": "string", "required": True, "description": "article id or URL"}})
    def summarise(self, article):
        row = self._article(article) if str(article).isdigit() else (self.ctx.db.one("SELECT * FROM articles WHERE url=?", (article,)) or {"url": article})
        r = self.ctx.call("browser", "summarise_page", {"url": row["url"]})
        if not r.ok:
            if row.get("summary"):
                return {"message": f"(Couldn't open the full article: {r.message[:80]}) Feed summary: {row['summary']}", "data": {"url": row["url"]}}
            return {"ok": False, "message": r.message}
        if row.get("id"):
            self.ctx.db.execute("UPDATE articles SET read_at=? WHERE id=?", (time.time(), row["id"]))
        return {"message": r.message, "data": {**(r.data or {}), "article": row.get("id")}}

    @command(description="Compare how several stored articles (ids) or URLs report the same story.", permissions=("network", "ai.use", "plugins.call"),
             timeout=130, params={"articles": {"type": "array", "required": True}, "focus": {"type": "string"}})
    def compare(self, articles, focus=""):
        urls = [self._article(a)["url"] if str(a).isdigit() else a for a in articles]
        r = self.ctx.call("browser", "compare_pages", {"urls": urls, "focus": focus})
        return {"ok": r.ok, "message": r.message, "data": r.data}

    @command(description="Save or bookmark an article (id) for later.",
             params={"id": {"type": "integer", "required": True}, "bookmark": {"type": "boolean", "default": False}, "unsave": {"type": "boolean", "default": False}})
    def save(self, id, bookmark=False, unsave=False):
        a = self._article(id)
        col = "bookmarked" if bookmark else "saved"
        self.ctx.db.execute(f"UPDATE articles SET {col}=? WHERE id=?", (0 if unsave else 1, id))
        return f"{'Removed' if unsave else 'Saved'} '{clip(a['title'], 60)}' {'from' if unsave else 'to'} {'bookmarks' if bookmark else 'saved articles'}."

    @command(description="Saved or bookmarked articles.", readonly=True, params={"bookmarked": {"type": "boolean", "default": False}})
    def saved(self, bookmarked=False):
        rows = self._query(saved=not bookmarked, bookmarked=bookmarked, limit=50, unique=False)
        return {"message": f"{len(rows)} article(s)." + (" " + " | ".join(self._line(r) for r in rows[:4]) if rows else ""), "data": {"articles": rows}}

    @command(description="Mark an article as read (reading history).", params={"id": {"type": "integer", "required": True}})
    def mark_read(self, id):
        self._article(id)
        self.ctx.db.execute("UPDATE articles SET read_at=? WHERE id=?", (time.time(), id))
        return "Marked as read."

    @command(description="Reading history.", readonly=True, params={"limit": {"type": "integer", "default": 20}})
    def history(self, limit=20):
        rows = self.ctx.db.query("SELECT * FROM articles WHERE read_at IS NOT NULL ORDER BY read_at DESC LIMIT ?", (limit,))
        return {"message": f"{len(rows)} article(s) read.", "data": {"articles": rows}}

    # ── briefings ─────────────────────────────────────────────────────
    @command(description="A concise, source-backed briefing: daily, weekly, or for one category. Duplicate stories are merged.",
             permissions=("network", "ai.use"), timeout=150,
             params={"kind": {"type": "string", "enum": ["daily", "weekly", "category"], "default": "daily"},
                     "category": {"type": "string", "enum": list(CATEGORIES)}, "per_category": {"type": "integer", "default": 3, "minimum": 1, "maximum": 8}})
    def briefing(self, kind="daily", category="", per_category=3):
        if kind == "category" and not category:
            raise PluginError("Which category?")
        cats = [category] if category else list(CATEGORIES)
        horizon = time.time() - (7 if kind == "weekly" else 1.5) * 86400
        if not self.ctx.db.one("SELECT id FROM articles WHERE fetched>?", (time.time() - 3600,)):
            self.refresh(category)
        sections, stamp = [], datetime.now().strftime("%A %d %B %Y, %H:%M")
        for c in cats:
            rows = [r for r in self._query(c, limit=per_category * 3) if (r["published"] or r["fetched"]) >= horizon][:per_category]
            if rows:
                sections.append({"category": c, "title": CATEGORIES[c], "articles": rows})
        if not sections:
            return {"ok": False, "message": "No recent stories are stored and I couldn't fetch any."}
        title = {"daily": "Daily briefing", "weekly": "Weekly overview", "category": f"{CATEGORIES.get(category)} briefing"}[kind]
        text = f"{title} — {stamp}\n" + "\n".join(
            f"\n{s['title']}\n" + "\n".join(f"  • {self._line(a)}\n    {a['url']}" for a in s["articles"]) for s in sections)
        how = "headline list"
        if self.ctx._e.ai.available():
            try:
                digest = "\n".join(f"[{s['title']}] {a['title']} ({a['publisher']}): {a['summary']}" for s in sections for a in s["articles"])
                r = self.ctx.llm("Write a concise briefing (max 150 words) of the major developments below. Use only this information; "
                                 "do not add facts. Group by theme.\n<headlines>\n" + clip(digest, 6000) + "\n</headlines>",
                                 purpose="news", max_tokens=400)
                text = f"{title} — {stamp}\n{r.text.strip()}\n\nSources:" + "".join(
                    f"\n  • {a['publisher']}: {a['title']} — {a['url']}" for s in sections for a in s["articles"])
                how = f"AI summary ({r.provider}) of headlines"
            except Exception:
                pass
        return {"message": text, "data": {"title": title, "generated": stamp, "sections": sections, "method": how}}


PLUGINS = [News]
