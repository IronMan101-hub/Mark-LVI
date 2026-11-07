"""Credential storage.

Windows: values are sealed with DPAPI (CryptProtectData) so only the same
Windows user on the same machine can open them.
Elsewhere (and as a fallback): Fernet with a random key kept in a 0600 file
beside the database. That protects against casual reading of the database but
NOT against someone who can read both files — `backend_name()` says honestly
which one is in use, and the dashboard shows it.

Secrets are never written to the settings table, activity log or logs.
"""
from __future__ import annotations

import base64
import os
import sys
from typing import Optional

from . import paths
from .db import Database


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


class SecretStore:
    NS = "secrets"

    def __init__(self, db: Database):
        self.db = db
        self._fernet = None
        self._backend = "dpapi" if sys.platform == "win32" else "fernet-file"
        if self._backend == "fernet-file":
            self._init_fernet()

    def backend_name(self) -> str:
        return {"dpapi": "Windows DPAPI (per-user)",
                "fernet-file": "Fernet key file (obfuscation at rest — not an OS keystore)"}[self._backend]

    def _init_fernet(self) -> None:
        from cryptography.fernet import Fernet
        keyfile = paths.home() / ".secret.key"
        if keyfile.exists():
            key = keyfile.read_bytes()
        else:
            key = Fernet.generate_key()
            fd = os.open(str(keyfile), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(key)
        self._fernet = Fernet(key)

    def _seal(self, text: str) -> str:
        raw = text.encode("utf-8")
        blob = _dpapi(raw, True) if self._backend == "dpapi" else self._fernet.encrypt(raw)
        return base64.b64encode(blob).decode("ascii")

    def _open(self, sealed: str) -> str:
        blob = base64.b64decode(sealed)
        raw = _dpapi(blob, False) if self._backend == "dpapi" else self._fernet.decrypt(blob)
        return raw.decode("utf-8")

    @staticmethod
    def _name(scope: str, name: str) -> str:
        return f"{scope}/{name}"

    def set(self, scope: str, name: str, value: str) -> None:
        if not value:
            return self.delete(scope, name)
        self.db.kv_set(self.NS, self._name(scope, name), self._seal(value))

    def get(self, scope: str, name: str) -> Optional[str]:
        env = os.environ.get(f"GALAHAD_SECRET_{scope}_{name}".upper().replace("-", "_").replace(".", "_"))
        if env:
            return env
        sealed = self.db.kv_get(self.NS, self._name(scope, name))
        if sealed:
            try:
                return self._open(sealed)
            except Exception:
                return None
        return None

    def delete(self, scope: str, name: str) -> None:
        self.db.kv_delete(self.NS, self._name(scope, name))

    def has(self, scope: str, name: str) -> bool:
        return self.get(scope, name) is not None

    def names(self, scope: str) -> list[str]:
        prefix = f"{scope}/"
        return sorted(k[len(prefix):] for k in self.db.kv_all(self.NS) if k.startswith(prefix))


class ScopedSecrets:
    """What a plugin receives: it can only touch its own scope (plus read-only
    shared provider keys when it holds the `ai.use` permission, via ctx.llm)."""

    def __init__(self, store: SecretStore, scope: str):
        self._s, self._scope = store, scope

    def get(self, name: str) -> Optional[str]:
        return self._s.get(self._scope, name)

    def set(self, name: str, value: str) -> None:
        self._s.set(self._scope, name, value)

    def delete(self, name: str) -> None:
        self._s.delete(self._scope, name)

    def has(self, name: str) -> bool:
        return self._s.has(self._scope, name)
