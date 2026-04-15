"""
Motivated Seller Lead Scraper - Harris County, TX (Houston)
Scrapes: https://www.cclerk.hctx.net/applications/websearch/RP.aspx
Uses Selenium to handle ASP.NET form submission and pagination.
Sends Slack notifications for new 30+ leads daily.
"""

import json, time, logging, random, csv, io, urllib.parse, os
from datetime import datetime, date
from pathlib import Path
from dataclasses import dataclass, field, asdict
from urllib.request import urlopen, Request
from urllib.error import URLError

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
COUNTY_NAME   = "Harris County, TX"
SEARCH_URL    = "https://www.cclerk.hctx.net/applications/websearch/RP.aspx"
DASHBOARD_URL = "https://andrewwinter13-ops.github.io/Bexar-County-Intel/Harris/index.html"

# Date range to search
DATE_FROM = "01/01/2024"
DATE_TO   = datetime.utcnow().strftime("%m/%d/%Y")

MAX_PAGES        = 20
PAGE_LOAD_WAIT   = 20
BETWEEN_PAGES    = 3
NOTIFY_MIN_SCORE = 30

OUTPUT_DIR     = Path("Harris")
OUTPUT_FILE    = OUTPUT_DIR / "output.json"
DASHBOARD_FILE = OUTPUT_DIR / "index.html"
PREV_DOCS_FILE = OUTPUT_DIR / "prev_doc_numbers.json"

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

# ─────────────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────────────
SCORE_WEIGHTS = {
    "tax_delinquent": 30, "code_violation": 25,
    "probate_filing": 20, "multiple_liens": 30, "divorce_bankruptcy": 10,
}

DISTRESS_KEYWORDS = {
    "tax_delinquent":     ["tax lien", "federal tax lien", "state tax lien", "delinquent", "irs lien", "ftl"],
    "code_violation":     ["code violation", "notice of violation", "abatement", "nuisance"],
    "probate_filing":     ["probate", "estate of", "successor trustee", "letters testamentary", "heirship"],
    "multiple_liens":     ["mechanic lien", "judgment lien", "lis pendens", "notice of default", "ucc", "lien"],
    "divorce_bankruptcy": ["dissolution", "bankruptcy", "chapter 7", "chapter 13", "quitclaim"],
}

