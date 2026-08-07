#!/usr/bin/env python3
"""
OpenGov -> Twidget bid feeder

Logs into your OpenGov Procurement vendor account, pulls the list of open
bids from your vendor's "Open Bids" page, and POSTs each NEW bid (one
that hasn't been sent before) as a JSON payload to the /opengov-intake
endpoint you built in Twidget. Twidget then handles the NAICS/keyword
filtering, dedup check, and Trello card creation.

Designed to run on a schedule via GitHub Actions -- same pattern as the
DIBBS script in this repo, not something you run manually or something
Claude runs for you.

--------------------------------------------------------------------
SETUP
--------------------------------------------------------------------
1. Install deps (the GitHub Actions workflow file handles this for you):
     pip install playwright requests
     playwright install chromium

2. Your Twidget endpoint (/opengov-intake) already exists and expects a
   JSON body shaped like:
     {
       "bidId": "12345",
       "title": "Purchase of Medical Supplies",
       "description": "Solicitation for medical supplies and equipment",
       "naicsCode": "423450",
       "agency": "City of Example",
       "closeDate": "2026-09-01",
       "bidValue": 50000,
       "bidUrl": "https://procurement.opengov.com/vendors/274125/open-bids/..."
     }
   Twidget handles filtering, dedup, and Trello card creation from there
   -- this script's only job is: log in, scrape, POST each bid.

3. Set these as GitHub Actions encrypted secrets (Settings -> Secrets and
   variables -> Actions) -- never commit real values to the repo:
     OPENGOV_EMAIL           - vendor account login email
     OPENGOV_PASSWORD        - vendor account login password
     TWIDGET_OPENGOV_URL     - your /opengov-intake endpoint URL

4. Run once manually (see the GitHub Actions workflow's "workflow_dispatch"
   trigger, or run locally with the env vars set) to confirm it's finding
   bids and Twidget is receiving them, before trusting it to a schedule.

--------------------------------------------------------------------
IMPORTANT -- READ BEFORE RUNNING ON A SCHEDULE
--------------------------------------------------------------------
I (Claude) could not access procurement.opengov.com/vendors/274125/open-bids
directly -- it redirected to a login page every time, which is expected
since it's your authenticated vendor account. That means:

CONFIRMED (from the actual redirect Claude saw when fetching the URL):
  - The login page is a JS-rendered single-page app, not a plain HTML
    form -- confirmed by the fact that a plain HTTP GET returned only a
    login shell, not usable content. This is why Playwright (a real
    headless browser) is used here instead of requests/BeautifulSoup.
  - The login flow is two-step: an "Email Address" field with a
    "Continue" button, THEN (presumably) a password field appears after
    email is submitted -- this is a common pattern (email-first, then
    password) but the password step's exact field name/selector is
    UNCONFIRMED since Claude never saw it.
  - There's a distinct "vendor login" vs "public login" -- you confirmed
    this exists, but Claude has not seen either login form's actual HTML,
    so the selectors below are best-guess placeholders.

STILL UNTESTED / NEEDS YOUR CONFIRMATION (run once manually with
HEADLESS=false so you can watch the browser, or with DEBUG_SCREENSHOTS=true
to capture screenshots at each step -- see bottom of this docstring):
  - The exact selectors for the email field, continue button, password
    field, and final login/submit button.
  - Whether logging in lands you directly on the vendor dashboard or
    requires an extra navigation step to reach /vendors/274125/open-bids.
  - The actual structure of the open-bids listing: is it an HTML table,
    a list of cards/divs, or does it load data via an internal API call
    Playwright could intercept instead of scraping rendered HTML? (If
    it's the latter, this script could likely be simplified a lot --
    worth checking your browser's Network tab while logged in.)
  - The exact field names/labels on each bid listing: does the page show
    NAICS code directly, or only category names that would need mapping
    back to NAICS codes? Does it show a close date, bid value, and a
    direct link/ID for each bid?
  - Whether the open-bids list paginates, infinite-scrolls, or shows
    everything on one page.

HOW TO HELP CONFIRM THESE: run this script locally once with:
    HEADLESS=false DEBUG_SCREENSHOTS=true python scrape_opengov.py
This opens a real (visible) browser window and saves a screenshot after
each major step into ./debug_screenshots/, so we can see exactly where
the selectors below need fixing -- same troubleshooting approach used to
get the Twidget endpoint working.
"""

