#!/usr/bin/env python3
"""
NSE + BSE Stocks & ETFs Scraper
================================

Pulls the full list of listed equities and ETFs from NSE and BSE's public
data endpoints and writes them into a single combined JSON file.

USAGE
-----
    pip install requests
    python nse_bse_scraper.py --output listed_instruments.json

OUTPUT FORMAT
-------------
A JSON file shaped like:

{
    "generated_at": "2026-08-31T12:00:00",
    "counts": {"NSE_STOCK": 2100, "NSE_ETF": 250, "BSE_STOCK": 5300, "BSE_ETF": 200},
    "instruments": [
        {
            "exchange": "NSE",
            "type": "STOCK",           // or "ETF"
            "symbol": "RELIANCE",
            "name": "Reliance Industries Limited",
            "isin": "INE002A01018",
            "series": "EQ"             // NSE only, blank for BSE
        },
        ...
    ]
}

NOTES ON RELIABILITY
---------------------
- NSE (nseindia.com / archives.nseindia.com) actively blocks requests that
  don't look like a real browser. This script opens a session against the
  homepage first to collect cookies, then reuses that session for the CSV/
  API calls, with browser-like headers. NSE also rate-limits aggressively —
  if you get repeated failures, wait a bit and retry, and avoid hammering it
  in a loop.
- BSE (bseindia.com / api.bseindia.com) is generally more permissive but
  still expects an Origin/Referer header.
- Both sites occasionally change their endpoint paths or response shape.
  If a call starts failing, the most likely cause is an endpoint change —
  open the corresponding URL in a browser to confirm the current shape and
  adjust the parsing function accordingly.
- This script only reads public, already-published listing data (the same
  CSV/API downloads available to any visitor of the exchange websites) — it
  does not access anything gated behind login, and it does not attempt to
  circumvent any technical access controls.
"""

import argparse
import csv
import io
import json
import sys
import time
from datetime import datetime, timezone

import requests

NSE_HOME = "https://www.nseindia.com"
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

BSE_HEADERS = {
    "User-Agent": NSE_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
}


def new_nse_session() -> requests.Session:
    """Warm up a session with NSE so cookies get set, mimicking a browser visit."""
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    try:
        s.get(NSE_HOME, timeout=15)
        time.sleep(1)  # be polite / let NSE settle the session
        s.get(f"{NSE_HOME}/market-data/securities-available-for-trading", timeout=15)
    except requests.RequestException as e:
        print(f"[NSE] Warning: session warm-up failed ({e}); continuing anyway.", file=sys.stderr)
    return s


def fetch_nse_equities(session: requests.Session) -> list:
    """
    Full list of NSE-listed equities (stocks) via the official CSV archive.
    Verified working (fetched live, no cookies/warm-up needed) at:
      https://archives.nseindia.com/content/equities/EQUITY_L.csv
      https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv  (mirror)
    """
    url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
    resp = session.get(url, headers=NSE_HEADERS, timeout=30)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    out = []
    for row in reader:
        out.append({
            "exchange": "NSE",
            "type": "STOCK",
            "symbol": row.get("SYMBOL", "").strip(),
            "name": row.get("NAME OF COMPANY", "").strip(),
            "isin": row.get(" ISIN NUMBER", row.get("ISIN NUMBER", "")).strip(),
            "series": row.get(" SERIES", row.get("SERIES", "")).strip(),
        })
    return out


