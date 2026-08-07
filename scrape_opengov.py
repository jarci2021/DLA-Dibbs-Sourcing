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
STATUS -- what's confirmed vs. still worth watching
--------------------------------------------------------------------
CONFIRMED via real login + scrape runs (Aug 2026):
  - Full login flow works end-to-end: email field
    (data-qa="login-inputText-email") -> Tab to blur -> wait for
    Continue button (data-qa="login-button-continue") to enable ->
    click -> password field (data-qa="login-inputText-password") ->
    submit button (data-qa="login-button-submit") -> wait for URL to
    leave /login (more reliable than waiting for network idle, since
    this site runs several always-on trackers that prevent true
    "network idle" from ever triggering).
  - The open-bids listing is a ReactTable: div.rt-tr rows inside
    div.rt-tbody, columns in order Project Title / Organization /
    State / Status / Release Date / Due Date. Title cells contain an
    <a href="#"> -- not a usable link on its own, click triggers
    client-side routing to a real URL.
  - Clicking a bid title navigates to
    /portal/{org-slug}/projects/{numeric-id} -- that numeric ID is
    used as bidId.
  - The resulting detail page embeds a full structured JSON object at
    window.__data.publicProject.project with id, title, rawSummary
    (description), government.organization.name (agency),
    proposalDeadline (close date), and categories (NAICS codes) all in
    one place -- far more reliable than scraping rendered HTML on that
    page.
  - NAICS/category codes are NOT always present -- confirmed a real
    posting (Sacramento Metro Fire's roof replacement RFB) with an
    empty categories array. This is normal, not a scraping bug.
  - No dollar bid-value/estimate field exists anywhere in this data --
    bidValue is always sent as 0.
  - Pagination uses div.pagination-bottom with a "Next" button that
    gets a `disabled` attribute on the last page.

WORTH WATCHING / not yet stress-tested:
  - This has only been run against a handful of real bids so far --
    if a bid's detail page is missing a field this script expects
    (e.g. a different template shape), the per-bid try/except should
    skip it gracefully and log a warning, but hasn't been proven
    against a wide variety of posting templates yet.
  - MAX_PAGES defaults to 5 (see CONFIG below) as a balance between
    catching new postings and run time -- watch for whether real new
    bids ever show up further back than that in practice.


HOW TO HELP CONFIRM THESE: run this script locally once with:
    HEADLESS=false DEBUG_SCREENSHOTS=true python scrape_opengov.py
This opens a real (visible) browser window and saves a screenshot after
each major step into ./debug_screenshots/, so we can see exactly where
the selectors below need fixing -- same troubleshooting approach used to
get the Twidget endpoint working.
"""

import os
import re
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

# How many listing pages to scan per run (each page = 20 rows, per the
# site's default). New postings are far more likely to appear on
# earlier pages than deep in the 72-page total list -- raise this if
# you find real new bids being missed further back.
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))

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
    # Auth0 "silent auth" check). However, confirmed via a real debug run:
    # the password step actually stays on procurement.opengov.com and
    # does NOT redirect to a separate Auth0-hosted page -- that iframe is
    # just a background silent-SSO check, not the actual login flow.
    #
    # CONFIRMED from real page HTML: the password input has
    # data-qa="login-inputText-password" and type="password" -- this
    # matches what was already guessed below, so no change needed there.
    password_input = page.locator('[data-qa="login-inputText-password"]')
    password_input.wait_for(state="visible", timeout=15000)
    password_input.fill(OPENGOV_PASSWORD)
    snap(page, "04_password_filled")

    # CONFIRMED from real page HTML: data-qa="login-button-submit".
    page.locator('[data-qa="login-button-submit"]').click()

    # A prior debug run caught this page mid-request (button still said
    # "Logging In..." and was disabled) even after wait_for_load_state
    # ("networkidle") had already returned -- likely because this page
    # runs several always-on trackers (Segment, Heap, Pendo, FullStory)
    # that keep making periodic background requests, so "network idle"
    # may never truly occur and Playwright moves on too early.
    #
    # Waiting for the URL to actually leave /login is a much more
    # reliable signal that login finished (successfully or not) than
    # waiting for network quiet. Generous 30s timeout since this is a
    # real login round-trip, not a local page transition.
    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=30000)
    except PlaywrightTimeoutError:
        snap(page, "05_still_on_login_after_30s")
        print("WARNING: still on the login page 30s after submitting. "
              "This likely means login failed (wrong password? account "
              "locked? unexpected extra step?) rather than just being "
              "slow -- check 05_still_on_login_after_30s.html for any "
              "visible error message on the page.")
        raise

    page.wait_for_load_state("networkidle")
    snap(page, "05_after_login")


# --------------------------------------------------------------------
# Filtering (cheap pre-check before ever clicking into a bid)
# --------------------------------------------------------------------
# Same 11 keywords used in the Twidget endpoint's title filter -- kept
# here too so we never click into (or even count against page limits)
# a bid whose title clearly won't pass Twidget's filter anyway. This
# does NOT replace Twidget's filter -- Twidget still re-checks NAICS +
# keywords once the full payload is sent -- it's purely a local
# optimization to avoid wasted clicks/page loads.
TITLE_KEYWORDS = [
    "purchase", "inventory", "parts", "supplies", "containers",
    "batteries", "uniform", "medical", "apparel", "jewelry", "equipment",
]


def title_matches_keywords(title):
    lower = title.lower()
    return any(kw in lower for kw in TITLE_KEYWORDS)


# --------------------------------------------------------------------
# Scrape open bids
# --------------------------------------------------------------------
def scrape_open_bids(page, seen_fingerprints, max_pages=5):
    """
    Two-stage scrape of the vendor's open-bids ReactTable listing:

    STAGE 1 (listing page, cheap): for each row, read Project Title,
    Organization, State, Release Date, Due Date directly from the
    table -- CONFIRMED structure from real page HTML:
        div.rt-tr (one per bid) inside div.rt-tbody, cells in order:
        [0] Project Title (a link, but href="#" -- title text only,
            no usable ID/URL at this stage), [1] Organization,
        [2] State, [3] Status, [4] Release Date, [5] Due Date.

    Rows whose title doesn't match TITLE_KEYWORDS are skipped
    immediately -- no click, no detail page load.

    Rows whose title matches AND whose "title|organization"
    fingerprint isn't already in `seen_fingerprints` (bids we've
    already sent to Twidget in a prior run) get clicked into for
    STAGE 2.

    STAGE 2 (detail page, click-through): clicking a matching title
    navigates to /portal/{org-slug}/projects/{id} -- CONFIRMED from a
    real click-through. That page embeds a full structured JSON object
    at window.__data.publicProject.project with everything we need:
    id, title, rawSummary (description), government.organization.name
    (agency), proposalDeadline (close date), categories (NAICS codes,
    when the posting agency filled them in -- CONFIRMED some postings
    have an empty categories array, so this can legitimately be blank).
    No dollar bid-value field exists in this data at all -- OpenGov
    doesn't appear to publish an estimate/budget figure the way some
    other sources do, so bidValue is left at 0 here.

    Paginates up to `max_pages` (ReactTable "Next" button, CONFIRMED
    selector from real HTML) -- new postings are far more likely to
    appear on earlier pages, and scanning all 72 pages every run would
    be slow for little benefit once a source has been running a while.
    Raise max_pages (via MAX_PAGES env var) if real new bids are found
    to be showing up further back than this default catches.

    Returns a list of dicts shaped to match the Twidget endpoint schema:
        bidId, title, description, naicsCode, agency, closeDate,
        bidValue, bidUrl
    """
    # domcontentloaded, not networkidle -- this site's always-on
    # trackers (Segment, Heap, Pendo, FullStory, Faro) never let true
    # network idle occur on authenticated pages, so networkidle waits
    # here would eventually time out (CONFIRMED with the reload() call
    # further down in this function, which hit exactly this problem
    # before being switched to domcontentloaded too).
    page.goto(OPEN_BIDS_URL, wait_until="domcontentloaded")

    # domcontentloaded fires once the raw HTML is parsed, but this is a
    # React app -- the actual table rows only appear after JS runs and
    # fetches data, which happens a beat later. CONFIRMED by a prior
    # run finding 0 rows on the very first page load (later pages
    # "accidentally" worked because the pagination code already waits
    # for content to change before moving on -- this is that same kind
    # of explicit wait, just for the very first load too).
    page.wait_for_selector("div.rt-tbody div.rt-tr", timeout=20000)
    snap(page, "06_open_bids_page")

    bids = []
    new_fingerprints = set()
    # Extra safety net: even if pagination silently fails to advance
    # (which a prior run's log strongly suggested was happening -- the
    # same bid titles kept reappearing across "different" pages), this
    # prevents processing the exact same title more than once within a
    # single run.
    processed_this_run = set()

    for page_num in range(1, max_pages + 1):
        rows = page.locator("div.rt-tbody div.rt-tr").all()
        print(f"DEBUG: page {page_num} -- found {len(rows)} row(s).")

        for row in rows:
            cells = row.locator("div.rt-td")
            try:
                title = cells.nth(0).locator("a").inner_text().strip()
                organization = cells.nth(1).inner_text().strip()
                due_date = cells.nth(5).inner_text().strip()
            except Exception as e:
                print(f"WARNING: failed to read a row's title/org/date, skipping: {e}")
                continue

            if title in processed_this_run:
                continue  # safety net against pagination not actually advancing
            processed_this_run.add(title)

            if not title_matches_keywords(title):
                continue  # cheap skip, no click

            fingerprint = f"{title}|{organization}"
            if fingerprint in seen_fingerprints:
                continue  # already sent to Twidget in a prior run

            # STAGE 2: click into the matching, not-yet-seen bid.
            #
            # IMPORTANT: this does a SINGLE click-based navigation only
            # -- CONFIRMED via a captured screenshot that calling
            # page.reload() on these /portal/... URLs triggers
            # Cloudflare's "Verify you are human" bot challenge, which
            # cannot be (and should not be) automated around. These
            # detail pages are public-facing (no login required to
            # view them), which is presumably exactly why Cloudflare
            # guards them against scraping -- unlike the authenticated
            # /vendors/.../open-bids listing page, which has never
            # triggered this.
            #
            # Because of that, this no longer reads window.__data at
            # all (that required a reload to populate, since it's only
            # set on a genuine SSR page load, not a client-side route
            # change). Instead: title/agency/closeDate come from the
            # listing page (already have them from Stage 1), bidId
            # comes from the URL after the single click, and
            # description is scraped directly from the rendered DOM.
            # naicsCode is left blank as a result -- it's genuinely not
            # visible anywhere except window.__data, and title-keyword
            # matching remains the primary, reliable filter regardless.
            try:
                cells.nth(0).locator("a").click()
                page.wait_for_url(lambda url: "/projects/" in url, timeout=15000)

                match = re.search(r"/projects/(\d+)", page.url)
                bid_id = match.group(1) if match else ""

                # Best-effort description scrape from the rendered page
                # -- CONFIRMED class name ".introduction-description"
                # from real page HTML (the "Summary" section
                # specifically; there's also a "Background" section
                # with the same class further down the page, we just
                # take the first one). Not every posting template may
                # include this exact class, so this degrades to an
                # empty string rather than failing the whole bid if
                # it's missing.
                try:
                    description = page.locator(".introduction-description").first.inner_text(timeout=5000).strip()
                except Exception:
                    description = ""

                bids.append({
                    "bidId": bid_id,
                    "title": title,
                    "description": description,
                    "naicsCode": "",  # not available without triggering Cloudflare's bot check
                    "agency": organization,
                    "closeDate": due_date,
                    "bidValue": 0,  # no such field exists in OpenGov's data
                    "bidUrl": page.url,
                })
                new_fingerprints.add(fingerprint)

                page.go_back(wait_until="domcontentloaded")
                page.wait_for_url(lambda url: "/open-bids" in url, timeout=15000)
            except Exception as e:
                print(f"WARNING: failed to process bid '{title}', skipping: {e}")
                # Try to get back to the listing even if something above failed.
                if "/open-bids" not in page.url:
                    page.goto(OPEN_BIDS_URL, wait_until="domcontentloaded")
                continue

        # Pagination: CONFIRMED selector from real HTML --
        # div.pagination-bottom button (text "Next"), disabled via the
        # `disabled` attribute on the last page.
        if page_num < max_pages:
            next_button = page.locator(".pagination-bottom .-next button")
            if next_button.is_disabled():
                print(f"DEBUG: Next button disabled after page {page_num} -- "
                      "reached the last page.")
                break

            # Capture the first row's title before clicking, so we can
            # confirm the table content actually changed -- a prior run's
            # log showed the same bids repeating across "different"
            # pages, suggesting this click wasn't reliably advancing.
            first_row_before = rows[0].locator("div.rt-td").nth(0).locator("a").inner_text().strip() if rows else ""

            next_button.click()
            page.wait_for_load_state("domcontentloaded")

            try:
                page.wait_for_function(
                    """(prevTitle) => {
                        const firstRow = document.querySelector('div.rt-tbody div.rt-tr');
                        if (!firstRow) return false;
                        const link = firstRow.querySelector('div.rt-td a');
                        return link && link.textContent.trim() !== prevTitle;
                    }""",
                    arg=first_row_before,
                    timeout=10000,
                )
                print(f"DEBUG: page {page_num} -> {page_num + 1} advanced successfully.")
            except PlaywrightTimeoutError:
                print(f"WARNING: page did not appear to change after clicking Next "
                      f"on page {page_num} -- stopping pagination early to avoid "
                      "re-scanning the same content.")
                break

    return bids, new_fingerprints


# --------------------------------------------------------------------
# State tracking (avoid re-sending bids Twidget already saw, AND avoid
# re-clicking into bids we've already fully processed in a prior run --
# Twidget's own dedup table is the ultimate safety net on the bidId
# side, but fingerprints are what let us skip the click/page-load
# entirely for bids we recognize from the listing page alone)
# --------------------------------------------------------------------
def load_seen():
    """
    Returns (seen_bid_ids, seen_fingerprints) -- two separate sets
    loaded from the same state file.
    """
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        return set(data.get("bid_ids", [])), set(data.get("fingerprints", []))
    return set(), set()


def save_seen(seen_bid_ids, seen_fingerprints):
    STATE_FILE.write_text(json.dumps({
        "bid_ids": sorted(seen_bid_ids),
        "fingerprints": sorted(seen_fingerprints),
    }))


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
def process_bids(bids, seen_bid_ids):
    """
    Sends each bid not already in our local seen_bid_ids set to
    Twidget. (The fingerprint-based skip already happened earlier, in
    scrape_open_bids, before these bids were even fully scraped -- this
    is the second, bidId-based check, matching Twidget's own dedup key.)
    """
    sent = 0
    failed = 0
    for bid in bids:
        key = bid["bidId"]
        if not key or key in seen_bid_ids:
            continue
        try:
            send_to_twidget(bid, log_response=(sent == 0))
            seen_bid_ids.add(key)
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

    is_first_run = not STATE_FILE.exists()
    seen_bid_ids, seen_fingerprints = load_seen()

    # First run ever (no state file yet): scan every page to capture the
    # full current backlog of open bids. Every run after that only
    # scans the first MAX_PAGES pages, since by then we're just looking
    # for newly-posted bids, not re-discovering the whole 1,000+ backlog.
    # 100 is comfortably above the ~72 pages seen at 20 rows/page during
    # testing -- if the real count is ever higher, the pagination loop
    # naturally stops itself once the "Next" button becomes disabled, so
    # this is just a safety ceiling, not a hard assumption about count.
    pages_to_scan = 100 if is_first_run else MAX_PAGES
    if is_first_run:
        print("No state file found -- treating this as the first run and "
              f"scanning up to {pages_to_scan} pages to capture the full "
              "current backlog. Future runs will only scan the first "
              f"{MAX_PAGES} page(s) for new postings.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()

        try:
            login(page)
            bids, new_fingerprints = scrape_open_bids(page, seen_fingerprints, max_pages=pages_to_scan)
        except PlaywrightTimeoutError as e:
            snap(page, "ERROR_timeout")
            print(f"ERROR: timed out waiting for an expected element: {e}")
            browser.close()
            sys.exit(1)
        finally:
            browser.close()

    print(f"Found {len(bids)} new keyword-matching bid(s) across up to {pages_to_scan} page(s).")
    new_count = process_bids(bids, seen_bid_ids)
    seen_fingerprints |= new_fingerprints
    save_seen(seen_bid_ids, seen_fingerprints)
    print(f"Done. {new_count} new bid(s) sent to Twidget.")


if __name__ == "__main__":
    main()
