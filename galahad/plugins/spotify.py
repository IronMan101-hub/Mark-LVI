"""Spotify via the official Web API (Authorization Code + PKCE, no client secret).

Honest limits: playback CONTROL requires a Spotify Premium account and an active
device (open Spotify on this PC first); search, playlists and 'now playing' work
on free accounts. You create the app at developer.spotify.com and register
http://127.0.0.1:<port>/callback as its redirect URI.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets as pysecrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from ..plugin_api import Health, Manifest, Plugin, PluginError, command

AUTH = "https://accounts.spotify.com/authorize"
TOKEN = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing user-read-recently-played "
          "playlist-read-private playlist-read-collaborative user-library-read")


class Spotify(Plugin):
    manifest = Manifest(
        id="spotify", name="Spotify", category="Media", version="1.0.0",
        description="Play, pause, skip, volume, search, playlists, devices and now-playing for Spotify (official API).",
        permissions=["media.control", "network", "system.control"],
        config_schema=[
            {"key": "client_id", "label": "Spotify app Client ID", "secret": True, "required": True},
            {"key": "redirect_port", "label": "Redirect port (register http://127.0.0.1:<port>/callback)", "type": "integer", "default": 8888}],
        setup_help="1) developer.spotify.com/dashboard → Create app. 2) Add redirect URI http://127.0.0.1:8888/callback. "
                   "3) Paste the Client ID here, then run 'connect'. Playback control needs Spotify Premium.")

    def initialize(self, ctx):
        super().initialize(ctx)
        self._auth_error = ""

    def health(self):
        if not self.ctx.secret("client_id"):
            return Health("unconfigured", "Needs a Spotify app Client ID (see setup).")
        if not self.ctx.secret("token_json"):
            return Health("unconfigured", "Client ID saved; run 'connect' to sign in.")
        return Health("ok", "signed in")

    # ── auth ──────────────────────────────────────────────────────────
    @command(description="Sign in to Spotify (opens your browser once).", permissions=("network",))
    def connect(self):
        cid = self.ctx.secret("client_id")
        if not cid:
            raise PluginError("Enter your Spotify Client ID in this plugin's settings first.")
        port = int(self.ctx.config.get("redirect_port") or 8888)
        redirect = f"http://127.0.0.1:{port}/callback"
        verifier = pysecrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = pysecrets.token_urlsafe(12)
        got: dict = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if q.get("state", [""])[0] != state:
                    self.send_response(400); self.end_headers(); return
                got["code"], got["error"] = q.get("code", [""])[0], q.get("error", [""])[0]
                self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
                self.wfile.write(b"<h3>GALAHAD is connected to Spotify. You can close this tab.</h3>")

            def log_message(self, *a): pass
        try:
            srv = HTTPServer(("127.0.0.1", port), H)
        except OSError:
            raise PluginError(f"Port {port} is busy. Change the redirect port in settings (and in your Spotify app).")

        def serve():
            srv.timeout = 1
            end = time.time() + 300
            while time.time() < end and "code" not in got:
                srv.handle_request()
            srv.server_close()
            if got.get("code"):
                try:
                    r = outer.ctx.http_post(TOKEN, data={"client_id": cid, "grant_type": "authorization_code", "code": got["code"],
                                                         "redirect_uri": redirect, "code_verifier": verifier})
                    j = r.json()
                    if "access_token" in j:
                        j["obtained"] = time.time()
                        outer.ctx.secrets.set("token_json", json.dumps(j))
                    else:
                        outer._auth_error = j.get("error_description", "token exchange failed")
                except Exception as e:  # noqa: BLE001
                    outer._auth_error = str(e)
        threading.Thread(target=serve, daemon=True, name="gal-spotify-oauth").start()
        url = AUTH + "?" + urllib.parse.urlencode({"client_id": cid, "response_type": "code", "redirect_uri": redirect, "scope": SCOPES,
                                                  "state": state, "code_challenge_method": "S256", "code_challenge": challenge})
        try:
            import webbrowser; webbrowser.open(url)
        except Exception:
            pass
        return {"message": "Approve GALAHAD in the Spotify page that just opened.", "data": {"url": url}}

    def _token(self) -> str:
        raw = self.ctx.secret("token_json")
        if not raw:
            raise PluginError("Not signed in to Spotify. Run 'connect' (see this plugin's setup).")
        tok = json.loads(raw)
        if time.time() < tok["obtained"] + tok.get("expires_in", 3600) - 60:
            return tok["access_token"]
        r = self.ctx.http_post(TOKEN, data={"grant_type": "refresh_token", "refresh_token": tok.get("refresh_token", ""),
                                            "client_id": self.ctx.secret("client_id")})
        j = r.json()
        if "access_token" not in j:
            raise PluginError("Spotify session expired — run 'connect' again.")
        tok.update(j); tok["obtained"] = time.time()
        self.ctx.secrets.set("token_json", json.dumps(tok))
        return tok["access_token"]

    def _api(self, method: str, path: str, *, ok=(200, 201, 202, 204), **kw):
        r = self.ctx._e.http.request(method, API + path, headers={"Authorization": f"Bearer {self._token()}"}, **kw)
        if r.status_code in ok:
            return r.json() if r.content else {}
        try:
            err = r.json().get("error", {})
        except Exception:
            err = {}
        reason = err.get("reason", "") if isinstance(err, dict) else ""
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        if r.status_code == 404 and reason == "NO_ACTIVE_DEVICE" or "No active device" in msg:
            raise PluginError("Spotify has no active device. Open Spotify on this PC or phone (say 'open Spotify'), play anything once, then retry.")
        if r.status_code == 403 and (reason == "PREMIUM_REQUIRED" or "Premium" in msg):
            raise PluginError("Spotify only allows remote playback control on Premium accounts. Search and playlists still work.")
        if r.status_code == 401:
            raise PluginError("Spotify rejected the session — run 'connect' again.")
        if r.status_code == 429:
            raise PluginError("Spotify is rate-limiting requests; try again in a moment.")
        raise PluginError(f"Spotify error {r.status_code}: {msg or 'unknown'}")

    @staticmethod
    def _track(t: dict) -> dict:
        imgs = (t.get("album") or {}).get("images") or []
        return {"name": t.get("name"), "artists": [a["name"] for a in t.get("artists", [])], "album": (t.get("album") or {}).get("name"),
                "uri": t.get("uri"), "artwork": imgs[0]["url"] if imgs else None, "duration_ms": t.get("duration_ms")}

    # ── status ────────────────────────────────────────────────────────
    @command(description="What is playing right now (track, artist, album, artwork, progress, volume, device).", permissions=("network",),
             readonly=True)
    def now_playing(self):
        st = self._api("GET", "/me/player", ok=(200, 204))
        if not st or not st.get("item"):
            return {"message": "Nothing is playing on Spotify.", "data": {"playing": False}}
        t = self._track(st["item"])
        dev = st.get("device") or {}
        data = {"playing": bool(st.get("is_playing")), **t, "progress_ms": st.get("progress_ms"), "volume": dev.get("volume_percent"),
                "device": dev.get("name"), "shuffle": st.get("shuffle_state"), "repeat": st.get("repeat_state")}
        return {"message": f"{'Playing' if data['playing'] else 'Paused'}: {t['name']} by {', '.join(t['artists'])} on {dev.get('name', 'unknown device')}.",
                "data": data}

    @command(description="Spotify playback devices.", permissions=("network",), readonly=True)
    def devices(self):
        d = self._api("GET", "/me/player/devices").get("devices", [])
        return {"message": f"{len(d)} device(s): " + ", ".join(f"{x['name']}{' (active)' if x['is_active'] else ''}" for x in d) if d else
                "No Spotify devices are online. Open Spotify on this PC.", "data": {"devices": d}}

    @command(description="Move playback to a device (by name).", permissions=("network", "media.control"),
             params={"device": {"type": "string", "required": True}})
    def transfer(self, device):
        d = [x for x in self._api("GET", "/me/player/devices").get("devices", []) if device.lower() in x["name"].lower()]
        if not d:
            raise PluginError(f"No Spotify device matching '{device}'.")
        self._api("PUT", "/me/player", json={"device_ids": [d[0]["id"]], "play": True})
        return f"Moved playback to {d[0]['name']}."

    # ── control ───────────────────────────────────────────────────────
    def _confirm_state(self, want_playing: bool) -> bool:
        for _ in range(6):
            time.sleep(0.5)
            st = self._api("GET", "/me/player", ok=(200, 204))
            if st and bool(st.get("is_playing")) == want_playing:
                return True
        return False

    @command(description="Playback control: play (resume) | pause | next | previous | volume | seek | shuffle | repeat.",
             permissions=("network", "media.control"), timeout=30,
             params={"action": {"type": "string", "required": True, "enum": ["play", "pause", "next", "previous", "volume", "seek", "shuffle", "repeat"]},
                     "value": {"type": "integer", "description": "volume 0-100 / seek seconds / shuffle 0|1"},
                     "mode": {"type": "string", "enum": ["off", "track", "context"], "description": "for repeat"}})
    def playback(self, action, value=None, mode="off"):
        if action == "play":
            self._api("PUT", "/me/player/play")
            ok = self._confirm_state(True)
            return {"ok": ok, "message": "Resumed." if ok else "Spotify accepted the command but isn't playing."}
        if action == "pause":
            self._api("PUT", "/me/player/pause")
            ok = self._confirm_state(False)
            return {"ok": ok, "message": "Paused." if ok else "Spotify accepted the command but is still playing."}
        if action in ("next", "previous"):
            before = (self._api("GET", "/me/player", ok=(200, 204)) or {}).get("item", {}).get("uri")
            self._api("POST", f"/me/player/{action}")
            time.sleep(1.0)
            now = self.now_playing()
            changed = now["data"].get("uri") != before
            return {"ok": changed, "message": ("Now: " + now["message"]) if changed else "The track didn't change (end of queue?)."}
        if action == "volume":
            if value is None or not 0 <= value <= 100:
                raise PluginError("Give a volume from 0 to 100.")
            self._api("PUT", "/me/player/volume", params={"volume_percent": value})
            time.sleep(0.6)
            v = (self._api("GET", "/me/player", ok=(200, 204)) or {}).get("device", {}).get("volume_percent")
            return {"ok": v == value, "message": f"Spotify volume {v}%." if v == value else f"Asked for {value}% but Spotify reports {v}% (some devices don't allow remote volume)."}
        if action == "seek":
            self._api("PUT", "/me/player/seek", params={"position_ms": int((value or 0) * 1000)})
            return f"Jumped to {value or 0}s."
        if action == "shuffle":
            self._api("PUT", "/me/player/shuffle", params={"state": "true" if value else "false"})
            return f"Shuffle {'on' if value else 'off'}."
        self._api("PUT", "/me/player/repeat", params={"state": mode})
        return f"Repeat {mode}."

    # ── search & library ──────────────────────────────────────────────
    @command(description="Search Spotify for tracks, albums, artists or playlists.", permissions=("network",), readonly=True,
             params={"query": {"type": "string", "required": True}, "type": {"type": "string", "enum": ["track", "album", "artist", "playlist"], "default": "track"},
                     "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20}})
    def search(self, query, type="track", limit=5):
        j = self._api("GET", "/search", params={"q": query, "type": type, "limit": limit})
        items = [i for i in j.get(type + "s", {}).get("items", []) if i]
        out = []
        for i in items:
            if type == "track":
                out.append(self._track(i))
            else:
                imgs = i.get("images") or []
                out.append({"name": i.get("name"), "uri": i.get("uri"), "artwork": imgs[0]["url"] if imgs else None,
                            "artists": [a["name"] for a in i.get("artists", [])] if type == "album" else [],
                            "owner": (i.get("owner") or {}).get("display_name") if type == "playlist" else None})
        if not out:
            return {"ok": True, "message": f"No {type}s found for '{query}'.", "data": {"results": []}}
        return {"message": f"{len(out)} {type}(s): " + "; ".join(o["name"] + (f" — {', '.join(o['artists'])}" if o["artists"] else "") for o in out),
                "data": {"results": out}}

    @command(description="Search and play: a track, album, artist or playlist by name.", permissions=("network", "media.control"), timeout=40,
             params={"query": {"type": "string", "required": True}, "type": {"type": "string", "enum": ["track", "album", "artist", "playlist"], "default": "track"}})
    def play(self, query, type="track"):
        res = self.search(query, type, 1)["data"]["results"]
        if not res:
            raise PluginError(f"I couldn't find a {type} called '{query}' on Spotify.")
        item = res[0]
        body = {"uris": [item["uri"]]} if type == "track" else {"context_uri": item["uri"]}
        self._api("PUT", "/me/player/play", json=body)
        ok = self._confirm_state(True)
        return {"ok": ok, "message": f"Playing {item['name']}." if ok else f"Spotify accepted '{item['name']}' but isn't playing yet.", "data": item}

    @command(description="Your playlists.", permissions=("network",), readonly=True, params={"limit": {"type": "integer", "default": 30, "maximum": 50}})
    def playlists(self, limit=30):
        items = self._api("GET", "/me/playlists", params={"limit": limit}).get("items", [])
        out = [{"name": p["name"], "uri": p["uri"], "tracks": (p.get("tracks") or {}).get("total"),
                "artwork": (p.get("images") or [{}])[0].get("url")} for p in items if p]
        return {"message": f"{len(out)} playlist(s): " + ", ".join(p["name"] for p in out[:8]), "data": {"playlists": out}}

    @command(description="Play one of your playlists by (part of) its name.", permissions=("network", "media.control"), timeout=40,
             params={"name": {"type": "string", "required": True}})
    def play_playlist(self, name):
        pls = self.playlists()["data"]["playlists"]
        m = [p for p in pls if name.lower() in p["name"].lower()]
        if not m:
            raise PluginError(f"None of your playlists matches '{name}'.")
        self._api("PUT", "/me/player/play", json={"context_uri": m[0]["uri"]})
        ok = self._confirm_state(True)
        return {"ok": ok, "message": f"Playing your playlist {m[0]['name']}." if ok else "Spotify accepted it but isn't playing yet.", "data": m[0]}

    @command(description="Artist details and top tracks.", permissions=("network",), readonly=True, params={"name": {"type": "string", "required": True}})
    def artist_info(self, name):
        a = self._api("GET", "/search", params={"q": name, "type": "artist", "limit": 1}).get("artists", {}).get("items", [])
        if not a:
            raise PluginError(f"No artist called '{name}'.")
        a = a[0]
        top = self._api("GET", f"/artists/{a['id']}/top-tracks", params={"market": "from_token"}, ok=(200, 400, 404)) or {}
        tracks = [t["name"] for t in top.get("tracks", [])][:5]
        return {"message": f"{a['name']}: {a['followers']['total']:,} followers; genres {', '.join(a.get('genres', [])[:3]) or 'n/a'}."
                           + (f" Top: {', '.join(tracks)}." if tracks else ""),
                "data": {"name": a["name"], "followers": a["followers"]["total"], "genres": a.get("genres"), "popularity": a.get("popularity"),
                         "top_tracks": tracks, "artwork": (a.get("images") or [{}])[0].get("url"), "uri": a["uri"]}}

    @command(description="Album details and tracklist.", permissions=("network",), readonly=True, params={"name": {"type": "string", "required": True}})
    def album_info(self, name):
        a = self._api("GET", "/search", params={"q": name, "type": "album", "limit": 1}).get("albums", {}).get("items", [])
        if not a:
            raise PluginError(f"No album called '{name}'.")
        full = self._api("GET", f"/albums/{a[0]['id']}")
        tracks = [t["name"] for t in full.get("tracks", {}).get("items", [])]
        return {"message": f"{full['name']} by {', '.join(x['name'] for x in full['artists'])} ({full['release_date']}), {len(tracks)} tracks.",
                "data": {"name": full["name"], "artists": [x["name"] for x in full["artists"]], "release_date": full["release_date"],
                         "tracks": tracks, "artwork": (full.get("images") or [{}])[0].get("url"), "uri": full["uri"]}}

    @command(description="Recently played tracks.", permissions=("network",), readonly=True, params={"limit": {"type": "integer", "default": 10, "maximum": 50}})
    def recently_played(self, limit=10):
        items = self._api("GET", "/me/player/recently-played", params={"limit": limit}).get("items", [])
        out = [self._track(i["track"]) | {"played_at": i["played_at"]} for i in items]
        return {"message": f"{len(out)} recent track(s): " + "; ".join(o["name"] for o in out[:5]), "data": {"tracks": out}}

    @command(description="Add a track to the play queue.", permissions=("network", "media.control"), params={"query": {"type": "string", "required": True}})
    def queue(self, query):
        res = self.search(query, "track", 1)["data"]["results"]
        if not res:
            raise PluginError(f"No track called '{query}'.")
        self._api("POST", "/me/player/queue", params={"uri": res[0]["uri"]})
        return f"Queued {res[0]['name']}."


PLUGINS = [Spotify]
