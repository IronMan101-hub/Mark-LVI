"""Microsoft PowerPoint presentations (.pptx) via python-pptx."""
from __future__ import annotations

import re
from pathlib import Path

from ..ai import AIError, parse_json
from ..plugin_api import Health, Manifest, Plugin, PluginError, command
from ..textutil import clip
from . import _office as O

LAYOUTS = {"title": 0, "title_content": 1, "section": 2, "two_content": 3, "comparison": 4, "title_only": 5, "blank": 6}
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}


class PowerPoint(Plugin):
    manifest = Manifest(
        id="powerpoint", name="Microsoft PowerPoint", category="Office", version="1.0.0",
        description="Create decks, add slides, edit text, insert images, apply layouts, generate structured presentations, save.",
        permissions=["office.documents", "fs.write", "ai.use", "system.control"], requires_python=["pptx"])

    def health(self):
        return Health("ok", "python-pptx available")

    def _prs(self, p: Path):
        from pptx import Presentation
        try:
            return Presentation(str(p))
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"I can't open {p.name} as a presentation ({type(e).__name__}).")

    @staticmethod
    def _fill_slide(prs, spec: dict, layout: str = "title_content"):
        lay = prs.slide_layouts[LAYOUTS.get(spec.get("layout") or layout, 1)]
        s = prs.slides.add_slide(lay)
        if s.shapes.title is not None:
            s.shapes.title.text = str(spec.get("title", ""))[:200]
        bullets = [str(b)[:300] for b in (spec.get("bullets") or [])][:12]
        body = next((ph for ph in s.placeholders if ph.placeholder_format.idx == 1), None)
        if body is not None and bullets:
            tf = body.text_frame
            tf.text = bullets[0]
            for b in bullets[1:]:
                tf.add_paragraph().text = b
        elif body is not None and spec.get("subtitle"):
            body.text_frame.text = str(spec["subtitle"])[:200]
        if spec.get("notes"):
            s.notes_slide.notes_text_frame.text = str(spec["notes"])[:2000]
        img = spec.get("image")
        if img:
            ip = Path(img).expanduser()
            if ip.suffix.lower() not in IMG_EXT or not ip.is_file():
                raise PluginError(f"Image not found or unsupported: {img}")
            from pptx.util import Inches
            s.shapes.add_picture(str(ip), Inches(5.5), Inches(1.8), width=Inches(4))
        return s

    @command(description="Open a presentation in PowerPoint.", permissions=("system.control",), params={"path": {"type": "string", "required": True}})
    def open(self, path):
        return O.open_in_app(O.resolve(path, ".pptx", must_exist=True))

    @command(description="Create a presentation from a title and slides: [{title, bullets[], notes, image, layout}]. Asks before replacing a file.",
             permissions=("office.documents", "fs.write"), confirm=O.overwrite_confirm("path", ".pptx"),
             params={"path": {"type": "string", "required": True}, "title": {"type": "string", "required": True}, "subtitle": {"type": "string", "default": ""},
                     "slides": {"type": "array"}})
    def create(self, path, title, subtitle="", slides=None):
        from pptx import Presentation
        p = O.resolve(path, ".pptx")
        b = O.backup(p) if p.exists() else None
        p.parent.mkdir(parents=True, exist_ok=True)
        prs = Presentation()
        self._fill_slide(prs, {"title": title, "subtitle": subtitle, "layout": "title"})
        for sp in slides or []:
            if not isinstance(sp, dict):
                raise PluginError("Each slide must be an object with a title and bullets.")
            self._fill_slide(prs, sp)
        prs.save(str(p))
        O.verify_saved(p, 5000)
        n = len(self._prs(p).slides)
        return {"ok": n == 1 + len(slides or []), "message": f"Saved {p} with {n} slide(s)." + (f" Original backed up to {b.name}." if b else ""), "data": {"path": str(p), "slides": n}}

    @command(description="Generate a structured presentation on a topic with your AI provider (only the topic is sent).", permissions=("office.documents", "fs.write", "ai.use"),
             timeout=150, confirm=O.overwrite_confirm("path", ".pptx"),
             params={"path": {"type": "string", "required": True}, "topic": {"type": "string", "required": True},
                     "slides": {"type": "integer", "default": 6, "minimum": 3, "maximum": 15}})
    def generate(self, path, topic, slides=6):
        if not self.ctx._e.ai.available():
            raise PluginError("Generating a deck needs an AI provider (Plugins → AI Providers). You can still build one with 'create'.")
        try:
            r = self.ctx.llm(f'Create a {slides}-slide presentation outline on: {topic}. Reply as JSON {{"title": str, "subtitle": str, "slides": '
                             '[{"title": str, "bullets": [3-5 short strings], "notes": str}]}. Facts only, no invented statistics.',
                             purpose="documents", json_mode=True, max_tokens=2500)
        except (AIError, PluginError) as e:
            raise PluginError(str(e))
        d = parse_json(r.text)
        if not isinstance(d, dict) or not d.get("slides"):
            raise PluginError("The AI didn't return a usable outline; nothing was written.")
        return self.create(path, d.get("title", topic), d.get("subtitle", ""), d["slides"][:15])

    @command(description="List slides with their titles and text.", permissions=("office.documents",), readonly=True, params={"path": {"type": "string", "required": True}})
    def read(self, path):
        p = O.resolve(path, ".pptx", must_exist=True)
        out = []
        for i, s in enumerate(self._prs(p).slides, 1):
            texts = [sh.text_frame.text for sh in s.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
            out.append({"slide": i, "title": s.shapes.title.text if s.shapes.title is not None else "", "text": texts,
                        "notes": s.notes_slide.notes_text_frame.text if s.has_notes_slide else ""})
        return {"message": f"{len(out)} slide(s): " + "; ".join(f"{o['slide']}. {o['title'] or '(untitled)'}" for o in out[:12]), "data": {"slides": out}}

    @command(description="Add a slide (layout: title | title_content | section | two_content | comparison | title_only | blank).", permissions=("office.documents", "fs.write"),
             params={"path": {"type": "string", "required": True}, "title": {"type": "string", "required": True}, "bullets": {"type": "array"},
                     "layout": {"type": "string", "enum": list(LAYOUTS), "default": "title_content"}, "notes": {"type": "string"}, "image": {"type": "string"}})
    def add_slide(self, path, title, bullets=None, layout="title_content", notes="", image=""):
        p = O.resolve(path, ".pptx", must_exist=True)
        prs = self._prs(p)
        before = len(prs.slides)
        self._fill_slide(prs, {"title": title, "bullets": bullets, "layout": layout, "notes": notes, "image": image})
        O.backup(p)
        prs.save(str(p))
        n = len(self._prs(p).slides)
        return {"ok": n == before + 1, "message": f"Added slide {n}: {title}.", "data": {"slide": n}}

    @command(description="Replace text on a slide (find → replace).", permissions=("office.documents", "fs.write"),
             params={"path": {"type": "string", "required": True}, "slide": {"type": "integer", "required": True, "minimum": 1},
                     "find": {"type": "string", "required": True}, "replace": {"type": "string", "required": True}})
    def edit_slide_text(self, path, slide, find, replace):
        p = O.resolve(path, ".pptx", must_exist=True)
        prs = self._prs(p)
        if slide > len(prs.slides):
            raise PluginError(f"The deck has only {len(prs.slides)} slides.")
        n = 0
        for sh in prs.slides[slide - 1].shapes:
            if sh.has_text_frame:
                for para in sh.text_frame.paragraphs:
                    for run in para.runs:
                        if find in run.text:
                            run.text = run.text.replace(find, replace); n += 1
        if not n:
            raise PluginError(f"“{find}” isn't on slide {slide} (text split across formatting runs can hide it).")
        O.backup(p)
        prs.save(str(p))
        s2 = " ".join(sh.text_frame.text for sh in self._prs(p).slides[slide - 1].shapes if sh.has_text_frame)
        return {"ok": replace in s2, "message": f"Updated {n} place(s) on slide {slide}."}

    @command(description="Insert an image on a slide.", permissions=("office.documents", "fs.write"),
             params={"path": {"type": "string", "required": True}, "slide": {"type": "integer", "required": True, "minimum": 1}, "image": {"type": "string", "required": True},
                     "width_inches": {"type": "number", "default": 4}})
    def insert_image(self, path, slide, image, width_inches=4):
        from pptx.util import Inches
        p = O.resolve(path, ".pptx", must_exist=True)
        ip = Path(image).expanduser()
        if ip.suffix.lower() not in IMG_EXT or not ip.is_file():
            raise PluginError(f"Image not found or unsupported: {image}")
        prs = self._prs(p)
        if slide > len(prs.slides):
            raise PluginError(f"The deck has only {len(prs.slides)} slides.")
        before = sum(1 for sh in prs.slides[slide - 1].shapes if sh.shape_type == 13)
        prs.slides[slide - 1].shapes.add_picture(str(ip), Inches(1), Inches(2), width=Inches(width_inches))
        O.backup(p)
        prs.save(str(p))
        after = sum(1 for sh in self._prs(p).slides[slide - 1].shapes if sh.shape_type == 13)
        return {"ok": after == before + 1, "message": f"Inserted {ip.name} on slide {slide}."}

    @command(description="Save a copy under a new name. Asks before replacing a file.", permissions=("office.documents", "fs.write"),
             confirm=O.overwrite_confirm("dest", ".pptx"), params={"path": {"type": "string", "required": True}, "dest": {"type": "string", "required": True}})
    def save_as(self, path, dest):
        src, dst = O.resolve(path, ".pptx", must_exist=True), O.resolve(dest, ".pptx")
        b = O.backup(dst) if dst.exists() else None
        dst.parent.mkdir(parents=True, exist_ok=True)
        self._prs(src).save(str(dst))
        O.verify_saved(dst, 5000)
        return {"message": f"Saved as {dst}" + (f" (previous file backed up to {b.name})" if b else ""), "data": {"path": str(dst)}}


PLUGINS = [PowerPoint]
