"""
Linebarger Tax Sales Scraper — All Counties Edition
=====================================================
- Scrapes ALL counties on taxsales.lgbs.com
- High priority scoring for ESTATE owners
- Groups results by county for navigation
- Writes dashboard/records.json + data/leads_export.csv
"""
from __future__ import annotations
import asyncio, csv, json, logging, os, re, sys, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("lgbs_scraper")

BASE_DIR      = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = BASE_DIR / "dashboard"
DATA_DIR      = BASE_DIR / "data"
DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILTER   = os.getenv("STATE_FILTER",   "")
COUNTY_FILTER  = os.getenv("COUNTY_FILTER",  "")
DAYS_BACK      = int(os.getenv("DAYS_BACK",  "7"))
SALE_DATE_ONLY = os.getenv("SALE_DATE_ONLY", "")

LGBS_API_SALES    = "https://taxsales.lgbs.com/api/property_sales/"
LGBS_API_COUNTIES = "https://taxsales.lgbs.com/api/sale_counties/"
LGBS_MAP_URL = (
    "https://taxsales.lgbs.com/map?lat=39.576604&lon=-96.721782&zoom=4"
    "&offset=0&ordering=precinct,sale_nbr,uid"
    "&sale_type=SALE,STRUCK%20OFF,FUTURE%20SALE"
    "&in_bbox=-137.239360125,17.356559635816986,-56.204203875,56.44163313230667"
)
NATIONWIDE_BBOX = "-137.239360125,17.356559635816986,-56.204203875,56.44163313230667"
RETRY_ATTEMPTS  = 3
RETRY_DELAY     = 8

ESTATE_KEYWORDS = [
    "ESTATE","EST OF","EST.","HEIRS","HEIR OF","DECEASED","DECEDENT",
    "PROBATE","ADMINISTRATOR","ADMINISTRATRIX","EXECUTOR","EXECUTRIX",
    "SURVIVING HEIR","IN TRUST","GUARDIANSHIP",
]

# ── Helpers ──────────────────────────────────────────────────────────────────

def retry(fn, *args, attempts=RETRY_ATTEMPTS, delay=RETRY_DELAY, **kwargs):
    last = None
    for i in range(1, attempts+1):
        try: return fn(*args, **kwargs)
        except Exception as e:
            last = e
            log.warning("Attempt %d/%d failed: %s", i, attempts, e)
            if i < attempts: time.sleep(delay)
    raise RuntimeError(f"All {attempts} attempts failed") from last

def safe_float(v):
    if v is None: return None
    c = re.sub(r"[^\d.]","",str(v))
    try: return float(c) if c else None
    except: return None

def split_name(full):
    p = full.strip().split()
    if not p: return ("","")
    if len(p)==1: return (p[0],"")
    return (" ".join(p[:-1]), p[-1])

def parse_date(raw):
    if not raw: return ""
    for fmt in ("%Y-%m-%d","%m/%d/%Y","%m-%d-%Y","%Y%m%d"):
        try: return datetime.strptime(str(raw).strip()[:10],fmt).strftime("%Y-%m-%d")
        except: pass
    return str(raw).strip()[:10]

def is_estate(owner):
    u = owner.upper()
    return any(k in u for k in ESTATE_KEYWORDS)

def owner_label(owner):
    if is_estate(owner): return "🏛 ESTATE"
    u = owner.upper()
    if any(k in u for k in ["LLC","INC","CORP","LTD"," LP","L.P."]): return "🏢 CORP/LLC"
    return "👤 Individual"

def make_session():
    s = requests.Session()
    # Retry adapter with backoff
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    adapter = HTTPAdapter(
        max_retries=Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
        ),
        pool_connections=1,
        pool_maxsize=1,
    )
    s.mount('https://', adapter)
    s.headers.update({
        "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://taxsales.lgbs.com/map",
        "Origin":          "https://taxsales.lgbs.com",
    })
    return s

# ── Scoring ──────────────────────────────────────────────────────────────────

