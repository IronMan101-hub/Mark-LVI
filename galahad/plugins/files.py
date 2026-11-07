"""File management: search, open, create, rename, move, copy, delete (to Recycle Bin),
organise. Destructive operations are confirmed, never overwrite silently, and
refuse system locations."""
from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..plugin_api import Health, Manifest, Plugin, PluginError, command

TYPE_GROUPS = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".heic", ".tiff"},
    "Documents": {".doc", ".docx", ".pdf", ".txt", ".rtf", ".odt", ".md"},
    "Spreadsheets": {".xls", ".xlsx", ".csv", ".ods"},
    "Presentations": {".ppt", ".pptx", ".odp"},
    "Audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"},
    "Video": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "Installers": {".exe", ".msi", ".dmg", ".pkg", ".deb"},
    "Code": {".py", ".js", ".ts", ".html", ".css", ".json", ".java", ".c", ".cpp", ".cs", ".go", ".rs"},
}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "$recycle.bin", "system volume information", "appdata", ".cache",
             "windows", "program files", "program files (x86)", "programdata", ".venv", "venv"}
KIND_EXT = {"document": TYPE_GROUPS["Documents"] | {".docx"}, "word": {".doc", ".docx"}, "excel": {".xls", ".xlsx", ".csv"},
            "spreadsheet": TYPE_GROUPS["Spreadsheets"], "powerpoint": {".ppt", ".pptx"}, "presentation": TYPE_GROUPS["Presentations"],
            "pdf": {".pdf"}, "image": TYPE_GROUPS["Images"], "audio": TYPE_GROUPS["Audio"], "video": TYPE_GROUPS["Video"]}


def protected_path(p: Path) -> Optional[str]:
    """Reason this path must not be modified, or None."""
    try:
        rp = p.resolve()
    except OSError:
        return "that path can't be resolved"
    s = str(rp).lower().replace("\\", "/").rstrip("/")
    home = str(Path.home().resolve()).lower().replace("\\", "/").rstrip("/")
    if s in ("", "/") or (len(s) == 2 and s[1] == ":") or (len(s) == 3 and s[1:] == ":/"):
        return "that is a drive root"
    if s == home:
        return "that is your home folder"
    for bad in ("c:/windows", "c:/program files", "c:/program files (x86)", "c:/programdata", "/etc", "/usr", "/bin", "/sbin",
                "/boot", "/system", "/lib", "/var/lib", "/proc", "/sys", "/dev"):
        if s == bad or s.startswith(bad + "/"):
            return "that is a system location"
    parts = set(s.split("/"))
    if ".git" in parts or "appdata" in parts and "/appdata/local/microsoft" in s:
        return "that is an application/system folder"
    return None


def human_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