DISTRESS_DOC_TYPES = {
    "tax_delinquent":     ["TAX LIEN", "FED TAX", "STATE TAX", "IRS", "FTL"],
    "code_violation":     ["CODE VIOL", "VIOLATION"],
    "probate_filing":     ["PROBATE", "HEIRSHIP", "LETTERS TEST", "AFFIDAVIT"],
    "multiple_liens":     ["JUDGMENT", "LIS PENDENS", "MECHANIC LIEN", "UCC", "LIEN"],
    "divorce_bankruptcy": ["QUITCLAIM", "BANKRUPTCY", "DISSOLUTION"],
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class PropertyRecord:
    document_number:    str = ""
    file_date:          str = ""
    grantor:            str = ""
    grantee:            str = ""
    legal_description:  str = ""
    property_address:   str = ""
    doc_type:           str = ""
    film_code:          str = ""
    pages:              str = ""
    tax_delinquent:     bool = False
    code_violation:     bool = False
    probate_filing:     bool = False
    multiple_liens:     bool = False
    divorce_bankruptcy: bool = False
    seller_score:       int = 0
    days_on_record:     int = 0
    maps_url:           str = ""
    scraped_at:         str = field(default_factory=lambda: datetime.utcnow().isoformat())
    source_url:         str = ""


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def calc_days(file_date_str: str) -> int:
    if not file_date_str:
        return 0
    for fmt in ["%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"]:
        try:
            return (date.today() - datetime.strptime(file_date_str.strip(), fmt).date()).days
        except: continue
    return 0


def make_maps_url(name: str) -> str:
    q = urllib.parse.quote(f"{name}, Houston, TX")
    return f"https://www.google.com/maps/search/?api=1&query={q}"


def score_record(rec):
    searchable = " ".join([rec.grantor, rec.grantee, rec.legal_description, rec.doc_type]).lower()
    score = 0
    for signal, keywords in DISTRESS_KEYWORDS.items():
        if any(kw in searchable for kw in keywords):
            setattr(rec, signal, True)
            score += SCORE_WEIGHTS[signal]
    doc_upper = rec.doc_type.upper()
    for signal, doc_types in DISTRESS_DOC_TYPES.items():
        if any(dt in doc_upper for dt in doc_types):
            if not getattr(rec, signal):
                setattr(rec, signal, True)
                score += SCORE_WEIGHTS[signal]
    days = rec.days_on_record
    if days > 365: score += 15
    elif days > 180: score += 10
    elif days > 90: score += 5
    rec.seller_score = min(score, 100)


# ─────────────────────────────────────────────────────────────────────────────
# SLACK
# ─────────────────────────────────────────────────────────────────────────────
def slack_send(message: dict):
    if not SLACK_WEBHOOK:
        log.warning("No SLACK_WEBHOOK_URL — skipping Slack.")
        return
    try:
        data = json.dumps(message).encode("utf-8")
        req  = Request(SLACK_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
        urlopen(req, timeout=10)
        log.info("Slack sent.")
    except URLError as e:
        log.error(f"Slack error: {e}")


def slack_daily_summary(records, new_leads):
    total    = len(records)
    above_30 = sum(1 for r in records if r.seller_score >= NOTIFY_MIN_SCORE)
    hot      = sum(1 for r in records if r.seller_score >= 50)
    warm     = sum(1 for r in records if 25 <= r.seller_score < 50)
    today    = datetime.utcnow().strftime("%B %d, %Y")
    blocks = [
        {"type":"header","text":{"type":"plain_text","text":f"🏠 Harris County Lead Scraper — {today}"}},
        {"type":"section","fields":[
            {"type":"mrkdwn","text":f"*Total Records:*\n{total}"},
            {"type":"mrkdwn","text":f"*Score 30+:*\n{above_30}"},
            {"type":"mrkdwn","text":f"*🔥 Hot (50+):*\n{hot}"},
            {"type":"mrkdwn","text":f"*⚠️ Warm (25-49):*\n{warm}"},
            {"type":"mrkdwn","text":f"*🆕 New Today:*\n{len(new_leads)}"},
        ]},
        {"type":"divider"},
        {"type":"section","text":{"type":"mrkdwn","text":f"<{DASHBOARD_URL}|📊 Open Harris County Dashboard>"}}
    ]
    slack_send({"blocks": blocks})


def slack_new_lead_alerts(new_leads):
    if not new_leads:
        return
    lead_lines = []
    for r in new_leads[:10]:
        signals = []
        if r.tax_delinquent:    signals.append("Tax Lien")
        if r.code_violation:    signals.append("Code Viol.")
        if r.probate_filing:    signals.append("Probate")
        if r.multiple_liens:    signals.append("Multi-Lien")
        if r.divorce_bankruptcy: signals.append("Divorce/BK")
        sig_str   = " · ".join(signals) if signals else r.doc_type
        days_str  = f"{r.days_on_record}d old" if r.days_on_record else ""
        score_emoji = "🔥" if r.seller_score >= 50 else "⚠️"
        lead_lines.append(
            f"{score_emoji} *{r.grantor}* — Score: *{r.seller_score}*\n"
            f"   {r.doc_type} · Filed: {r.file_date} {days_str}\n"
            f"   Signals: {sig_str}\n"
            f"   <{r.maps_url}|🔍 Search in Maps>"
        )
    overflow = f"\n_...and {len(new_leads)-10} more. <{DASHBOARD_URL}|View all →>_" if len(new_leads) > 10 else ""
    blocks = [
        {"type":"header","text":{"type":"plain_text","text":f"🆕 {len(new_leads)} New Harris County Lead{'s' if len(new_leads)!=1 else ''} (Score 30+)"}},
        {"type":"section","text":{"type":"mrkdwn","text":"\n\n".join(lead_lines)+overflow}},
        {"type":"divider"},
        {"type":"section","text":{"type":"mrkdwn","text":f"<{DASHBOARD_URL}|📊 Open Harris County Dashboard>"}}
    ]
    slack_send({"blocks": blocks})


# ─────────────────────────────────────────────────────────────────────────────
# PREVIOUS RECORDS TRACKING
# ─────────────────────────────────────────────────────────────────────────────
def load_prev_doc_numbers() -> set:
    try:
        if PREV_DOCS_FILE.exists():
            with open(PREV_DOCS_FILE) as f:
                return set(json.load(f))
    except: pass
    return set()


def save_doc_numbers(records):
    with open(PREV_DOCS_FILE, "w") as f:
        json.dump([r.document_number for r in records if r.document_number], f)


def find_new_leads(records, prev_doc_numbers: set) -> list:
    return [r for r in records
            if r.seller_score >= NOTIFY_MIN_SCORE
            and r.document_number not in prev_doc_numbers]


# ─────────────────────────────────────────────────────────────────────────────
# SELENIUM SCRAPER
# ─────────────────────────────────────────────────────────────────────────────
def make_driver():
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36")
    driver = webdriver.Chrome(options=opts)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver


def fill_search_form(driver):
    """Fill in the Harris County search form and submit."""
    wait = WebDriverWait(driver, PAGE_LOAD_WAIT)

    # Wait for form to load
    wait.until(EC.presence_of_element_located((By.ID, "DateFrom")))
    time.sleep(2)

    try:
        # Clear and fill date from
        date_from = driver.find_element(By.ID, "DateFrom")
        date_from.clear()
        date_from.send_keys(DATE_FROM)

        # Clear and fill date to
        date_to = driver.find_element(By.ID, "DateTo")
        date_to.clear()
        date_to.send_keys(DATE_TO)

        # Try to set instrument type to common distress types
        # Leave blank to get all types, then filter by scoring
        log.info(f"Searching Harris County records from {DATE_FROM} to {DATE_TO}")

        # Click Search button
        search_btn = driver.find_element(By.ID, "btnSearch")
        driver.execute_script("arguments[0].click();", search_btn)
        time.sleep(3)

    except NoSuchElementException as e:
        log.warning(f"Form element not found: {e}")
        # Try alternative button IDs
        for btn_id in ["Button1", "cmdSearch", "SearchButton", "btnGo"]:
            try:
                btn = driver.find_element(By.ID, btn_id)
                btn.click()
                time.sleep(3)
                break
            except: continue


def extract_harris_rows(driver) -> list:
    """Extract records from Harris County results table."""
    records = []
    try:
        # Harris County table has columns:
        # File Number | File Date | Doc Type | Grantor | Grantee | Legal Description | Pages | Film Code
        rows = driver.find_elements(By.CSS_SELECTOR, "table tr")
        if not rows:
            rows = driver.find_elements(By.CSS_SELECTOR, "#GridView1 tr, .rgRow, .rgAltRow")

        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 4:
                    continue

                def cell(i, d=""):
                    try: return cells[i].text.strip() or d
                    except: return d

                # Harris County column order:
                # File Number | File Date | Doc Type | Grantor | Grantee | Legal Desc | Pages | Film Code
                file_num  = cell(0)
                file_date = cell(1)
                doc_type  = cell(2)
                grantor   = cell(3)
                grantee   = cell(4)
                legal     = cell(5)
                pages     = cell(6)
                film_code = cell(7)

                # Skip header rows
                if not file_num or file_num.lower() in ["file number", "file no", "#"]:
                    continue

                rec = PropertyRecord(
                    document_number=file_num,
                    file_date=file_date,
                    doc_type=doc_type,
                    grantor=grantor,
                    grantee=grantee,
                    legal_description=legal,
                    pages=pages,
                    film_code=film_code,
                    source_url=driver.current_url,
                    days_on_record=calc_days(file_date),
                    maps_url=make_maps_url(grantor),
                )

                if rec.grantor or rec.document_number:
                    score_record(rec)
                    records.append(rec)

            except Exception as e:
                log.debug(f"Row error: {e}")

    except Exception as e:
        log.error(f"Extract error: {e}")

    return records


def click_harris_next(driver) -> bool:
    """Click next page on Harris County results."""
    # Harris County uses ASP.NET paging
    next_selectors = [
        "a[href*='Page$Next']",
        "a[title='Next Page']",
        "a[href*='__doPostBack'][href*='Next']",
        ".rgPageNext",
        "input[value='Next']",
    ]
    for sel in next_selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(BETWEEN_PAGES)
                return True
        except: continue

    # Try finding by text
    try:
        links = driver.find_elements(By.TAG_NAME, "a")
        for link in links:
            if link.text.strip() in ["Next", "›", "»", "Next >"]:
                if link.is_displayed():
                    driver.execute_script("arguments[0].click();", link)
                    time.sleep(BETWEEN_PAGES)
                    return True
    except: pass

    return False


def scrape_harris_selenium():
    """Main scraper for Harris County records."""
    driver = make_driver()
    records = []

    try:
        log.info(f"Loading Harris County search: {SEARCH_URL}")
        driver.get(SEARCH_URL)
        time.sleep(5)

        # Fill and submit search form
        fill_search_form(driver)

        # Wait for results
        wait = WebDriverWait(driver, PAGE_LOAD_WAIT)
        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tr td")))
            time.sleep(2)
        except TimeoutException:
            log.warning("Timeout waiting for results.")
            with open("Harris/debug_page.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            return []

        for page_num in range(1, MAX_PAGES + 1):
            log.info(f"Scraping page {page_num}...")
            page_records = extract_harris_rows(driver)
            records.extend(page_records)
            log.info(f"  Page {page_num}: {len(page_records)} records (total: {len(records)})")

            if not page_records:
                log.info("No records on page, stopping.")
                break

            if not click_harris_next(driver):
                log.info("No next page.")
                break

            time.sleep(BETWEEN_PAGES)

    except Exception as e:
        log.error(f"Selenium error: {e}", exc_info=True)
    finally:
        driver.quit()

    return records


# ─────────────────────────────────────────────────────────────────────────────
# DEMO DATA
# ─────────────────────────────────────────────────────────────────────────────
ADDRESS_LOOKUP_MIN_SCORE = 70
HARRIS_GIS = "https://www.gis.hctx.net/arcgis/rest/services/HCAD/Parcels/MapServer/0/query"


def lookup_hcad_addresses(records):
    """
    Query Harris County GIS ArcGIS API for property addresses.
    No Selenium — pure JSON API calls.
    """
    targets = [r for r in records
               if r.seller_score >= ADDRESS_LOOKUP_MIN_SCORE
               and not r.property_address]

    if not targets:
        log.info("No 70+ records need address lookup.")
        return

    log.info(f"Harris GIS address lookup for {len(targets)} records scoring 70+...")
    import requests as req_module
    session = req_module.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    found = 0

    for i, rec in enumerate(targets):
        try:
            name = rec.grantor.strip().upper()
            last = name.split(",")[0].strip() if "," in name else name.split()[0].strip()
            last = last.replace("'", "''")
            params = {
                "where": f"owner_name_1 LIKE '{last}%'",
                "outFields": "owner_name_1,site_addr_1,site_addr_2,site_addr_3",
                "returnGeometry": "false",
                "f": "json",
                "resultRecordCount": 10,
            }
            resp = session.get(HARRIS_GIS, params=params, timeout=10)
            data = resp.json()
            addr = ""
            for feat in data.get("features", []):
                attrs = feat.get("attributes", {})
                owner = (attrs.get("owner_name_1") or "").upper()
                name_parts = [p for p in name.split() if len(p) > 2]
                if any(p in owner for p in name_parts[:2]):
                    parts = [attrs.get("site_addr_1",""), attrs.get("site_addr_2",""), attrs.get("site_addr_3","")]
                    addr = " ".join(p for p in parts if p).strip()
                    if addr and len(addr) > 5:
                        addr = addr.title()
                        break

            if addr:
                rec.property_address = addr
                q = urllib.parse.quote(f"{addr}, Houston, TX")
                rec.maps_url = f"https://www.google.com/maps/search/?api=1&query={q}"
                found += 1
                log.info(f"  [{i+1}/{len(targets)}] {rec.grantor} → {addr}")
            else:
                log.info(f"  [{i+1}/{len(targets)}] {rec.grantor} → not found")

            time.sleep(0.5)

        except Exception as e:
            log.debug(f"Harris GIS error {rec.grantor}: {e}")

    log.info(f"Harris GIS lookup complete: {found}/{len(targets)} found")

def generate_demo_records(n=50):
    firstnames = ["James","Maria","Robert","Linda","Michael","Patricia","William","Barbara","David","Elizabeth"]
    lastnames  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Wilson","Anderson","Rodriguez","Martinez"]
    companies  = ["WELLS FARGO BANK","INTERNAL REVENUE SERVICE","STATE OF TEXAS","JP MORGAN CHASE","QUICKEN LOANS","US BANK NA"]
    streets    = ["Westheimer Rd","Memorial Dr","Bellaire Blvd","Richmond Ave","Kirby Dr","Shepherd Dr","Montrose Blvd"]
    doc_types  = [
        ("DEED OF TRUST",False),("FEDERAL TAX LIEN",True),("STATE TAX LIEN",True),
        ("JUDGMENT LIEN",True),("DEED",False),("AFFIDAVIT OF HEIRSHIP",True),
        ("LIS PENDENS",True),("MECHANIC LIEN",True),("QUITCLAIM DEED",True),
    ]
    records = []
    for i in range(n):
        grantor  = (random.choice(companies) if random.random()>0.5 else
                    f"{random.choice(lastnames).upper()} {random.choice(firstnames).upper()}")
        grantee  = (random.choice(companies) if random.random()>0.6 else
                    f"{random.choice(lastnames).upper()} {random.choice(firstnames).upper()}")
        doc_type,_ = random.choice(doc_types)
        doc_num  = f"2024-{random.randint(100000,999999)}"
        days_ago = random.randint(30,800)
        filed    = date.fromordinal(date.today().toordinal()-days_ago)
        file_date = filed.strftime("%m/%d/%Y")
        address  = f"{random.randint(100,9999)} {random.choice(streets)}, Houston TX 770{random.randint(10,99)}"
        rec = PropertyRecord(
            document_number=doc_num, file_date=file_date, grantor=grantor,
            grantee=grantee, doc_type=doc_type,
            legal_description=f"Lot {random.randint(1,25)} Block {random.randint(1,15)} {random.choice(streets)} Sub",
            property_address=address, source_url=SEARCH_URL,
            days_on_record=days_ago, maps_url=make_maps_url(grantor),
        )
        score_record(rec)
        records.append(rec)
    records.sort(key=lambda r: r.seller_score, reverse=True)
    return records


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD  (same style as Bexar)
# ─────────────────────────────────────────────────────────────────────────────
def build_dashboard(records, new_lead_doc_numbers=None):
    if new_lead_doc_numbers is None:
        new_lead_doc_numbers = set()

    now_str   = datetime.utcnow().strftime("%-m/%-d/%Y, %-I:%M:%S %p")
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    total     = len(records)
    is_live   = any(r.source_url and "cclerk" in r.source_url for r in records)
    data_note = "LIVE DATA" if is_live else "DEMO DATA"

    type_map = {
        "FEDERAL TAX LIEN":"TAX","STATE TAX LIEN":"TAX","TAX LIEN":"TAX",
        "JUDGMENT LIEN":"JUD","JUDGMENT":"JUD","LIS PENDENS":"LIS",
        "NOTICE OF DEFAULT":"NOD","MECHANIC LIEN":"MECH","DEED OF TRUST":"DOT",
        "QUITCLAIM":"QCD","PROBATE":"PRO","AFFIDAVIT":"AFF","UCC":"UCC",
    }

    rows_data = []
    for r in records:
        code = "OTH"
        for k,v in type_map.items():
            if k in r.doc_type.upper():
                code = v; break
        signals = []
        if r.tax_delinquent:    signals.append("Tax lien")
        if r.code_violation:    signals.append("Code violation")
        if r.probate_filing:    signals.append("Probate / estate")
        if r.multiple_liens:    signals.append("Judgment lien")
        if r.divorce_bankruptcy: signals.append("Lis pendens")
        rows_data.append({
            "score": r.seller_score, "doc_type": r.doc_type, "code": code,
            "doc_num": r.document_number, "filed": r.file_date, "days": r.days_on_record,
            "grantor": r.grantor, "grantee": r.grantee,
            "address": r.property_address or r.legal_description[:60],
            "legal": r.legal_description, "maps_url": r.maps_url,
            "signals": signals,
            "is_new": r.document_number in new_lead_doc_numbers,
            "this_week": r.days_on_record <= 7,
            "film_code": r.film_code,
        })

    tax_count  = sum(1 for r in rows_data if r["code"]=="TAX")
    jud_count  = sum(1 for r in rows_data if r["code"]=="JUD")
    lis_count  = sum(1 for r in rows_data if r["code"]=="LIS")
    nod_count  = sum(1 for r in rows_data if r["code"]=="NOD")
    new_count  = sum(1 for r in rows_data if r["is_new"])
    week_count = sum(1 for r in rows_data if r["this_week"])
    rows_json  = json.dumps(rows_data)
    csv_data   = build_csv(records).replace("\\","\\\\").replace("`","'")

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Harris County Motivated Sellers</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@400;500&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
:root{{--bg:#0e0e0e;--surface:#161616;--border:#2a2a2a;--text:#f0f0f0;--muted:#666;--accent:#f5a623;--purple:#a855f7;--orange:#f97316;--red:#ef4444;--green:#22c55e;--blue:#3b82f6;}}
body{{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;display:flex;flex-direction:column;min-height:100vh;}}
.topbar{{display:flex;align-items:center;justify-content:space-between;padding:.5rem 1.5rem;border-bottom:1px solid var(--border);background:#111;}}
.brand{{font-family:'Bebas Neue',sans-serif;font-size:1.1rem;letter-spacing:.15em;}}
.brand span{{color:var(--accent);}}
.updated{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);}}
.updated::before{{content:'● ';color:var(--green);}}
.db-badge{{font-family:'DM Mono',monospace;font-size:10px;padding:2px 8px;border-radius:4px;background:{'rgba(34,197,94,.15)' if is_live else 'rgba(234,179,8,.15)'};color:{'#86efac' if is_live else '#fde047'};border:1px solid {'rgba(34,197,94,.3)' if is_live else 'rgba(234,179,8,.3)'};}}
.export-btn{{background:var(--accent);color:#000;border:none;border-radius:5px;font-family:'DM Mono',monospace;font-size:11px;font-weight:500;padding:.4rem 1rem;cursor:pointer;}}
.export-btn:hover{{background:#e09510;}}
.hero{{padding:1.5rem 1.5rem 1rem;border-bottom:1px solid var(--border);}}
.hero h1{{font-family:'Bebas Neue',sans-serif;font-size:clamp(2.5rem,6vw,4rem);line-height:.95;color:var(--accent);letter-spacing:.05em;}}
.hero h2{{font-family:'Bebas Neue',sans-serif;font-size:clamp(2.5rem,6vw,4rem);line-height:.95;color:var(--text);letter-spacing:.05em;margin-bottom:.75rem;}}
.hero-meta{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);}}
.total-badge{{float:right;text-align:right;}}
.total-num{{font-family:'Bebas Neue',sans-serif;font-size:3rem;color:var(--accent);line-height:1;}}
.total-lbl{{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:.15em;text-transform:uppercase;}}
.stats-bar{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));border-bottom:1px solid var(--border);}}
.stat-cell{{padding:.9rem 1.5rem;border-right:1px solid var(--border);}}
.stat-cell:last-child{{border-right:none;}}
.stat-num{{font-family:'Bebas Neue',sans-serif;font-size:2.2rem;line-height:1;}}
.stat-lbl{{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase;margin-top:2px;}}
.purple{{color:var(--purple);}}.orange{{color:var(--orange);}}.red{{color:var(--red);}}.blue{{color:var(--blue);}}.green{{color:var(--green);}}.accent{{color:var(--accent);}}
.filter-row{{display:flex;align-items:center;gap:.5rem;padding:.75rem 1.5rem;border-bottom:1px solid var(--border);flex-wrap:wrap;background:#111;}}
.search-input{{background:#1a1a1a;border:1px solid var(--border);border-radius:5px;color:var(--text);font-size:12px;padding:.4rem .8rem;width:200px;outline:none;}}
.search-input:focus{{border-color:var(--accent);}}.search-input::placeholder{{color:var(--muted);}}
select{{background:#1a1a1a;border:1px solid var(--border);border-radius:5px;color:var(--text);font-size:12px;padding:.4rem .8rem;outline:none;cursor:pointer;}}
.chip{{background:#1a1a1a;border:1px solid var(--border);border-radius:20px;color:var(--muted);cursor:pointer;font-size:11px;padding:.3rem .85rem;white-space:nowrap;transition:all .15s;}}
.chip:hover,.chip.active{{background:rgba(245,166,35,.12);border-color:var(--accent);color:var(--accent);}}
.chip.new-chip{{border-color:var(--purple);color:var(--purple);}}
.chip.new-chip.active{{background:rgba(168,85,247,.15);}}
.main{{display:flex;flex:1;overflow:hidden;}}
.table-pane{{flex:1;overflow:auto;}}
.detail-pane{{width:320px;border-left:1px solid var(--border);background:#111;overflow-y:auto;flex-shrink:0;display:none;}}
.detail-pane.open{{display:block;}}
table{{width:100%;border-collapse:collapse;}}
thead th{{background:#111;border-bottom:1px solid var(--border);color:var(--muted);font-family:'DM Mono',monospace;font-size:10px;font-weight:500;padding:.6rem 1rem;text-align:left;text-transform:uppercase;letter-spacing:.08em;white-space:nowrap;position:sticky;top:0;z-index:10;}}
tbody tr{{border-bottom:1px solid #1a1a1a;cursor:pointer;transition:background .1s;}}
tbody tr:hover{{background:#1a1a1a;}}tbody tr.selected{{background:#1f1a0e;border-left:2px solid var(--accent);}}tbody tr.hidden{{display:none;}}
td{{padding:.55rem 1rem;vertical-align:middle;}}
.sc{{width:36px;height:36px;border-radius:50%;border:2px solid;display:flex;align-items:center;justify-content:center;font-family:'Bebas Neue',sans-serif;font-size:16px;margin:0 auto;}}
.sc.hot{{border-color:var(--accent);color:var(--accent);}}.sc.warm{{border-color:var(--orange);color:var(--orange);}}.sc.cold{{border-color:var(--muted);color:var(--muted);}}
.type-badge{{border-radius:4px;font-family:'DM Mono',monospace;font-size:10px;font-weight:500;padding:3px 8px;white-space:nowrap;display:inline-block;}}
.tb-TAX{{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.3);}}
.tb-JUD{{background:rgba(168,85,247,.15);color:#d8b4fe;border:1px solid rgba(168,85,247,.3);}}
.tb-LIS{{background:rgba(249,115,22,.15);color:#fdba74;border:1px solid rgba(249,115,22,.3);}}
.tb-NOD{{background:rgba(239,68,68,.2);color:#fca5a5;border:1px solid rgba(239,68,68,.4);}}
.tb-MECH{{background:rgba(59,130,246,.15);color:#93c5fd;border:1px solid rgba(59,130,246,.3);}}
.tb-OTH,.tb-UCC,.tb-AFF,.tb-DEED,.tb-QCD,.tb-PRO,.tb-DOT{{background:rgba(255,255,255,.06);color:#aaa;border:1px solid rgba(255,255,255,.12);}}
.code-cell,.filed-cell,.amt-cell{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);white-space:nowrap;}}
.owner-cell{{font-weight:500;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.grantor-cell{{color:#aaa;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.addr-cell{{color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;}}
.lookup-btns{{display:flex;flex-direction:column;gap:3px;}}
.maps-link{{color:#60a5fa;text-decoration:none;font-size:11px;white-space:nowrap;}}
.maps-link:hover{{color:#93c5fd;}}
.cad-link{{color:#86efac;text-decoration:none;font-size:11px;white-space:nowrap;}}
.cad-link:hover{{color:#4ade80;}}
.new-tag{{display:inline-block;background:rgba(168,85,247,.18);color:#c4b5fd;border:1px solid rgba(168,85,247,.35);border-radius:3px;font-size:9px;font-family:'DM Mono',monospace;padding:1px 5px;margin-left:4px;}}
.week-tag{{display:inline-block;background:rgba(245,166,35,.15);color:var(--accent);border:1px solid rgba(245,166,35,.3);border-radius:3px;font-size:9px;font-family:'DM Mono',monospace;padding:1px 5px;margin-left:4px;}}
.detail-header{{display:flex;align-items:center;justify-content:space-between;padding:1rem;border-bottom:1px solid var(--border);}}
.detail-title{{font-family:'DM Mono',monospace;font-size:12px;color:var(--accent);letter-spacing:.1em;text-transform:uppercase;}}
.close-btn{{background:none;border:none;color:var(--muted);cursor:pointer;font-size:18px;}}
.close-btn:hover{{color:var(--text);}}
.detail-score{{text-align:center;padding:1.5rem 1rem 1rem;border-bottom:1px solid var(--border);}}
.big-score{{width:80px;height:80px;border-radius:50%;border:3px solid var(--accent);display:flex;align-items:center;justify-content:center;font-family:'Bebas Neue',sans-serif;font-size:2.5rem;color:var(--accent);margin:0 auto .75rem;}}
.detail-name{{font-weight:600;font-size:15px;text-align:center;}}
.detail-sub{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);text-align:center;margin-top:4px;}}
.detail-chips{{display:flex;flex-wrap:wrap;gap:5px;justify-content:center;margin-top:.75rem;}}
.detail-chip{{border-radius:3px;font-family:'DM Mono',monospace;font-size:10px;padding:2px 8px;}}
.section-title{{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:.12em;text-transform:uppercase;padding:.75rem 1rem .4rem;border-top:1px solid var(--border);}}
.detail-row{{display:flex;justify-content:space-between;align-items:flex-start;padding:.35rem 1rem;gap:1rem;}}
.detail-key{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);white-space:nowrap;flex-shrink:0;}}
.detail-val{{font-size:12px;text-align:right;word-break:break-word;}}
.detail-link{{color:var(--blue);text-decoration:none;font-size:12px;}}
.detail-link:hover{{color:#60a5fa;}}
.underwrite-btn{{display:block;margin:1rem;background:var(--green);color:#000;border:none;border-radius:6px;padding:.75rem;font-size:13px;font-weight:600;cursor:pointer;text-align:center;width:calc(100% - 2rem);}}
.underwrite-btn:hover{{background:#16a34a;color:#fff;}}
</style></head><body>

<div class="topbar">
  <div class="brand"><span>HARRIS</span>LEADS</div>
  <div class="updated">Updated {now_str}</div>
  <div class="db-badge">{data_note}</div>
  <button class="export-btn" onclick="exportCSV()">⬇ EXPORT GHL CSV</button>
</div>

<div class="hero">
  <div class="total-badge"><div class="total-num">{total}</div><div class="total-lbl">Total Leads</div></div>
  <h1>HARRIS COUNTY</h1>
  <h2>MOTIVATED SELLERS</h2>
  <div class="hero-meta">cclerk.hctx.net &nbsp;·&nbsp; {DATE_FROM} – {DATE_TO} &nbsp;·&nbsp; Generated {today_str}</div>
</div>

<div class="stats-bar">
  <div class="stat-cell"><div class="stat-num purple">{jud_count}</div><div class="stat-lbl">Judgment</div></div>
  <div class="stat-cell"><div class="stat-num orange">{tax_count}</div><div class="stat-lbl">Tax Lien</div></div>
  <div class="stat-cell"><div class="stat-num red">{lis_count}</div><div class="stat-lbl">Lis Pendens</div></div>
  <div class="stat-cell"><div class="stat-num blue">{nod_count}</div><div class="stat-lbl">Notice of Default</div></div>
  <div class="stat-cell"><div class="stat-num green">{new_count}</div><div class="stat-lbl">New Leads</div></div>
  <div class="stat-cell"><div class="stat-num accent">{week_count}</div><div class="stat-lbl">This Week</div></div>
</div>

<div class="filter-row">
  <input class="search-input" type="text" placeholder="Owner, address, doc #..." oninput="applyFilters()">
  <select onchange="applyFilters()" id="typeSel">
    <option value="">All Types</option>
    <option value="TAX">Tax Lien</option>
    <option value="JUD">Judgment</option>
    <option value="LIS">Lis Pendens</option>
    <option value="NOD">Notice of Default</option>
    <option value="MECH">Mechanic Lien</option>
    <option value="PRO">Probate</option>
  </select>
  <select onchange="applyFilters()" id="scoreSel">
    <option value="0">Any Score</option>
    <option value="30">30+</option>
    <option value="50">50+</option>
    <option value="70">70+</option>
  </select>
  <button class="chip active" onclick="setChip('all',this)">All</button>
  <button class="chip" onclick="setChip('LIS',this)">Lis pendens</button>
  <button class="chip" onclick="setChip('NOD',this)">Pre-foreclosure</button>
  <button class="chip" onclick="setChip('JUD',this)">Judgment lien</button>
  <button class="chip" onclick="setChip('TAX',this)">Tax lien</button>
  <button class="chip" onclick="setChip('MECH',this)">Mechanic lien</button>
  <button class="chip" onclick="setChip('PRO',this)">Probate / estate</button>
  <button class="chip new-chip" onclick="setChip('new',this)">New this week</button>
</div>

<div class="main">
  <div class="table-pane">
    <table>
      <thead><tr>
        <th>Score</th><th>Type</th><th>Code</th><th>Filed</th>
        <th>Property Owner</th><th>Grantor / Plaintiff</th>
        <th>Property Address</th><th>Lookup</th><th>Amount</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
  <div class="detail-pane" id="detailPane">
    <div class="detail-header">
      <div class="detail-title">Lead Detail</div>
      <button class="close-btn" onclick="closeDetail()">✕</button>
    </div>
    <div class="detail-score">
      <div class="big-score" id="d-score">—</div>
      <div class="detail-name" id="d-name">—</div>
      <div class="detail-sub" id="d-sub">—</div>
      <div class="detail-chips" id="d-chips"></div>
    </div>
    <div class="section-title">Document</div>
    <div class="detail-row"><span class="detail-key">Doc Number</span><span class="detail-val" id="d-docnum">—</span></div>
    <div class="detail-row"><span class="detail-key">Type</span><span class="detail-val" id="d-type">—</span></div>
    <div class="detail-row"><span class="detail-key">Filed</span><span class="detail-val" id="d-filed">—</span></div>
    <div class="detail-row"><span class="detail-key">Days on Record</span><span class="detail-val" id="d-days">—</span></div>
    <div class="detail-row"><span class="detail-key">Film Code</span><span class="detail-val" id="d-film">—</span></div>
    <div class="detail-row"><span class="detail-key">Property Owner</span><span class="detail-val" id="d-owner">—</span></div>
    <div class="detail-row"><span class="detail-key">Grantor / Plaintiff</span><span class="detail-val" id="d-grantor">—</span></div>
    <div class="detail-row"><span class="detail-key">Legal Desc</span><span class="detail-val" id="d-legal">—</span></div>
    <div class="section-title">Links</div>
    <div class="detail-row"><span class="detail-key">Maps</span><span class="detail-val"><a class="detail-link" id="d-maps" href="#" target="_blank">Search in Google Maps →</a></span></div>
    <div class="section-title">Actions</div>
    <button class="underwrite-btn" onclick="underwrite()">🏠 UNDERWRITE THIS PROPERTY</button>
  </div>
</div>

<script>
const allRows = {rows_json};
let activeChip = 'all', selectedIdx = null;

function scoreClass(s){{ return s>=50?'hot':s>=30?'warm':'cold'; }}

document.getElementById('tbody').innerHTML = allRows.map((r,i) => {{
  const sc = scoreClass(r.score);
  const newTag = r.is_new ? '<span class="new-tag">NEW</span>' : '';
  const weekTag = r.this_week ? '<span class="week-tag">New this week</span>' : '';
  return `<tr data-idx="${{i}}" onclick="selectRow(${{i}})">
    <td><div class="sc ${{sc}}">${{r.score}}</div></td>
    <td><span class="type-badge tb-${{r.code}}">${{r.doc_type.length>18?r.doc_type.slice(0,18)+'…':r.doc_type}}</span></td>
    <td class="code-cell">${{r.code}}</td>
    <td class="filed-cell">${{r.filed}}</td>
    <td class="owner-cell">${{r.grantor}}${{newTag}}</td>
    <td class="grantor-cell">${{r.grantee||'—'}}</td>
    <td class="addr-cell">${{r.address||'—'}}${{weekTag}}</td>
    <td><div class="lookup-btns">
      <a href="${{r.maps_url}}" target="_blank" class="maps-link">📍 Maps</a>
      <a href="https://public.hcad.org/records/details.asp?searchtype=ownername&search_term=${{encodeURIComponent(r.grantor)}}" target="_blank" class="cad-link">🏠 HCAD</a>
    </div></td>
    <td class="amt-cell">—</td>
  </tr>`;
}}).join('');

function applyFilters(){{
  const q = document.querySelector('.search-input').value.toLowerCase();
  const type = document.getElementById('typeSel').value;
  const score = parseInt(document.getElementById('scoreSel').value)||0;
  document.querySelectorAll('#tbody tr').forEach((row,i) => {{
    const r = allRows[i]; if(!r) return;
    const matchQ = !q||(r.grantor+r.grantee+r.address+r.doc_num).toLowerCase().includes(q);
    const matchType = !type||r.code===type;
    const matchScore = r.score>=score;
    const matchChip = activeChip==='all'||(activeChip==='new'?r.this_week:r.code===activeChip);
    row.classList.toggle('hidden',!(matchQ&&matchType&&matchScore&&matchChip));
  }});
}}

function setChip(val,btn){{
  activeChip=val;
  document.querySelectorAll('.chip').forEach(c=>c.classList.remove('active'));
  btn.classList.add('active');
  applyFilters();
}}

function selectRow(idx){{
  selectedIdx=idx; const r=allRows[idx];
  document.querySelectorAll('#tbody tr').forEach(tr=>tr.classList.remove('selected'));
  document.querySelector(`#tbody tr[data-idx="${{idx}}"]`)?.classList.add('selected');
  const sc=scoreClass(r.score);
  const scoreEl=document.getElementById('d-score');
  scoreEl.textContent=r.score;
  scoreEl.style.borderColor=sc==='hot'?'#f5a623':sc==='warm'?'#f97316':'#666';
  scoreEl.style.color=sc==='hot'?'#f5a623':sc==='warm'?'#f97316':'#666';
  document.getElementById('d-name').textContent=r.grantor||'—';
  document.getElementById('d-sub').textContent=r.doc_type+(r.code?' · '+r.code:'');
  document.getElementById('d-docnum').textContent=r.doc_num||'—';
  document.getElementById('d-type').textContent=r.doc_type||'—';
  document.getElementById('d-filed').textContent=r.filed||'—';
  document.getElementById('d-days').textContent=r.days?r.days+' days':'—';
  document.getElementById('d-film').textContent=r.film_code||'—';
  document.getElementById('d-owner').textContent=r.grantor||'—';
  document.getElementById('d-grantor').textContent=r.grantee||'—';
  document.getElementById('d-legal').textContent=r.legal||'—';
  const mapsEl=document.getElementById('d-maps');
  if(r.maps_url){{mapsEl.href=r.maps_url;mapsEl.style.display='';}}
  else{{mapsEl.style.display='none';}}
  document.getElementById('d-chips').innerHTML=r.signals.map(s=>`<span class="detail-chip type-badge tb-${{r.code}}">${{s}}</span>`).join('')+(r.is_new?'<span class="detail-chip" style="background:rgba(168,85,247,.2);color:#d8b4fe;border:1px solid rgba(168,85,247,.3);">NEW</span>':'')+(r.this_week?'<span class="detail-chip" style="background:rgba(245,166,35,.15);color:#f5a623;border:1px solid rgba(245,166,35,.3);">New this week</span>':'');
  document.getElementById('detailPane').classList.add('open');
}}

function closeDetail(){{
  document.getElementById('detailPane').classList.remove('open');
  document.querySelectorAll('#tbody tr').forEach(tr=>tr.classList.remove('selected'));
  selectedIdx=null;
}}

function underwrite(){{
  const r=allRows[selectedIdx]; if(!r) return;
  window.open('https://www.google.com/search?q='+encodeURIComponent((r.grantor||'')+' '+(r.address||'')+' Houston TX'),'_blank');
}}

const csvData=`{csv_data}`;
function exportCSV(){{
  const blob=new Blob([csvData],{{type:'text/csv'}});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');a.href=url;a.download='harris-leads-{today_str}.csv';a.click();
  URL.revokeObjectURL(url);
}}
</script></body></html>"""


def build_csv(records) -> str:
    output = io.StringIO()
    fields = ["document_number","file_date","days_on_record","doc_type","grantor","grantee",
              "legal_description","property_address","seller_score","tax_delinquent",
              "code_violation","probate_filing","multiple_liens","divorce_bankruptcy","maps_url","film_code"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for r in records:
        writer.writerow({f: getattr(r,f,"") for f in fields})
    return output.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    prev_doc_numbers = load_prev_doc_numbers()
    log.info(f"Previously seen: {len(prev_doc_numbers)} records")

    log.info(f"Starting scrape of {COUNTY_NAME}...")
    try:
        records = scrape_harris_selenium()
    except Exception as e:
        log.error(f"Scrape failed: {e}")
        records = []

    if not records:
        log.warning("No live records — using demo data.")
        records = generate_demo_records(50)
    else:
        records.sort(key=lambda r: r.seller_score, reverse=True)
        log.info(f"SUCCESS — {len(records)} real records!")

    # Look up real addresses for 70+ scored records
    lookup_hcad_addresses(records)

    new_leads = find_new_leads(records, prev_doc_numbers)
    new_lead_doc_numbers = {r.document_number for r in new_leads}
    log.info(f"New leads (30+): {len(new_leads)}")

    save_doc_numbers(records)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "county": COUNTY_NAME,
            "generated_at": datetime.utcnow().isoformat(),
            "total_records": len(records),
            "live_data": any(r.source_url and "cclerk" in r.source_url for r in records),
            "new_leads_count": len(new_leads),
            "records": [asdict(r) for r in records],
        }, f, indent=2)
    log.info("JSON saved.")

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(build_dashboard(records, new_lead_doc_numbers))
    log.info("Dashboard saved.")

    slack_daily_summary(records, new_leads)
    if new_leads:
        slack_new_lead_alerts(new_leads)

    hot  = sum(1 for r in records if r.seller_score>=50)
    warm = sum(1 for r in records if 25<=r.seller_score<50)
    log.info(f"Hot: {hot}  Warm: {warm}  Cold: {len(records)-hot-warm}")


if __name__ == "__main__":
    main()