def compute_score(rec):
    score, flags = 30, []
    sale_type = (rec.get("sale_type") or "").upper()
    amount    = rec.get("amount_owed") or 0
    owner     = rec.get("owner") or ""
    filed     = rec.get("sale_date","")

    # ESTATE = highest priority
    if is_estate(owner):
        flags.append("🏛 Estate / Probate Owner")
        score += 25

    if sale_type.strip() == "SALE":
        flags.append("Active Tax Sale"); score += 20
    elif "STRUCK" in sale_type:
        flags.append("Struck Off – Gov Owned"); score += 15
    elif "FUTURE" in sale_type:
        flags.append("Future Sale – Early Window"); score += 10

    if amount > 50_000:   flags.append("High Debt (>$50k)");      score += 15
    elif amount > 10_000: flags.append("Significant Debt (>$10k)"); score += 10
    elif amount > 0:      score += 3

    if (rec.get("mail_address") and
            rec.get("mail_address") != rec.get("prop_address")):
        flags.append("Absentee Owner"); score += 10

    if rec.get("prop_address"): score += 5

    try:
        if abs((datetime.now()-datetime.strptime(filed,"%Y-%m-%d")).days) <= 7:
            flags.append("Sale This Week"); score += 5
    except: pass

    u = owner.upper()
    if any(k in u for k in ["LLC","INC","CORP","LTD"]) and not is_estate(owner):
        flags.append("LLC / Corp Owner"); score += 8

    return min(score,100), flags

# ── API calls ────────────────────────────────────────────────────────────────

def fetch_counties(sale_date):
    session = make_session()
    for params in [{"sale_date_only": sale_date, "limit":500}, {"limit":500}]:
        try:
            r = retry(session.get, LGBS_API_COUNTIES, params=params, timeout=60)
            r.raise_for_status()
            d = r.json()
            items = d if isinstance(d,list) else (d.get("results") or d.get("data") or [])
            if items:
                log.info("Found %d counties for %s", len(items), sale_date)
                return items
        except Exception as e:
            log.warning("County fetch error: %s", e)
    return []

def fetch_sales(sale_date, county="", state=""):
    session = make_session()
    records = []
    base = {"sale_date_only":sale_date,
            "sale_type":"SALE,STRUCK OFF,FUTURE SALE",
            "ordering":"precinct,sale_nbr,uid",
            "in_bbox":NATIONWIDE_BBOX,
            "limit":500,"offset":0}
    if county: base["county"] = county
    if state:  base["state"]  = state

    param_sets = [
        base,
        {k:v for k,v in base.items() if k!="in_bbox"},
        {"sale_date_only":sale_date,"in_bbox":NATIONWIDE_BBOX,"limit":500,"offset":0},
        {"limit":500,"offset":0},
    ]

    for params in param_sets:
        try:
            offset = 0
            while True:
                params["offset"] = offset
                r = retry(session.get, LGBS_API_SALES, params=params, timeout=60)
                r.raise_for_status()
                d = r.json()
                items = d if isinstance(d,list) else \
                        (d.get("results") or d.get("data") or
                         d.get("properties") or d.get("features") or [])
                if not items: break
                for item in items:
                    rec = map_record(item, sale_date)
                    if rec: records.append(rec)
                if len(items) < params.get("limit",500): break
                offset += len(items)
            if records: return records
        except Exception as e:
            log.debug("Param set failed: %s", e)
    return records

def map_record(item, sale_date):
    try:
        if item.get("type") == "Feature":
            props = item.get("properties",{}); geom = item.get("geometry") or {}
        else:
            props = item; geom = {}
        coords = geom.get("coordinates") or []
        lat = coords[1] if len(coords)>=2 else None
        lon = coords[0] if len(coords)>=2 else None

        def g(*keys):
            for k in keys:
                v = props.get(k)
                if v not in (None,"","N/A","null","NULL"): return str(v).strip()
            return ""

        owner     = g("owner_name","owner","grantor","taxpayer_name","taxpayer","account_owner")
        prop_addr = g("situs_address","property_address","address","situs","site_address")
        prop_city = g("situs_city","property_city","city")
        prop_st   = g("state","property_state","st") or STATE_FILTER or ""
        prop_zip  = g("situs_zip","property_zip","zip","zip_code")
        county    = g("county","county_name","jurisdiction","sale_county")
        precinct  = g("precinct","precinct_nbr","pct")
        sale_nbr  = g("sale_nbr","sale_number","sale_no")
        uid       = g("uid","id","pk")
        sale_type = g("sale_type","type","status","sale_status") or "SALE"
        legal     = g("legal_description","legal","legal_desc","prop_legal")
        acct_id   = g("account_number","account_id","acct_nbr","parcel_id","account_no")
        mail_addr = g("mail_address","mailing_address","owner_address","mail_addr")
        mail_city = g("mail_city","mailing_city")
        mail_st   = g("mail_state","mailing_state")
        mail_zip  = g("mail_zip","mailing_zip")
        amt_raw   = g("amount_due","amount_owed","taxes_due","tax_due","total_due","amount")
        sale_dt   = g("sale_date","sale_date_only","auction_date") or sale_date

        if STATE_FILTER  and prop_st and prop_st.upper() != STATE_FILTER.upper(): return None
        if COUNTY_FILTER and county  and COUNTY_FILTER.upper() not in county.upper(): return None

        link_id   = uid or sale_nbr or acct_id
        clerk_url = (f"https://taxsales.lgbs.com/property/{link_id}"
                     if link_id else "https://taxsales.lgbs.com/map")

        return {
            "doc_num":      sale_nbr or uid,
            "uid":          uid,
            "account_id":   acct_id,
            "sale_type":    sale_type.upper(),
            "sale_date":    parse_date(sale_dt),
            "county":       county,
            "state":        prop_st,
            "precinct":     precinct,
            "owner":        owner,
            "owner_label":  owner_label(owner),
            "is_estate":    is_estate(owner),
            "is_corp":      any(k in owner.upper() for k in ["LLC","INC","CORP","LTD"]),
            "prop_address": prop_addr,
            "prop_city":    prop_city,
            "prop_state":   prop_st,
            "prop_zip":     prop_zip[:5] if prop_zip else "",
            "mail_address": mail_addr,
            "mail_city":    mail_city,
            "mail_state":   mail_st,
            "mail_zip":     mail_zip[:5] if mail_zip else "",
            "amount_owed":  safe_float(amt_raw),
            "legal":        legal,
            "lat":          lat, "lon": lon,
            "clerk_url":    clerk_url,
            "flags":        [], "score": 0,
        }
    except Exception as e:
        log.debug("map_record error: %s", e)
        return None

