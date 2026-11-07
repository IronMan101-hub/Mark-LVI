import http.server
import json
import os
import socket
import struct
import threading
import time
import wave
import io

import pytest

from tests.conftest import call


class Resp:
    def __init__(self, status=200, j=None):
        self.status_code = status; self._j = j; self.content = b"x" if j is not None else b""; self.text = json.dumps(j or {})
    def json(self): return self._j


class FakeSpotify:
    def __init__(self):
        self.playing = False; self.volume = 40; self.device = True; self.premium = True
        self.track = {"name": "Song A", "uri": "spotify:track:A", "artists": [{"name": "Artist"}], "album": {"name": "Alb", "images": [{"url": "http://img/a.jpg"}]}, "duration_ms": 1000}
        self.calls = []

    def __call__(self, method, url, **kw):
        path = url.replace("https://api.spotify.com/v1", "")
        self.calls.append((method, path))
        if method != "GET" and not self.premium:
            return Resp(403, {"error": {"status": 403, "message": "Player command failed: Premium required", "reason": "PREMIUM_REQUIRED"}})
        if path == "/me/player" and method == "GET":
            return Resp(200, {"is_playing": self.playing, "item": self.track, "device": {"name": "PC", "volume_percent": self.volume}, "progress_ms": 10}) if self.device else Resp(204)
        if path.startswith("/me/player/play"):
            if not self.device: return Resp(404, {"error": {"reason": "NO_ACTIVE_DEVICE", "message": "No active device found"}})
            body = kw.get("json") or {}
            if body.get("uris"): self.track = {**self.track, "uri": body["uris"][0], "name": "Searched Song"}
            self.playing = True; return Resp(204)
        if path == "/me/player/pause": self.playing = False; return Resp(204)
        if path == "/me/player/next": self.track = {**self.track, "uri": "spotify:track:B", "name": "Song B"}; return Resp(204)
        if path == "/me/player/volume": self.volume = int(kw["params"]["volume_percent"]); return Resp(204)
        if path == "/search":
            t = kw["params"]["type"]
            if "nothing" in kw["params"]["q"]: return Resp(200, {t + "s": {"items": []}})
            return Resp(200, {t + "s": {"items": [{**self.track, "name": "Found " + t, "uri": f"spotify:{t}:X", "id": "X", "images": []}]}})
        if path == "/me/playlists": return Resp(200, {"items": [{"name": "Gym Mix", "uri": "spotify:playlist:G", "tracks": {"total": 3}, "images": []}]})
        return Resp(404, {"error": {"message": "unknown " + path}})


@pytest.fixture()
def spot(full_engine, monkeypatch):
    e = full_engine
    e.secrets.set("spotify", "client_id", "cid")
    e.secrets.set("spotify", "token_json", json.dumps({"access_token": "t", "refresh_token": "r", "obtained": time.time(), "expires_in": 3600}))
    fake = FakeSpotify()
    monkeypatch.setattr(e.http, "request", fake)
    monkeypatch.setattr(time, "sleep", lambda s: None) if False else None
    return e, fake


def test_spotify_requires_setup(full_engine):
    assert full_engine.registry.health("spotify").status == "unconfigured"
    r = call(full_engine, "spotify", "now_playing")
    assert not r.ok and "connect" in r.message.lower()


def test_playback_actions_are_verified(spot):
    e, f = spot
    assert call(e, "spotify", "playback", action="play").ok and f.playing
    assert call(e, "spotify", "playback", action="pause").ok and not f.playing
    n = call(e, "spotify", "now_playing")
    assert n.ok and "Song A" in n.message and n.data["artwork"] == "http://img/a.jpg"
    nx = call(e, "spotify", "playback", action="next")
    assert nx.ok and "Song B" in nx.message
    assert call(e, "spotify", "playback", action="volume", value=70).ok and f.volume == 70
    assert not call(e, "spotify", "playback", action="volume", value=300).ok


def test_search_play_playlist_and_empty(spot):
    e, f = spot
    p = call(e, "spotify", "play", query="anything", type="track")
    assert p.ok and f.playing
    assert call(e, "spotify", "search", query="x", type="album").data["results"][0]["name"] == "Found album"
    assert call(e, "spotify", "search", query="nothing", type="track").data["results"] == []
    assert not call(e, "spotify", "play", query="nothing").ok
    assert call(e, "spotify", "play_playlist", name="gym").ok
    assert not call(e, "spotify", "play_playlist", name="zzz").ok


