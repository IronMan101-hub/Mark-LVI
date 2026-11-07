"""Persistent, typed application settings stored in SQLite."""
from __future__ import annotations

import json
import time
from typing import Any

from . import legacy_sync
from .db import Database
from .events import EventBus

# key -> (default, type, description)
DEFAULTS: dict[str, tuple[Any, str, str]] = {
    "general.app_name": ("GALAHAD", "str", "Application name"),
    "general.language": ("en", "str", "Interface language"),
    "general.clock_24h": (False, "bool", "Show times in 24-hour format"),
    "general.timezone": ("", "str", "IANA timezone, blank = system"),
    "general.default_browser": ("", "str", "Default browser (edge, chrome, firefox or blank)"),
    "startup.start_with_windows": (False, "bool", "Start GALAHAD when Windows starts"),
    "startup.start_minimised": (False, "bool", "Start minimised to tray"),
    "startup.minimise_to_tray": (True, "bool", "Closing the window keeps GALAHAD running"),
    "startup.voice_service": (True, "bool", "Enable the voice service"),
    "startup.notifications": (True, "bool", "Enable notifications"),
    "startup.background_plugins": ([], "list", "Plugin IDs whose background services run"),
    "ai.provider": ("gemini", "str", "Provider for conversation"),
    "ai.model": ("", "str", "Model for conversation (blank = provider default)"),
    "ai.temperature": (0.4, "float", "Sampling temperature"),
    "ai.max_tokens": (1024, "int", "Max response tokens"),
    "ai.fallback_providers": ([], "list", "Providers to try, in order, when the primary fails"),
    "ai.workflow_providers": ({}, "dict", "Per-purpose provider overrides, e.g. {'research': 'anthropic'}"),
    "ai.context_messages": (12, "int", "How many recent messages are sent as context"),
    "memory.enabled": (True, "bool", "Remember preferences between conversations"),
    "memory.history_days": (90, "int", "Days of conversation history kept"),
    "voice.microphone": ("", "str", "Input device"),
    "voice.speaker": ("", "str", "Output device"),
    "voice.voice": ("", "str", "Voice name"),
    "voice.speed": (1.0, "float", "Speaking speed"),
    "voice.wake_word": ("hey jarvis", "str", "Wake word"),
    "voice.listening_mode": ("wake_word", "str", "wake_word | push_to_talk | continuous"),
    "privacy.screen_capture": (False, "bool", "Allow screen capture"),
    "privacy.share_private_with_ai": (False, "bool", "Allow email/file/finance content to be sent to an AI provider"),
    "privacy.log_level": ("INFO", "str", "Log level"),
    "privacy.activity_retention_days": (60, "int", "Days of activity history kept"),
    "privacy.search_roots": ([], "list", "Extra folders unified search may scan (besides Documents/Desktop/Downloads)"),
    "appearance.theme": ("dark", "str", "dark | light"),
    "appearance.accent": ("#35e0ff", "str", "Accent colour"),
    "appearance.background": ("", "str", "CSS background (colour or image URL)"),
    "appearance.transparency": (0.85, "float", "Panel opacity 0.3-1.0"),
    "appearance.animations": (True, "bool", "Enable animations"),
    "appearance.layout": ({"home": ["clock", "weather", "system", "tasks", "reminders", "news", "notifications", "assistant", "quick"]},
                          "dict", "Dashboard widget layout"),
    "notifications.min_priority": (0, "int", "Hide notifications below this priority"),
    "notifications.voice": (True, "bool", "Speak reminder/alert notifications"),
    "notifications.quiet_hours": ("", "str", "Quiet hours as HH:MM-HH:MM (blank = off)"),
    "location.city": ("", "str", "City for weather"),
    "location.latitude": (0.0, "float", "Latitude"),
    "location.longitude": (0.0, "float", "Longitude"),
    "dashboard.port": (8765, "int", "Local dashboard port"),
    "dashboard.enabled": (True, "bool", "Run the local dashboard"),
}


def _coerce(value: Any, kind: str) -> Any:
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if kind == "int":
        return int(float(value))
    if kind == "float":
        return float(value)
    if kind == "list":
        if isinstance(value, str):
            value = json.loads(value) if value.strip().startswith("[") else [v.strip() for v in value.split(",") if v.strip()]
        if not isinstance(value, list):
            raise ValueError("expected a list")
        return value
    if kind == "dict":
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    return str(value)


class Settings:
    def __init__(self, db: Database, bus: EventBus):
        self.db, self.bus = db, bus

    def get(self, key: str, default: Any = None) -> Any:
        if key in legacy_sync.MAPPED:                  # voice settings live in MARK LVI's own config
            found, val = legacy_sync.read(key)
            if found:
                return val
        row = self.db.one("SELECT value FROM settings WHERE key=?", (key,))
        if row is not None:
            try:
                return json.loads(row["value"])
            except ValueError:
                pass
        if key in DEFAULTS:
            return DEFAULTS[key][0]
        return default

    def set(self, key: str, value: Any) -> Any:
        if key not in DEFAULTS:
            raise KeyError(f"unknown setting '{key}'")
        kind = DEFAULTS[key][1]
        value = _coerce(value, kind)
        if key == "appearance.transparency":
            value = min(1.0, max(0.3, value))
        if key == "appearance.theme" and value not in ("dark", "light"):
            raise ValueError("theme must be dark or light")
        if key == "voice.listening_mode" and value not in ("wake_word", "push_to_talk", "continuous"):
            raise ValueError("listening_mode must be wake_word, push_to_talk or continuous")
        if key == "dashboard.port" and not (1024 <= value <= 65535):
            raise ValueError("port must be 1024-65535")
        self.last_note = ""
        if key in legacy_sync.MAPPED:
            self.last_note = legacy_sync.write(key, value, getattr(self, "player", None))
        self.db.execute(
            "INSERT INTO settings(key,value,updated) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key, json.dumps(value), time.time()))
        self.bus.publish("settings.changed", {"key": key, "value": value})
        return value

    def reset(self, key: str) -> None:
        self.db.execute("DELETE FROM settings WHERE key=?", (key,))
        self.bus.publish("settings.changed", {"key": key, "value": self.get(key)})

    def all(self) -> list[dict]:
        out = []
        for key, (default, kind, desc) in DEFAULTS.items():
            out.append({"key": key, "value": self.get(key), "default": default,
                        "type": kind, "description": desc, "section": key.split(".")[0]})
        return out
