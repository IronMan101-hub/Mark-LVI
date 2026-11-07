# GALAHAD architecture

GALAHAD is a layer on top of MARK LVI (JARVIS). Nothing in MARK LVI was removed; GALAHAD adds a headless
`galahad/` package and three thin hooks (`main.py`, `ui.py`, `plugins/galahad_*.py`).

```
 ┌────────────────────────── one process ───────────────────────────┐
 │  PyQt6 window (ui.py)  ── tray / minimise-to-tray (qt_integration)│
 │  Gemini Live voice (main.py) ── tools ──┐                         │
 │                                         ▼                         │
 │  plugins/galahad_*.py  (9 bridge tools: plugin + command + args)  │
 │                                         │                         │
 │   Local dashboard (FastAPI, 127.0.0.1) ─┤   python -m galahad run │
 │                                         ▼                         │
 │  ┌──────────────────────── Engine ─────────────────────────────┐  │
 │  │ Orchestrator → WorkflowEngine → Registry.execute()          │  │
 │  │   validate args → permission check → confirmation gate →    │  │
 │  │   run on worker thread with timeout → verify → activity log │  │
 │  │ Registry (lifecycle, health, config)   Scheduler (1 thread) │  │
 │  │ SQLite (migrations)  SecretStore  Settings  Memory  Events  │  │
 │  │ Notifications  ActivityLog  ConfirmationBroker  AIManager   │  │
 │  └──────────┬──────────────────────────────────────────────────┘  │
 │             ▼ PluginContext (the only handle a plugin gets)       │
 │   22 plugins in galahad/plugins/*.py                              │
 └───────────────────────────────────────────────────────────────────┘
```

## Layers (spec §2)
| Layer | Where |
|---|---|
| Core engine | `engine.py` (lifecycle), `events.py`, `settings.py`, `logs.py`, `db.py`, `scheduler.py` |
| Plugin engine | `plugin_api.py` (contract), `registry.py` (discover → validate → register → initialise → activate → execute → suspend → disable → unload) |
| AI orchestration | `orchestrator.py` (NL → plan), `workflows.py` (execution, conditions, retries, cancel, triggers), `ai.py` (providers) |
| Data layer | `db.py` — SQLite, WAL, versioned migrations (`PRAGMA user_version`). 20 tables |
| Security | `permissions.py`, `secrets.py`, `confirmations.py`, `textutil.check_url` (SSRF), path guards in `plugins/files.py` |
| Interface | `web/` (dashboard), `qt_integration.py` (tray), `bridge.py` (voice tools), `__main__.py` (CLI) |
| Background | `scheduler.py` + plugin `ticks()`; `instance.py` (single instance); `autostart.py` |

## The one pipeline
Every action — voice, dashboard, chat, workflow, scheduled job, plugin-to-plugin — goes through
`Registry.execute()`:

1. plugin exists and is **active**; command exists
2. arguments **validated and coerced** against the command's schema (unknown keys dropped, so a forged `confirmed=yes` is ignored)
3. required **permissions** are granted (checked on every call, revocable)
4. **confirmation gate**: if the command declares `confirm=`, nothing runs; a pending request is created and can only be
   resolved by the interface (dashboard button / HUD banner) via `Registry.approve()` — never by the model
5. runs on a daemon worker thread with a per-command **timeout**; cooperative cancellation via `ctx.check_cancel()`
6. result normalised; a plugin returning `{"ok": False}` is a **failure** (verification failed ≠ success)
7. **activity record** (pending → running → awaiting_confirmation → completed/failed/cancelled), events published, health updated

## Data model (SQLite, `data/galahad.db`)
settings · plugin_state · permissions · kv (plugin-scoped; secrets are sealed values here) · activity · notifications ·
conversations · memory_long · tasks · reminders · events_local · workflows · workflow_runs · articles · research ·
finance_entries/budgets/goals/holdings · ai_usage. New schema = append a tuple to `db.MIGRATIONS`; never edit old ones.

## Memory (spec §4)
* **short-term** — `conversations` rows for the current session + RAM working context
* **long-term** — `memory_long`, explicit key/value, editable, user-approved only
* **operational** — *derived* from `activity` and `workflow_runs`, so it cannot drift from what really happened
All three are inspectable, editable, exportable (`/api/memory/export`) and erasable (Settings → Memory manager).

## Isolation: what it is and what it is not
Plugins are Python modules in the same process. Isolation is **cooperative and enforced at the API boundary**:
plugins receive only a `PluginContext` whose services check permissions; commands run with timeouts on separate
threads and crash in isolation; user-supplied plugin files are loaded only after the user trusts that exact file hash.
It is **not** an OS sandbox — a malicious plugin author could still import `os`. Only install plugins you trust.

## Voice (spec §4, §22)
MARK LVI's Gemini Live pipeline (STT, TTS, wake word, push-to-talk, VAD, interruption, device selection) is preserved
unchanged. GALAHAD connects to it three ways: the 9 `galahad_*` tools; `voice.*` settings are *views* of MARK's own
config (`legacy_sync.py`) so the two never disagree; and `assistant.say` / reminders speak through `JarvisLive.plugin_say`.

## Threading rules (AGENTS.md)
`galahad/` never touches a Qt widget except `qt_integration.py`, which runs on the Qt thread and only creates its own tray
objects. Everything else reaches the UI through `player.write_log` and the event bus.
