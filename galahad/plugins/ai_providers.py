"""AI provider & model management (OpenAI, Anthropic, Gemini, OpenAI-compatible/local)."""
from __future__ import annotations

from ..ai import PROVIDERS, SUBSCRIPTION_NOTE, AIError
from ..plugin_api import Health, Manifest, Plugin, PluginError, command


class AIProviders(Plugin):
    manifest = Manifest(
        id="ai_providers", name="AI Providers", category="AI", version="1.0.0",
        description="Choose which AI provider/model handles conversation and specific workflows; store API keys securely; "
                    "test connections; see token usage; set fallbacks.",
        permissions=["ai.use", "network"],
        setup_help=SUBSCRIPTION_NOTE + " Keys are stored encrypted (Windows DPAPI on Windows). Local models (Ollama / LM Studio) "
                   "need no key — choose 'OpenAI-compatible' and set its address.")

    def health(self):
        ai = self.ctx._e.ai
        configured = [p for p in PROVIDERS if ai.is_configured(p) and (PROVIDERS[p]["needs_key"] or p == "openai_compatible" and False)]
        if ai.available():
            return Health("ok", f"Active provider: {self.ctx.setting('ai.provider')}")
        return Health("unconfigured", "No provider has an API key yet. " + SUBSCRIPTION_NOTE)

    @command(description="List providers with their model, configuration state and which is active.", readonly=True)
    def list_providers(self):
        st = self.ctx._e.ai.status()
        return {"message": "; ".join(f"{p['label']}: {'ready' if p['configured'] else 'needs API key'}"
                                      f"{' (active)' if p['active'] else ''}" for p in st),
                "data": {"providers": st, "note": SUBSCRIPTION_NOTE,
                         "fallbacks": self.ctx.setting("ai.fallback_providers", []),
                         "workflow_providers": self.ctx.setting("ai.workflow_providers", {})}}

    @command(description="Store a provider's API key / model / endpoint / sampling settings. The key is encrypted at rest.",
             params={"provider": {"type": "string", "required": True, "enum": list(PROVIDERS)},
                     "api_key": {"type": "string"}, "model": {"type": "string"}, "base_url": {"type": "string"},
                     "temperature": {"type": "number", "minimum": 0, "maximum": 2}, "max_tokens": {"type": "integer", "minimum": 16, "maximum": 32000}})
    def configure(self, provider, api_key="", model="", base_url="", temperature=None, max_tokens=None):
        try:
            self.ctx._e.ai.configure(provider, api_key=api_key or None, model=model or None, base_url=base_url or None,
                                     temperature=temperature, max_tokens=max_tokens)
        except AIError as e:
            raise PluginError(str(e))
        return f"{PROVIDERS[provider]['label']} updated" + (" (key stored securely)." if api_key else ".")

    @command(description="Choose the provider/model for conversation, an ordered fallback list, and per-purpose overrides "
                         "(purposes: assistant, research, news, email, documents).",
             params={"provider": {"type": "string", "enum": list(PROVIDERS)}, "model": {"type": "string"},
                     "fallbacks": {"type": "array", "description": "Provider ids, in order"},
                     "purpose": {"type": "string"}, "purpose_provider": {"type": "string", "enum": list(PROVIDERS)}})
    def select(self, provider="", model="", fallbacks=None, purpose="", purpose_provider=""):
        e = self.ctx._e
        notes = []
        if provider:
            e.settings.set("ai.provider", provider)
            notes.append(f"conversation → {provider}")
            if model:
                e.ai.configure(provider, model=model)
                notes.append(f"model → {model}")
        if fallbacks is not None:
            bad = [f for f in fallbacks if f not in PROVIDERS]
            if bad:
                raise PluginError(f"Unknown provider(s): {', '.join(bad)}")
            e.settings.set("ai.fallback_providers", fallbacks)
            notes.append("fallbacks → " + (", ".join(fallbacks) or "none"))
        if purpose and purpose_provider:
            m = dict(e.settings.get("ai.workflow_providers", {}) or {})
            m[purpose] = purpose_provider
            e.settings.set("ai.workflow_providers", m)
            notes.append(f"{purpose} → {purpose_provider}")
        if not notes:
            raise PluginError("Nothing to change.")
        return "Updated: " + "; ".join(notes)

    @command(description="Test a provider connection with a tiny request.", permissions=("ai.use", "network"), timeout=70,
             params={"provider": {"type": "string", "required": True, "enum": list(PROVIDERS)}})
    def test_connection(self, provider):
        r = self.ctx._e.ai.test(provider)
        return {"ok": r["ok"], "message": r["message"], "data": r}

    @command(description="Token and call usage by provider/model for the last N days.", readonly=True,
             params={"days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 365}})
    def usage(self, days=30):
        u = self.ctx._e.ai.usage(days)
        total = sum(r["calls"] for r in u["by_model"])
        return {"message": f"{total} AI call(s) in {days} days.", "data": u}

    @command(description="Ask the selected AI provider a question directly.", permissions=("ai.use", "network"), timeout=90,
             params={"prompt": {"type": "string", "required": True}})
    def ask(self, prompt):
        try:
            r = self.ctx.llm(prompt, purpose="assistant")
        except AIError as e:
            raise PluginError(str(e))
        return {"message": r.text.strip(), "data": {"provider": r.provider, "model": r.model}}


PLUGINS = [AIProviders]
