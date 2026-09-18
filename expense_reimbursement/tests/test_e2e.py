"""End-to-end test: drives the real UI in a real browser against a real
uvicorn server (PIPELINE_MODE=fake, expense_test DB) and clicks every
button on every screen, asserting each change is visible immediately
and still there after a reload. Screenshots of each screen at desktop
(1280px) and mobile (390px) widths are saved to test_screenshots/.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
SCREENSHOTS_DIR = BASE_DIR / "test_screenshots"
UPLOADS_DIR = BASE_DIR / "uploads"
PORT = 8799
BASE_URL = f"http://127.0.0.1:{PORT}"

FORBIDDEN_TEXT = ["confidence", "additional_fields", "extraction_notes", "telecom_bill", "restaurant_bill",
                  "local_conveyance_form", "approval_correspondence", "generic_receipt"]


@pytest.fixture(scope="module")
def live_server():
    env = os.environ.copy()
    from dotenv import dotenv_values

    file_env = dotenv_values(BASE_DIR / ".env")
    env["DATABASE_URL"] = file_env["TEST_DATABASE_URL"]
    env["PIPELINE_MODE"] = "fake"

    # stdout/stderr go to a file, not a PIPE: an unread PIPE fills its OS
    # buffer (~64KB) once uvicorn's access-log output accumulates across
    # the many requests this test makes, and the server then blocks on
    # write() -- everything hangs mid-request with no error, which is
    # exactly what happened here before this fix.
    log_path = BASE_DIR / "test_screenshots" / "_live_server.log"
    log_path.parent.mkdir(exist_ok=True)
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(BASE_DIR),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 20
        ready = False
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{BASE_URL}/api/claims", timeout=1)
                if r.status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        if not ready:
            proc.terminate()
            log_file.close()
            out = log_path.read_text(errors="replace")
            raise RuntimeError(f"live server did not start in time.\n{out}")

        # clean slate: truncate expense_test directly through the app's own db module
        import importlib

        os.environ["DATABASE_URL"] = env["DATABASE_URL"]
        sys.path.insert(0, str(BASE_DIR))
        db = importlib.import_module("db")
        from sqlalchemy import text

        with db.get_engine().begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE audit_events, corrections, check_results, extractions, "
                    "documents, claims, employees RESTART IDENTITY CASCADE"
                )
            )
        yield BASE_URL
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()


def assert_no_horizontal_overflow(page):
    scroll_w = page.evaluate("document.documentElement.scrollWidth")
    client_w = page.evaluate("document.documentElement.clientWidth")
    assert scroll_w <= client_w + 1, f"page overflows horizontally: scrollWidth={scroll_w} clientWidth={client_w}"


def assert_no_forbidden_text(page):
    body_text = page.inner_text("body")
    for forbidden in FORBIDDEN_TEXT:
        assert forbidden not in body_text, f"forbidden text visible on page: {forbidden!r}"


def shot(page, name):
    page.screenshot(path=str(SCREENSHOTS_DIR / name), full_page=True)


def test_full_ui_flow_every_button(live_server):
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    console_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        # ---- My claims (empty) ----
        page.goto(f"{live_server}/#/")
        page.wait_for_selector("text=My claims")
        assert_no_horizontal_overflow(page)
        shot(page, "01_my_claims_empty_desktop.png")

        # New claim button
        page.click("#new-claim")
        page.wait_for_url("**/#/claims/*")
        claim_url = page.url
        page.wait_for_selector("#dropzone")
        shot(page, "02_claim_page_empty_desktop.png")

        # reload persistence
        page.reload()
        page.wait_for_selector("#dropzone")
        assert page.url == claim_url

        # Upload a document (dropzone's hidden file input)
        page.set_input_files("#file-input", str(UPLOADS_DIR / "may26_mobile.pdf"))
        page.wait_for_selector(".doc-row")
        page.wait_for_function(
            "() => !document.querySelector('.chip-processing')", timeout=15000
        )
        shot(page, "03_claim_page_one_doc_desktop.png")
        assert_no_forbidden_text(page)

        # ---- Document review: mobile bill ----
        page.click(".doc-row")
        page.wait_for_selector("#fields-container")
        assert_no_horizontal_overflow(page)
        assert_no_forbidden_text(page)
        assert page.get_by_text("Back to claim").count() == 1, "only one Back to claim control, not one in the action bar too"
        shot(page, "04_doc_review_desktop.png")

        total_input = page.locator('#fields-container input[data-path="total"]')
        assert total_input.input_value() == "1417.18"

        # Typing marks dirty + live-validates
        total_input.fill("1420.00")
        page.wait_for_selector("text=Unsaved changes")
        page.wait_for_timeout(600)  # let the 400ms-debounced /validate round-trip settle

        # Save changes
        page.click("#btn-save")
        page.wait_for_selector("text=Saved")
        doc_url = page.url
        page.reload()
        page.wait_for_selector("#fields-container")
        assert page.locator('#fields-container input[data-path="total"]').input_value() == "1420.00"
        shot(page, "05_doc_review_saved_desktop.png")

        # Undo my changes
        page.click("#btn-undo")
        page.wait_for_function(
            "() => document.querySelector('#fields-container input[data-path=\"total\"]')?.value === '1417.18'"
        )
        page.reload()
        page.wait_for_selector("#fields-container")
        assert page.locator('#fields-container input[data-path="total"]').input_value() == "1417.18"

        # A second, kept edit -- so there's a real correction on the books
        # by the time the claim is submitted (the previous one was undone).
        page.locator('#fields-container input[data-path="vendor_name"]').fill("Jio")
        page.wait_for_selector("text=Unsaved changes")
        page.click("#btn-save")
        page.wait_for_selector("text=Saved")

        # Confirm
        page.click("#btn-confirm")
        page.wait_for_url("**/#/claims/*")
        page.wait_for_selector(".chip-confirmed")
        page.reload()
        page.wait_for_selector(".chip-confirmed")
        shot(page, "06_claim_page_confirmed_desktop.png")

        # ---- Upload a second (throwaway) doc and remove it ----
        page.set_input_files("#file-input", str(UPLOADS_DIR / "May-26 Local conveyance.pdf"))
        page.wait_for_selector(".doc-row >> nth=1")
        page.wait_for_function("() => !document.querySelector('.chip-processing')", timeout=15000)
        doc_rows_before = page.locator(".doc-row").count()
        page.locator(".doc-row", has_text="Local conveyance").click()
        page.wait_for_selector("#btn-remove")
        page.click("#btn-remove")
        page.wait_for_selector("#inline-remove #confirm-remove")  # inline confirm, no browser dialog
        page.click("#confirm-remove")
        page.wait_for_url("**/#/claims/*")
        page.wait_for_timeout(300)
        assert page.locator(".doc-row").count() == doc_rows_before - 1
        page.reload()
        page.wait_for_selector(".doc-row")
        assert page.locator(".doc-row").count() == doc_rows_before - 1

        # ---- Upload the approval correspondence (read-only, no Confirm) ----
        page.set_input_files("#file-input", str(UPLOADS_DIR / "May-26 mail approval.pdf"))
        page.wait_for_function("() => !document.querySelector('.chip-processing')", timeout=15000)
        page.locator(".doc-row", has_text="mail approval").click()
        page.wait_for_selector("#fields-container")
        assert page.locator("#btn-confirm").count() == 0  # no Confirm button for a read-only doc
        assert page.locator(".read-only-note").count() == 1
        shot(page, "08_doc_review_readonly_desktop.png")
        page.click(".back-link")  # the action bar's own Back button was removed (duplicate)
        page.wait_for_url("**/#/claims/*")

        # ---- Note to approver ----
        page.fill("#note-field", "Daily allowance line explained separately.")
        page.wait_for_timeout(600)
        page.reload()
        page.wait_for_selector("#note-field")
        assert page.input_value("#note-field") == "Daily allowance line explained separately."

        # ---- Submit claim ----
        submit_btn = page.locator("#submit-claim")
        submit_btn.wait_for(state="visible")
        assert not submit_btn.is_disabled(), "submit should be enabled once every confirmable doc is confirmed"
        submit_btn.click()
        page.wait_for_selector("text=Submitted on")
        page.wait_for_selector("text=Values you corrected")
        page.reload()
        page.wait_for_selector("text=Submitted on")
        assert_no_horizontal_overflow(page)
        shot(page, "07_claim_page_submitted_desktop.png")

        # a submitted claim has no dropzone / editable note
        assert page.locator("#dropzone").count() == 0
        assert page.get_attribute("#note-field", "readonly") is not None

        browser.close()

    assert console_errors == [], f"browser console errors during the flow: {console_errors}"


def test_mobile_screenshots(live_server):
    """A second pass at 390px: fresh claim through upload + review, so
    the responsive layout gets checked on every screen independently of
    the desktop flow's state."""
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})

        page.goto(f"{live_server}/#/")
        page.wait_for_selector("text=My claims")
        assert_no_horizontal_overflow(page)
        shot(page, "m01_my_claims_mobile.png")

        page.click("#new-claim")
        page.wait_for_url("**/#/claims/*")
        page.wait_for_selector("#dropzone")
        assert_no_horizontal_overflow(page)
        shot(page, "m02_claim_page_empty_mobile.png")

        page.set_input_files("#file-input", str(UPLOADS_DIR / "May-26 Local conveyance.pdf"))
        page.wait_for_function("() => !document.querySelector('.chip-processing')", timeout=15000)
        assert_no_horizontal_overflow(page)
        shot(page, "m03_claim_page_one_doc_mobile.png")

        page.click(".doc-row")
        page.wait_for_selector("#fields-container")
        assert_no_horizontal_overflow(page)
        assert_no_forbidden_text(page)
        shot(page, "m04_doc_review_mobile.png")

        # trips table scrolls inside its own container, not the page
        table_scroll = page.locator(".table-scroll").first
        overflow_x = table_scroll.evaluate("el => getComputedStyle(el).overflowX")
        assert overflow_x == "auto"

        browser.close()


