#!/usr/bin/env python3
"""
DIBBS -> Twidget RFQ feeder

Pulls open RFQ solicitations from DLA's public DIBBS site (dibbs.bsm.dla.mil)
for a list of FSC codes and/or NSNs, and POSTs each NEW solicitation (one
it hasn't seen before) as a JSON payload to a webhook/API endpoint you build
in Twidget. Twidget then handles turning that into a Trello card (and
anything else you want to layer on top).

Designed to run on a schedule via GitHub Actions -- not something Claude
runs for you, and not something that needs to run on your own computer.

--------------------------------------------------------------------
SETUP
--------------------------------------------------------------------
1. Install deps (only relevant for GitHub Actions' environment --
   the workflow file already does this step for you):
     pip install requests beautifulsoup4

2. Build an endpoint in Twidget that accepts a POST with a JSON body
   shaped like:
     {
       "solicitation_number": "SPE4AX-26-T-0109",
       "nsn_part_number": "5307-00-111-3939",
       "mil_spec": false,
       "nomenclature": "STUD SHOULDERED",
       "fsc": "5307",
       "technical_docs": false,
       "rfq_quote_status": "Open",
       "purchase_request_number": "7017662685",
       "purchase_request_qty": "200",
       "aidc": false,
       "fast_award_candidate": false,
       "set_aside_type": "Unrestricted",
       "issued": "07-30-2026",
       "return_by": "08-06-2026",
       "url": "https://dibbs.bsm.dla.mil/rfq/rfqrec.aspx?sn=..."
     }
   and have Twidget's no-code logic create the Trello card from that.

3. Fill in the CONFIG block below (or set as environment variables --
   recommended if this lives in a public GitHub repo, so you don't
   commit your webhook URL/secret in plain text).

4. Run once manually to confirm it's finding solicitations and your
   Twidget endpoint is receiving them before putting it on a schedule.

--------------------------------------------------------------------
IMPORTANT -- READ BEFORE RUNNING ON A SCHEDULE
--------------------------------------------------------------------
DIBBS is a public DoD system but every page is gated behind a "DoD Notice
and Consent" banner that sets a cookie before showing real content. The
`get_dibbs_session()` function below handles that handshake. HOWEVER:
I was not able to execute live network calls against dibbs.bsm.dla.mil
from my sandbox (it's not on my allowed outbound domain list), so this
has never actually been run end-to-end -- it's built from real page HTML
(view-source) you sent me, not from live testing.

CONFIRMED from real HTML:
  - Results land on https://dibbs.bsm.dla.mil/Rfq/RfqRecs.aspx with
    columns: # | NSN/Part Number (+ optional "Mil-Spec" 2nd line) |
    Nomenclature | Technical Documents | Solicitation (+ badge icons) |
    RFQ/Quote Status | Purchase Request (+ QTY) | Issued | Return By.
  - The search form (https://dibbs.bsm.dla.mil/RFQ/) uses field names
    ctl00$cph1$ddlCategory / ctl00$cph1$txtValue / ctl00$cph1$ddlScope /
    ctl00$cph1$butDbGo, and accepts multiple comma-separated values in
    one search (so all FSC codes go in ONE request, not one per code).
  - Sorting by Issued date is a postback (not a URL), confirmed via:
    __doPostBack('ctl00$cph1$grdRfqSearch','Sort$SORT_IS_DTE')
  - All 8 set-aside/special-flag badge icons' alt text, confirmed one
    by one against real HTML.

STILL UNTESTED (this is a best-effort build from static HTML, not a
live-verified script -- run it manually once, well before trusting it
to a schedule, and watch closely for errors or empty results):
  - Whether get_rfq_search_page()'s GET actually lands on the DoD-banner
    -accepted page vs the banner itself on a fresh session.
  - Whether submit_rfq_database_search()'s POST is missing some other
    required field not visible from static HTML alone (e.g. a hidden
    validation step JS normally runs before submit).
  - Whether combining multiple FSC codes in one search actually returns
    a combined list the way the help text implies.

CONFIRMED via dev tools: the results table's id is
ctl00_cph1_grdRfqSearch -- parse_search_results() now selects against
that directly instead of a guessed class name.

I'm glad to debug any of this together once you've tried a manual run --
paste me the console output or error and we'll fix it from there.
"""


