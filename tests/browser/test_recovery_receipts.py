"""Run: uv run --with playwright pytest -q tests/browser/test_recovery_receipts.py."""

from threading import Thread

import pytest
from tests.integration import test_editor_conflicts
from tests.integration.conftest import file_db_engine, migrated_db_url  # noqa: F401
from werkzeug.serving import make_server

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

admin_app = test_editor_conflicts.admin_app
db_engine = test_editor_conflicts.db_engine
editor = test_editor_conflicts.editor


@pytest.mark.parametrize(
    "flow",
    ["preview", "repeated", "rejected", "newer-source", "other-scope", "newer-before-response"],
)
def test_saved_preview_clears_its_browser_recovery(editor, db_session_factory, flow):
    client, root, _, model, item_id = editor
    cookie = client.get_cookie("bragi_sid")
    assert cookie is not None
    server = make_server("127.0.0.1", 0, client.application)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context()
            context.route("https://**", lambda route: route.abort())
            context.add_cookies([{"name": "bragi_sid", "value": cookie.value, "url": origin}])
            page = context.new_page()
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto(origin + root + "/edit")
            form = page.locator("form[data-editor-recovery]")
            form.locator('[name="_recovery_id"]').wait_for(state="attached")
            original_id = form.locator('[name="_recovery_id"]').input_value()
            form.locator('[name="title"]').fill(
                " " if flow == "rejected" else "Previewed then saved"
            )
            page.evaluate("window.dispatchEvent(new Event('pagehide'))")
            assert page.evaluate(
                "id => !!localStorage.getItem('bragi:editor-recovery:' + id)", original_id
            )
            if flow == "newer-before-response":
                other_tab = context.new_page()
                other_tab.goto(origin + root + "/edit")

                def change_source(route):
                    other_tab.evaluate(
                        """id => {
                        const key = 'bragi:editor-recovery:' + id;
                        const record = JSON.parse(localStorage.getItem(key));
                        record.savedAt += 1000;
                        localStorage.setItem(key, JSON.stringify(record));
                    }""",
                        original_id,
                    )
                    route.continue_()

                page.route(origin + root + "/edit", change_source, times=1)
            with page.expect_navigation():
                if flow == "rejected":
                    form.locator('.actions button[type="submit"]').first.click()
                else:
                    page.get_by_role("button", name="Update previews", exact=True).click()
            form.locator('[name="_recovery_id"]').wait_for(state="attached")
            assert form.locator('[name="_recovery_id"]').input_value() != original_id
            if flow == "rejected":
                assert page.locator("[data-editor-saved]").count() == 0
                form.locator('[name="title"]').fill("Previewed then saved")
            assert form.locator('[name="title"]').input_value() == "Previewed then saved"
            if flow == "repeated":
                with page.expect_navigation():
                    page.get_by_role("button", name="Update previews", exact=True).click()
                form.locator('[name="_recovery_id"]').wait_for(state="attached")
            if flow in {"newer-source", "other-scope"}:
                page.evaluate(
                    """([id, flow]) => {
                    const key = 'bragi:editor-recovery:' + id;
                    const record = JSON.parse(localStorage.getItem(key));
                    if (flow === 'newer-source') record.savedAt += 1000;
                    else record.scope = JSON.stringify(['other', 'site', 'post:99', 'live']);
                    localStorage.setItem(key, JSON.stringify(record));
                }""",
                    [original_id, flow],
                )
            assert page.evaluate(
                "id => !!localStorage.getItem('bragi:editor-recovery:' + id)", original_id
            )
            with page.expect_navigation():
                form.locator('.actions button[type="submit"]').first.click()
            with db_session_factory() as db:
                assert db.get(model, item_id).title == "Previewed then saved"
            leftovers = page.evaluate(
                "Object.keys(localStorage).filter(k => k.startsWith('bragi:editor-recovery:'))"
                ".map(k => JSON.parse(localStorage[k]))"
            )
            if flow in {"newer-source", "other-scope", "newer-before-response"}:
                assert [record["id"] for record in leftovers] == [original_id]
            else:
                assert leftovers == [], leftovers
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