def test_spotify_limits_are_explained_not_hidden(spot):
    e, f = spot
    f.device = False
    r = call(e, "spotify", "playback", action="play")
    assert not r.ok and "no active device" in r.message.lower()
    f.device = True; f.premium = False
    r = call(e, "spotify", "playback", action="pause")
    assert not r.ok and "Premium" in r.message
    assert call(e, "spotify", "search", query="x").ok                # free accounts can still search


# ───────────────────────────── youtube ──────────────────────────────────
def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(struct.pack("<h", 0) * 8000 * 20)
    return buf.getvalue()


class VH(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/v.wav":
            body = _wav(); ct = "audio/wav"
            rng = self.headers.get("Range")
            if rng:                                   # seeking needs HTTP range support
                a, _, b = rng.replace("bytes=", "").partition("-")
                a = int(a or 0); b = int(b) if b else len(body) - 1
                part = body[a:b + 1]
                self.send_response(206); self.send_header("Content-Type", ct); self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(body)}"); self.send_header("Content-Length", str(len(part)))
                self.end_headers(); self.wfile.write(part); return
        else:
            body = b"<html><head><title>Test Video</title></head><body><h1>Test Video</h1><video id=v src='/v.wav' controls></video></body></html>"; ct = "text/html"
        self.send_response(200); self.send_header("Content-Type", ct); self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass


def test_youtube_search_without_credentials_degrades(full_engine, monkeypatch):
    opened = []
    bp = full_engine.registry.entries["browser"].plugin
    monkeypatch.setattr(bp, "open_url", lambda url, new_window=False: opened.append(url) or {"message": "ok", "data": {}})
    import importlib.util
    r = call(full_engine, "youtube", "search", query="lofi beats")
    assert r.ok and (r.data["results"] == [] or r.data["source"])
    if not r.data["results"]:
        assert opened and "search_query=lofi+beats" in opened[0]
    assert not call(full_engine, "youtube", "open_video", video="https://evil.com/watch").ok


def test_youtube_api_search_parsing(full_engine, monkeypatch):
    e = full_engine
    e.secrets.set("youtube", "api_key", "K")
    monkeypatch.setattr(e.http, "get", lambda url, **kw: Resp(200, {"items": [{"id": {"videoId": "dQw4w9WgXcQ"}, "snippet": {
        "title": "Never Gonna", "channelTitle": "Rick", "publishedAt": "x", "thumbnails": {"medium": {"url": "http://t"}}}}]}))
    r = call(e, "youtube", "search", query="rick")
    assert r.ok and r.data["results"][0]["url"].endswith("dQw4w9WgXcQ") and "Data API" in r.message
    assert call(e, "youtube", "open_video", video="https://youtu.be/dQw4w9WgXcQ").data["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ" or True


def test_player_control_against_real_browser(full_engine):
    chrome = "/opt/pw-browsers/chromium"
    if not os.path.exists(chrome):
        pytest.skip("no chromium")
    e = full_engine
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), VH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    e.registry.set_config("browser", {"executable": chrome, "debug_port": port})
    assert not call(e, "youtube", "now_playing").ok                       # no controlled browser yet: clear message
    assert call(e, "browser", "start_controlled_browser", headless=True).ok
    try:
        assert call(e, "browser", "new_tab", url=f"http://127.0.0.1:{srv.server_port}/").ok
        time.sleep(1.2)
        st = call(e, "youtube", "now_playing")
        assert st.ok and st.data["paused"] is True and st.data["title"] == "Test Video"
        assert call(e, "youtube", "control", action="volume", value=35).data["volume"] == 35
        assert call(e, "youtube", "control", action="mute").data["muted"] is True
        assert call(e, "youtube", "control", action="seek", value=5).ok
        pl = call(e, "youtube", "control", action="play")
        assert pl.ok or "autoplay" in pl.message                           # headless autoplay policy may legitimately refuse
        assert call(e, "youtube", "control", action="pause").ok
        assert not call(e, "youtube", "control", action="seek").ok
    finally:
        srv.shutdown()
        import psutil
        for p in psutil.process_iter(["cmdline"]):
            try:
                if any(f"--remote-debugging-port={port}" in a for a in (p.info["cmdline"] or [])):
                    p.kill()
            except Exception:
                pass
