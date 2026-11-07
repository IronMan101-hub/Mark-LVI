"""Google OAuth 2.0 for installed apps (loopback redirect + PKCE), using only
`requests`. The user creates their own OAuth client in Google Cloud Console
(Desktop app type) and enters its id/secret in the plugin's settings — GALAHAD
ships no shared client, so no one else's quota or consent screen is involved.
Tokens are stored in the SecretStore."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets as pysecrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleAuthError(Exception):
    pass


class GoogleAuth:
    def __init__(self, ctx, scopes: list[str]):
        self.ctx, self.scopes = ctx, scopes
        self._pending: dict = {}

    # ── configuration state ───────────────────────────────────────────
    def configured(self) -> bool:
        return bool(self.ctx.secret("client_id") and self.ctx.secret("client_secret"))

    def connected(self) -> bool:
        return bool(self.ctx.secret("token_json"))

    def disconnect(self) -> None:
        self.ctx.secrets.delete("token_json")

    # ── sign-in ───────────────────────────────────────────────────────
    def begin(self) -> str:
        """Start the loopback listener and return the URL the user must open."""
        if not self.configured():
            raise GoogleAuthError("Enter the Google OAuth client ID and secret in this plugin's settings first.")
        verifier = pysecrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = pysecrets.token_urlsafe(16)
        result: dict = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if q.get("state", [""])[0] != state:
                    self.send_response(400); self.end_headers(); self.wfile.write(b"State mismatch."); return
                result["code"] = q.get("code", [""])[0]
                result["error"] = q.get("error", [""])[0]
                self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
                self.wfile.write(b"<h3>GALAHAD is connected to Google. You can close this tab.</h3>")

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        redirect = f"http://127.0.0.1:{srv.server_port}"

        def serve():
            srv.timeout = 1
            end = time.time() + 300
            while time.time() < end and "code" not in result:
                srv.handle_request()
            srv.server_close()
            if result.get("code"):
                try:
                    outer._exchange(result["code"], redirect, verifier)
                except Exception as e:  # noqa: BLE001
                    outer._pending["error"] = str(e)
            elif result.get("error"):
                outer._pending["error"] = result["error"]

        self._pending = {"started": time.time()}
        threading.Thread(target=serve, daemon=True, name="gal-google-oauth").start()
        params = {"client_id": self.ctx.secret("client_id"), "redirect_uri": redirect, "response_type": "code",
                  "scope": " ".join(self.scopes), "state": state, "code_challenge": challenge,
                  "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"}
        return AUTH_URL + "?" + urllib.parse.urlencode(params)

    def _exchange(self, code: str, redirect: str, verifier: str) -> None:
        r = self.ctx.http_post(TOKEN_URL, data={
            "code": code, "client_id": self.ctx.secret("client_id"), "client_secret": self.ctx.secret("client_secret"),
            "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier})
        j = r.json()
        if r.status_code != 200 or "access_token" not in j:
            raise GoogleAuthError(j.get("error_description") or j.get("error") or f"HTTP {r.status_code}")
        j["obtained"] = time.time()
        self.ctx.secrets.set("token_json", json.dumps(j))

    # ── tokens ────────────────────────────────────────────────────────
    def access_token(self) -> str:
        raw = self.ctx.secret("token_json")
        if not raw:
            raise GoogleAuthError("Not signed in to Google. Open this plugin's settings and press Connect.")
        tok = json.loads(raw)
        if time.time() < tok.get("obtained", 0) + tok.get("expires_in", 3600) - 60:
            return tok["access_token"]
        if not tok.get("refresh_token"):
            raise GoogleAuthError("Google session expired. Reconnect in the plugin's settings.")
        r = self.ctx.http_post(TOKEN_URL, data={
            "client_id": self.ctx.secret("client_id"), "client_secret": self.ctx.secret("client_secret"),
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        j = r.json()
        if r.status_code != 200 or "access_token" not in j:
            raise GoogleAuthError("Google refused to refresh the session: " + (j.get("error_description") or str(j.get("error"))))
        tok.update(j)
        tok["obtained"] = time.time()
        self.ctx.secrets.set("token_json", json.dumps(tok))
        return tok["access_token"]

    def get(self, url: str, **params):
        r = self.ctx.http_get(url, params=params or None, headers={"Authorization": f"Bearer {self.access_token()}"})
        return self._check(r)

    def post(self, url: str, body: dict, method: str = "POST"):
        r = self.ctx._e.http.request(method, url, json=body, headers={"Authorization": f"Bearer {self.access_token()}"})
        return self._check(r)

    @staticmethod
    def _check(r):
        if r.status_code >= 400:
            try:
                msg = r.json()["error"]["message"]
            except Exception:
                msg = r.text[:150]
            raise GoogleAuthError(f"Google API error {r.status_code}: {msg}")
        return r.json() if r.content else {}
