"""
Linebarger Tax Sales Scraper
============================
Intercepts the LGBS map API to extract tax sale property records,
scores each lead, and writes:
  - dashboard/records.json
  - data/records.json
  - data/leads_export.csv  (GHL-ready)

Supports any county/state on taxsales.lgbs.com
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("lgbs_scraper")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR      = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = BASE_DIR / "dashboard"
DATA_DIR      = BASE_DIR / "data"
DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Config  (override via environment variables in GitHub Actions)
# ---------------------------------------------------------------------------
# Target county/state — set STATE_FILTER and/or COUNTY_FILTER to narrow results
# Leave blank to collect ALL counties from the map
STATE_FILTER  = os.getenv("STATE_FILTER",  "")   # e.g. "TX"
COUNTY_FILTER = os.getenv("COUNTY_FILTER", "")   # e.g. "HARRIS"
DAYS_BACK     = int(os.getenv("DAYS_BACK", "7"))

# Sale types to collect
SALE_TYPES = ["SALE", "STRUCK OFF", "FUTURE SALE"]

# Base LGBS map URL (nationwide bounding box)
LGBS_MAP_URL = (
    "https://taxsales.lgbs.com/map"
    "?lat=39.576604&lon=-96.721782&zoom=4&offset=0"
    "&ordering=precinct,sale_nbr,uid"
    "&sale_type=SALE,STRUCK%20OFF,FUTURE%20SALE"
    "&in_bbox=-137.239360125,17.356559635816986,-56.204203875,56.44163313230667"
)

RETRY_ATTEMPTS = 3
RETRY_DELAY    = 4   # seconds

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def retry(fn, *args, attempts: int = RETRY_ATTEMPTS, delay: float = RETRY_DELAY, **kwargs):
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            log.warning("Attempt %d/%d failed: %s", attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(delay)
    raise RuntimeError(f"All {attempts} attempts failed") from last_exc


def safe_float(v) -> float | None:
    if v is None:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(v))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split()
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (" ".join(parts[:-1]), parts[-1])


def parse_date(raw) -> str:
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(str(raw).strip()[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return str(raw).strip()[:10]


# ---------------------------------------------------------------------------
# Seller Score
# ---------------------------------------------------------------------------

def compute_score(rec: dict) -> tuple[int, list[str]]:
    score = 30
    flags: list[str] = []

    sale_type = (rec.get("sale_type") or "").upper()
    amount    = rec.get("amount_owed") or 0
    filed     = rec.get("sale_date", "")

    # Sale type flags
    if sale_type == "SALE":
        flags.append("Active Tax Sale")
        score += 20
    elif "STRUCK" in sale_type:
        flags.append("Struck Off – Gov Owned")
        score += 15
    elif "FUTURE" in sale_type:
        flags.append("Future Sale – Early Window")
        score += 10

    # Amount owed
    if amount and amount > 50_000:
        flags.append("High Debt (>$50k)")
        score += 15
    elif amount and amount > 10_000:
        flags.append("Significant Debt (>$10k)")
        score += 10
    elif amount and amount > 0:
        score += 5

    # Has address
    if rec.get("prop_address"):
        score += 5

    # Has mailing address (owner not living there)
    if rec.get("mail_address") and rec.get("mail_address") != rec.get("prop_address"):
        flags.append("Absentee Owner")
        score += 10

    # Filed / sale date within 7 days
    try:
        sale_dt = datetime.strptime(filed, "%Y-%m-%d")
        if abs((datetime.now() - sale_dt).days) <= 7:
            flags.append("Sale This Week")
            score += 5
    except Exception:
        pass

    # Owner looks like LLC/Corp
    owner = (rec.get("owner") or "").upper()
    if any(kw in owner for kw in ("LLC", "INC", "CORP", "LTD", " LP", "TRUST", "ESTATE")):
        flags.append("LLC / Corp / Estate Owner")
        score += 10

    # Multiple years delinquent (inferred from amount)
    if amount and amount > 20_000:
        flags.append("Likely Multi-Year Delinquent")

    return min(score, 100), flags


# ---------------------------------------------------------------------------
# API Discovery + Playwright Scraper
# ---------------------------------------------------------------------------

async def scrape_lgbs(date_from: str, date_to: str) -> list[dict]:
    """
    Use Playwright to:
    1. Load the LGBS map for each sale date in range
    2. Intercept XHR/fetch calls to discover the real API endpoint
    3. Collect all property records
    """
    from playwright.async_api import async_playwright

    records: list[dict] = []
    api_base: str | None = None

    # Generate list of dates to query
    dates = []
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end   = datetime.strptime(date_to,   "%Y-%m-%d")
    delta = end - start
    for i in range(delta.days + 1):
        dates.append((start + timedelta(days=i)).strftime("%Y-%m-%d"))

    log.info("Querying %d sale dates: %s → %s", len(dates), date_from, date_to)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            )
        )

        # ── Step 1: Intercept API calls on the first page load ──────────
        page = await context.new_page()
        captured_api_urls: list[str] = []

        async def on_request(request):
            url = request.url
            if "api" in url and ("properties" in url or "sales" in url or "lgbs" in url.lower()):
                captured_api_urls.append(url)
                log.info("  🔍 Intercepted API call: %s", url[:120])

        page.on("request", on_request)

        log.info("Loading LGBS map to discover API endpoint…")
        map_url = (
            f"{LGBS_MAP_URL}"
            f"&sale_date_only={dates[0]}"
        )
        try:
            await page.goto(map_url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(3)  # let all XHR settle
        except Exception as exc:
            log.warning("Map load warning (non-fatal): %s", exc)

        # ── Step 2: Figure out the API base from intercepted calls ───────
        for url in captured_api_urls:
            # Pattern: https://taxsales.lgbs.com/api/properties/?...
            m = re.match(r"(https://taxsales\.lgbs\.com/[^?]+)\?", url)
            if m:
                api_base = m.group(1)
                log.info("✅ Discovered API base: %s", api_base)
                break

        # ── Step 3: If we found the API, use requests for all dates ──────
        if api_base:
            await browser.close()
            for sale_date in dates:
                batch = _fetch_api_page(api_base, sale_date)
                records.extend(batch)
                log.info("  %s → %d records", sale_date, len(batch))
        else:
            # ── Step 4: Fallback – parse the DOM directly ────────────────
            log.info("API not intercepted – falling back to DOM parsing")
            for sale_date in dates:
                batch = await _scrape_dom(page, sale_date)
                records.extend(batch)
                log.info("  %s → %d records (DOM)", sale_date, len(batch))
            await browser.close()

    return records


def _fetch_api_page(api_base: str, sale_date: str) -> list[dict]:
    """
    Hit the discovered API endpoint with pagination,
    collecting all records for a given sale date.
    """
    records: list[dict] = []
    session = requests.Session()
    session.headers.update({
        "User-Agent":   "Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0",
        "Accept":       "application/json",
        "Referer":      "https://taxsales.lgbs.com/",
    })

    # Try common API parameter patterns
    param_sets = [
        {"sale_date_only": sale_date, "sale_type": "SALE,STRUCK OFF,FUTURE SALE",
         "format": "json", "limit": 500, "offset": 0},
        {"sale_date": sale_date, "type": "SALE,STRUCK OFF,FUTURE SALE",
         "format": "json", "limit": 500, "offset": 0},
        {"date": sale_date, "format": "json", "limit": 500},
    ]

    for params in param_sets:
        try:
            offset = 0
            while True:
                params["offset"] = offset
                resp = retry(session.get, api_base, params=params, timeout=30)
                resp.raise_for_status()

                data = resp.json()

                # Handle both list and {results:[...]} shapes
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = (
                        data.get("results") or
                        data.get("data") or
                        data.get("properties") or
                        data.get("features") or
                        []
                    )
                else:
                    items = []

                if not items:
                    break

                for item in items:
                    rec = _map_api_record(item, sale_date)
                    if rec:
                        records.append(rec)

                # Pagination
                if len(items) < params.get("limit", 500):
                    break
                offset += len(items)

            if records:
                return records   # found with this param set

        except Exception as exc:
            log.debug("API param set failed: %s", exc)
            continue

    return records


def _map_api_record(item: dict, sale_date: str) -> dict | None:
    """Map one API JSON item to our unified schema."""
    try:
        # Handle GeoJSON feature wrapper
        if item.get("type") == "Feature":
            props = item.get("properties", {})
            geom  = item.get("geometry", {})
        else:
            props = item
            geom  = {}

        # Extract coordinates if available
        coords = geom.get("coordinates", []) if geom else []
        lat = coords[1] if len(coords) >= 2 else None
        lon = coords[0] if len(coords) >= 2 else None

        # Flexible field mapping — LGBS uses slightly different keys per county
        def g(*keys):
            for k in keys:
                v = props.get(k)
                if v not in (None, "", "N/A", "null"):
                    return str(v).strip()
            return ""

        prop_addr  = g("situs_address", "property_address", "address", "situs", "site_address")
        prop_city  = g("situs_city",    "property_city",    "city")
        prop_state = g("state",         "property_state",   "st") or STATE_FILTER or "TX"
        prop_zip   = g("situs_zip",     "property_zip",     "zip", "zip_code")
        owner      = g("owner_name",    "owner",            "grantor", "taxpayer_name", "taxpayer")
        county     = g("county",        "county_name",      "jurisdiction")
        precinct   = g("precinct",      "precinct_nbr")
        sale_nbr   = g("sale_nbr",      "sale_number",      "uid", "id")
        sale_type  = g("sale_type",     "type",             "status")
        legal      = g("legal_description", "legal",        "legal_desc")
        account_id = g("account_number", "account_id",      "acct_nbr", "parcel_id")
        mail_addr  = g("mail_address",  "mailing_address",  "owner_address")
        mail_city  = g("mail_city",     "mailing_city")
        mail_state = g("mail_state",    "mailing_state")
        mail_zip   = g("mail_zip",      "mailing_zip")
        amount_raw = g("amount_due",    "amount_owed",      "taxes_due",
                       "tax_due",       "total_due",        "amount")

        # Apply state/county filters
        if STATE_FILTER  and prop_state.upper() != STATE_FILTER.upper():
            return None
        if COUNTY_FILTER and county.upper() not in COUNTY_FILTER.upper():
            return None

        # Build direct link
        uid       = g("uid", "id", "sale_nbr", "account_number")
        clerk_url = (
            f"https://taxsales.lgbs.com/property/{uid}"
            if uid else "https://taxsales.lgbs.com/map"
        )

        return {
            "doc_num":      sale_nbr or uid,
            "account_id":   account_id,
            "sale_type":    sale_type or "SALE",
            "sale_date":    parse_date(sale_date),
            "county":       county,
            "precinct":     precinct,
            "owner":        owner,
            "prop_address": prop_addr,
            "prop_city":    prop_city,
            "prop_state":   prop_state,
            "prop_zip":     prop_zip[:5] if prop_zip else "",
            "mail_address": mail_addr,
            "mail_city":    mail_city,
            "mail_state":   mail_state,
            "mail_zip":     mail_zip[:5] if mail_zip else "",
            "legal":        legal,
            "amount_owed":  safe_float(amount_raw),
            "lat":          lat,
            "lon":          lon,
            "clerk_url":    clerk_url,
            "flags":        [],
            "score":        0,
        }
    except Exception as exc:
        log.debug("Record map error: %s", exc)
        return None


async def _scrape_dom(page, sale_date: str) -> list[dict]:
    """Fallback: parse property cards directly from the LGBS map DOM."""
    from playwright.async_api import TimeoutError as PWTimeout

    records: list[dict] = []
    url = (
        f"https://taxsales.lgbs.com/map"
        f"?lat=39.576604&lon=-96.721782&zoom=4&offset=0"
        f"&ordering=precinct,sale_nbr,uid"
        f"&sale_type=SALE,STRUCK%20OFF,FUTURE%20SALE"
        f"&sale_date_only={sale_date}"
        f"&in_bbox=-137.239360125,17.356559635816986,-56.204203875,56.44163313230667"
    )

    try:
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        await asyncio.sleep(4)
    except PWTimeout:
        log.warning("DOM fallback timeout for %s", sale_date)
        return records

    html = await page.content()
    soup = BeautifulSoup(html, "lxml")

    # Try common card/list item patterns used by React map apps
    selectors = [
        ("[class*='property-card']",  _parse_card),
        ("[class*='PropertyCard']",   _parse_card),
        ("[class*='list-item']",      _parse_card),
        ("tr[data-id]",               _parse_table_row),
        ("tr",                        _parse_table_row),
    ]
    for selector, parser in selectors:
        items = soup.select(selector)
        if items:
            log.info("  DOM selector '%s' matched %d items", selector, len(items))
            for el in items:
                rec = parser(el, sale_date)
                if rec:
                    records.append(rec)
            break

    return records


def _parse_card(el, sale_date: str) -> dict | None:
    try:
        text = el.get_text(" ", strip=True)
        link = el.find("a", href=True)
        url  = "https://taxsales.lgbs.com" + link["href"] if link else ""

        # Heuristic: first line is usually the address
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        return {
            "doc_num":      el.get("data-id", ""),
            "account_id":   "",
            "sale_type":    "SALE",
            "sale_date":    parse_date(sale_date),
            "county":       "",
            "precinct":     "",
            "owner":        lines[1] if len(lines) > 1 else "",
            "prop_address": lines[0] if lines else "",
            "prop_city":    "",
            "prop_state":   STATE_FILTER or "TX",
            "prop_zip":     "",
            "mail_address": "",
            "mail_city":    "",
            "mail_state":   "",
            "mail_zip":     "",
            "legal":        "",
            "amount_owed":  None,
            "lat":          None,
            "lon":          None,
            "clerk_url":    url,
            "flags":        [],
            "score":        0,
        }
    except Exception:
        return None


def _parse_table_row(el, sale_date: str) -> dict | None:
    try:
        cells = [c.get_text(strip=True) for c in el.find_all(["td", "th"])]
        if len(cells) < 2:
            return None
        return {
            "doc_num":      cells[0],
            "account_id":   "",
            "sale_type":    "SALE",
            "sale_date":    parse_date(sale_date),
            "county":       cells[1] if len(cells) > 1 else "",
            "precinct":     cells[2] if len(cells) > 2 else "",
            "owner":        cells[3] if len(cells) > 3 else "",
            "prop_address": cells[4] if len(cells) > 4 else "",
            "prop_city":    "",
            "prop_state":   STATE_FILTER or "TX",
            "prop_zip":     "",
            "mail_address": "",
            "mail_city":    "",
            "mail_state":   "",
            "mail_zip":     "",
            "legal":        cells[5] if len(cells) > 5 else "",
            "amount_owed":  safe_float(cells[6] if len(cells) > 6 else ""),
            "lat":          None,
            "lon":          None,
            "clerk_url":    "https://taxsales.lgbs.com/map",
            "flags":        [],
            "score":        0,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

def deduplicate(records: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out:  list[dict] = []
    for r in records:
        key = r.get("doc_num") or (r.get("owner", "") + r.get("prop_address", ""))
        if key and key not in seen:
            seen.add(key)
            out.append(r)
    return out


def score_all(records: list[dict]) -> list[dict]:
    for r in records:
        score, flags = compute_score(r)
        r["score"] = score
        r["flags"] = flags
    records.sort(key=lambda x: x["score"], reverse=True)
    return records


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_json(records: list[dict], date_from: str, date_to: str) -> None:
    state_label  = f" – {STATE_FILTER}"  if STATE_FILTER  else ""
    county_label = f" / {COUNTY_FILTER}" if COUNTY_FILTER else " (All Counties)"
    payload: dict[str, Any] = {
        "fetched_at":   datetime.utcnow().isoformat() + "Z",
        "source":       f"Linebarger Tax Sales{state_label}{county_label}",
        "date_range":   {"from": date_from, "to": date_to},
        "total":        len(records),
        "with_address": sum(1 for r in records if r.get("prop_address")),
        "high_score":   sum(1 for r in records if r.get("score", 0) >= 60),
        "records":      records,
    }
    for path in [DASHBOARD_DIR / "records.json", DATA_DIR / "records.json"]:
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        log.info("Wrote %s", path)


def write_csv_ghl(records: list[dict]) -> None:
    path = DATA_DIR / "leads_export.csv"
    fieldnames = [
        "First Name", "Last Name",
        "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Property Address", "Property City", "Property State", "Property Zip",
        "County", "Precinct", "Account ID",
        "Sale Type", "Sale Date", "Amount Owed",
        "Seller Score", "Motivated Seller Flags",
        "Legal Description", "Source", "Public Records URL",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            first, last = split_name(r.get("owner", ""))
            writer.writerow({
                "First Name":             first,
                "Last Name":              last,
                "Mailing Address":        r.get("mail_address", ""),
                "Mailing City":           r.get("mail_city", ""),
                "Mailing State":          r.get("mail_state", ""),
                "Mailing Zip":            r.get("mail_zip", ""),
                "Property Address":       r.get("prop_address", ""),
                "Property City":          r.get("prop_city", ""),
                "Property State":         r.get("prop_state", ""),
                "Property Zip":           r.get("prop_zip", ""),
                "County":                 r.get("county", ""),
                "Precinct":               r.get("precinct", ""),
                "Account ID":             r.get("account_id", ""),
                "Sale Type":              r.get("sale_type", ""),
                "Sale Date":              r.get("sale_date", ""),
                "Amount Owed":            r.get("amount_owed", "") or "",
                "Seller Score":           r.get("score", 0),
                "Motivated Seller Flags": "; ".join(r.get("flags", [])),
                "Legal Description":      r.get("legal", ""),
                "Source":                 "Linebarger Tax Sales (taxsales.lgbs.com)",
                "Public Records URL":     r.get("clerk_url", ""),
            })
    log.info("Wrote GHL CSV: %s (%d rows)", path, len(records))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    today    = datetime.now()
    date_to  = today.strftime("%Y-%m-%d")
    date_from = (today - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")

    # Allow scraping a specific date via env var (useful for manual runs)
    specific_date = os.getenv("SALE_DATE_ONLY", "")
    if specific_date:
        date_from = date_to = specific_date

    log.info("=" * 60)
    log.info("Linebarger Tax Sales Scraper")
    log.info("Date range  : %s → %s", date_from, date_to)
    log.info("State filter: %s", STATE_FILTER or "ALL")
    log.info("County      : %s", COUNTY_FILTER or "ALL")
    log.info("=" * 60)

    # Scrape
    records = await scrape_lgbs(date_from, date_to)
    log.info("Raw records collected: %d", len(records))

    # Deduplicate
    records = deduplicate(records)
    log.info("After dedup: %d", len(records))

    # Score
    records = score_all(records)

    # Write outputs
    write_json(records, date_from, date_to)
    write_csv_ghl(records)

    log.info("=" * 60)
    log.info("Done! Total: %d | With address: %d | Score ≥60: %d",
             len(records),
             sum(1 for r in records if r.get("prop_address")),
             sum(1 for r in records if r.get("score", 0) >= 60))
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