import os
import json
import time
import requests
from bs4 import BeautifulSoup
from pathlib import Path

# --------------------------------------------------------------------
# CONFIG -- fill these in, or set as environment variables of the same name
# --------------------------------------------------------------------
# Comma-separated list, e.g. "5310,5305,5307,5306,5340,5330"
FSC_CODES = [c.strip() for c in os.environ.get("DIBBS_FSCS", "").split(",") if c.strip()]

# Optional: specific NSNs on top of the FSC codes, comma-separated
NSN_LIST = [n.strip() for n in os.environ.get("DIBBS_NSNS", "").split(",") if n.strip()]

# Your Twidget endpoint that accepts the JSON payload described above.
TWIDGET_WEBHOOK_URL = os.environ.get("TWIDGET_WEBHOOK_URL", "")

# Optional: if your Twidget endpoint expects an auth header/key, set it here.
TWIDGET_API_KEY = os.environ.get("TWIDGET_API_KEY", "")

STATE_FILE = Path(__file__).parent / "seen_solicitations.json"

DIBBS_BASE = "https://www.dibbs.bsm.dla.mil"
HEADERS = {
    # Identify honestly rather than spoofing a normal browser UA.
    "User-Agent": "Mozilla/5.0 (compatible; small-business-rfq-tracker/1.0; contact: you@example.com)"
}

DEBUG_DIR = Path(__file__).parent / "debug_html"


def save_debug(name, html):
    """
    Saves a snapshot of raw HTML received at a given step, when the
    DEBUG_HTML environment variable is set to true. This exists because
    I (Claude) can't make live network calls to dibbs.bsm.dla.mil from my
    own sandbox -- if the script runs but finds 0 results, these snapshots
    are how we find out WHERE it went wrong (banner not accepted? search
    not actually submitted? results table not matching?) without more
    rounds of screenshot-guessing.
    """
    if os.environ.get("DEBUG_HTML", "").lower() not in ("1", "true", "yes"):
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / f"{name}.html").write_text(html, encoding="utf-8")


