"""YouTube: search (official Data API when a key is set), open videos, and control playback
in the GALAHAD-controlled browser via its DevTools connection. Never claims control of a
player in a browser it cannot reach."""
from __future__ import annotations

import re
import time
import urllib.parse

from ..plugin_api import Health, Manifest, Plugin, PluginError, command

VIDEO_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")

JS_STATE = """() => { const v = document.querySelector('video'); if (!v) return null;
  const t = document.querySelector('h1.ytd-watch-metadata, h1.title, h1');
  const ch = document.querySelector('#owner #channel-name a, ytd-channel-name a, #channel-name');
  return {paused: v.paused, volume: Math.round(v.volume*100), muted: v.muted, time: Math.round(v.currentTime), duration: Math.round(v.duration||0),
          title: (t ? t.innerText : document.title).trim(), channel: ch ? ch.innerText.trim() : '', url: location.href, rate: v.playbackRate}; }"""


class YouTube(Plugin):
    manifest = Manifest(
        id="youtube", name="YouTube", category="Media", version="1.0.0",
        description="Search YouTube, open videos, and play/pause/seek/volume in the controlled browser; video titles and info.",
        permissions=["media.control", "network", "browser.control", "plugins.call"], requires_plugins=["browser"],
        optional_python=["yt_dlp", "playwright"],
        config_schema=[{"key": "api_key", "label": "YouTube Data API v3 key (optional, enables in-app search results)", "secret": True}],
        setup_help="Opening and searching YouTube works with no setup (results open in your browser). A free YouTube Data API key lets GALAHAD "
                   "read search results and video info itself. Playback control needs the controlled browser (Browser → start_controlled_browser).")

    def health(self):
        if self.ctx.secret("api_key"):
            return Health("ok", "Data API key set")
        return Health("degraded", "No API key: search opens results in the browser; metadata via yt-dlp if installed.")

    # ── search / info ─────────────────────────────────────────────────
    def _api_search(self, query: str, limit: int) -> list[dict]:
        r = self.ctx.http_get("https://www.googleapis.com/youtube/v3/search",
                              params={"part": "snippet", "q": query, "type": "video", "maxResults": limit, "key": self.ctx.secret("api_key")})
        j = r.json()
        if r.status_code != 200:
            raise PluginError("YouTube API: " + j.get("error", {}).get("message", f"HTTP {r.status_code}"))
        return [{"id": i["id"]["videoId"], "title": i["snippet"]["title"], "channel": i["snippet"]["channelTitle"],
                 "thumbnail": i["snippet"]["thumbnails"].get("medium", {}).get("url"), "published": i["snippet"]["publishedAt"],
                 "url": "https://www.youtube.com/watch?v=" + i["id"]["videoId"]} for i in j.get("items", [])]

    def _ytdlp_search(self, query: str, limit: int) -> list[dict]:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "skip_download": True, "socket_timeout": 10}) as y:
            info = y.extract_info(f"ytsearch{limit}:{query}", download=False)
        return [{"id": e["id"], "title": e.get("title"), "channel": e.get("channel") or e.get("uploader"),
                 "thumbnail": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg", "url": "https://www.youtube.com/watch?v=" + e["id"],
                 "duration": e.get("duration")} for e in info.get("entries", []) if e]

    @command(description="Search YouTube. Returns results when a Data API key or yt-dlp is available; otherwise opens the results page.",
             permissions=("network", "browser.control"), timeout=40,
             params={"query": {"type": "string", "required": True}, "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 15},
                     "open": {"type": "boolean", "default": False, "description": "Also open the results page"}})
    def search(self, query, limit=5, open=False):
        results, how = [], ""
        try:
            if self.ctx.secret("api_key"):
                results, how = self._api_search(query, limit), "YouTube Data API"
            else:
                import importlib.util
                if importlib.util.find_spec("yt_dlp"):
                    results, how = self._ytdlp_search(query, limit), "yt-dlp metadata"
        except PluginError:
            raise
        except Exception as e:  # noqa: BLE001
            how = f"lookup failed ({type(e).__name__})"
        url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
        if open or not results:
            self.ctx.call("browser", "open_url", {"url": url})
        if not results:
            return {"message": f"I opened the YouTube results for '{query}'. I can't read the list without an API key or yt-dlp ({how or 'not configured'}).",
                    "data": {"results": [], "url": url}}
        return {"message": f"{len(results)} video(s) via {how}: " + "; ".join(f"{r['title']} ({r['channel']})" for r in results[:4]),
                "data": {"results": results, "url": url, "source": how}}

    @command(description="Open a YouTube video by URL, or search-and-open the first result for a title.", permissions=("network", "browser.control"), timeout=45,
             params={"video": {"type": "string", "required": True, "description": "URL or title"}})
    def open_video(self, video):
        m = VIDEO_RE.search(video)
        if not m and video.startswith(("http://", "https://")):
            raise PluginError("That doesn't look like a YouTube video link.")
        if m:
            url = "https://www.youtube.com/watch?v=" + m.group(1)
        else:
            res = self.search(video, 1)["data"]["results"]
            if not res:
                r = self.ctx.call("browser", "open_url", {"url": "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(video)})
                return {"message": "I can't pick a video without search access, so I opened the results page for you to choose.", "data": {}}
            url = res[0]["url"]
        r = self.ctx.call("browser", "open_url", {"url": url})
        return {"ok": r.ok, "message": f"Opened {url}" if r.ok else r.message, "data": {"url": url}}

    @command(description="Basic info about a video (title, channel, duration) from the Data API, yt-dlp, or the open tab.",
             permissions=("network",), readonly=True, timeout=40, params={"video": {"type": "string", "required": True}})
    def video_info(self, video):
        m = VIDEO_RE.search(video)
        vid = m.group(1) if m else video if re.fullmatch(r"[A-Za-z0-9_-]{11}", video) else ""
        if not vid:
            raise PluginError("Give me a YouTube link or video ID.")
        if self.ctx.secret("api_key"):
            j = self.ctx.http_get("https://www.googleapis.com/youtube/v3/videos", params={"part": "snippet,contentDetails,statistics", "id": vid,
                                                                                         "key": self.ctx.secret("api_key")}).json()
            if not j.get("items"):
                raise PluginError("No such video (or it's private).")
            i = j["items"][0]
            return {"message": f"{i['snippet']['title']} — {i['snippet']['channelTitle']} ({i['contentDetails']['duration']}, {int(i['statistics'].get('viewCount', 0)):,} views)",
                    "data": {"id": vid, "title": i["snippet"]["title"], "channel": i["snippet"]["channelTitle"], "duration": i["contentDetails"]["duration"],
                             "views": int(i["statistics"].get("viewCount", 0)), "thumbnail": i["snippet"]["thumbnails"]["medium"]["url"]}}
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "socket_timeout": 10}) as y:
                i = y.extract_info("https://www.youtube.com/watch?v=" + vid, download=False)
            return {"message": f"{i['title']} — {i.get('channel')} ({i.get('duration_string')})",
                    "data": {"id": vid, "title": i["title"], "channel": i.get("channel"), "duration": i.get("duration"), "thumbnail": i.get("thumbnail")}}
        except ImportError:
            raise PluginError("Video details need a YouTube Data API key (plugin settings) or yt-dlp.")
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"Couldn't read that video ({type(e).__name__}).")

    # ── playback control in the controlled browser ────────────────────
    def _player(self, fn_js: str, arg=None):
        br = self.ctx._e.registry.entries.get("browser")
        if not br or br.state != "active" or not br.plugin._tabs_online():
            raise PluginError("I can only control playback in the GALAHAD-controlled browser (say 'start the controlled browser', then open the video there).")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise PluginError("Playback control needs the 'playwright' package.")
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{br.plugin.port}")
            try:
                pages = [pg for c in b.contexts for pg in c.pages]
                cands = [pg for pg in pages if "youtube.com" in pg.url] or [pg for pg in pages if pg.evaluate("() => !!document.querySelector('video')")]
                if not cands:
                    raise PluginError("No video page is open in the controlled browser.")
                pg = cands[0]
                return pg.evaluate(fn_js, arg) if arg is not None else pg.evaluate(fn_js)
            finally:
                b.close()

    @command(description="What video is open and its playback state (title, channel, time, volume, paused).", permissions=("media.control",),
             readonly=True, timeout=30)
    def now_playing(self):
        s = self._player(JS_STATE)
        if not s:
            raise PluginError("The page has no video player.")
        m = VIDEO_RE.search(s["url"])
        s["thumbnail"] = f"https://i.ytimg.com/vi/{m.group(1)}/mqdefault.jpg" if m else None
        return {"message": f"{'Paused' if s['paused'] else 'Playing'}: {s['title']} — {s['channel'] or 'unknown channel'} ({s['time']}s/{s['duration']}s, volume {s['volume']}%).", "data": s}

    @command(description="Control the video: play | pause | toggle | seek | volume | mute | unmute | speed.", permissions=("media.control",), timeout=30,
             params={"action": {"type": "string", "required": True, "enum": ["play", "pause", "toggle", "seek", "volume", "mute", "unmute", "speed"]},
                     "value": {"type": "number", "description": "seconds (seek), 0-100 (volume), rate (speed)"}})
    def control(self, action, value=None):
        js = """([a, val]) => { const v = document.querySelector('video'); if (!v) return {error: 'no video'};
          if (a==='play') return v.play().then(()=>({}), e=>({error:String(e)}));
          if (a==='pause') v.pause(); if (a==='toggle') { v.paused ? v.play().catch(()=>{}) : v.pause(); }
          if (a==='seek') v.currentTime = val; if (a==='volume') v.volume = Math.max(0, Math.min(1, val/100));
          if (a==='mute') v.muted = true; if (a==='unmute') v.muted = false; if (a==='speed') v.playbackRate = val; return {}; }"""
        if action in ("seek", "volume", "speed") and value is None:
            raise PluginError(f"'{action}' needs a value.")
        r = self._player(js, [action, value])
        if isinstance(r, dict) and r.get("error"):
            raise PluginError("The player refused: " + r["error"])
        time.sleep(0.4)
        s = self._player(JS_STATE)
        checks = {"play": lambda: not s["paused"], "pause": lambda: s["paused"], "mute": lambda: s["muted"], "unmute": lambda: not s["muted"],
                  "volume": lambda: abs(s["volume"] - value) <= 1, "seek": lambda: abs(s["time"] - value) <= 3 or s["time"] >= value,
                  "speed": lambda: abs(s["rate"] - value) < 0.01, "toggle": lambda: True}
        ok = bool(s) and checks[action]()
        return {"ok": ok, "message": f"{action} done — now {'paused' if s['paused'] else 'playing'}, volume {s['volume']}%." if ok else
                f"I sent '{action}' but the player state doesn't match (autoplay rules can block play until you click the page once).", "data": s}

    @command(description="Press the system media key (play/pause, next, previous, volume) — controls whatever app owns media.",
             permissions=("media.control",),
             params={"key": {"type": "string", "required": True, "enum": ["playpause", "nexttrack", "prevtrack", "volumeup", "volumedown", "volumemute"]}})
    def media_key(self, key):
        try:
            import pyautogui
            pyautogui.press(key)
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"Media keys need a desktop session with pyautogui ({type(e).__name__}).")
        return f"Sent the {key} media key. (Can't verify which app handled it.)"


PLUGINS = [YouTube]
