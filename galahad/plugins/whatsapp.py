"""WhatsApp.

WhatsApp has NO official API for reading or sending from a personal account. What is
supported, and labelled as such:
  * contacts book (local), drafting a message, and opening the chat pre-filled in the
    WhatsApp app/Web (`whatsapp://send` / wa.me deep link) — YOU press Send;
  * real sending through the official WhatsApp Business Cloud API, if you configure it
    (it can only message people who messaged your business number in the last 24 hours,
    or approved templates — that is Meta's rule, not ours);
  * reading/searching your chats: NOT possible through any permitted interface → not offered.
Every send shows the recipient and text and waits for your confirmation.
"""
from __future__ import annotations

import re
import time
import urllib.parse

from ..plugin_api import Health, Manifest, Plugin, PluginError, command
from ..textutil import clip

E164 = re.compile(r"^\+?[1-9]\d{6,14}$")


def normalise_number(raw: str) -> str:
    n = re.sub(r"[\s\-().]", "", raw or "")
    if not E164.match(n):
        raise PluginError("That doesn't look like an international phone number (use +<country code><number>).")
    return n.lstrip("+")


class WhatsApp(Plugin):
    manifest = Manifest(
        id="whatsapp", name="WhatsApp", category="Communication", version="1.0.0",
        description="Contacts, message drafts opened in WhatsApp, and (optionally) real sending via the official Business Cloud API.",
        permissions=["messages.send", "network", "browser.control", "system.control", "plugins.call"],
        config_schema=[
            {"key": "phone_number_id", "label": "Business Cloud API phone-number ID (optional)", "type": "string", "default": ""},
            {"key": "access_token", "label": "Business Cloud API access token (optional)", "secret": True},
            {"key": "default_country_code", "label": "Default country code for local numbers (e.g. 91)", "type": "string", "default": ""},
            {"key": "use_desktop_app", "label": "Open chats in the WhatsApp desktop app (otherwise WhatsApp Web)", "type": "boolean", "default": True}],
        setup_help="Drafting needs no setup. Real sending needs a Meta WhatsApp Business account (Cloud API); personal-account sending is not "
                   "offered by WhatsApp. Reading chats is not available through any permitted interface.")

    def health(self):
        if self._cloud():
            return Health("ok", "Business Cloud API configured; drafts also available")
        return Health("degraded", "Draft-and-open mode only (no Business Cloud API configured).")

    def _cloud(self) -> bool:
        return bool(self.ctx.config.get("phone_number_id") and self.ctx.secret("access_token"))

    # ── contacts ──────────────────────────────────────────────────────
    def _book(self) -> dict:
        return self.ctx.kv_get("contacts", {}) or {}

    def _resolve(self, who: str) -> tuple[str, str]:
        who = who.strip()
        book = self._book()
        for name, num in book.items():
            if name.lower() == who.lower():
                return name, num
        hits = [(n, v) for n, v in book.items() if who.lower() in n.lower()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise PluginError("More than one contact matches '" + who + "': " + ", ".join(n for n, _ in hits) + ". Be more specific.")
        raw = re.sub(r"[\s\-().]", "", who)
        if re.fullmatch(r"\+?\d{5,}", raw):
            cc = str(self.ctx.config.get("default_country_code") or "")
            if not raw.startswith("+") and cc and not raw.startswith(cc) and len(raw) <= 10:
                raw = cc + raw.lstrip("0")
            return raw, normalise_number(raw)
        raise PluginError(f"I don't have a contact called '{who}'. Add one with add_contact.")

    @command(description="Save a contact (name → phone number) in GALAHAD's local address book.",
             params={"name": {"type": "string", "required": True}, "number": {"type": "string", "required": True}})
    def add_contact(self, name, number):
        book = self._book()
        book[name.strip()] = normalise_number(number)
        self.ctx.kv_set("contacts", book)
        return f"Saved {name.strip()} (+{book[name.strip()]})."

    @command(description="Search the local contacts.", readonly=True, params={"query": {"type": "string"}})
    def contacts(self, query=""):
        rows = [{"name": n, "number": "+" + v} for n, v in sorted(self._book().items()) if query.lower() in n.lower()]
        return {"message": f"{len(rows)} contact(s)" + (": " + ", ".join(r["name"] for r in rows[:8]) if rows else "."), "data": {"contacts": rows}}

    @command(description="Delete a saved contact.", params={"name": {"type": "string", "required": True}}, confirm="Delete contact {name}?")
    def delete_contact(self, name):
        book = self._book()
        if name not in book:
            raise PluginError(f"No contact '{name}'.")
        del book[name]; self.ctx.kv_set("contacts", book)
        return "Contact deleted."

    # ── opening / drafting ────────────────────────────────────────────
    def _open_chat(self, number: str, text: str) -> str:
        q = urllib.parse.quote(text)
        if self.ctx.config.get("use_desktop_app", True):
            uri = f"whatsapp://send?phone={number}&text={q}"
            import os, subprocess, sys
            try:
                if sys.platform == "win32":
                    os.startfile(uri)  # noqa: S606
                    return "WhatsApp desktop app"
                if sys.platform == "darwin":
                    subprocess.Popen(["open", uri]); return "WhatsApp app"
            except OSError:
                pass
        url = f"https://wa.me/{number}?text={q}"
        r = self.ctx.call("browser", "open_url", {"url": url})
        if not r.ok:
            raise PluginError("Couldn't open WhatsApp: " + r.message)
        return "WhatsApp Web"

    @command(description="Open WhatsApp.", permissions=("browser.control",))
    def open_whatsapp(self):
        r = self.ctx.call("browser", "open_url", {"url": "https://web.whatsapp.com/"})
        return {"ok": r.ok, "message": "Opened WhatsApp Web." if r.ok else r.message}

    def _log(self, kind, who, text):
        log = self.ctx.kv_get("log", []) or []
        log.append({"t": time.time(), "kind": kind, "to": who, "text": text[:500]})
        self.ctx.kv_set("log", log[-100:])

    @command(description="Draft a WhatsApp message: opens the chat with the text pre-filled. NOTHING is sent — you press Send.",
             permissions=("browser.control",), params={"to": {"type": "string", "required": True}, "text": {"type": "string", "required": True, "max_length": 2000}})
    def draft(self, to, text):
        name, number = self._resolve(to)
        where = self._open_chat(number, text)
        self._log("draft", name, text)
        return {"message": f"Opened a chat with {name} in {where} with your message typed in. I haven't sent it — press Send there.",
                "data": {"to": name, "number": "+" + number}}

    @staticmethod
    def _confirm(a):
        return f"SEND WHATSAPP\nTo: {a['to']}\n\n{clip(a['text'], 600)}"

    @command(description="Send a WhatsApp message. Shows recipient and text and waits for your confirmation. Via the Business Cloud API when configured; "
                         "otherwise opens the chat pre-filled for you to press Send.",
             permissions=("messages.send", "network", "browser.control"), confirm=_confirm.__func__, destructive=True, timeout=40,
             params={"to": {"type": "string", "required": True}, "text": {"type": "string", "required": True, "max_length": 2000}})
    def send(self, to, text):
        name, number = self._resolve(to)
        recent = [e for e in (self.ctx.kv_get("log", []) or []) if e["kind"] == "sent" and e["t"] > time.time() - 3600]
        if len(recent) >= 20:
            raise PluginError("Safety limit: 20 WhatsApp sends per hour.")
        if self._cloud():
            r = self.ctx.http_post(f"https://graph.facebook.com/v20.0/{self.ctx.config['phone_number_id']}/messages",
                                   headers={"Authorization": f"Bearer {self.ctx.secret('access_token')}"},
                                   json={"messaging_product": "whatsapp", "to": number, "type": "text", "text": {"body": text}})
            j = r.json() if r.content else {}
            if r.status_code != 200 or not j.get("messages"):
                err = (j.get("error") or {}).get("message", f"HTTP {r.status_code}")
                raise PluginError(f"WhatsApp refused the message: {err} (the Cloud API only allows messaging people who wrote to your business number in the last 24 h, or approved templates).")
            self._log("sent", name, text)
            return {"message": f"Message accepted by WhatsApp for {name} (id {j['messages'][0].get('id', '?')}).", "data": {"id": j["messages"][0].get("id")}}
        where = self._open_chat(number, text)
        self._log("draft", name, text)
        return {"message": f"I can't send from a personal WhatsApp account automatically. I opened {name}'s chat in {where} with the message ready — press Send. "
                           "(Not marked as sent.)", "data": {"sent": False, "to": name}}

    @command(description="Messages GALAHAD drafted or sent (local log only — WhatsApp chats themselves can't be read).", readonly=True)
    def log(self):
        return {"message": f"{len(self.ctx.kv_get('log', []) or [])} logged item(s).", "data": {"log": self.ctx.kv_get("log", []) or []}}

    @command(description="What this integration can and cannot do.", readonly=True)
    def capabilities(self):
        caps = {"draft_and_open_chat": True, "contacts_local": True, "send_via_business_cloud_api": self._cloud(),
                "send_from_personal_account": False, "read_or_search_chats": False}
        return {"message": "Supported: contacts, drafts opened in WhatsApp" + (", Business Cloud API sending" if self._cloud() else "") +
                ". Not possible (WhatsApp provides no permitted interface): reading/searching chats, sending from a personal account automatically.",
                "data": caps}


PLUGINS = [WhatsApp]