import os
import sys
import json
import time
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# --------------------------------------------------------------------
# CONFIG -- set these as environment variables (GitHub Actions secrets)
# --------------------------------------------------------------------
OPENGOV_EMAIL = os.environ.get("OPENGOV_EMAIL", "")
OPENGOV_PASSWORD = os.environ.get("OPENGOV_PASSWORD", "")
TWIDGET_OPENGOV_URL = os.environ.get("TWIDGET_OPENGOV_URL", "")

VENDOR_ID = os.environ.get("OPENGOV_VENDOR_ID", "274125")
LOGIN_URL = "https://procurement.opengov.com/login"
OPEN_BIDS_URL = f"https://procurement.opengov.com/vendors/{VENDOR_ID}/open-bids"

# Debugging aids -- turn on locally when fixing selectors, leave off
# (default) in the GitHub Actions scheduled run.
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
DEBUG_SCREENSHOTS = os.environ.get("DEBUG_SCREENSHOTS", "").lower() in ("1", "true", "yes")

SCREENSHOT_DIR = Path(__file__).parent / "debug_screenshots"
STATE_FILE = Path(__file__).parent / "opengov_seen_bids.json"


def snap(page, name):
    """
    Saves a screenshot AND the raw page HTML at a given step, only when
    DEBUG_SCREENSHOTS is on. The HTML dump is what actually lets us find
    real selectors from a headless CI run (a screenshot alone doesn't
    show element attributes/names) -- open the .html file in a browser
    and use dev tools/view-source on it, or just search the raw text for
    things like "email", "password", "naics", etc.
    """
    if not DEBUG_SCREENSHOTS:
        return
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(SCREENSHOT_DIR / f"{name}.png"), full_page=True)
    (SCREENSHOT_DIR / f"{name}.html").write_text(page.content(), encoding="utf-8")