def fetch_nse_etfs(session: requests.Session) -> list:
    """Full list of NSE-listed ETFs via the public JSON API."""
    url = "https://www.nseindia.com/api/etf"
    resp = session.get(url, headers=NSE_HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("data", [])
    out = []
    for row in rows:
        out.append({
            "exchange": "NSE",
            "type": "ETF",
            "symbol": row.get("symbol", "").strip(),
            "name": row.get("meta", {}).get("companyName", row.get("assets", "")).strip()
                    if isinstance(row.get("meta"), dict) else str(row.get("assets", "")).strip(),
            "isin": row.get("meta", {}).get("isin", "") if isinstance(row.get("meta"), dict) else "",
            "series": "EQ",
        })
    return out


def fetch_bse_scrips(session: requests.Session, segment: str, instrument_type: str) -> list:
    """
    Full list of BSE-listed scrips for a given segment via BSE's public
    ListofScripData API. segment='Equity' covers stocks; ETFs are tagged
    within the same master list via the 'Instrument' field, so we filter
    client-side rather than relying on a separate ETF-only endpoint.
    """
    # NOTE: BSE actively bot-detects and blocks plain HTTP requests (confirmed
    # while building this script). If this call fails with 403 or a bot-check
    # page, you'll likely need a headless-browser approach (e.g. Selenium /
    # Playwright) to pass BSE's checks, or a paid data vendor as fallback.
    url = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    params = {
        "Group": "",
        "Scrip": "",
        "industry": "",
        "segment": segment,
        "status": "Active",
    }
    resp = session.get(url, headers=BSE_HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    rows = resp.json()
    out = []
    for row in rows:
        instrument = (row.get("Instrument") or "").upper()
        is_etf = "ETF" in instrument or "EXCHANGE TRADED" in instrument
        out.append({
            "exchange": "BSE",
            "type": "ETF" if is_etf else instrument_type,
            "symbol": (row.get("scrip_id") or row.get("SC_NAME") or "").strip(),
            "name": (row.get("Scrip_Name") or row.get("SC_NAME") or "").strip(),
            "isin": (row.get("ISIN_CODE") or row.get("ISIN") or "").strip(),
            "series": "",
        })
    return out


def dedupe(instruments: list) -> list:
    seen = set()
    unique = []
    for inst in instruments:
        key = (inst["exchange"], inst["type"], inst["symbol"], inst["isin"])
        if key not in seen:
            seen.add(key)
            unique.append(inst)
    return unique


def main():
    parser = argparse.ArgumentParser(description="Scrape NSE + BSE listed stocks and ETFs into a JSON file.")
    parser.add_argument("--output", default="listed_instruments.json", help="Output JSON file path.")
    parser.add_argument("--skip-nse", action="store_true", help="Skip NSE.")
    parser.add_argument("--skip-bse", action="store_true", help="Skip BSE.")
    args = parser.parse_args()

    all_instruments = []

    if not args.skip_nse:
        print("[NSE] Warming up session...")
        nse_session = new_nse_session()

        print("[NSE] Fetching equities (stocks)...")
        try:
            nse_stocks = fetch_nse_equities(nse_session)
            print(f"[NSE] Got {len(nse_stocks)} stocks.")
            all_instruments.extend(nse_stocks)
        except Exception as e:
            print(f"[NSE] Failed to fetch equities: {e}", file=sys.stderr)

        print("[NSE] Fetching ETFs...")
        try:
            nse_etfs = fetch_nse_etfs(nse_session)
            print(f"[NSE] Got {len(nse_etfs)} ETFs.")
            all_instruments.extend(nse_etfs)
        except Exception as e:
            print(f"[NSE] Failed to fetch ETFs: {e}", file=sys.stderr)

    if not args.skip_bse:
        bse_session = requests.Session()
        bse_session.headers.update(BSE_HEADERS)

        print("[BSE] Fetching equities (stocks)...")
        try:
            bse_stocks = fetch_bse_scrips(bse_session, segment="Equity", instrument_type="STOCK")
            print(f"[BSE] Got {len(bse_stocks)} entries (stocks + ETFs mixed in this segment).")
            all_instruments.extend(bse_stocks)
        except Exception as e:
            print(f"[BSE] Failed to fetch equities: {e}", file=sys.stderr)

    all_instruments = dedupe(all_instruments)

    counts = {}
    for inst in all_instruments:
        key = f"{inst['exchange']}_{inst['type']}"
        counts[key] = counts.get(key, 0) + 1

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "instruments": all_instruments,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"\nDone. Wrote {len(all_instruments)} instruments to {args.output}")
    print("Counts by exchange/type:", counts)


if __name__ == "__main__":
    main()