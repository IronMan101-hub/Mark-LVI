# Installing GALAHAD

GALAHAD ships inside the MARK LVI repository; one install covers both.

```bash
git clone https://github.com/IronMan101-hub/GALAHAD   # or your existing Mark-LVI checkout
cd GALAHAD
python setup.py            # installs requirements.txt (Windows/macOS/Linux)
python main.py             # JARVIS + GALAHAD (window, tray, voice, dashboard)
```

Python 3.10+ (developed on 3.13). On Windows, `tzdata`, `pycaw`, `pywin32`, `win10toast` are installed automatically.

## Ways to run
| Command | What you get |
|---|---|
| `python main.py` | The full desktop app. The engine, tray icon and local dashboard start with it. |
| `python main.py --minimized` | Same, starting hidden in the tray. |
| `python -m galahad serve` | Headless: engine + dashboard + reminders/workflows/notifications, no window or voice. |
| `python -m galahad dashboard` | Start the dashboard and open it in your browser. |
| `python -m galahad run "remind me to call Sam in 20 minutes"` | One request from the terminal. |
| `python -m galahad plugins` / `status` / `doctor` | Inspect and diagnose. |
| `python -m galahad autostart on` | Start with Windows (per-user Run key, no admin). |

Only one instance runs per user: a second launch asks the first to show itself. Set `GALAHAD_ALLOW_MULTI=1` to bypass.

## Dashboard
Opens at `http://127.0.0.1:8765` (next free port if busy; bound to loopback only). The 🛰 **GALAHAD DASHBOARD** button in
the HUD drawer, the tray menu, or `python -m galahad dashboard` opens it with a one-time token that becomes a
SameSite=Strict cookie. Nothing else on your network can reach it.

## Optional extras (everything degrades gracefully without them)
`pip install pytesseract` + the Tesseract binary (offline OCR) · `yt-dlp` (YouTube metadata without an API key) ·
`ddgs` (better web search) · `faster-whisper`/`piper-tts` (local voice, already documented in the MARK sections).

## Tests
```bash
pip install pytest
python -m pytest tests -q        # ~2 min; browser tests need Chromium (Playwright's, or set the path in tests)
```

## Updating
`git pull`, then `python setup.py`. Database migrations run automatically on start. If you change plugins' commands, run
`python -m galahad catalog` to refresh `galahad/catalog.json` (a test fails if it is stale).
