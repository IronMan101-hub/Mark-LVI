import json
from pathlib import Path

import pytest

from tests.conftest import call


@pytest.fixture()
def eng(full_engine, tmp_path, monkeypatch):
    import galahad.plugins._office as O
    monkeypatch.setattr(O, "default_dir", lambda: tmp_path / "GALAHAD")
    return full_engine


def test_word_create_read_append_replace_with_backups(eng, tmp_path):
    e = eng
    p = tmp_path / "docs" / "report.docx"
    r = call(e, "word", "create", path=str(p), title="Quarterly Report", text="First paragraph about revenue growth.\n\nSecond paragraph about margins.")
    assert r.ok and p.exists()
    assert "revenue growth" in call(e, "word", "read", path=str(p)).message
    # creating again over an existing file requires confirmation and keeps the original
    again = call(e, "word", "create", path=str(p), title="New", text="Different content here for overwrite.")
    assert again.status == "awaiting_confirmation" and "revenue growth" in call(e, "word", "read", path=str(p)).message
    res = e.registry.approve(again.pending_id)
    assert res.ok and res.data["backup"] and Path(res.data["backup"]).exists()
    assert "Different content" in call(e, "word", "read", path=str(p)).message
    assert call(e, "word", "append", path=str(p), text="Appended closing remarks for the board.").ok
    assert "closing remarks" in call(e, "word", "read", path=str(p)).message
    assert call(e, "word", "insert_after", path=str(p), anchor="Different content", text="Inserted line.").ok
    # replace: dry-run first, real run confirmed
    dry = call(e, "word", "replace_text", path=str(p), find="board", replace="committee")
    assert dry.ok and dry.data["count"] == 1 and "board" in call(e, "word", "read", path=str(p)).message
    real = call(e, "word", "replace_text", path=str(p), find="board", replace="committee", dry_run=False)
    assert real.status == "awaiting_confirmation"
    assert e.registry.approve(real.pending_id).ok
    assert "committee" in call(e, "word", "read", path=str(p)).message
    s = call(e, "word", "search", path=str(p), query="inserted")
    assert s.data["count"] == 1
    assert call(e, "word", "summarise", path=str(p)).data["method"] == "local extract"


def test_word_rejects_bad_inputs(eng, tmp_path):
    e = eng
    assert not call(e, "word", "read", path=str(tmp_path / "missing.docx")).ok
    bad = tmp_path / "fake.docx"; bad.write_text("not a zip")
    r = call(e, "word", "read", path=str(bad))
    assert not r.ok and "can't open" in r.message
    assert not call(e, "word", "create", path="/etc/evil.docx", title="x").ok or True
    r = call(e, "word", "create", path="/etc/evil.docx", title="x")
    assert not r.ok and "system location" in r.message
    assert not call(e, "word", "read", path=str(tmp_path / "x.txt")).ok


def test_excel_write_validate_calculate_format_chart(eng, tmp_path):
    e = eng
    p = tmp_path / "budget.xlsx"
    assert call(e, "excel", "create", path=str(p), sheet="Q1", rows=[["Item", "Cost"], ["Rent", 1200], ["Food", 450.5], ["Fun", 100]]).ok
    r = call(e, "excel", "read_sheet", path=str(p), sheet="Q1")
    assert r.data["rows"][1] == ["Rent", 1200]
    assert call(e, "excel", "calculate", path=str(p), sheet="Q1", range="B2:B4", op="sum").data["value"] == 1750.5
    assert call(e, "excel", "get_cell", path=str(p), cell="B3", sheet="Q1").data["value"] == 450.5
    f = call(e, "excel", "add_formula", path=str(p), cell="B5", formula="SUM(B2:B4)", sheet="Q1")
    assert f.ok
    assert call(e, "excel", "get_cell", path=str(p), cell="B5", sheet="Q1").data["raw"] == "=SUM(B2:B4)"
    assert call(e, "excel", "format_cells", path=str(p), range="A1:B1", sheet="Q1", bold=True, fill="FFFF00", number_format="General").ok
    assert call(e, "excel", "create_table", path=str(p), range="A1:B4", name="Costs", sheet="Q1").ok
    assert call(e, "excel", "create_chart", path=str(p), data_range="A1:B4", chart="bar", title="Costs", sheet="Q1").ok
    assert call(e, "excel", "add_sheet", path=str(p), name="Notes").ok
    assert not call(e, "excel", "add_sheet", path=str(p), name="Notes").ok
    assert not call(e, "excel", "add_sheet", path=str(p), name="bad/name").ok
    info = call(e, "excel", "info", path=str(p))
    assert {s["name"] for s in info.data["sheets"]} == {"Q1", "Notes"}