# --------------------------------------------------------------------
# Login
# --------------------------------------------------------------------
def login(page):
    """
    Logs into the OpenGov vendor account. Two-step login assumed
    (email -> continue -> password -> log in) based on what Claude saw
    on the login page shell. SELECTORS BELOW ARE UNCONFIRMED -- if this
    step fails, run with HEADLESS=false to watch it live and fix the
    selectors against the real rendered page.
    """
    page.goto(LOGIN_URL, wait_until="networkidle")
    snap(page, "01_login_page")

    # CONFIRMED from real page HTML (view-source, Aug 2026): the email
    # input has data-qa="login-inputText-email" and id="form-group-email".
    # Note it's type="text", NOT type="email" -- worth noting since that
    # was the original (wrong) guess.
    email_input = page.locator('[data-qa="login-inputText-email"]')
    email_input.wait_for(state="visible", timeout=15000)
    email_input.fill(OPENGOV_EMAIL)

    # The Continue button starts disabled and only enables once the form
    # considers the email field valid. .fill() dispatches an input event,
    # but some React forms only re-validate on blur -- pressing Tab here
    # forces that blur, which is the fix for what looked like the button
    # staying disabled after fill() alone in an earlier debug run.
    page.keyboard.press("Tab")
    snap(page, "02_email_filled")

    continue_button = page.locator('[data-qa="login-button-continue"]')
    # Explicitly wait for the button to lose its disabled state before
    # clicking, rather than clicking immediately -- if validation is
    # asynchronous (e.g. a debounced format check) this wait absorbs
    # that delay instead of racing it.
    try:
        continue_button.wait_for(state="visible", timeout=15000)
        page.wait_for_function(
            """() => {
                const btn = document.querySelector('[data-qa="login-button-continue"]');
                return btn && !btn.disabled;
            }""",
            timeout=10000,
        )
    except PlaywrightTimeoutError:
        snap(page, "02b_continue_still_disabled")
        print("WARNING: Continue button never became enabled after filling "
              "email + blur. Check 02b_continue_still_disabled.html/.png "
              "for what's on the page -- might be a validation error "
              "message we're not detecting, or the field needs a "
              "different fill approach (e.g. typing character-by-character "
              "instead of .fill()).")
        raise

    continue_button.click()
    page.wait_for_load_state("networkidle")
    snap(page, "03_after_continue")

    # UNCONFIRMED AND FLAGGED AS LIKELY WRONG: the real login page's HTML
    # revealed this site runs on Auth0 (there's a hidden iframe pointing
    # to an /authorize endpoint with an auth0Client parameter -- classic
    # Auth0 "silent auth" check). That strongly suggests clicking Continue
    # navigates to an Auth0-hosted Universal Login page for the password
    # step -- possibly on a DIFFERENT DOMAIN than procurement.opengov.com
    # entirely, not just a different page.
    #
    # Because of that domain change:
    #   - The password field's real selector is still unknown (we've
    #     never seen this page -- snap(page, "03_after_continue") above
    #     is the debug capture that will finally show it).
    #   - If Playwright navigated to a new domain, that's expected and
    #     fine -- Playwright follows redirects/navigations within the
    #     same page/tab automatically, no extra code needed for that
    #     part. We just need the real selector once we see the HTML.
    #
    # DO NOT trust the selectors below yet -- they're the same
    # best-guess placeholder as before, kept only so the script doesn't
    # crash immediately at import/definition time. After running this
    # once, check the "opengov-debug-output" artifact's
    # 03_after_continue.html file and replace this block with the real
    # selectors found there.
    password_input = page.locator('input[type="password"], input[name="password"]').first
    password_input.wait_for(state="visible", timeout=15000)
    password_input.fill(OPENGOV_PASSWORD)
    snap(page, "04_password_filled")

    # UNCONFIRMED for the same reason as above.
    page.get_by_role("button", name="Log In").click()
    page.wait_for_load_state("networkidle")
    snap(page, "05_after_login")


# --------------------------------------------------------------------
# Scrape open bids
# --------------------------------------------------------------------
def scrape_open_bids(page):
    """
    Navigates to the vendor's open-bids page and extracts each bid's
    fields. STRUCTURE BELOW IS A BEST-GUESS PLACEHOLDER -- Claude has
    never seen this page's real HTML (it's behind login). Once you can
    log in and view the page yourself, use your browser's dev tools
    (right-click a bid row -> Inspect) to find the real selectors, and
    we'll swap them in here together.

    Returns a list of dicts shaped to match the Twidget endpoint schema:
        bidId, title, description, naicsCode, agency, closeDate,
        bidValue, bidUrl
    """
    page.goto(OPEN_BIDS_URL, wait_until="networkidle")
    snap(page, "06_open_bids_page")

    bids = []

    # UNCONFIRMED: this assumes each bid is a row/card with a common
    # selector like [data-testid="bid-row"] or similar -- placeholder
    # only. Inspect the real page and replace this selector.
    bid_elements = page.locator('[data-testid="bid-row"], .bid-list-item, tr.bid-row').all()

    if not bid_elements:
        print("WARNING: no bid elements found with placeholder selectors. "
              "This almost certainly means the selectors need to be updated "
              "-- run with DEBUG_SCREENSHOTS=true and inspect "
              "06_open_bids_page.png, or open the page yourself and use "
              "dev tools to find the real structure.")

    for el in bid_elements:
        # UNCONFIRMED: every field extraction below is a placeholder.
        # Replace each selector/attribute with what you find in dev tools.
        try:
            bid_id = el.get_attribute("data-bid-id") or ""
            title = el.locator('.bid-title, [data-testid="bid-title"]').first.inner_text().strip()
            agency = el.locator('.bid-agency, [data-testid="bid-agency"]').first.inner_text().strip()
            naics_code = el.locator('.bid-naics, [data-testid="bid-naics"]').first.inner_text().strip()
            close_date = el.locator('.bid-close-date, [data-testid="bid-close-date"]').first.inner_text().strip()
            bid_value_text = el.locator('.bid-value, [data-testid="bid-value"]').first.inner_text().strip()
            bid_url = el.locator("a").first.get_attribute("href") or ""
            if bid_url and not bid_url.startswith("http"):
                bid_url = "https://procurement.opengov.com" + bid_url

            # Bid value likely comes through as text like "$50,000" --
            # strip non-numeric characters before converting.
            bid_value = "".join(c for c in bid_value_text if c.isdigit() or c == ".")
            bid_value = float(bid_value) if bid_value else 0

            # UNCONFIRMED: description may not be shown on the listing
            # page at all -- might require opening each bid's detail
            # page individually, which would need an extra step here.
            description = ""

            bids.append({
                "bidId": bid_id,
                "title": title,
                "description": description,
                "naicsCode": naics_code,
                "agency": agency,
                "closeDate": close_date,
                "bidValue": bid_value,
                "bidUrl": bid_url,
            })
        except Exception as e:
            print(f"WARNING: failed to parse a bid row, skipping: {e}")
            continue

    return bids


