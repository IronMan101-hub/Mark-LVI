# Configuring GALAHAD

Everything is in the dashboard (**Settings**, **Plugins**). Keys never appear in the UI after saving.

## 1. Connect an AI provider (recommended first step)
Settings → AI providers. **A chat subscription is not API access** — ChatGPT Plus, Claude Pro and Gemini Advanced include no API
credits. Create a key in the provider's developer console:
Gemini https://aistudio.google.com/apikey · OpenAI https://platform.openai.com/api-keys · Anthropic https://console.anthropic.com/settings/keys.
Local models (Ollama, LM Studio, llama.cpp, Groq, OpenRouter…): choose **OpenAI-compatible**, set the base URL
(e.g. `http://localhost:11434/v1`) and model. If you already use MARK LVI with a Gemini key, GALAHAD reuses it.
Press **Test connection**, then **Use for conversation**. Optional: fallback order, per-purpose providers
(`ai.workflow_providers`, e.g. `{"research": "anthropic"}`), temperature, max tokens. Usage per model is listed below the cards.

Without a provider GALAHAD still works through a built-in offline command router (tasks, reminders, weather, calculator, files,
news, system info…); free-form requests, summaries and research cross-checking need a provider.

## 2. What you need to supply, per integration
| Integration | You supply | Where |
|---|---|---|
| Spotify | Client ID from developer.spotify.com (redirect URI `http://127.0.0.1:8888/callback`); Premium for playback control | Plugins → Spotify, then `connect` |
| Gmail / Google Calendar | OAuth **Desktop** client ID + secret from Google Cloud Console (enable Gmail/Calendar API, add yourself as a test user) | Plugins → Gmail / Productivity, then `connect`; grant `mail.read` |
| YouTube search results | optional YouTube Data API v3 key (or install `yt-dlp`) | Plugins → YouTube |
| WhatsApp sending | optional Meta WhatsApp **Business Cloud API** phone-number ID + token | Plugins → WhatsApp (drafting needs nothing) |
| Weather | nothing (Open-Meteo); set your city | Settings → Location |
| Currency / markets | nothing (ECB via Frankfurter; Stooq) | — |
| News | nothing (public RSS; add your own feeds) | Intelligence → Sources |
| Tab control | Edge/Chrome installed; run **Start controlled browser** once | Media / Plugins → Browser |
| Screen understanding | turn on Settings → Privacy → Screen capture **and** grant `screen.capture` | Settings / Plugins → Vision, Computer |

## 3. Privacy controls (Settings → Privacy)
* **share private data with AI** (default OFF): email, documents and finance data are summarised locally; an AI provider only sees them if
  you turn this on. Every private send is recorded in Activity as `ai.private_send`.
* Images (Vision) are never sent without a per-request confirmation naming the provider.
* **Screen capture** (default OFF) plus the sensitive `screen.capture` permission. No continuous capture exists.
* Sensitive permissions (`mail.read`, `screen.capture`, `finance.data`) are denied until you grant them, per plugin.
* Search roots: only Documents/Desktop/Downloads/Pictures/Music/Videos and folders you add.
* Memory: view / edit / export / erase at any time; retention days for history and activity.

## 4. Where data lives
`data/` beside the app (or `GALAHAD_HOME`): `galahad.db` (all records), `.secret.key` (Fernet key — **on Windows secrets use DPAPI instead**),
`logs/` (rotating, secrets scrubbed), `exports/`, `screenshots/`, `browser-profile/`, `user_plugins/`. All git-ignored.
MARK's `config/api_keys.json` is still used for the Gemini key and voice settings; GALAHAD never writes to it directly (it goes through
`memory.config_manager`).

## 5. Background & startup
Settings → Startup: start with Windows, start minimised, minimise-to-tray, voice service, notifications, which background plugins run.
Quiet hours and minimum priority: Settings → Notifications.
