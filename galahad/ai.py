"""Provider-independent AI access.

Adapters speak each vendor's official HTTP API. Keys live in the SecretStore
(never in settings or logs). Subscriptions (ChatGPT Plus, Claude Pro, Gemini
Advanced) are NOT API access and carry no API credits — every provider below
needs an API key from its developer console, and the dashboard says so.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from . import logs
from .db import Database
from .http import Http
from .secrets import SecretStore
from .settings import Settings

log = logs.get("ai")


class AIError(Exception):
    pass


@dataclass
class AIResponse:
    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    attempts: list = field(default_factory=list)


PROVIDERS: dict[str, dict] = {
    "gemini": {"label": "Google Gemini", "kind": "gemini", "default_model": "gemini-2.5-flash",
               "base_url": "https://generativelanguage.googleapis.com/v1beta", "needs_key": True,
               "console": "https://aistudio.google.com/apikey"},
    "openai": {"label": "OpenAI", "kind": "openai", "default_model": "gpt-4o-mini",
               "base_url": "https://api.openai.com/v1", "needs_key": True,
               "console": "https://platform.openai.com/api-keys"},
    "anthropic": {"label": "Anthropic Claude", "kind": "anthropic", "default_model": "claude-sonnet-5-5",
                  "base_url": "https://api.anthropic.com/v1", "needs_key": True,
                  "console": "https://console.anthropic.com/settings/keys"},
    "openai_compatible": {"label": "OpenAI-compatible (Ollama, LM Studio, Groq, OpenRouter…)", "kind": "openai",
                          "default_model": "llama3.1", "base_url": "http://localhost:11434/v1", "needs_key": False,
                          "console": ""},
}
SUBSCRIPTION_NOTE = ("An AI API key is separate from a chat subscription: ChatGPT Plus, Claude Pro and Gemini "
                     "Advanced do not include API credits. Create a key in the provider's developer console.")


def _legacy_gemini_key() -> Optional[str]:
    """Reuse the key MARK LVI already stores, so the user doesn't enter it twice."""
    try:
        from memory.config_manager import get_gemini_key
        return get_gemini_key() or None
    except Exception:
        return None