# ── Playwright discovery ──────────────────────────────────────────────────────

async def playwright_discover(dates):
    from playwright.async_api import async_playwright
    records, api_base = [], LGBS_API_SALES

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"))
        page = await ctx.new_page()

        async def on_response(resp):
            url = resp.url
            if "taxsales.lgbs.com/api/" in url and "google" not in url:
                try:
                    body = await resp.json()
                    items = body if isinstance(body,list) else \
                            (body.get("results") or body.get("data") or [])
                    if items and "property_sales" in url:
                        for item in items:
                            rec = map_record(item, dates[0])
                            if rec: records.append(rec)
                        log.info("  📡 Playwright harvested %d from %s", len(items), url[:80])
                except: pass

        page.on("response", on_response)
        try:
            await page.goto(f"{LGBS_MAP_URL}&sale_date_only={dates[0]}",
                            wait_until="networkidle", timeout=35_000)
            await asyncio.sleep(4)
        except Exception as e:
            log.warning("Playwright map load: %s", e)
        await browser.close()

    log.info("Playwright discovery complete — %d initial records", len(records))
    return records

# ── Main orchestrator ─────────────────────────────────────────────────────────

async def scrape_all(date_from, date_to):
    dates = []
    s = datetime.strptime(date_from,"%Y-%m-%d")
    e = datetime.strptime(date_to,  "%Y-%m-%d")
    for i in range((e-s).days+1):
        dates.append((s+timedelta(days=i)).strftime("%Y-%m-%d"))

    log.info("Scraping %d date(s): %s", len(dates), ", ".join(dates))
    all_records = await playwright_discover(dates)

    for sale_date in dates:
        log.info("Fetching all sales for %s …", sale_date)
        counties = fetch_counties(sale_date)

        if counties and not COUNTY_FILTER:
            log.info("Processing %d counties…", len(counties))
            for co in counties:
                co_name  = (co.get("county") or co.get("county_name") or co.get("name") or "")
                co_state = (co.get("state") or co.get("state_abbr") or "")
                if STATE_FILTER and co_state.upper() != STATE_FILTER.upper(): continue
                batch = fetch_sales(sale_date, county=co_name, state=co_state)
                log.info("  %s County, %s → %d records", co_name, co_state, len(batch))
                all_records.extend(batch)
                time.sleep(2)  # polite delay between county requests
        else:
            batch = fetch_sales(sale_date, county=COUNTY_FILTER, state=STATE_FILTER)
            log.info("  Nationwide/filtered → %d records", len(batch))
            all_records.extend(batch)

    return all_records

def deduplicate(records):
    seen, out = set(), []
    for r in records:
        key = (r.get("uid") or r.get("account_id") or
               (r.get("owner","") + r.get("prop_address","") + r.get("sale_date","")))
        if key and key not in seen:
            seen.add(key); out.append(r)
    return out

def score_all(records):
    for r in records:
        sc, fl = compute_score(r)
        r["score"] = sc; r["flags"] = fl
    records.sort(key=lambda x: (0 if x.get("is_estate") else 1, -x.get("score",0)))
    return records

