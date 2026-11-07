"""Drives the real dashboard in a real (headless Chromium) browser against a real engine."""
import os
import time

import pytest

pw = pytest.importorskip("playwright.sync_api")
CHROME = "/opt/pw-browsers/chromium"
pytestmark = pytest.mark.skipif(not os.path.exists(CHROME), reason="no chromium available")

VIEWS = ["home", "assistant", "system", "media", "productivity", "intelligence", "finance", "documents",
         "plugins", "execution", "notifications", "activity", "settings"]


@pytest.fixture()
def ui(full_engine):
    from galahad.web.server import DashboardServer
    e = full_engine
    srv = DashboardServer(e, port=18777); srv.start()
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        ctx = b.new_context(viewport={"width": 1366, "height": 850})
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda x: errors.append(str(x)))
        page.goto(srv.url(True))
        page.wait_for_selector("#nav .nav-btn")
        yield e, page, errors, srv
        b.close()
    srv.stop()


def go(page, view, wait=0.8):
    page.evaluate(f"location.hash='#/{view}'"); time.sleep(wait)


def test_all_thirteen_sections_render_without_errors(ui):
    e, page, errors, srv = ui
    assert page.locator("#nav .nav-btn").count() == 13
    for v in VIEWS:
        go(page, v, 1.0)
        assert page.locator("#view").inner_text().strip(), f"{v} rendered nothing"
        failed = page.locator("#view .err-state:has(button:has-text('Retry'))")      # the loader's failure state
        assert failed.count() == 0, f"{v} failed to load: {failed.first.inner_text()}"
        assert page.locator("#page-title").inner_text().lower().startswith(v[:4]) or v in ("execution",)
    assert errors == [], errors


def test_create_and_complete_a_task_through_the_ui(ui):
    e, page, errors, _ = ui
    go(page, "productivity")
    page.fill("input[aria-label='Task title']", "Ship the dashboard")
    page.fill("input[aria-label='Due']", "tomorrow 5pm")
    page.click("button:has-text('Add')")
    time.sleep(0.8)
    assert any(t["title"] == "Ship the dashboard" for t in e.db.query("SELECT title FROM tasks"))      # really written to the DB
    assert page.locator("text=Ship the dashboard").count() >= 1
    page.locator("input[aria-label='Complete Ship the dashboard']").first.check()
    time.sleep(0.8)
    assert e.db.one("SELECT status FROM tasks WHERE title='Ship the dashboard'")["status"] == "completed"
    assert errors == []


def test_confirmation_banner_blocks_until_user_approves(ui, tmp_path):
    e, page, errors, _ = ui
    f = tmp_path / "ui-delete.txt"; f.write_text("x")
    e.registry.execute("files", "delete", {"path": str(f)}, origin="voice")
    page.reload(); page.wait_for_selector("#banner .confirm", timeout=8000)
    assert "Recycle Bin" in page.locator("#banner .confirm").inner_text() and f.exists()
    page.click("#banner button:has-text('Deny')"); time.sleep(0.8)
    assert f.exists() and page.locator("#banner .confirm").count() == 0
    e.registry.execute("files", "delete", {"path": str(f)}, origin="voice")
    page.reload(); page.wait_for_selector("#banner .confirm")
    page.click("#banner button:has-text('Approve')"); time.sleep(1.2)
    assert not f.exists()
    assert errors == []


def test_chat_runs_a_real_command(ui):
    e, page, errors, _ = ui
    go(page, "assistant")
    page.fill(".compose input", "add a task to water the plants")
    page.click(".compose button"); time.sleep(1.5)
    assert page.locator(".msg.assistant").last.inner_text()
    assert any("water the plants" in t["title"] for t in e.db.query("SELECT title FROM tasks"))
    assert errors == []


def test_plugin_disable_enable_and_permission_toggle(ui):
    e, page, errors, _ = ui
    go(page, "plugins", 1.2)
    page.click(".card:has-text('Utilities'):has-text('Weather') >> nth=0")
    page.wait_for_selector(".modal")
    page.click(".modal button:has-text('Disable')"); time.sleep(1.0)
    assert e.registry.entries["utilities"].state == "disabled"
    go(page, "plugins", 1.2)
    page.click(".card:has-text('Weather') >> nth=0"); page.wait_for_selector(".modal")
    page.click(".modal button:has-text('Enable')"); time.sleep(1.0)
    assert e.registry.entries["utilities"].state == "active"
    go(page, "plugins", 1.2)
    page.click(".card:has-text('Gmail') >> nth=0"); page.wait_for_selector(".modal")
    page.locator(".modal label:has-text('mail.read') input").check(); time.sleep(0.8)
    assert e.permissions.is_granted("gmail", "mail.read")
    assert errors == []


def test_settings_persist_and_theme_applies(ui):
    e, page, errors, _ = ui
    go(page, "settings", 1.0)
    page.click(".tab:has-text('Appearance')")
    page.select_option("select >> nth=0", "light"); time.sleep(0.8)
    assert e.settings.get("appearance.theme") == "light"
    page.reload(); page.wait_for_selector("#nav .nav-btn"); time.sleep(0.8)
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    assert errors == []


def test_text_from_data_is_never_rendered_as_html(ui):
    e, page, errors, _ = ui
    e.db.execute("INSERT INTO articles(url,title,publisher,category,published,summary,fetched) VALUES(?,?,?,?,?,?,?)",
                 ("https://example.org/x", "<img src=x onerror=window.__pwned=1>Evil headline", "<b>Pub</b>", "science", time.time(), "<script>window.__pwned=1</script>", time.time()))
    go(page, "intelligence", 1.5)
    assert page.evaluate("window.__pwned") is None
    assert page.locator("text=Evil headline").count() >= 1
    assert page.locator("#view img[src='x']").count() == 0
    assert errors == []


def test_no_horizontal_overflow_on_phone_width(ui):
    e, page, errors, srv = ui
    page.set_viewport_size({"width": 390, "height": 800})
    for v in ("home", "plugins", "settings", "execution", "finance"):
        go(page, v, 1.0)
        assert not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"), v
