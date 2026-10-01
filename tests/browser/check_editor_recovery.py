"""Run: uv run --with playwright python tests/browser/check_editor_recovery.py.

Install the browser once: uv run --with playwright playwright install chromium.
Uses real browser storage and Bragi's editor assets/partials with a tiny HTTP host.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

from bragi.api import ResumeData

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "src/bragi/static/admin"
env = Environment(
    loader=FileSystemLoader(
        [ROOT / "src/bragi/templates", ROOT / "src/bragi/contrib/page/templates"]
    ),
    autoescape=True,
)
env.globals["url_for"] = lambda _endpoint, filename: "/" + filename
RESUME = env.get_template("admin/_resume_fieldset.html").render(resume_data_for_form=ResumeData())
RICH = env.get_template("admin/_tiptap_editor.html").render(
    editor_enable_media=False, editor_enable_internal_links=False
)
HTML = """<!doctype html><div class="admin-content">
<details data-recovery-library data-recovery-user="USER" data-recovery-site="1">
<summary>Browser recovery</summary><div data-recovery-list></div></details>
<form method="post" data-editor-recovery
 data-recovery-user="USER" data-recovery-site="1" data-recovery-entity="post:1"
 data-recovery-context="live" hx-history="false">
<input name="_csrf_token" value="secret"><input name="_edit_token" value="baseline">
<input name="title" value="Original">
<textarea id="body_markdown" name="body_markdown">Body</textarea>
<input name="is_pinned" type="checkbox">EXTRA<button>Save</button></form></div>
<script src="/htmx.min.js"></script><script src="/editor-recovery.js"></script>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = ASSETS / parsed.path.lstrip("/")
        if path.parent == ASSETS and path.is_file():
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/javascript" if path.suffix == ".js" else "text/css"
            )
            self.end_headers()
            self.wfile.write(path.read_bytes())
            return
        query = parse_qs(parsed.query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        extra = RESUME if "resume" in query else RICH if "rich" in query else ""
        if "resume" in query:
            extra = (
                '<select id="kind" name="kind"><option>resume</option><option>static</option>'
                '</select><fieldset id="page-body-fieldset"></fieldset>'
                '<fieldset id="page-profile-notice"></fieldset>'
                '<div id="page-resume-fieldset">' + extra + "</div>"
                '<script src="/page-kind-toggle.js"></script>'
            )
        html = HTML.replace("USER", "2" if "other" in query else "1").replace("EXTRA", extra)
        if "working" in query:
            html = html.replace(
                'data-recovery-context="live"', 'data-recovery-context="working-copy"'
            )
        if "other_site" in query:
            html = html.replace('data-recovery-site="1"', 'data-recovery-site="2"')
        if "dashboard" in query:
            start, end = html.index("<form"), html.index("</form>") + len("</form>")
            html = html[:start] + html[end:]
        self.wfile.write(html.encode())

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(409)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML.replace("USER", "1").replace("EXTRA", "").encode())

    def log_message(self, *_args):
        pass


def records(page):
    return page.evaluate(
        "Object.keys(localStorage).filter(k => k.startsWith('bragi:editor-recovery:'))"
        ".flatMap(k => {try {return [JSON.parse(localStorage[k])]} catch (_) {return []}})"
    )


def flush(page):
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")