class AIManager:
    def __init__(self, db: Database, settings: Settings, secrets: SecretStore, http: Http):
        self.db, self.settings, self.secrets, self.http = db, settings, secrets, http

    # ── provider config ───────────────────────────────────────────────
    def provider_config(self, pid: str) -> dict:
        base = dict(PROVIDERS[pid])
        stored = self.db.kv_get("ai", f"provider:{pid}", {}) or {}
        base["model"] = stored.get("model") or base["default_model"]
        base["base_url"] = stored.get("base_url") or base["base_url"]
        base["settings"] = {"temperature": stored.get("temperature"), "max_tokens": stored.get("max_tokens")}
        return base

    def configure(self, pid: str, *, api_key: Optional[str] = None, model: Optional[str] = None,
                  base_url: Optional[str] = None, temperature: Optional[float] = None,
                  max_tokens: Optional[int] = None) -> None:
        if pid not in PROVIDERS:
            raise AIError(f"Unknown provider '{pid}'.")
        stored = self.db.kv_get("ai", f"provider:{pid}", {}) or {}
        for k, v in (("model", model), ("base_url", base_url), ("temperature", temperature), ("max_tokens", max_tokens)):
            if v not in (None, ""):
                stored[k] = v
        if base_url is not None and base_url != "" and not base_url.startswith(("http://", "https://")):
            raise AIError("base_url must start with http:// or https://")
        self.db.kv_set("ai", f"provider:{pid}", stored)
        if api_key:
            self.secrets.set("ai", pid, api_key.strip())

    def api_key(self, pid: str) -> Optional[str]:
        key = self.secrets.get("ai", pid)
        if not key and pid == "gemini":
            key = _legacy_gemini_key()
        return key

    def is_configured(self, pid: str) -> bool:
        cfg = PROVIDERS[pid]
        if cfg["needs_key"]:
            return bool(self.api_key(pid))
        # keyless (local) providers only count once the user has actually pointed GALAHAD at one
        return bool(self.db.kv_get("ai", f"provider:{pid}"))

    def status(self) -> list[dict]:
        out = []
        for pid in PROVIDERS:
            c = self.provider_config(pid)
            out.append({"id": pid, "label": c["label"], "model": c["model"], "base_url": c["base_url"],
                        "configured": self.is_configured(pid), "needs_key": c["needs_key"],
                        "key_source": ("stored" if self.secrets.has("ai", pid) else
                                       "MARK LVI config" if pid == "gemini" and _legacy_gemini_key() else "none"),
                        "console": c["console"], "settings": c["settings"],
                        "active": self.settings.get("ai.provider") == pid})
        return out

    def usage(self, days: int = 30) -> dict:
        since = time.time() - days * 86400
        rows = self.db.query(
            "SELECT provider, model, COUNT(*) calls, SUM(prompt_tokens) pt, SUM(completion_tokens) ct, "
            "SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) errors FROM ai_usage WHERE created>? GROUP BY provider, model", (since,))
        return {"days": days, "by_model": rows, "note": "Token counts are what the provider reports; blank = not reported."}

    # ── choosing a provider order ─────────────────────────────────────
    def order(self, purpose: str) -> list[str]:
        primary = (self.settings.get("ai.workflow_providers", {}) or {}).get(purpose) or self.settings.get("ai.provider")
        chain = [primary] + [p for p in self.settings.get("ai.fallback_providers", []) if p != primary]
        return [p for p in chain if p in PROVIDERS and self.is_configured(p)]

    def available(self) -> bool:
        return bool(self.order("general"))

    # ── main entry ────────────────────────────────────────────────────
    def complete(self, prompt: str, *, system: str = "", purpose: str = "general", json_mode: bool = False,
                 max_tokens: Optional[int] = None, private: bool = False, origin: str = "core",
                 messages: Optional[list] = None, provider: Optional[str] = None,
                 images: Optional[list] = None, timeout: float = 60) -> AIResponse:
        chain = [provider] if provider else self.order(purpose)
        if not chain:
            raise AIError("No AI provider is configured. Add an API key in Plugins → AI Providers "
                          "(or Settings → AI). Subscriptions such as ChatGPT Plus do not provide API access.")
        history = list(messages or [])
        attempts = []
        for pid in chain:
            cfg = self.provider_config(pid)
            t = time.time()
            try:
                text, pt, ct = self._call(pid, cfg, prompt, system, history, json_mode,
                                          max_tokens or cfg["settings"].get("max_tokens") or self.settings.get("ai.max_tokens"),
                                          cfg["settings"].get("temperature") if cfg["settings"].get("temperature") is not None
                                          else self.settings.get("ai.temperature"), images or [], timeout)
                self._record(pid, cfg["model"], purpose, pt, ct, True)
                if private:
                    self._audit_private(pid, cfg["model"], origin, len(prompt))
                return AIResponse(text, pid, cfg["model"], pt, ct, attempts)
            except Exception as e:  # noqa: BLE001
                self._record(pid, cfg["model"], purpose, 0, 0, False)
                attempts.append({"provider": pid, "error": _clean_err(e), "seconds": round(time.time() - t, 2)})
                log.warning("provider %s failed: %s", pid, _clean_err(e))
        raise AIError("All AI providers failed: " + "; ".join(f"{a['provider']}: {a['error']}" for a in attempts))

    def text(self, prompt: str, **kw) -> Optional[str]:
        try:
            return self.complete(prompt, **kw).text
        except AIError:
            return None

    def json(self, prompt: str, **kw):
        kw["json_mode"] = True
        try:
            return parse_json(self.complete(prompt, **kw).text)
        except AIError:
            return None

    def test(self, pid: str) -> dict:
        if pid not in PROVIDERS:
            return {"ok": False, "message": "Unknown provider."}
        if not self.is_configured(pid):
            return {"ok": False, "message": "No API key stored. " + SUBSCRIPTION_NOTE}
        t = time.time()
        try:
            r = self.complete("Reply with exactly: OK", provider=pid, purpose="test", max_tokens=16)
            return {"ok": True, "message": f"Connected. Model {r.model} replied: {r.text.strip()[:40]}",
                    "seconds": round(time.time() - t, 2)}
        except AIError as e:
            return {"ok": False, "message": str(e)}

    def _audit_private(self, pid: str, model: str, origin: str, size: int) -> None:
        aid = self.db.execute(
            "INSERT INTO activity(action,plugin,origin,args,status,result,started,finished) VALUES(?,?,?,?,?,?,?,?)",
            ("ai.private_send", origin, "system", json.dumps({"provider": pid, "model": model, "chars": size}),
             "completed", f"Private content ({size} chars) sent to {pid}", time.time(), time.time()))
        return None

    def _record(self, pid, model, purpose, pt, ct, ok) -> None:
        self.db.execute("INSERT INTO ai_usage(provider,model,purpose,prompt_tokens,completion_tokens,ok,created) "
                        "VALUES(?,?,?,?,?,?,?)", (pid, model, purpose, pt, ct, int(ok), time.time()))

    # ── adapters ──────────────────────────────────────────────────────
    def _call(self, pid, cfg, prompt, system, history, json_mode, max_tokens, temperature, images=(), timeout=60):
        key = self.api_key(pid)
        kind = cfg["kind"]
        base = cfg["base_url"].rstrip("/")
        max_tokens = int(max_tokens or 1024)
        temperature = float(temperature if temperature is not None else 0.4)
        if kind == "openai":
            msgs = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": m["role"] if m["role"] in ("user", "assistant") else "user", "content": m["content"]} for m in history] + \
                   [{"role": "user", "content": prompt if not images else
                     [{"type": "text", "text": prompt}] + [{"type": "image_url", "image_url": {"url": f"data:{m};base64,{base64.b64encode(b).decode()}"}} for m, b in images]}]
            body = {"model": cfg["model"], "messages": msgs, "temperature": temperature, "max_tokens": max_tokens}
            if json_mode and pid == "openai":
                body["response_format"] = {"type": "json_object"}
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            r = self.http.post(f"{base}/chat/completions", json=body, headers=headers, timeout=timeout)
            _raise(r)
            j = r.json()
            u = j.get("usage") or {}
            return j["choices"][0]["message"]["content"] or "", u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        if kind == "anthropic":
            msgs = [{"role": m["role"], "content": m["content"]} for m in history if m["role"] in ("user", "assistant")]
            msgs.append({"role": "user", "content": prompt if not images else
                         [{"type": "image", "source": {"type": "base64", "media_type": m, "data": base64.b64encode(b).decode()}} for m, b in images]
                         + [{"type": "text", "text": prompt}]})
            body = {"model": cfg["model"], "max_tokens": max_tokens, "temperature": min(1.0, temperature), "messages": msgs}
            if system:
                body["system"] = system
            r = self.http.post(f"{base}/messages", json=body, timeout=timeout,
                               headers={"x-api-key": key or "", "anthropic-version": "2023-06-01"})
            _raise(r)
            j = r.json()
            u = j.get("usage") or {}
            text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
            return text, u.get("input_tokens", 0), u.get("output_tokens", 0)
        if kind == "gemini":
            contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                        for m in history if m["role"] in ("user", "assistant")]
            contents.append({"role": "user", "parts": [{"text": prompt}] + [{"inline_data": {"mime_type": m, "data": base64.b64encode(b).decode()}} for m, b in images]})
            gen = {"temperature": temperature, "maxOutputTokens": max_tokens}
            if json_mode:
                gen["responseMimeType"] = "application/json"
            body = {"contents": contents, "generationConfig": gen}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            r = self.http.post(f"{base}/models/{cfg['model']}:generateContent", json=body, timeout=timeout,
                               headers={"x-goog-api-key": key or ""})
            _raise(r)
            j = r.json()
            cands = j.get("candidates") or []
            if not cands:
                raise AIError("empty response (blocked or no candidates)")
            text = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))
            u = j.get("usageMetadata") or {}
            return text, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0)
        raise AIError(f"unsupported provider kind {kind}")


def _raise(r) -> None:
    if r.status_code >= 400:
        try:
            msg = r.json()
            msg = msg.get("error", msg)
            msg = msg.get("message", msg) if isinstance(msg, dict) else msg
        except Exception:
            msg = r.text[:200]
        hint = " (check the API key)" if r.status_code in (401, 403) else " (rate limit or quota)" if r.status_code == 429 else ""
        raise AIError(f"HTTP {r.status_code}: {str(msg)[:200]}{hint}")


def _clean_err(e: Exception) -> str:
    return logs.scrub(str(e))[:240]


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def parse_json(text: str):
    if not text:
        return None
    m = _FENCE.search(text)
    cand = m.group(1) if m else text
    for s, e in (("{", "}"), ("[", "]")):
        i, j = cand.find(s), cand.rfind(e)
        if i != -1 and j > i:
            try:
                return json.loads(cand[i:j + 1])
            except ValueError:
                continue
    return None