def test_excel_blocks_dangerous_or_invalid_writes(eng, tmp_path):
    e = eng
    p = tmp_path / "x.xlsx"
    call(e, "excel", "create", path=str(p))
    for bad in ("=cmd|' /C calc'!A0", "=HYPERLINK(\"http://evil\")", "=SUM(A1", "='C:\\secret.xlsx'!A1", "=WEBSERVICE(\"http://x\")"):
        assert not call(e, "excel", "write_range", path=str(p), rows=[[bad]]).ok, bad
    assert not call(e, "excel", "write_range", path=str(p), rows=[[1]], start_cell="ZZZZ99999999").ok
    assert not call(e, "excel", "write_range", path=str(p), rows="nope").ok
    assert not call(e, "excel", "calculate", path=str(p), range="A1:A2", op="sum").ok       # no numbers
    assert not call(e, "excel", "read_sheet", path=str(p), range="Z9:A1").ok
    assert not call(e, "excel", "format_cells", path=str(p), range="A1", fill="red").ok
    assert call(e, "excel", "write_range", path=str(p), rows=[[1, 2], [3, 4]], start_cell="C3").ok
    assert Path(p.parent / ".galahad-backup").exists()                                       # edits keep originals


def test_powerpoint_create_edit_images_and_save_as(eng, tmp_path):
    e = eng
    from PIL import Image
    img = tmp_path / "logo.png"; Image.new("RGB", (80, 60), "teal").save(img)
    p = tmp_path / "deck.pptx"
    r = call(e, "powerpoint", "create", path=str(p), title="Strategy 2027", subtitle="Board update",
             slides=[{"title": "Goals", "bullets": ["Grow revenue", "Cut costs"], "notes": "say hello"}, {"title": "Risks", "bullets": ["FX"], "layout": "title_content"}])
    assert r.ok and r.data["slides"] == 3
    rd = call(e, "powerpoint", "read", path=str(p))
    assert [s["title"] for s in rd.data["slides"]] == ["Strategy 2027", "Goals", "Risks"] and rd.data["slides"][1]["notes"] == "say hello"
    assert call(e, "powerpoint", "add_slide", path=str(p), title="Roadmap", bullets=["Q1", "Q2"], layout="two_content").ok
    assert call(e, "powerpoint", "edit_slide_text", path=str(p), slide=2, find="Grow revenue", replace="Grow revenue 20%").ok
    assert not call(e, "powerpoint", "edit_slide_text", path=str(p), slide=2, find="nonexistent", replace="x").ok
    assert not call(e, "powerpoint", "edit_slide_text", path=str(p), slide=99, find="a", replace="b").ok
    assert call(e, "powerpoint", "insert_image", path=str(p), slide=2, image=str(img)).ok
    assert not call(e, "powerpoint", "insert_image", path=str(p), slide=2, image=str(tmp_path / "nope.png")).ok
    dest = tmp_path / "copy.pptx"
    assert call(e, "powerpoint", "save_as", path=str(p), dest=str(dest)).ok
    again = call(e, "powerpoint", "save_as", path=str(p), dest=str(dest))
    assert again.status == "awaiting_confirmation"
    assert "Grow revenue 20%" in json.dumps(call(e, "powerpoint", "read", path=str(dest)).data)


def test_ai_features_need_provider_and_never_fake_output(eng, tmp_path):
    e = eng
    for plugin, cmd, args in (("word", "draft", {"path": str(tmp_path / "d.docx"), "topic": "x"}),
                              ("powerpoint", "generate", {"path": str(tmp_path / "g.pptx"), "topic": "x"})):
        r = call(e, plugin, cmd, **args)
        assert not r.ok and "AI provider" in r.message
    assert not (tmp_path / "d.docx").exists() and not (tmp_path / "g.pptx").exists()


def test_powerpoint_generate_from_ai_outline(eng, tmp_path, monkeypatch):
    from galahad.ai import AIResponse
    e = eng
    outline = {"title": "Solar", "subtitle": "Intro", "slides": [{"title": "Why", "bullets": ["a", "b", "c"], "notes": "n"}, {"title": "How", "bullets": ["d"]}]}
    monkeypatch.setattr(e.ai, "available", lambda: True)
    monkeypatch.setattr(e.ai, "complete", lambda *a, **k: AIResponse(json.dumps(outline), "fake", "m"))
    r = call(e, "powerpoint", "generate", path=str(tmp_path / "solar.pptx"), topic="solar power", slides=3)
    assert r.ok and r.data["slides"] == 3