def test_confirmed_document_hides_warnings_and_reopens(live_server):
    """Bug: a confirmed document's warnings came back on reopening it,
    because they were recomputed from the AI's confidence/checks on
    every load and ignored document status. Once confirmed, the
    employee view must show no AI warnings and the document is
    read-only until "Edit again" is clicked."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        page.goto(f"{live_server}/#/")
        page.wait_for_selector("text=My claims")
        page.click("#new-claim")
        page.wait_for_url("**/#/claims/*")
        claim_url = page.url
        page.wait_for_selector("#dropzone")

        page.set_input_files("#file-input", str(UPLOADS_DIR / "May-26 Local conveyance.pdf"))
        page.wait_for_selector(".doc-row")
        page.wait_for_function("() => !document.querySelector('.chip-processing')", timeout=15000)

        page.click(".doc-row")
        page.wait_for_selector("#fields-container")
        # sanity check: the completeness warning is there before confirming
        assert page.locator(".warning-line", has_text="5886").count() == 1

        page.click("#btn-confirm")
        page.wait_for_url("**/#/claims/*")
        page.wait_for_selector(".chip-confirmed")

        # navigate away, then open the document again
        page.goto(claim_url)
        page.wait_for_selector(".doc-row")
        page.click(".doc-row")
        page.wait_for_selector("#fields-container")

        assert page.locator(".warning-line").count() == 0, "a confirmed document must show no AI warnings"
        assert page.locator("#fields-container input").count() == 0, "a confirmed document's fields must not be inputs"
        # values must still be shown, just read-only -- checked generically
        # (not a specific hardcoded number) since the real extraction isn't
        # perfectly deterministic run to run
        total_km_row = page.locator(".field-row", has=page.locator("label", has_text="Total km"))
        assert total_km_row.locator(".field-value").inner_text().strip() != ""
        assert page.locator(".chip-confirmed").count() >= 1
        assert page.locator("#btn-edit-again").count() == 1
        assert page.locator("#btn-save").count() == 0
        assert page.locator("#btn-remove").count() == 0

        # reload: the same assertions must hold, not just in-memory state
        page.reload()
        page.wait_for_selector("#fields-container")
        assert page.locator(".warning-line").count() == 0
        assert page.locator("#fields-container input").count() == 0
        assert page.locator("#btn-edit-again").count() == 1

        # Edit again -> back to edit mode, status needs_review
        page.click("#btn-edit-again")
        page.wait_for_selector("#fields-container input")
        assert page.locator("#fields-container input").count() > 0, "fields must be editable again"
        assert page.locator(".warning-line", has_text="5886").count() == 1, "the warning must reappear once reopened"
        assert page.locator(".chip-confirmed").count() == 0
        assert page.locator("#btn-edit-again").count() == 0

        # status is genuinely needs_review server-side, not just the UI's guess
        doc_id = page.url.rsplit("/", 1)[-1]
        api_status = httpx.get(f"{live_server}/api/documents/{doc_id}").json()["status"]
        assert api_status == "needs_review"

        browser.close()


def test_currency_display_and_dropdown(live_server):
    """Bug 3: currency silently defaulted to INR and was never shown to
    the employee. An ambiguous currency (None) must render as an
    editable dropdown with the "couldn't tell" warning; a known,
    non-INR currency must show formatted with its symbol and code
    (e.g. "$780.75 USD"), never silently as rupees."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.goto(f"{live_server}/#/")
        page.wait_for_selector("text=My claims")

        ambiguous_doc = {
            "id": "fake-1", "claim_id": "fake-claim", "claim_status": "draft",
            "original_name": "hotel.png", "status": "needs_review", "mime_type": "image/png",
            "corrections": [],
            "review": {
                "document_type": "generic_receipt",
                "currency": None,
                "fields": [
                    {"key": "vendor_name", "label": "Vendor", "value": "Grand Plaza Hotel", "editable": True},
                    {"key": "date", "label": "Date", "value": "2026-03-18", "editable": True},
                    {"key": "amount", "label": "Amount", "value": "780.75", "editable": True},
                    {
                        "key": "currency", "label": "Currency", "value": None, "editable": True,
                        "type": "select", "options": ["INR", "USD", "EUR", "GBP"],
                    },
                ],
                "collapsible": None,
                "warnings": ["Couldn't tell which currency this bill is in."],
                "editable_fields": ["vendor_name", "date", "amount", "currency"],
                "needs_confirm": True,
                "needs_review": True,
            },
        }
        page.evaluate(
            """(doc) => {
                document.getElementById('app').innerHTML = '<div id="doc-body">Loading</div>';
                docPageState = { doc, edits: {}, dirty: false, saving: false };
                drawDocumentPage();
            }""",
            ambiguous_doc,
        )
        page.wait_for_selector("#fields-container")

        assert page.locator("text=Couldn't tell which currency this bill is in.").count() == 1
        assert page.locator('select[data-path="currency"]').count() == 1, "currency must be an editable dropdown when ambiguous"

        # a known, non-INR currency: shown formatted, no dropdown (confirmed/locked here too)
        known_doc = {
            **ambiguous_doc,
            "status": "confirmed",
            "review": {
                **ambiguous_doc["review"],
                "currency": "USD",
                "fields": [f for f in ambiguous_doc["review"]["fields"] if f["key"] != "currency"],
                "editable_fields": [],
                "warnings": [],
                "needs_review": False,
            },
        }
        page.evaluate(
            """(doc) => { docPageState = { doc, edits: {}, dirty: false, saving: false }; drawDocumentPage(); }""",
            known_doc,
        )
        page.wait_for_selector("#fields-container")
        assert page.locator('select[data-path="currency"]').count() == 0
        amount_value = page.locator(".field-value", has_text="780.75").inner_text()
        assert amount_value.strip() == "$780.75 USD", amount_value

        browser.close()


