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
       "solicitation_number": "SPE1C1-25-Q-0001",
       "nsn": "5310-01-234-5678",
       "fsc": "5310",
       "description": "WASHER,LOCK",
       "quantity": "500",
       "return_by": "2026-08-15",
       "url": "https://www.dibbs.bsm.dla.mil/rfq/rfqrec.aspx?sn=..."
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
from my sandbox (it's not on my allowed outbound domain list), so the
CSS selectors in `parse_search_results()` are my best-structured guess
based on how DIBBS's ASP.NET result tables are typically laid out --
NOT verified against the live HTML. Before your first real run:
  1. Open the DIBBS RFQ search in your browser for your FSC/NSN.
  2. Right-click a result row -> Inspect, and check the actual table/row
     class names.
  3. Compare against the selectors marked "VERIFY THIS" below and adjust.

I'm happy to fix the selectors with you once you paste in a snippet of
the actual HTML -- that'll take 5 minutes and make this solid.
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

    # Some DoD warning pages require an explicit "OK" form POST rather than
    # just a GET. If parse_search_results() below keeps getting the banner
    # HTML instead of real results, this is the first place to look --
    # inspect the warning page's <form> action/fields and POST to it here.
    return session


def search_by_fsc(session, fsc_code):
    """Search DIBBS RFQs by Federal Supply Class."""
    # VERIFY THIS: exact query param name/path for FSC search.
    url = f"{DIBBS_BASE}/RFQ/RFQFsc.aspx"
    resp = session.get(url, params={"fsc": fsc_code}, timeout=30)
    resp.raise_for_status()
    return parse_search_results(resp.text, fsc=fsc_code)


def search_by_nsn(session, nsn):
    """Search DIBBS RFQs by National Stock Number."""
    # VERIFY THIS: exact query param name/path for NSN search.
    url = f"{DIBBS_BASE}/RFQ/RFQNsn.aspx"
    resp = session.get(url, params={"nsn": nsn}, timeout=30)
    resp.raise_for_status()
    return parse_search_results(resp.text, fsc=nsn[:4] if len(nsn) >= 4 else "")


def parse_search_results(html, fsc=""):
    """
    Parses a DIBBS RFQ search results page into a list of dicts:
    {solicitation_number, nsn, fsc, description, quantity, return_by, url}

    VERIFY THIS whole function against real HTML -- table/row/column
    structure is a best guess pending you sending me a real sample.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # Guess: results live in a table with rows per solicitation.
    rows = soup.select("table.rfq-results tr")  # VERIFY THIS selector
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue  # likely a header row

        link = row.find("a", href=True)
        solicitation_number = cells[0].get_text(strip=True)
        nsn = cells[1].get_text(strip=True)
        description = cells[2].get_text(strip=True)
        quantity = cells[3].get_text(strip=True)
        return_by = cells[4].get_text(strip=True)
        detail_url = DIBBS_BASE + link["href"] if link else None

        results.append({
            "solicitation_number": solicitation_number,
            "nsn": nsn,
            "fsc": fsc or (nsn[:4] if len(nsn) >= 4 else ""),
            "description": description,
            "quantity": quantity,
            "return_by": return_by,
            "url": detail_url,
        })

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
def main():
    if not TWIDGET_WEBHOOK_URL:
        raise SystemExit("Missing config: TWIDGET_WEBHOOK_URL. Fill in CONFIG or set env var.")
    if not FSC_CODES and not NSN_LIST:
        raise SystemExit("Missing config: set DIBBS_FSCS and/or DIBBS_NSNS.")

    seen = load_seen()
    session = get_dibbs_session()

    all_results = []
    for fsc in FSC_CODES:
        all_results.extend(search_by_fsc(session, fsc))
        time.sleep(1)  # be polite between requests -- 6 FSCs = 6 requests per run
    for nsn in NSN_LIST:
        all_results.extend(search_by_nsn(session, nsn))
        time.sleep(1)

    new_count = 0
    for sol in all_results:
        key = sol["solicitation_number"]
        if key in seen:
            continue
        send_to_twidget(sol)
        seen.add(key)
        new_count += 1
        time.sleep(0.5)  # be polite to the webhook too

    save_seen(seen)
    print(f"Done. {new_count} new solicitation(s) sent to Twidget out of {len(all_results)} found "
          f"across {len(FSC_CODES)} FSC code(s) and {len(NSN_LIST)} NSN(s).")


if __name__ == "__main__":
    main()