# --------------------------------------------------------------------
# DIBBS session / search
# --------------------------------------------------------------------
def get_dibbs_session():
    """
    Establishes a session with DIBBS, accepting the DoD warning banner
    so subsequent requests return real content instead of the banner page.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    # Hitting the home page triggers the dodwarning.aspx redirect and
    # typically sets a cookie once followed. requests follows redirects
    # by default, so this single GET should leave us with a valid cookie jar.
    resp = session.get(DIBBS_BASE + "/", timeout=30)
    resp.raise_for_status()
    save_debug("01_after_banner_handshake", resp.text)

    # Some DoD warning pages require an explicit "OK" form POST rather than
    # just a GET. If parse_search_results() below keeps getting the banner
    # HTML instead of real results, this is the first place to look --
    # inspect the warning page's <form> action/fields and POST to it here.
    return session


def extract_form_state(html):
    """
    Captures the FULL current state of every form field on a DIBBS page --
    not just hidden ASP.NET tokens (__VIEWSTATE etc.), but also the current
    value of every text input/textarea and the currently-selected option
    of every <select>. A real browser sends all of this automatically on
    every form submission (postback or otherwise); since we're not running
    an actual browser, we reconstruct it by hand from the HTML so the
    server doesn't lose context (like which FSC was searched) between
    requests.
    """
    soup = BeautifulSoup(html, "html.parser")
    data = {}

    for inp in soup.find_all("input"):
        itype = (inp.get("type") or "text").lower()
        name = inp.get("name")
        if not name:
            continue
        if itype in ("submit", "button", "image", "reset", "file"):
            continue  # don't resend unclicked buttons
        if itype in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                data[name] = inp.get("value", "on")
            continue
        data[name] = inp.get("value", "")

    for ta in soup.find_all("textarea"):
        name = ta.get("name")
        if name:
            data[name] = ta.text or ""

    for sel in soup.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        chosen = sel.find("option", selected=True)
        if chosen is None:
            chosen = sel.find("option")  # fall back to first option
        data[name] = chosen.get("value", "") if chosen else ""

    return data


def do_postback(session, url, current_html, event_target, event_argument, extra_fields=None):
    """
    Simulates clicking an ASP.NET postback link (e.g. a sortable column
    header) by capturing the current page's full form state and POSTing
    it back along with __EVENTTARGET/__EVENTARGUMENT set to whatever the
    link's __doPostBack(...) call specified -- literally replaying what
    that click sends.
    """
    form_data = extract_form_state(current_html)
    form_data["__EVENTTARGET"] = event_target
    form_data["__EVENTARGUMENT"] = event_argument
    if extra_fields:
        form_data.update(extra_fields)

    resp = session.post(url, data=form_data, timeout=30)
    resp.raise_for_status()
    return resp.text


def sort_by_issued_descending(session, url, html):
    """
    Replicates clicking the "Issued" column header twice, which is how
    you get newest-first ordering on DIBBS's results grid. Confirmed via
    real inspection that the link is:
        __doPostBack('ctl00$cph1$grdRfqSearch','Sort$SORT_IS_DTE')
    First click -> ascending, second click on the same column -> descending.
    """
    event_target = "ctl00$cph1$grdRfqSearch"
    event_argument = "Sort$SORT_IS_DTE"

    html_after_first_click = do_postback(session, url, html, event_target, event_argument)
    save_debug("03_after_sort_click_1", html_after_first_click)
    html_after_second_click = do_postback(session, url, html_after_first_click, event_target, event_argument)
    save_debug("04_after_sort_click_2", html_after_second_click)
    return html_after_second_click


def get_rfq_search_page(session):
    """
    GETs the RFQ Area landing page (https://dibbs.bsm.dla.mil/RFQ/), which
    contains the "RFQ Database Search" form -- the one with the Search
    Categories dropdown (NSN/Part Number, Federal Supply Class,
    Solicitation #, PR, Nomenclature, CAGE, Part Number).
    """
    url = f"{DIBBS_BASE}/RFQ/"
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp


def submit_rfq_database_search(session, category, value, scope="open"):
    """
    Submits DIBBS's "RFQ Database Search" form. Field names below are
    CONFIRMED from real page HTML (view-source):
        ctl00$cph1$ddlCategory -- "fsc" for Federal Supply Class,
                                   "nsn" for NSN/Part Number
        ctl00$cph1$txtValue    -- the search value(s). Confirmed that this
                                   field accepts MULTIPLE values on one
                                   line, comma-separated (per the page's
                                   own help text) -- so all FSC codes can
                                   be searched in a single request instead
                                   of one request per code.
        ctl00$cph1$ddlScope    -- "open" (default: RFQs available for
                                   quoting), "todays", "recent", "all"
        ctl00$cph1$butDbGo     -- "Search" (the button clicked)
    """
    landing = get_rfq_search_page(session)
    form_data = extract_form_state(landing.text)
    form_data.update({
        "ctl00$cph1$ddlCategory": category,
        "ctl00$cph1$txtValue": value.upper(),  # site JS uppercases input; matching that behavior
        "ctl00$cph1$ddlScope": scope,
        "ctl00$cph1$butDbGo": "Search",
    })

    resp = session.post(landing.url, data=form_data, timeout=30)
    resp.raise_for_status()
    save_debug("02_after_search_submit", resp.text)
    return resp


def search_by_fsc_codes(session, fsc_codes):
    """
    Searches ALL given FSC codes in a single request (comma-separated),
    then sorts results by Issued date descending. Since multiple FSCs are
    mixed together in one result set, each row's `fsc` field is derived
    from its own NSN prefix during parsing rather than passed in here.
    """
    value = ",".join(fsc_codes)
    resp = submit_rfq_database_search(session, category="fsc", value=value)
    sorted_html = sort_by_issued_descending(session, resp.url, resp.text)
    return parse_search_results(sorted_html)


def search_by_nsns(session, nsn_list):
    """
    Searches ALL given NSNs in a single request (comma-separated), then
    sorts results by Issued date descending.
    """
    value = ",".join(nsn_list)
    resp = submit_rfq_database_search(session, category="nsn", value=value)
    sorted_html = sort_by_issued_descending(session, resp.url, resp.text)
    return parse_search_results(sorted_html)


def parse_search_results(html, fsc=""):
    """
    Parses a DIBBS RFQ search results page into a list of dicts matching
    the fields visible on the DIBBS front end. Column layout confirmed
    from a real screenshot of https://dibbs.bsm.dla.mil/Rfq/RfqRecs.aspx:

        # | NSN/Part Number | Nomenclature | Technical Documents |
        Solicitation | RFQ/Quote Status | Purchase Request (+ QTY) |
        Issued | Return By

    Returns dicts shaped like:
    {
        solicitation_number, nsn_part_number, mil_spec (bool),
        nomenclature, fsc,
        technical_docs (bool), rfq_quote_status,
        purchase_request_number, purchase_request_qty,
        aidc (bool), fast_award_candidate (bool),
        set_aside_type (text: "Unrestricted", "Small Business", "HUBZone",
            "SDVOSB", "WOSB", "EDWOSB", or ""),
        issued, return_by, url
    }

    NOTE: this page paginates (seen: "Records Found: 425" across 9 pages).
    This function only parses whatever HTML it's given -- pagination
    (following page 2, 3, ... links) still needs to be added once we
    confirm the page-link URL pattern. Flagging so it doesn't get missed.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # CONFIRMED from real dev tools inspection: the results table's id is
    # ctl00_cph1_grdRfqSearch (rows use class "AwdRecs", but selecting all
    # <tr> and filtering by cell count below is more robust than depending
    # on that class matching every row type, e.g. header rows).
    rows = soup.select("table#ctl00_cph1_grdRfqSearch tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 9:
            continue  # likely a header row

        # Column order per the screenshot:
        # 0=#, 1=NSN/Part Number, 2=Nomenclature, 3=Technical Documents,
        # 4=Solicitation, 5=RFQ/Quote Status, 6=Purchase Request,
        # 7=Issued, 8=Return By
        # The NSN/Part Number cell sometimes has a second line of red text
        # reading "Mil-Spec" underneath the NSN itself (confirmed from a
        # real screenshot). Capture that as its own flag rather than
        # letting it get folded into nsn_part_number.
        nsn_cell_text = cells[1].get_text(separator="\n", strip=True)
        nsn_cell_lines = [line.strip() for line in nsn_cell_text.split("\n") if line.strip()]
        nsn_part_number = nsn_cell_lines[0] if nsn_cell_lines else ""
        mil_spec = any("mil-spec" in line.lower() or "mil spec" in line.lower() for line in nsn_cell_lines[1:])

        nomenclature = cells[2].get_text(strip=True)

        tech_docs_text = cells[3].get_text(strip=True).lower()
        technical_docs = "tech docs" in tech_docs_text  # "None" -> False

        sol_link = cells[4].find("a", href=True)
        solicitation_number = sol_link.get_text(strip=True) if sol_link else cells[4].get_text(strip=True)
        detail_url = DIBBS_BASE + sol_link["href"] if sol_link else None

        # The Solicitation cell also carries small badge icons for
        # set-asides and special flags -- confirmed against real HTML for
        # all 8 badge types below (each alt text pasted in and verified):
        #   Unrestricted/Not Set Aside, Small Business Set-Aside,
        #   Automated Indefinite Delivery Contract (AIDC),
        #   Fast Award Candidate, HubZone Set-Aside,
        #   Service Disabled Veteran Owned Small Business (SDVOSB)
        #   Set-Aside, Economically Disadvantaged Women Owned Small
        #   Business Set Aside, Woman Owned Small Business (WOSB)
        #   Set-Aside.
        # Detection matches on each image's alt text (case-insensitive).
        #
        # A solicitation is one and only one set-aside type at a time, so
        # rather than 6 separate boolean columns, these collapse into a
        # single set_aside_type text field (e.g. "HUBZone", "Unrestricted").
        # AIDC and Fast Award Candidate stay as their own separate booleans
        # since a solicitation can carry those independent of, and at the
        # same time as, whatever its set-aside type is.
        badge_alts = [
            (img.get("alt") or img.get("title") or "").strip().lower()
            for img in cells[4].find_all("img")
        ]

        def has_badge(*needles):
            return any(all(n in alt for n in needles) for alt in badge_alts)

        if has_badge("veteran"):
            set_aside_type = "SDVOSB"
        elif has_badge("economically disadvantaged"):
            set_aside_type = "EDWOSB"
        elif has_badge("woman") or has_badge("women"):
            set_aside_type = "WOSB"
        elif has_badge("hubzone"):
            set_aside_type = "HUBZone"
        elif has_badge("small business"):
            set_aside_type = "Small Business"
        elif has_badge("unrestricted"):
            set_aside_type = "Unrestricted"
        else:
            set_aside_type = ""  # no matching badge found -- VERIFY THIS if it happens often

        labels = {
            "aidc": has_badge("automated indefinite delivery"),
            "fast_award_candidate": has_badge("fast award"),
            "set_aside_type": set_aside_type,
        }

        rfq_quote_status = cells[5].get_text(strip=True)

        # Purchase Request cell contains PR number and "QTY: ###" stacked
        # on separate lines -- split them out into two clean fields.
        pr_text = cells[6].get_text(separator="\n", strip=True)
        pr_lines = [line.strip() for line in pr_text.split("\n") if line.strip()]
        purchase_request_number = pr_lines[0] if pr_lines else ""
        purchase_request_qty = ""
        for line in pr_lines[1:]:
            if line.upper().startswith("QTY"):
                purchase_request_qty = line.split(":", 1)[-1].strip()
                break

        issued = cells[7].get_text(strip=True)
        return_by = cells[8].get_text(strip=True)

        result = {
            "solicitation_number": solicitation_number,
            "nsn_part_number": nsn_part_number,
            "mil_spec": mil_spec,
            "nomenclature": nomenclature,
            "fsc": fsc or (nsn_part_number[:4] if len(nsn_part_number) >= 4 else ""),
            "technical_docs": technical_docs,
            "rfq_quote_status": rfq_quote_status,
            "purchase_request_number": purchase_request_number,
            "purchase_request_qty": purchase_request_qty,
            "issued": issued,
            "return_by": return_by,
            "url": detail_url,
        }
        result.update(labels)
        results.append(result)

    return results


# --------------------------------------------------------------------
# State tracking (avoid duplicate Trello cards across runs)
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
def send_to_twidget(solicitation):
    headers = {"Content-Type": "application/json"}
    if TWIDGET_API_KEY:
        # Adjust this to match however your Twidget endpoint expects auth
        # (header name/scheme depends on how you set it up in Twidget).
        headers["Authorization"] = f"Bearer {TWIDGET_API_KEY}"

    resp = requests.post(
        TWIDGET_WEBHOOK_URL,
        headers=headers,
        json=solicitation,
        timeout=30,
    )
    resp.raise_for_status()
    return resp


# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------
def process_results(results, seen):
    """
    Sends only NEW solicitations to Twidget. Since DIBBS sorts results by
    Issued Date Descending, as soon as we hit one we've already sent
    before, everything after it (further down the page) is older news
    we've already processed in a prior run -- so we stop early instead
    of continuing to check the rest of the page. This also means we
    only ever need page 1 of results, not the full paginated set.
    """
    sent = 0
    for sol in results:
        key = sol["solicitation_number"]
        if key in seen:
            break  # everything from here down is older, already-seen
        send_to_twidget(sol)
        seen.add(key)
        sent += 1
        time.sleep(0.5)  # be polite to the webhook
    return sent


def main():
    if not TWIDGET_WEBHOOK_URL:
        raise SystemExit("Missing config: TWIDGET_WEBHOOK_URL. Fill in CONFIG or set env var.")
    if not FSC_CODES and not NSN_LIST:
        raise SystemExit("Missing config: set DIBBS_FSCS and/or DIBBS_NSNS.")

    seen = load_seen()
    session = get_dibbs_session()

    new_count = 0
    total_found = 0

    if FSC_CODES:
        # All FSC codes searched in ONE request -- DIBBS's search box
        # accepts multiple comma-separated values on one line.
        results = search_by_fsc_codes(session, FSC_CODES)
        total_found += len(results)
        new_count += process_results(results, seen)
        time.sleep(1)  # be polite before any further requests to DIBBS

    if NSN_LIST:
        results = search_by_nsns(session, NSN_LIST)
        total_found += len(results)
        new_count += process_results(results, seen)

    save_seen(seen)
    print(f"Done. {new_count} newly issued solicitation(s) sent to Twidget "
          f"(checked {total_found} listed across {len(FSC_CODES)} FSC code(s) "
          f"and {len(NSN_LIST)} NSN(s)).")


if __name__ == "__main__":
    main()