# --------------------------------------------------------------------
# State tracking (avoid re-sending bids Twidget already saw --
# Twidget's own dedup table is the real safety net, this is just an
# optimization to skip obviously-already-sent bids before making the
# HTTP call at all)
# --------------------------------------------------------------------
def load_seen():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen(seen_set):
    STATE_FILE.write_text(json.dumps(sorted(seen_set)))


# --------------------------------------------------------------------
# Twidget webhook
# --------------------------------------------------------------------
def send_to_twidget(bid, log_response=False):
    resp = requests.post(
        TWIDGET_OPENGOV_URL,
        headers={"Content-Type": "application/json"},
        json=bid,
        timeout=60,
    )
    resp.raise_for_status()
    if log_response:
        print(f"DEBUG: Twidget response for bid {bid['bidId']}: {resp.text[:500]}")
    return resp


# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------
def process_bids(bids, seen):
    """
    Sends each bid not already in our local 'seen' set to Twidget.
    Twidget's own dedup table (keyed on bidId) is the authoritative
    safety net -- this local check just avoids unnecessary HTTP calls
    for bids we already know we've sent in a prior run.
    """
    sent = 0
    failed = 0
    for bid in bids:
        key = bid["bidId"]
        if not key or key in seen:
            continue
        try:
            send_to_twidget(bid, log_response=(sent == 0))
            seen.add(key)
            sent += 1
        except requests.exceptions.RequestException as e:
            failed += 1
            print(f"WARNING: failed to send bid {key} to Twidget: {e}")
        time.sleep(0.5)  # be polite to the webhook
    if failed:
        print(f"WARNING: {failed} bid(s) failed to send and will be retried next run.")
    return sent


def main():
    missing = [name for name, val in [
        ("OPENGOV_EMAIL", OPENGOV_EMAIL),
        ("OPENGOV_PASSWORD", OPENGOV_PASSWORD),
        ("TWIDGET_OPENGOV_URL", TWIDGET_OPENGOV_URL),
    ] if not val]
    if missing:
        raise SystemExit(f"Missing required environment variable(s): {', '.join(missing)}")

    seen = load_seen()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()

        try:
            login(page)
            bids = scrape_open_bids(page)
        except PlaywrightTimeoutError as e:
            snap(page, "ERROR_timeout")
            print(f"ERROR: timed out waiting for an expected element: {e}")
            browser.close()
            sys.exit(1)
        finally:
            browser.close()

    print(f"Found {len(bids)} open bid(s) on the page.")
    new_count = process_bids(bids, seen)
    save_seen(seen)
    print(f"Done. {new_count} new bid(s) sent to Twidget (out of {len(bids)} found).")


if __name__ == "__main__":
    main()
