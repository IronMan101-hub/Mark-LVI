# Troubleshooting

Run `python -m galahad doctor` first; it lists missing packages, plugin errors and the AI/secret-store state.

| Symptom | Cause / fix |
|---|---|
| Dashboard says "engine offline" | The app closed. Start `python main.py` or `python -m galahad serve`. |
| Dashboard 401 / "Session expired" | Reopen it from the 🛰 button / tray (the link carries a fresh token). |
| Port 8765 busy | It silently uses the next free port; the HUD button / tray opens the right one. Change `dashboard.port` in Settings. |
| "No AI provider — offline mode" | Settings → AI providers. A ChatGPT/Claude/Gemini *subscription* is not an API key. |
| "All AI providers failed: HTTP 401" | Wrong/expired key. HTTP 429 = quota or rate limit — add a fallback provider. |
| Plugin shows **error** with "Missing Python package" | Run the `pip install …` shown, then Plugins → Update. |
| Plugin **unconfigured** | Open it in Plugins; the Setup box says exactly what to supply. |
| "Permission not granted: mail.read / screen.capture / finance.data" | Sensitive by design: Plugins → that plugin → Permissions. |
| Screenshot refused | Needs Settings → Privacy → Screen capture **and** `screen.capture` on *Computer* (and *Vision*). |
| Spotify "no active device" | Open Spotify on this PC/phone and play once. "Premium required": playback control is Premium-only (search still works). |
| Spotify redirect error | The redirect URI in your Spotify app must be exactly `http://127.0.0.1:8888/callback` (or your configured port). |
| Google "access blocked / app not verified" | Add your account as a **test user** on the OAuth consent screen; use a *Desktop app* client. |
| Tab control unavailable | Run **Start controlled browser**. A browser already open *without* a debug port can't be attached to — that's a browser limit. GALAHAD uses its own profile (sign in once). |
| Web search returns nothing | The engine may be blocking automated requests; install `ddgs`. GALAHAD never solves CAPTCHAs or bypasses blocks. |
| News "No headlines" | Offline or a feed moved: Intelligence → Sources shows per-feed status; add your own RSS feeds there. |
| Reminder didn't speak | Voice announcements need the desktop app's live session; the notification still appears (Notifications page, tray balloon). Quiet hours (Settings → Notifications) suppress delivery but not storage. |
| Two JARVIS windows | Not possible by default (single-instance lock). `GALAHAD_ALLOW_MULTI=1` disables the guard. |
| Closing the window doesn't quit | Minimise-to-tray is on. Tray → *Quit GALAHAD*, or turn it off in Settings → Startup. |
| Start-with-Windows toggled but nothing happens | Windows only; check Task Manager → Startup. `python -m galahad autostart status`. |
| Word/Excel/PowerPoint "can't open" | The file isn't a real OOXML document (or is open exclusively elsewhere). Old `.doc/.xls/.ppt` aren't supported. |
| Excel formulas show no values | Formulas are stored; Excel calculates them on open. `calculate` computes plain numbers only. |
| Stale `catalog.json` test failure | `python -m galahad catalog` and commit. |
| Where are the logs? | `data/logs/galahad.log` (rotating; API keys are scrubbed). |
| Reset everything | Quit, delete `data/` (settings, tasks, memory, keys). MARK's `config/api_keys.json` is separate. |

## What GALAHAD will not do
Bypass logins/CAPTCHAs, read WhatsApp or Instagram data, send as your personal WhatsApp automatically, execute trades or transfer money,
record your screen continuously, or send mail/files/finance data to an AI provider without your setting/consent.