def group_by_county(records):
    grouped = {}
    for r in records:
        co  = r.get("county") or "Unknown"
        st  = r.get("state") or ""
        key = f"{co}, {st}" if st else co
        grouped.setdefault(key,[]).append(r)
    for k in grouped:
        grouped[k].sort(key=lambda x:(0 if x.get("is_estate") else 1,-x.get("score",0)))
    return grouped

def write_json(records, date_from, date_to):
    grouped = group_by_county(records)
    county_summary = []
    for key, recs in sorted(grouped.items()):
        county_summary.append({
            "key":       key,
            "total":     len(recs),
            "estates":   sum(1 for r in recs if r.get("is_estate")),
            "active":    sum(1 for r in recs if r.get("sale_type")=="SALE"),
            "struck":    sum(1 for r in recs if "STRUCK" in (r.get("sale_type") or "")),
            "future":    sum(1 for r in recs if "FUTURE" in (r.get("sale_type") or "")),
            "avg_score": round(sum(r.get("score",0) for r in recs)/len(recs),1) if recs else 0,
        })
    payload = {
        "fetched_at":     datetime.utcnow().isoformat()+"Z",
        "source":         "Linebarger Tax Sales (taxsales.lgbs.com) — All Counties",
        "date_range":     {"from":date_from,"to":date_to},
        "total":          len(records),
        "with_address":   sum(1 for r in records if r.get("prop_address")),
        "estates":        sum(1 for r in records if r.get("is_estate")),
        "high_score":     sum(1 for r in records if r.get("score",0)>=60),
        "county_count":   len(grouped),
        "county_summary": county_summary,
        "records":        records,
    }
    for path in [DASHBOARD_DIR/"records.json", DATA_DIR/"records.json"]:
        path.write_text(json.dumps(payload,indent=2,default=str),encoding="utf-8")
        log.info("Wrote %s", path)

def write_csv(records):
    path = DATA_DIR/"leads_export.csv"
    cols = ["First Name","Last Name","Owner Type","Is Estate",
            "Mailing Address","Mailing City","Mailing State","Mailing Zip",
            "Property Address","Property City","Property State","Property Zip",
            "County","State","Precinct","Account ID",
            "Sale Type","Sale Date","Amount Owed",
            "Seller Score","Motivated Seller Flags",
            "Legal Description","Source","Public Records URL"]
    with open(path,"w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in records:
            first,last = split_name(r.get("owner",""))
            w.writerow({
                "First Name":first,"Last Name":last,
                "Owner Type":r.get("owner_label",""),
                "Is Estate":"YES" if r.get("is_estate") else "NO",
                "Mailing Address":r.get("mail_address",""),
                "Mailing City":r.get("mail_city",""),
                "Mailing State":r.get("mail_state",""),
                "Mailing Zip":r.get("mail_zip",""),
                "Property Address":r.get("prop_address",""),
                "Property City":r.get("prop_city",""),
                "Property State":r.get("prop_state",""),
                "Property Zip":r.get("prop_zip",""),
                "County":r.get("county",""),"State":r.get("state",""),
                "Precinct":r.get("precinct",""),"Account ID":r.get("account_id",""),
                "Sale Type":r.get("sale_type",""),"Sale Date":r.get("sale_date",""),
                "Amount Owed":r.get("amount_owed","") or "",
                "Seller Score":r.get("score",0),
                "Motivated Seller Flags":"; ".join(r.get("flags",[])),
                "Legal Description":r.get("legal",""),
                "Source":"Linebarger Tax Sales",
                "Public Records URL":r.get("clerk_url",""),
            })
    log.info("Wrote CSV: %s (%d rows)", path, len(records))

async def main():
    if SALE_DATE_ONLY:
        date_from = date_to = SALE_DATE_ONLY
    else:
        today = datetime.now()
        date_to   = today.strftime("%Y-%m-%d")
        date_from = (today-timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")

    log.info("="*60)
    log.info("Linebarger Tax Sales — All Counties Edition")
    log.info("Date range  : %s → %s", date_from, date_to)
    log.info("State filter: %s", STATE_FILTER or "ALL STATES")
    log.info("County      : %s", COUNTY_FILTER or "ALL COUNTIES")
    log.info("="*60)

    records = await scrape_all(date_from, date_to)
    records = deduplicate(records)
    records = score_all(records)

    estates = sum(1 for r in records if r.get("is_estate"))
    log.info("ESTATES: %d | Total: %d", estates, len(records))

    write_json(records, date_from, date_to)
    write_csv(records)
    log.info("Done!")

if __name__ == "__main__":
    asyncio.run(main())
