"""Keeps GALAHAD's voice settings and MARK LVI's own config in step.

MARK LVI already stores the voice, microphone, speaker, wake word and push-to-talk in
config/api_keys.json (through memory/config_manager.py) and the voice session reads them
from there. Rather than keep a second copy that could disagree, the `voice.*` settings
below are *views* of that config: reading returns MARK's value, writing updates MARK's
config and — when the desktop app is running — pokes the live session.
"""
from __future__ import annotations

from typing import Any, Optional

MAPPED = {"voice.voice", "voice.microphone", "voice.speaker", "voice.listening_mode"}


def _cm():
    try:
        from memory import config_manager as cm
        return cm
    except Exception:
        return None


def read(key: str) -> tuple[bool, Any]:
    cm = _cm()
    if cm is None or key not in MAPPED:
        return False, None
    try:
        if key == "voice.voice":
            return True, cm.get_voice()
        if key == "voice.microphone":
            return True, cm.get_input_device() or ""
        if key == "voice.speaker":
            return True, cm.get_output_device() or ""
        if key == "voice.listening_mode":
            if cm.get_push_to_talk_enabled():
                return True, "push_to_talk"
            return True, "wake_word" if cm.get_wake_word_enabled() else "continuous"
    except Exception:
        return False, None
    return False, None


def write(key: str, value: Any, player=None) -> str:
    """Returns a short note about how/when the change takes effect."""
    cm = _cm()
    if cm is None or key not in MAPPED:
        return ""
    live = player is not None
    try:
        if key == "voice.voice":
            cm.save_voice(str(value))
            if live and getattr(player, "on_voice_change", None):
                player.on_voice_change()
                return "Voice changed; the session is reconnecting."
            return "Voice saved; it applies the next time the voice session connects."
        if key == "voice.microphone":
            cm.save_input_device(str(value))
            return "Microphone saved; it applies when the voice session reconnects."
        if key == "voice.speaker":
            cm.save_output_device(str(value))
            return "Speaker saved; it applies when the voice session reconnects."
        if key == "voice.listening_mode":
            ptt, wake = value == "push_to_talk", value == "wake_word"
            cm.save_push_to_talk_enabled(ptt)
            cm.save_wake_word_enabled(wake)
            if live:
                if getattr(player, "on_push_to_talk", None):
                    player.on_push_to_talk(ptt)
                if getattr(player, "on_wake_toggle", None):
                    player.on_wake_toggle(wake)
                return f"Listening mode is now {value}."
            return f"Listening mode saved ({value}); it applies when the desktop app starts."
    except Exception as e:  # noqa: BLE001
        return f"Saved in GALAHAD, but MARK's voice config rejected it: {e}"
    return ""


def options() -> dict:
    """Choices for the settings UI. Empty lists mean 'unknown here', never invented."""
    out = {"voices": [], "microphones": [], "speakers": []}
    cm = _cm()
    if cm is not None:
        out["voices"] = list(getattr(cm, "AVAILABLE_VOICES", []) or [])
    try:
        from core import audio_devices as ad
        out["microphones"] = list(ad.list_devices("input"))
        out["speakers"] = list(ad.list_devices("output"))
    except Exception:
        try:
            import sounddevice as sd
            devs = sd.query_devices()
            out["microphones"] = sorted({d["name"] for d in devs if d["max_input_channels"] > 0})
            out["speakers"] = sorted({d["name"] for d in devs if d["max_output_channels"] > 0})
        except Exception:
            pass
    return out
