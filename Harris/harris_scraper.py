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

# Date range — from Jan 1 of current year to today, grows daily
DATE_TO   = datetime.utcnow().strftime("%m/%d/%Y")
DATE_FROM = datetime.utcnow().strftime("%m/01/%Y").replace(
    datetime.utcnow().strftime("%m"), "01")  # Jan 1 of this year
DATE_FROM = f"01/01/{datetime.utcnow().year}"


MAX_PAGES        = 10
PAGE_LOAD_WAIT   = 10
BETWEEN_PAGES    = 1.5
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
            log.warning("Timeout waiting for results — saving debug page.")
            with open("Harris/debug_page.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            return []

        # Save debug on first run to verify table structure
        with open("Harris/debug_page.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        log.info("Saved debug_page.html")

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

import re as _re

def _parse_lot_block(legal_desc: str):
    legal = legal_desc.upper()
    lot_m = _re.search(r'\bLO?T[:\s]+(\d+[A-Z]?)', legal)
    blk_m = _re.search(r'\bBL?O?C?K?[:\s]+(\d+[A-Z]?)', legal)
    return (lot_m.group(1) if lot_m else None,
            blk_m.group(1) if blk_m else None)


def lookup_hcad_addresses(records):
    """Query Harris County GIS API using Lot/Block from legal description."""
    targets = [r for r in records
               if r.seller_score >= ADDRESS_LOOKUP_MIN_SCORE
               and not r.property_address
               and r.legal_description]

    if not targets:
        log.info("No 70+ records need address lookup.")
        return

    log.info(f"Harris GIS address lookup for {len(targets)} records scoring 70+...")
    import requests as req_mod
    session = req_mod.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    found = 0

    for i, rec in enumerate(targets):
        try:
            lot, blk = _parse_lot_block(rec.legal_description)
            if not lot or not blk:
                log.debug(f"  No lot/block: {rec.legal_description[:50]}")
                continue

            params = {
                "where": f"lot = '{lot}' AND block = '{blk}'",
                "outFields": "lot,block,site_addr_1,site_addr_2,site_addr_3",
                "returnGeometry": "false",
                "f": "json",
                "resultRecordCount": 5,
            }
            resp = session.get(HARRIS_GIS, params=params, timeout=10)
            data = resp.json()

            if "error" in data:
                log.debug(f"  Harris GIS error: {data['error']}")
                continue

            addr = ""
            for feat in data.get("features", []):
                attrs = feat.get("attributes", {})
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
                log.info(f"  [{i+1}/{len(targets)}] Lot {lot} Blk {blk} → {addr}")
            else:
                log.info(f"  [{i+1}/{len(targets)}] Lot {lot} Blk {blk} → not found")

            time.sleep(0.3)

        except Exception as e:
            log.debug(f"Harris GIS error {rec.grantor}: {e}")

    log.info(f"Harris GIS complete: {found}/{len(targets)} addresses found")

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

    def sc(s): return "#ef4444" if s>=50 else "#f97316" if s>=25 else "#22c55e"
    def sl(s): return "HOT" if s>=50 else "WARM" if s>=25 else "COLD"
    def dl(d):
        if not d: return "—"
        if d>365: return f'<span class="days hot-age">{d}d</span>'
        if d>180: return f'<span class="days warm-age">{d}d</span>'
        return f'<span class="days">{d}d</span>'
    def sigs(r):
        s=[]
        if r.tax_delinquent:     s.append("Tax Lien")
        if r.code_violation:     s.append("Code Viol.")
        if r.probate_filing:     s.append("Probate")
        if r.multiple_liens:     s.append("Multi-Lien")
        if r.divorce_bankruptcy: s.append("Divorce/BK")
        return " ".join(f'<span class="badge badge-active">{x}</span>' for x in s) or "—"

    rows = ""
    for i, r in enumerate(records):
        nb = '<span class="new-badge">NEW</span>' if r.document_number in new_lead_doc_numbers else ""
        maps_btn = f'<a href="{r.maps_url}" target="_blank" class="maps-link">📍 Maps</a>' if r.maps_url else "—"
        ml = f'<div class="lookup-btns">{maps_btn}</div>'
        addr = r.property_address if r.property_address else (r.legal_description[:40]+"…" if len(r.legal_description)>40 else r.legal_description)
        rows += (f'<tr class="record-row {"hot-row" if r.seller_score>=50 else ""}" '
                 f'data-score="{r.seller_score}" '
                 f'data-text="{(r.grantor+r.grantee+r.doc_type+(r.property_address or "")).lower().replace(chr(34),"")}"> '
                 f'<td class="rank">#{i+1}{nb}</td>'
                 f'<td><div class="score-circle" style="--c:{sc(r.seller_score)}">{r.seller_score}</div>'
                 f'<div class="slbl" style="color:{sc(r.seller_score)}">{sl(r.seller_score)}</div></td>'
                 f'<td class="mono sm">{r.document_number}</td>'
                 f'<td class="nowrap sm">{r.file_date}</td>'
                 f'<td>{dl(r.days_on_record)}</td>'
                 f'<td class="nowrap bold">{r.doc_type}</td>'
                 f'<td class="name" title="{r.grantor}">{r.grantor}</td>'
                 f'<td class="name" title="{r.grantee}">{r.grantee}</td>'
                 f'<td class="address-cell" title="{addr}">{addr}</td>'
                 f'<td>{ml}</td>'
                 f'<td>{sigs(r)}</td></tr>')

    total   = len(records)
    hot     = sum(1 for r in records if r.seller_score>=50)
    warm    = sum(1 for r in records if 25<=r.seller_score<50)
    cold    = total-hot-warm
    above30 = sum(1 for r in records if r.seller_score>=30)
    avg     = round(sum(r.seller_score for r in records)/total,1) if total else 0
    addrs   = sum(1 for r in records if r.property_address)
    gen     = datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")
    today   = datetime.utcnow().strftime("%Y-%m-%d")
    live    = any(r.source_url and "cclerk" in r.source_url for r in records)
    dnote   = "LIVE DATA" if live else "DEMO DATA"
    nd      = json.dumps(list(new_lead_doc_numbers))
    csv_d   = build_csv(records).replace("\\","\\\\").replace("`","'")

    import random as _r; _r.seed(42)
    map_data = []
    for r in [x for x in records if x.seller_score>=25]:
        lat=round(29.6+_r.uniform(0,.35),5); lng=round(-95.65+_r.uniform(0,.45),5)
        map_data.append({"name":r.grantor,"score":r.seller_score,"doc_type":r.doc_type,
                         "date":r.file_date,"days":r.days_on_record,"maps_url":r.maps_url,
                         "address":r.property_address or "","lat":lat,"lng":lng})
    mj = json.dumps(map_data)

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Harris County Motivated Seller Leads</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@700;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0b0d12;--surface:#13161f;--border:#1e2233;--text:#e2e8f0;--muted:#64748b;--accent:#6366f1;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;}}
header{{background:linear-gradient(135deg,#0f1117,#13161f);border-bottom:1px solid var(--border);padding:1.5rem 2rem;}}
.hi{{max-width:1800px;margin:0 auto;display:flex;align-items:flex-start;justify-content:space-between;gap:2rem;flex-wrap:wrap;}}
.eyebrow{{font-family:'DM Mono',monospace;font-size:10px;color:var(--accent);letter-spacing:.2em;text-transform:uppercase;}}
h1{{font-family:'Syne',sans-serif;font-size:1.9rem;font-weight:800;background:linear-gradient(135deg,#e2e8f0 30%,#f97316);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}}
.sub{{font-size:12px;color:var(--muted);margin-top:.2rem;font-family:'DM Mono',monospace;}}
.db{{display:inline-block;font-family:'DM Mono',monospace;font-size:10px;padding:2px 8px;border-radius:4px;margin-top:5px;
  background:{"rgba(34,197,94,.15)" if live else "rgba(234,179,8,.15)"};
  color:{"#86efac" if live else "#fde047"};
  border:1px solid {"rgba(34,197,94,.3)" if live else "rgba(234,179,8,.3)"};}}
.addr-badge{{display:inline-block;font-family:'DM Mono',monospace;font-size:10px;padding:2px 8px;border-radius:4px;margin-top:5px;margin-left:6px;background:rgba(249,115,22,.15);color:#fdba74;border:1px solid rgba(249,115,22,.3);}}
.stats{{display:flex;gap:.75rem;flex-wrap:wrap;}}
.sc{{background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:10px;padding:.75rem 1.2rem;min-width:90px;text-align:center;}}
.sc .n{{font-family:'Syne',sans-serif;font-size:1.6rem;font-weight:800;line-height:1;}}
.sc .l{{font-size:10px;color:var(--muted);margin-top:.2rem;text-transform:uppercase;letter-spacing:.1em;}}
.nh{{color:#ef4444;}}.nw{{color:#f97316;}}.nc{{color:#22c55e;}}.nt{{color:var(--accent);}}.na{{color:#94a3b8;}}.n30{{color:#a78bfa;}}
.toolbar{{max-width:1800px;margin:1rem auto 0;padding:0 2rem;display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;}}
.fb{{background:var(--surface);border:1px solid var(--border);border-radius:7px;color:var(--muted);cursor:pointer;font-family:'DM Mono',monospace;font-size:11px;padding:.4rem .9rem;transition:all .15s;}}
.fb:hover,.fb.active{{border-color:var(--accent);color:var(--text);background:rgba(99,102,241,.1);}}
.sb{{background:var(--surface);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:12px;padding:.4rem .9rem;width:180px;outline:none;}}
.sb:focus{{border-color:var(--accent);}}.sb::placeholder{{color:var(--muted);}}
.map-btn{{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);border-radius:7px;color:#86efac;cursor:pointer;font-family:'DM Mono',monospace;font-size:11px;padding:.4rem .9rem;}}
.map-btn:hover{{background:rgba(34,197,94,.2);}}
.exp-btn{{margin-left:auto;background:rgba(249,115,22,.12);border:1px solid rgba(249,115,22,.35);border-radius:7px;color:#fdba74;cursor:pointer;font-family:'DM Mono',monospace;font-size:11px;padding:.4rem .9rem;}}
.exp-btn:hover{{background:rgba(249,115,22,.22);}}
.map-wrap{{max-width:1800px;margin:.75rem auto 0;padding:0 2rem;display:none;}}
.map-wrap.show{{display:block;}}
.map-note{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);margin-bottom:.5rem;}}
#map{{width:100%;height:460px;border-radius:10px;border:1px solid var(--border);}}
.tw{{max-width:1800px;margin:1rem auto 2rem;padding:0 2rem;overflow-x:auto;}}
table{{width:100%;border-collapse:collapse;border:1px solid var(--border);border-radius:10px;overflow:hidden;font-size:12px;}}
thead th{{background:var(--surface);border-bottom:1px solid var(--border);color:var(--muted);font-family:'DM Mono',monospace;font-size:10px;padding:.7rem .8rem;text-align:left;text-transform:uppercase;white-space:nowrap;}}
tbody tr{{border-bottom:1px solid var(--border);transition:background .1s;}}
tbody tr:hover{{background:rgba(255,255,255,.025);}}.hot-row{{background:rgba(239,68,68,.04);}}.hidden{{display:none;}}
td{{padding:.6rem .8rem;vertical-align:middle;}}
.rank{{color:var(--muted);font-family:'DM Mono',monospace;font-size:11px;text-align:center;}}
.new-badge{{display:inline-block;background:rgba(167,139,250,.2);color:#c4b5fd;border:1px solid rgba(167,139,250,.4);border-radius:3px;font-family:'DM Mono',monospace;font-size:8px;padding:1px 4px;margin-left:4px;vertical-align:middle;}}
.score-circle{{width:40px;height:40px;border-radius:50%;border:2px solid var(--c,#22c55e);color:var(--c,#22c55e);display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif;font-weight:800;font-size:14px;margin:0 auto 3px;}}
.slbl{{text-align:center;font-size:9px;font-family:'DM Mono',monospace;}}
.mono{{font-family:'DM Mono',monospace;color:var(--muted);}}.sm{{font-size:11px;}}.nowrap{{white-space:nowrap;}}.bold{{font-weight:500;}}
.name{{white-space:nowrap;max-width:130px;overflow:hidden;text-overflow:ellipsis;}}
.address-cell{{max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;color:#94a3b8;}}
.days{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);}}.hot-age{{color:#ef4444;font-weight:500;}}.warm-age{{color:#f97316;}}
.lookup-btns{{display:flex;flex-direction:column;gap:3px;}}
.maps-link{{color:#60a5fa;text-decoration:none;font-size:11px;white-space:nowrap;}}
.maps-link:hover{{color:#93c5fd;}}
.cad-link{{color:#86efac;text-decoration:none;font-size:11px;white-space:nowrap;}}
.cad-link:hover{{color:#4ade80;}}
.badge{{border-radius:4px;display:inline-block;font-family:'DM Mono',monospace;font-size:9px;font-weight:500;margin:1px;padding:2px 6px;white-space:nowrap;}}
.badge-active{{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.3);}}
footer{{border-top:1px solid var(--border);color:var(--muted);font-family:'DM Mono',monospace;font-size:10px;padding:1rem 2rem;text-align:center;}}
</style></head><body>
<header><div class="hi">
  <div>
    <div class="eyebrow">Motivated Seller Intelligence</div>
    <h1>Harris County Lead Dashboard</h1>
    <div class="sub">Generated {gen} &nbsp;·&nbsp; {total} records &nbsp;·&nbsp; cclerk.hctx.net</div>
    <div class="db">{dnote}</div>
    <div class="addr-badge">🏠 {addrs} addresses resolved</div>
  </div>
  <div class="stats">
    <div class="sc"><div class="n nh">{hot}</div><div class="l">Hot 50+</div></div>
    <div class="sc"><div class="n nw">{warm}</div><div class="l">Warm 25+</div></div>
    <div class="sc"><div class="n n30">{above30}</div><div class="l">Score 30+</div></div>
    <div class="sc"><div class="n nc">{cold}</div><div class="l">Cold</div></div>
    <div class="sc"><div class="n nt">{total}</div><div class="l">Total</div></div>
    <div class="sc"><div class="n na">{avg}</div><div class="l">Avg Score</div></div>
  </div>
</div></header>
<div class="toolbar">
  <button class="fb active" onclick="filt('all',this)">All</button>
  <button class="fb" onclick="filt('hot',this)">🔥 Hot (50+)</button>
  <button class="fb" onclick="filt('warm',this)">⚠️ Warm (25-49)</button>
  <button class="fb" onclick="filt('30',this)">⭐ Score 30+</button>
  <button class="fb" onclick="filt('new',this)">🆕 New Today</button>
  <button class="fb" onclick="filt('cold',this)">✓ Cold</button>
  <input class="sb" type="text" placeholder="Search name, address..." oninput="apply(this.value)">
  <button class="map-btn" onclick="toggleMap()">🗺️ Leads Map</button>
  <button class="exp-btn" onclick="exportCSV()">⬇ Export CSV</button>
</div>
<div class="map-wrap" id="mapWrap">
  <div class="map-note">Warm + hot leads across Houston &nbsp;·&nbsp; Click any pin for details</div>
  <div id="map"></div>
</div>
<div class="tw"><table>
  <thead><tr>
    <th>#</th><th>Score</th><th>Doc #</th><th>Filed</th><th>Age</th>
    <th>Doc Type</th><th>Grantor (Seller)</th><th>Grantee (Buyer)</th>
    <th>Property Address</th><th>Lookup</th><th>Signals</th>
  </tr></thead>
  <tbody id="tb">{rows}</tbody>
</table></div>
<footer>Harris County &nbsp;·&nbsp; cclerk.hctx.net &nbsp;·&nbsp; For informational use only</footer>
<script>
const newDocs=new Set({nd});
let cf='all';
function filt(t,b){{cf=t;document.querySelectorAll('.fb').forEach(x=>x.classList.remove('active'));b.classList.add('active');apply(document.querySelector('.sb').value);}}
function apply(query){{
  const q=query.toLowerCase();
  document.querySelectorAll('#tb tr').forEach(row=>{{
    const s=parseInt(row.dataset.score||0);
    const t=row.dataset.text||'';
    const rk=row.querySelector('.rank')?.textContent||'';
    const isNew=Array.from(newDocs).some(d=>rk.includes(d));
    const mf=cf==='all'||(cf==='hot'&&s>=50)||(cf==='warm'&&s>=25&&s<50)||
              (cf==='30'&&s>=30)||(cf==='new'&&isNew)||(cf==='cold'&&s<25);
    row.classList.toggle('hidden',!(mf&&(q===''||t.includes(q))));
  }});
}}
const csvData=`{csv_d}`;
function exportCSV(){{
  const blob=new Blob([csvData],{{type:'text/csv'}});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url;a.download='harris-leads-{today}.csv';a.click();
  URL.revokeObjectURL(url);
}}
const markers={mj};
let mapLoaded=false,mapVisible=false;
function toggleMap(){{
  const w=document.getElementById('mapWrap');
  mapVisible=!mapVisible;w.classList.toggle('show',mapVisible);
  if(mapVisible&&!mapLoaded){{mapLoaded=true;loadMap();}}
}}
function loadMap(){{
  const lc=document.createElement('link');lc.rel='stylesheet';lc.href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';document.head.appendChild(lc);
  const ls=document.createElement('script');ls.src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
  ls.onload=function(){{
    const map=L.map('map').setView([29.76,-95.37],11);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{attribution:'© OpenStreetMap'}}).addTo(map);
    markers.forEach(m=>{{
      const color=m.score>=50?'#ef4444':'#f97316';
      L.circleMarker([m.lat,m.lng],{{radius:m.score>=50?10:7,fillColor:color,color:'#fff',weight:1.5,opacity:1,fillOpacity:0.85}})
       .addTo(map).bindPopup('<b>'+m.name+'</b><br>'+(m.address||m.doc_type)+'<br>Filed: '+m.date+' ('+m.days+'d ago)<br>Score: <b>'+m.score+'</b>'+(m.maps_url?'<br><a href="'+m.maps_url+'" target="_blank">📍 Maps</a>':''));
    }});
  }};document.head.appendChild(ls);
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
