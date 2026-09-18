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
        page.click("#btn-back")
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