def load(page, url):
    page.goto(url)
    page.wait_for_function("!!document.querySelector('[name=_recovery_id]')")


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(ignore_https_errors=True)
        context.on("page", lambda page: page.on("dialog", lambda dialog: dialog.accept()))
        page = context.new_page()
        url = f"http://127.0.0.1:{server.server_port}/"
        # Cached htmx Back bypasses beforeSwap and must still flush the last keystroke.
        history_page = browser.new_page()
        load(history_page, url)
        history_page.evaluate("""() => {
          history.replaceState({htmx:true}, '', '/?previous=1');
          history.pushState({htmx:true}, '', '/');
          localStorage.setItem('htmx-history-cache', JSON.stringify([
            {url:'/?previous=1', content:'<p id="previous">Previous page</p>',
             title:'Previous', scroll:0}
          ]));
          const title = document.querySelector('[name=title]');
          title.value = 'Writing immediately before Back';
          title.dispatchEvent(new Event('input', {bubbles:true}));
          history.back();
        }""")
        history_page.locator("#previous").wait_for()
        assert any(
            "Writing immediately before Back" in str(record) for record in records(history_page)
        )
        # An uncached history response can arrive while the old editor is still editable.
        load(history_page, url)

        def finish_history_request(route):
            history_page.evaluate("""() => {
              const title = document.querySelector('[name=title]');
              title.value = 'Writing just before history response';
              title.dispatchEvent(new Event('input', {bubbles:true}));
            }""")
            route.fulfill(content_type="text/html", body='<p id="previous">Previous page</p>')

        history_page.route("**/?previous=1", finish_history_request)
        history_page.evaluate("""() => {
          localStorage.removeItem('htmx-history-cache');
          history.replaceState({htmx:true}, '', '/?previous=1');
          history.pushState({htmx:true}, '', '/');
          history.back();
        }""")
        history_page.locator("#previous").wait_for()
        assert any(
            "Writing just before history response" in str(record)
            for record in records(history_page)
        )
        history_page.close()
        # Full quota must not hide readable drafts' Restore or Discard controls.
        quota_page = browser.new_page()
        quota_page.on("dialog", lambda dialog: dialog.accept())
        load(quota_page, url)
        quota_page.locator('[name="title"]').fill("Readable draft at quota")
        flush(quota_page)
        quota_page.add_init_script(
            "Storage.prototype.setItem = () => {"
            "throw new DOMException('Full','QuotaExceededError')}"
        )
        quota_page.reload()
        assert len(records(quota_page)) == 1
        quota_page.get_by_role("button", name="Restore", exact=True).click(timeout=3000)
        assert quota_page.locator('[name="title"]').input_value() == "Readable draft at quota"
        quota_page.get_by_role("button", name="Discard", exact=True).click()
        assert records(quota_page) == []
        quota_page.close()
        # Review regressions: a stale discard must not delete a newer record.
        a, b = context.new_page(), context.new_page()
        load(a, url)
        a.locator('[name="title"]').fill("Old snapshot")
        flush(a)
        draft_id = a.locator('[name="_recovery_id"]').input_value()
        load(b, url)
        a.locator('[name="title"]').fill("Newer writing")
        flush(a)
        a.close()
        b.locator(f'[data-recovery-id="{draft_id}"]').get_by_role("button", name="Discard").click()
        assert any("Newer writing" in str(record) for record in records(b))
        assert "changed" in b.get_by_role("status").inner_text()
        # A dashboard must expose orphaned live and working-copy drafts for copying.
        a = context.new_page()
        load(a, url + "?working=1")
        a.locator('[name="body_markdown"]').fill("Orphaned working-copy text")
        flush(a)
        a.close()
        b.goto(url + "?dashboard=1")
        b.get_by_text("Browser recovery", exact=True).click()
        b.wait_for_function(
            "document.querySelectorAll('[data-recovery-library] textarea').length === 2"
        )
        recovered = b.locator("[data-recovery-library] textarea")
        assert recovered.count() == 2
        assert any(
            "Orphaned working-copy text" in text
            for text in recovered.evaluate_all("els => els.map(el => el.value)")
        )
        assert all(recovered.evaluate_all("els => els.map(el => el.readOnly)"))
        for query in ("other=1", "other_site=1"):
            b.goto(url + "?dashboard=1&" + query)
            b.get_by_text("Browser recovery", exact=True).click()
            b.get_by_text(
                "No browser recovery copies for this account and site.", exact=True
            ).wait_for()
            assert b.locator("[data-recovery-library] textarea").count() == 0
        b.evaluate("localStorage.clear()")
        b.close()
        load(page, url)
        page.locator('[name="title"]').fill("Unfinished writing")
        page.locator('[name="is_pinned"]').check()
        flush(page)
        saved = records(page)
        assert len(saved) == 1 and "secret" not in str(saved)
        source_id = saved[0]["id"]
        # Separate tabs must not overwrite each other's records.
        other = context.new_page()
        load(other, url)
        other.locator('[name="title"]').fill("Other tab")
        flush(other)
        assert len(records(page)) == 2
        other_id = other.locator('[name="_recovery_id"]').input_value()
        load(page, url + "?other=1")
        assert page.get_by_role("button", name="Restore", exact=True).count() == 0
        load(page, url)
        page.locator(f'[data-recovery-id="{source_id}"]').get_by_role(
            "button", name="Restore", exact=True
        ).click()
        assert page.locator('[name="title"]').input_value() == "Unfinished writing"
        assert page.locator('[name="is_pinned"]').is_checked()
        assert page.locator('[name="_edit_token"]').input_value() == "baseline"
        # Offline edits use no network, and an HTTP failure does not clear them.
        context.set_offline(True)
        page.locator('[name="body_markdown"]').fill("Offline body")
        flush(page)
        context.set_offline(False)
        assert any("Offline body" in str(record) for record in records(page))
        submitted_id = page.locator('[name="_recovery_id"]').input_value()
        page.get_by_role("button", name="Save", exact=True).click()
        page.wait_for_load_state()
        assert any(record["id"] == submitted_id for record in records(page))
        # Only a receipt clears the exact submission and unchanged restored source.
        page.evaluate(
            """id => {
          const receipt = document.createElement('span'); receipt.dataset.editorSaved = id;
          document.body.append(receipt); document.dispatchEvent(new Event('htmx:afterSwap'));
        }""",
            submitted_id,
        )
        remaining = {record["id"] for record in records(page)}
        assert submitted_id not in remaining and source_id not in remaining
        assert other_id in remaining
        # Undoing a change must not resurrect an intermediate draft after reload.
        page.locator('[name="title"]').fill("Temporary")
        flush(page)
        own_id = page.locator('[name="_recovery_id"]').input_value()
        page.locator('[name="title"]').fill("Original")
        flush(page)
        assert own_id not in {record["id"] for record in records(page)}
        # Retention removes expired records; corrupt records do not break editing.
        page.evaluate("""() => {
          localStorage.setItem('bragi:editor-recovery:expired', JSON.stringify({savedAt: 1}));
          localStorage.setItem('bragi:editor-recovery:broken', '{');
        }""")
        load(page, url)
        assert page.evaluate("localStorage.getItem('bragi:editor-recovery:expired')") is None
        # htmx navigation has a leave guard; swapping a new form wires recovery again.
        page.locator('[name="title"]').fill("Before navigation")
        flush(page)
        page.evaluate("window.confirm = () => false")
        assert (
            page.evaluate("""() => document.dispatchEvent(new CustomEvent('htmx:beforeRequest', {
          cancelable:true, detail:{target:document.querySelector('.admin-content')}
        }))""")
            is False
        )
        page.evaluate("window.confirm = () => true")
        # Typing immediately before a swap must be persisted without the debounce.
        page.locator('[name="title"]').fill("Last instant edit")
        page.evaluate("document.dispatchEvent(new Event('htmx:beforeSwap'))")
        own_id = page.locator('[name="_recovery_id"]').input_value()
        assert any(
            record["id"] == own_id and "Last instant edit" in str(record)
            for record in records(page)
            if record
        )
        # Changes during a pending POST survive its successful receipt, including undo.
        page.locator("form").dispatch_event("submit")
        sent_id = page.locator('[name="_recovery_id"]').input_value()
        page.locator('[name="title"]').fill("Original")
        flush(page)
        unsent_id = page.locator('[name="_recovery_id"]').input_value()
        assert sent_id != unsent_id
        page.evaluate(
            """id => {
          const receipt = document.createElement('span'); receipt.dataset.editorSaved = id;
          document.body.append(receipt); document.dispatchEvent(new Event('htmx:afterSwap'));
        }""",
            sent_id,
        )
        assert any(record["id"] == unsent_id for record in records(page) if record)
        # Storage blocked/quota failures remain visible while editing works.
        blocked = browser.new_context()
        blocked.add_init_script(
            "Storage.prototype.setItem = () => { throw new DOMException('Blocked'); }"
        )
        blocked_page = blocked.new_page()
        load(blocked_page, url)
        assert "unavailable" in blocked_page.get_by_role("status").inner_text()
        blocked_page.locator('[name="title"]').fill("Still editable")
        blocked.close()
        # Real resume partial, row handlers, linked positions and serializer.
        page.evaluate("localStorage.clear()")
        load(page, url + "?resume=1")
        page.wait_for_function(
            "document.querySelector('#resume-fieldset').dataset.resumeWired === '1'", timeout=90000
        )
        page.locator("details").evaluate_all("els => els.forEach(el => el.open = true)")
        group = page.locator('[data-field-name="experience"]')
        group.locator(".repeating-field__add").click()
        group.locator('[data-field="company"]').fill("Recovered company")
        group.locator('[data-field="role"]').fill("Writer")
        row_id = group.locator(".repeating-field__row").get_attribute("data-id")
        resume_id = page.locator('[name="_recovery_id"]').input_value()
        flush(page)
        load(page, url + "?resume=1")
        page.wait_for_function(
            "document.querySelector('#resume-fieldset').dataset.resumeWired === '1'", timeout=90000
        )
        page.locator(f'[data-recovery-id="{resume_id}"]').get_by_role(
            "button", name="Restore", exact=True
        ).click()
        assert group.locator('[data-field="company"]').input_value() == "Recovered company"
        assert group.locator(".repeating-field__row").get_attribute("data-id") == row_id
        page.locator("form").dispatch_event("submit")
        assert "Recovered company" in page.locator('[name="resume_data"]').input_value()
        page.get_by_text("Browser recovery", exact=True).click()
        page.wait_for_function(
            "Array.from(document.querySelectorAll('[data-recovery-library] textarea'))"
            ".some(el => el.value.includes('Recovered company'))"
        )
        # Inactive resume fields must not block native form validation.
        page.locator("details").evaluate_all("els => els.forEach(el => el.open = true)")
        group.locator(".repeating-field__add").click()
        assert page.locator("form").evaluate("form => form.checkValidity()") is False
        page.locator('[name="kind"]').select_option("static")
        assert page.locator("form").evaluate("form => form.checkValidity()") is True
        # Real TipTap module: visible rich text and canonical textarea both restore.
        page.evaluate("localStorage.clear()")
        load(page, url + "?rich=1")
        rich = page.locator(".tiptap[contenteditable=true]")
        rich.wait_for(timeout=90000)
        rich.fill("Rich-text recovery")
        rich_id = page.locator('[name="_recovery_id"]').input_value()
        flush(page)
        load(page, url + "?rich=1")
        rich.wait_for(timeout=90000)
        page.locator(f'[data-recovery-id="{rich_id}"]').get_by_role(
            "button", name="Restore", exact=True
        ).click()
        assert rich.inner_text() == "Rich-text recovery"
        assert page.locator('[name="body_markdown"]').input_value() == "Rich-text recovery"
        # Cached ESM modules must also initialize editors after boosted navigation.
        page.evaluate(
            """url => htmx.ajax('GET', url, {
          target:'.admin-content', select:'.admin-content', swap:'outerHTML'
        })""",
            url + "?rich=1",
        )
        rich.wait_for(timeout=90000)
        rich.fill("After boosted navigation")
        flush(page)
        assert any("After boosted navigation" in str(record) for record in records(page))
        browser.close()
finally:
    server.shutdown()
print("Browser recovery checks passed")