def _generic_receipt_doc(line_items, needs_review=False, warnings=None):
    return {
        "id": "fake-2", "claim_id": "fake-claim", "claim_status": "draft",
        "original_name": "receipt.png", "status": "needs_review" if needs_review else "ready",
        "mime_type": "image/png", "corrections": [],
        "review": {
            "document_type": "generic_receipt",
            "currency": "INR",
            "fields": [
                {"key": "vendor_name", "label": "Vendor", "value": "Corner Store", "editable": True},
                {"key": "date", "label": "Date", "value": "2026-03-01", "editable": True},
                {"key": "amount", "label": "Amount", "value": "30.00", "editable": True},
            ],
            "collapsible": {
                "key": "items", "label": f"Show items ({len(line_items)})",
                "auto_expand": needs_review, "line_items": line_items, "extra_fields": [], "editable": True,
            },
            "warnings": warnings or [],
            "editable_fields": ["vendor_name", "date", "amount", "line_items"],
            "needs_confirm": True,
            "needs_review": needs_review,
        },
    }


def test_empty_items_shows_add_item_not_an_empty_table(live_server):
    """Bug 5: 0 line items rendered an empty <table> with just headers.
    Must show a plain message and an Add item button instead, and the
    button must actually add an editable row."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.goto(f"{live_server}/#/")
        page.wait_for_selector("text=My claims")

        doc = _generic_receipt_doc([])
        page.evaluate(
            """(doc) => {
                document.getElementById('app').innerHTML = '<div id="doc-body">Loading</div>';
                docPageState = { doc, edits: {}, dirty: false, saving: false };
                drawDocumentPage();
            }""",
            doc,
        )
        page.wait_for_selector("#fields-container")

        # collapsed by default (0 items, no failing check)
        assert page.locator("details.collapsible").get_attribute("open") is None
        page.click("summary")
        assert page.locator("#items-table").count() == 0, "must not render an empty table"
        assert page.locator("text=No items found on this document.").count() == 1
        add_btn = page.locator("#btn-add-item")
        assert add_btn.count() == 1

        add_btn.click()
        page.wait_for_selector("#items-table")
        assert page.locator("details.collapsible").get_attribute("open") is not None, "adding an item must not re-collapse the section"
        assert page.locator("#items-table input").count() == 4, "one new editable row (4 columns)"

        browser.close()


def test_items_table_fits_desktop_width_without_horizontal_scroll(live_server):
    """Bug 5: all 4 columns (Item, Qty, Unit price, Amount) must fit at
    desktop width without a horizontal scrollbar; scrolling is only
    acceptable at 390px."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.goto(f"{live_server}/#/")
        page.wait_for_selector("text=My claims")

        doc = _generic_receipt_doc(
            [
                {"name": "Widget", "quantity": "2", "unit_price": "10.00", "total": "20.00"},
                {"name": "Gadget", "quantity": "1", "unit_price": "10.00", "total": "10.00"},
            ],
            needs_review=True,
            warnings=[],
        )
        doc["review"]["collapsible"]["auto_expand"] = True
        page.evaluate(
            """(doc) => {
                document.getElementById('app').innerHTML = '<div id="doc-body">Loading</div>';
                docPageState = { doc, edits: {}, dirty: false, saving: false };
                drawDocumentPage();
            }""",
            doc,
        )
        page.wait_for_selector("#items-table")
        assert_no_horizontal_overflow(page)

        table_scroll = page.locator("#items-table").locator("xpath=ancestor::div[contains(@class,'table-scroll')]")
        table_box = page.locator("#items-table").bounding_box()
        scroll_box = table_scroll.bounding_box()
        assert table_box["width"] <= scroll_box["width"] + 1, "items table itself must not need to scroll at desktop width"

        browser.close()