class Files(Plugin):
    manifest = Manifest(
        id="files", name="Files", category="System", version="1.0.0",
        description="Search, open, create, rename, move, copy, delete (recoverable) and organise files and folders.",
        permissions=["fs.read", "fs.write", "fs.delete", "fs.scan"], optional_python=["send2trash"],
        config_schema=[{"key": "extra_roots", "label": "Extra folders to search (comma separated)", "type": "string", "default": ""}])

    def initialize(self, ctx):
        super().initialize(ctx)
        Files._instance = self

    def health(self):
        return Health("ok", "recoverable delete via send2trash" if _has("send2trash") else
                      "send2trash not installed: delete moves files to a local .galahad-trash folder instead")

    # ── helpers ───────────────────────────────────────────────────────
    def roots(self) -> list[Path]:
        home = Path.home()
        cands = [home / n for n in ("Documents", "Desktop", "Downloads", "Pictures", "Music", "Videos", "OneDrive")]
        for extra in (self.ctx.config.get("extra_roots") or "").split(",") + list(self.ctx.setting("privacy.search_roots", [])):
            if extra.strip():
                cands.append(Path(extra.strip()).expanduser())
        return [c for c in cands if c.is_dir()]

    def _path(self, value: str) -> Path:
        p = Path(os.path.expandvars(os.path.expanduser(value.strip().strip('"'))))
        if not p.is_absolute():
            for r in self.roots():
                if (r / p).exists():
                    return r / p
            p = Path.home() / p
        return p

    def _walk(self, roots, max_depth=6):
        for root in roots:
            base_depth = len(Path(root).parts)
            for dp, dns, fns in os.walk(root, onerror=lambda e: None):
                self.ctx.check_cancel()
                dns[:] = [d for d in dns if d.lower() not in SKIP_DIRS and not d.startswith(".")]
                if len(Path(dp).parts) - base_depth >= max_depth:
                    dns[:] = []
                yield dp, dns, fns

    # ── search ────────────────────────────────────────────────────────
    @command(description="Search files/folders by name in Documents, Desktop, Downloads etc. kind filters by type "
                         "(word, excel, powerpoint, pdf, image, audio, video, document).",
             params={"query": {"type": "string", "required": True}, "kind": {"type": "string"},
                     "folder": {"type": "string", "description": "Restrict to this folder"},
                     "limit": {"type": "integer", "default": 20, "maximum": 200, "minimum": 1},
                     "folders_only": {"type": "boolean", "default": False}},
             permissions=("fs.scan",), readonly=True, timeout=45)
    def search(self, query, kind="", folder="", limit=20, folders_only=False):
        words = [w for w in query.lower().replace("*", " ").split() if w not in ("the", "my", "a", "file", "files", "folder", "called", "named")]
        exts = KIND_EXT.get(kind.lower()) if kind else None
        roots = [self._path(folder)] if folder else self.roots()
        if folder and not roots[0].is_dir():
            raise PluginError(f"'{folder}' is not a folder I can find.")
        hits, deadline = [], time.time() + 35
        for dp, dns, fns in self._walk(roots):
            names = dns if folders_only else (fns + dns)
            for n in names:
                low = n.lower()
                if words and not all(w in low for w in words):
                    continue
                full = Path(dp) / n
                if exts and full.suffix.lower() not in exts and not full.is_dir():
                    continue
                try:
                    st = full.stat()
                except OSError:
                    continue
                hits.append({"path": str(full), "name": n, "is_dir": full.is_dir(), "size": st.st_size, "modified": st.st_mtime})
            if len(hits) >= limit * 5 or time.time() > deadline:
                break
        hits.sort(key=lambda h: (-sum(w in h["name"].lower() for w in words), -h["modified"]))
        hits = hits[:limit]
        if not hits:
            return {"message": f"No files matching '{query}' in {', '.join(r.name for r in roots) or 'the search folders'}.", "data": {"results": []}}
        return {"message": f"Found {len(hits)}: " + "; ".join(h["path"] for h in hits[:5]), "data": {"results": hits}}

    @command(description="Recently modified files in the user's folders.", permissions=("fs.scan",), readonly=True, timeout=45,
             params={"days": {"type": "integer", "default": 7, "minimum": 1}, "kind": {"type": "string"},
                     "limit": {"type": "integer", "default": 15, "maximum": 100}})
    def recent(self, days=7, kind="", limit=15):
        exts = KIND_EXT.get(kind.lower()) if kind else None
        cutoff, out = time.time() - days * 86400, []
        for dp, _dns, fns in self._walk(self.roots(), max_depth=4):
            for n in fns:
                fp = Path(dp) / n
                if exts and fp.suffix.lower() not in exts:
                    continue
                try:
                    st = fp.stat()
                except OSError:
                    continue
                if st.st_mtime >= cutoff:
                    out.append({"path": str(fp), "name": n, "modified": st.st_mtime, "size": st.st_size})
        out.sort(key=lambda x: -x["modified"])
        out = out[:limit]
        return {"message": f"{len(out)} recent file(s)" + (": " + "; ".join(o["name"] for o in out[:5]) if out else "."), "data": {"results": out}}

    @command(description="Describe a file or folder (size, dates, item count).", permissions=("fs.read",), readonly=True,
             params={"path": {"type": "string", "required": True}})
    def info(self, path):
        p = self._path(path)
        if not p.exists():
            raise PluginError(f"'{path}' doesn't exist.")
        st = p.stat()
        d = {"path": str(p), "is_dir": p.is_dir(), "size": st.st_size, "modified": st.st_mtime, "created": st.st_ctime}
        if p.is_dir():
            d["items"] = sum(1 for _ in p.iterdir())
        return {"message": f"{p} — {'folder, ' + str(d['items']) + ' items' if p.is_dir() else human_size(st.st_size)}, "
                           f"modified {datetime.fromtimestamp(st.st_mtime):%d %b %Y %H:%M}", "data": d}

    @command(description="Open a file or folder with its default application.", permissions=("fs.read",),
             params={"path": {"type": "string", "required": True}})
    def open(self, path):
        p = self._path(path)
        if not p.exists():
            raise PluginError(f"'{path}' doesn't exist.")
        try:
            if sys.platform == "win32":
                os.startfile(str(p))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"Couldn't open it: {e}")
        return {"message": f"Opened {p}. (I can't confirm the window appeared — check your screen.)", "data": {"path": str(p)}}

    # ── create / rename / move / copy ─────────────────────────────────
    @command(description="Create a folder (and parents).", permissions=("fs.write",),
             params={"path": {"type": "string", "required": True}})
    def make_folder(self, path):
        p = self._path(path)
        if reason := protected_path(p):
            raise PluginError(f"I won't create that: {reason}.")
        p.mkdir(parents=True, exist_ok=True)
        if not p.is_dir():
            return {"ok": False, "message": f"I tried to create {p} but it does not exist."}
        return {"message": f"Folder ready: {p}", "data": {"path": str(p)}}

    @command(description="Rename a file or folder (same location).", permissions=("fs.write",),
             params={"path": {"type": "string", "required": True}, "new_name": {"type": "string", "required": True}})
    def rename(self, path, new_name):
        p = self._path(path)
        if not p.exists():
            raise PluginError(f"'{path}' doesn't exist.")
        if reason := protected_path(p):
            raise PluginError(f"I won't rename that: {reason}.")
        if any(c in new_name for c in '\\/:*?"<>|') or new_name in (".", ".."):
            raise PluginError("That name has characters Windows doesn't allow.")
        dest = p.with_name(new_name)
        if dest.exists():
            raise PluginError(f"'{new_name}' already exists there.")
        p.rename(dest)
        return {"message": f"Renamed to {dest.name}" if dest.exists() and not p.exists() else "Rename failed.",
                "ok": dest.exists() and not p.exists(), "data": {"path": str(dest)}}

    @staticmethod
    def _overwrite_needs_confirm(a: dict):
        return f"Overwrite existing file at {a.get('destination')}?" if a.get("overwrite") else None

    @command(description="Move a file or folder. Never overwrites unless overwrite=true (which asks you).",
             permissions=("fs.write",), confirm=_overwrite_needs_confirm.__func__,
             params={"source": {"type": "string", "required": True}, "destination": {"type": "string", "required": True},
                     "overwrite": {"type": "boolean", "default": False}})
    def move(self, source, destination, overwrite=False):
        src, dst = self._path(source), self._path(destination)
        if not src.exists():
            raise PluginError(f"'{source}' doesn't exist.")
        for p in (src, dst):
            if reason := protected_path(p):
                raise PluginError(f"I won't touch that: {reason}.")
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists() and not overwrite:
            raise PluginError(f"{dst} already exists; nothing was moved.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and overwrite:
            dst.unlink() if dst.is_file() else shutil.rmtree(dst)
        shutil.move(str(src), str(dst))
        ok = dst.exists() and not src.exists()
        return {"ok": ok, "message": f"Moved to {dst}" if ok else "The move did not complete.", "data": {"path": str(dst)}}

    @command(description="Copy a file or folder. Never overwrites unless overwrite=true (which asks you).",
             permissions=("fs.write",), confirm=_overwrite_needs_confirm.__func__,
             params={"source": {"type": "string", "required": True}, "destination": {"type": "string", "required": True},
                     "overwrite": {"type": "boolean", "default": False}})
    def copy(self, source, destination, overwrite=False):
        src, dst = self._path(source), self._path(destination)
        if not src.exists():
            raise PluginError(f"'{source}' doesn't exist.")
        if reason := protected_path(dst):
            raise PluginError(f"I won't write there: {reason}.")
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists() and not overwrite:
            raise PluginError(f"{dst} already exists; nothing was copied.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=overwrite)
        else:
            shutil.copy2(src, dst)
        return {"ok": dst.exists(), "message": f"Copied to {dst}" if dst.exists() else "The copy did not complete.", "data": {"path": str(dst)}}

    # ── delete ────────────────────────────────────────────────────────
    _instance = None

    @staticmethod
    def _delete_summary(a: dict) -> str:
        """Validated BEFORE the user is asked: no prompt for something that can't be deleted."""
        inst = Files._instance
        p = inst._path(a["path"]) if inst else Path(a["path"])
        if not p.exists():
            raise PluginError(f"'{a['path']}' doesn't exist.")
        if reason := protected_path(p):
            raise PluginError(f"I won't delete that: {reason}.")
        kind = "folder (and everything in it)" if p.is_dir() else "file"
        return f"Move the {kind} '{p}' to the Recycle Bin?"

    @command(description="Delete a file or folder (goes to the Recycle Bin / trash so it can be restored). Always asks first.",
             permissions=("fs.delete",), destructive=True, params={"path": {"type": "string", "required": True}},
             confirm=lambda a: Files._delete_summary(a))
    def delete(self, path):
        p = self._path(path)
        if not p.exists():
            raise PluginError(f"'{path}' doesn't exist.")
        if reason := protected_path(p):
            raise PluginError(f"I won't delete that: {reason}.")
        if _has("send2trash"):
            from send2trash import send2trash
            send2trash(str(p))
            where = "the Recycle Bin"
        else:
            trash = Path.home() / ".galahad-trash"
            trash.mkdir(exist_ok=True)
            shutil.move(str(p), str(trash / f"{int(time.time())}_{p.name}"))
            where = str(trash)
        gone = not p.exists()
        return {"ok": gone, "message": f"Moved {p.name} to {where}." if gone else f"{p} is still there — delete did not work."}

    # ── organise ──────────────────────────────────────────────────────
    @command(description="Organise a folder's loose files into sub-folders by type or by month. dry_run (default) only "
                         "shows the plan; a real run asks for confirmation and can be undone with undo_organise.",
             permissions=("fs.write",), timeout=60,
             params={"folder": {"type": "string", "required": True}, "by": {"type": "string", "enum": ["type", "month"], "default": "type"},
                     "dry_run": {"type": "boolean", "default": True}},
             confirm=lambda a: None if a.get("dry_run", True) else f"Reorganise files in '{a['folder']}' into sub-folders?")
    def organise(self, folder, by="type", dry_run=True):
        base = self._path(folder)
        if not base.is_dir():
            raise PluginError(f"'{folder}' is not a folder.")
        if reason := protected_path(base):
            raise PluginError(f"I won't organise that: {reason}.")
        plan = []
        for f in sorted(base.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            if by == "month":
                sub = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m")
            else:
                sub = next((g for g, exts in TYPE_GROUPS.items() if f.suffix.lower() in exts), "Other")
            plan.append({"from": str(f), "to": str(base / sub / f.name)})
        if dry_run:
            return {"message": f"Plan: {len(plan)} file(s) would be moved." + (" Example: " + plan[0]["from"] + " → " + plan[0]["to"] if plan else ""),
                    "data": {"plan": plan, "dry_run": True}}
        moved = []
        for step in plan:
            self.ctx.check_cancel()
            dst = Path(step["to"])
            if dst.exists():
                continue
            dst.parent.mkdir(exist_ok=True)
            shutil.move(step["from"], step["to"])
            if dst.exists():
                moved.append(step)
        self.ctx.kv_set("last_organise", moved)
        return {"ok": True, "message": f"Moved {len(moved)} of {len(plan)} file(s). Say 'undo organise' to reverse.", "data": {"moved": moved}}

    @command(description="Reverse the most recent organise.", permissions=("fs.write",))
    def undo_organise(self):
        moved = self.ctx.kv_get("last_organise") or []
        if not moved:
            raise PluginError("There's nothing to undo.")
        n = 0
        for step in reversed(moved):
            dst, src = Path(step["to"]), Path(step["from"])
            if dst.exists() and not src.exists():
                shutil.move(str(dst), str(src))
                n += 1
                try:
                    dst.parent.rmdir()
                except OSError:
                    pass
        self.ctx.kv_set("last_organise", [])
        return f"Restored {n} file(s)."


def _has(mod: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(mod) is not None


PLUGINS = [Files]
