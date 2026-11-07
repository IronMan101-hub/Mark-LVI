"""Instagram — navigation only.

Instagram offers no public API for personal accounts and forbids automated scraping or
messaging, so this plugin does only what is permitted and useful: open Instagram, open
a profile, and prepare a message on your clipboard for you to paste and send yourself.
It never logs in for you, never reads your feed or DMs, and never posts or messages."""
from __future__ import annotations

import re

from ..plugin_api import Health, Manifest, Plugin, PluginError, command

USER_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


class Instagram(Plugin):
    manifest = Manifest(
        id="instagram", name="Instagram", category="Communication", version="1.0.0",
        description="Open Instagram and profiles in your browser and prepare message drafts on the clipboard (navigation only).",
        permissions=["browser.control", "plugins.call", "system.control"],
        setup_help="No setup. Instagram has no permitted automation API for personal accounts, so profile data, posts and messages "
                   "can only be viewed/sent by you in the browser.")

    def health(self):
        return Health("degraded", "Navigation and clipboard drafts only (platform restriction).")

    def _open(self, url):
        r = self.ctx.call("browser", "open_url", {"url": url})
        if not r.ok:
            raise PluginError(r.message)
        return r

    @command(description="Open Instagram in the browser.", permissions=("browser.control", "plugins.call"))
    def open_instagram(self):
        self._open("https://www.instagram.com/")
        return "Opened Instagram. (Sign in yourself in the browser.)"

    @command(description="Open a public profile page by username.", permissions=("browser.control", "plugins.call"),
             params={"username": {"type": "string", "required": True}})
    def open_profile(self, username):
        u = username.strip().lstrip("@")
        if not USER_RE.match(u):
            raise PluginError("Instagram usernames use letters, numbers, dots and underscores (max 30).")
        self._open(f"https://www.instagram.com/{u}/")
        return {"message": f"Opened instagram.com/{u}. (I can't read the page for you — Instagram doesn't allow automated access.)", "data": {"url": f"https://www.instagram.com/{u}/"}}

    @command(description="Search Instagram for a name (opens the search in your browser).", permissions=("browser.control", "plugins.call"),
             params={"query": {"type": "string", "required": True}})
    def search(self, query):
        import urllib.parse
        self._open("https://www.google.com/search?q=" + urllib.parse.quote_plus(f"site:instagram.com {query}"))
        return f"Opened a web search for Instagram profiles matching '{query}'."

    @command(description="Prepare a direct-message draft: copies the text to the clipboard and opens your Instagram inbox. You paste and send it.",
             permissions=("browser.control", "plugins.call", "system.control"),
             params={"text": {"type": "string", "required": True, "max_length": 1000}, "username": {"type": "string"}})
    def draft_message(self, text, username=""):
        c = self.ctx.call("computer", "clipboard", {"action": "write", "text": text}) if self.ctx._e.registry.entries.get("computer") else None
        if username:
            u = username.strip().lstrip("@")
            if not USER_RE.match(u):
                raise PluginError("That username isn't valid.")
            self._open(f"https://www.instagram.com/{u}/")
        else:
            self._open("https://www.instagram.com/direct/inbox/")
        copied = bool(c and c.ok)
        return {"message": ("Your draft is on the clipboard — paste it into the chat and press Send yourself. " if copied else
                            "I opened Instagram but couldn't use the clipboard here; copy the text yourself. ") + "Nothing was sent.",
                "data": {"copied": copied}}


PLUGINS = [Instagram]
