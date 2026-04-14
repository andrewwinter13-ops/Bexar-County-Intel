"""
Motivated Seller Lead Scraper - Bexar County, TX
Features: Selenium scrape, scoring, CSV export, map, Slack notifications
Slack sends:
  1. Daily summary — total leads pulled, how many are 30+, dashboard link
  2. New lead alerts — any new grantor with score 30+ not seen in previous run
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
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
COUNTY_NAME  = "Bexar County, TX"
SEARCH_URL   = (
    "https://bexar.tx.publicsearch.us/results"
    "?department=RP&recordType=OR"
    "&dateFrom=01%2F01%2F2024&dateTo=12%2F31%2F2025"
)
DASHBOARD_URL = "https://andrewwinter13-ops.github.io/Bexar-County-Intel/Scraper/index.html"
MAX_PAGES      = 20
PAGE_LOAD_WAIT = 15
BETWEEN_PAGES  = 3
NOTIFY_MIN_SCORE = 30   # Alert threshold

OUTPUT_DIR     = Path("Scraper")
OUTPUT_FILE    = OUTPUT_DIR / "output.json"
DASHBOARD_FILE = OUTPUT_DIR / "index.html"
PREV_DOCS_FILE = OUTPUT_DIR / "prev_doc_numbers.json"  # Tracks seen records

# Slack webhook URL — set this as a GitHub Secret named SLACK_WEBHOOK_URL
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

# ─────────────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────────────
SCORE_WEIGHTS = {
    "tax_delinquent": 30, "code_violation": 25,
    "probate_filing": 20, "multiple_liens": 15, "divorce_bankruptcy": 10,
}
DISTRESS_KEYWORDS = {
    "tax_delinquent":     ["tax lien", "federal tax lien", "state tax lien", "delinquent tax", "irs lien", "ftl", "release of ftl"],
    "code_violation":     ["code violation", "notice of violation", "abatement", "nuisance"],
    "probate_filing":     ["probate", "estate of", "successor trustee", "letters testamentary", "affidavit of heirship"],
    "multiple_liens":     ["mechanic lien", "judgment lien", "lis pendens", "notice of default", "ucc", "assignment"],
    "divorce_bankruptcy": ["dissolution", "bankruptcy", "chapter 7", "chapter 13", "quitclaim", "easement"],
}
DISTRESS_DOC_TYPES = {
    "tax_delinquent":     ["FEDERAL TAX LIEN", "STATE TAX LIEN", "TAX LIEN", "RELEASE OF FTL"],
    "code_violation":     ["NOTICE OF VIOLATION", "CODE VIOLATION"],
    "probate_filing":     ["AFFIDAVIT", "PROBATE", "HEIRSHIP"],
    "multiple_liens":     ["UCC 1 REAL PROPERTY", "JUDGMENT LIEN", "LIS PENDENS", "ASSIGNMENT"],
    "divorce_bankruptcy": ["EASEMENT", "QUITCLAIM"],
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
    lot:                str = ""
    block:              str = ""
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
    q = urllib.parse.quote(f"{name}, San Antonio, TX")
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
    """Send a message payload to Slack webhook."""
    if not SLACK_WEBHOOK:
        log.warning("No SLACK_WEBHOOK_URL set — skipping Slack notification.")
        return
    try:
        data = json.dumps(message).encode("utf-8")
        req  = Request(SLACK_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
        urlopen(req, timeout=10)
        log.info("Slack notification sent.")
    except URLError as e:
        log.error(f"Slack error: {e}")


def slack_daily_summary(records, new_leads):
    """Send the daily run summary to Slack."""
    total      = len(records)
    above_30   = sum(1 for r in records if r.seller_score >= NOTIFY_MIN_SCORE)
    hot        = sum(1 for r in records if r.seller_score >= 50)
    warm       = sum(1 for r in records if 25 <= r.seller_score < 50)
    new_count  = len(new_leads)
    today      = datetime.utcnow().strftime("%B %d, %Y")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🏠 Bexar County Lead Scraper — {today}"}
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Total Records Pulled:*\n{total}"},
                {"type": "mrkdwn", "text": f"*Score 30+ Leads:*\n{above_30}"},
                {"type": "mrkdwn", "text": f"*🔥 Hot (50+):*\n{hot}"},
                {"type": "mrkdwn", "text": f"*⚠️ Warm (25-49):*\n{warm}"},
                {"type": "mrkdwn", "text": f"*🆕 New Leads Today:*\n{new_count}"},
            ]
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<{DASHBOARD_URL}|📊 Open Full Dashboard>"}
        }
    ]
    slack_send({"blocks": blocks})


def slack_new_lead_alerts(new_leads):
    """Send individual alerts for each new lead scoring 30+."""
    if not new_leads:
        return

    # Group into one message with up to 10 leads
    lead_lines = []
    for r in new_leads[:10]:
        signals = []
        if r.tax_delinquent:    signals.append("Tax Lien")
        if r.code_violation:    signals.append("Code Viol.")
        if r.probate_filing:    signals.append("Probate")
        if r.multiple_liens:    signals.append("Multi-Lien")
        if r.divorce_bankruptcy: signals.append("Divorce/BK")
        sig_str  = " · ".join(signals) if signals else r.doc_type
        days_str = f"{r.days_on_record}d old" if r.days_on_record else ""
        score_emoji = "🔥" if r.seller_score >= 50 else "⚠️"
        lead_lines.append(
            f"{score_emoji} *{r.grantor}* — Score: *{r.seller_score}*\n"
            f"   {r.doc_type} · Filed: {r.file_date} {days_str}\n"
            f"   Signals: {sig_str}\n"
            f"   <{r.maps_url}|🔍 Search in Maps>"
        )

    overflow = f"\n_...and {len(new_leads)-10} more. <{DASHBOARD_URL}|View all →>_" if len(new_leads) > 10 else ""

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🆕 {len(new_leads)} New Lead{'s' if len(new_leads)!=1 else ''} (Score 30+)"}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n\n".join(lead_lines) + overflow}
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<{DASHBOARD_URL}|📊 Open Full Dashboard>"}
        }
    ]
    slack_send({"blocks": blocks})


# ─────────────────────────────────────────────────────────────────────────────
# PREVIOUS RECORDS TRACKING
# ─────────────────────────────────────────────────────────────────────────────
def load_prev_doc_numbers() -> set:
    """Load document numbers from the previous run."""
    try:
        if PREV_DOCS_FILE.exists():
            with open(PREV_DOCS_FILE) as f:
                return set(json.load(f))
    except: pass
    return set()


def save_doc_numbers(records):
    """Save current document numbers for next run comparison."""
    doc_nums = [r.document_number for r in records if r.document_number]
    with open(PREV_DOCS_FILE, "w") as f:
        json.dump(doc_nums, f)


def find_new_leads(records, prev_doc_numbers: set) -> list:
    """Return records that are new (not seen before) and score >= NOTIFY_MIN_SCORE."""
    return [
        r for r in records
        if r.seller_score >= NOTIFY_MIN_SCORE
        and r.document_number not in prev_doc_numbers
    ]


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


def wait_for_records(driver, timeout=PAGE_LOAD_WAIT):
    wait = WebDriverWait(driver, timeout)
    for sel in ["table tbody tr", "tr.result-row", "[class*='ResultRow']", "[class*='tableRow']"]:
        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
            if driver.find_elements(By.CSS_SELECTOR, sel):
                return sel
        except TimeoutException: continue
    return None


def extract_rows(driver, selector):
    records = []
    try:
        for row in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 3: continue
                def cell(i, d=""):
                    try: return cells[i].text.strip() or d
                    except: return d
                file_date = cell(3)
                grantor   = cell(0)
                rec = PropertyRecord(
                    grantor=grantor, grantee=cell(1), doc_type=cell(2),
                    file_date=file_date, document_number=cell(4),
                    legal_description=cell(5), lot=cell(6), block=cell(7),
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


def click_next_page(driver):
    for sel in ["button[aria-label='Next page']", "a.next", "[class*='pagination'] button:last-child"]:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn.is_enabled() and btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                return True
        except: continue
    try:
        for btn in driver.find_elements(By.TAG_NAME, "button"):
            if btn.text.strip().lower() in ["next", "›", "»"]:
                if btn.is_enabled():
                    driver.execute_script("arguments[0].click();", btn)
                    return True
    except: pass
    return False


def scrape_bexar_selenium():
    driver = make_driver()
    records = []
    try:
        log.info(f"Loading {SEARCH_URL}")
        driver.get(SEARCH_URL)
        time.sleep(5)
        row_selector = wait_for_records(driver)
        if not row_selector:
            log.warning("No records found.")
            with open("Scraper/debug_page.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            return []
        for page_num in range(1, MAX_PAGES + 1):
            log.info(f"Scraping page {page_num}...")
            time.sleep(2)
            page_records = extract_rows(driver, row_selector)
            records.extend(page_records)
            log.info(f"  Page {page_num}: {len(page_records)} records (total: {len(records)})")
            if not page_records: break
            if not click_next_page(driver):
                log.info("No next page.")
                break
            time.sleep(BETWEEN_PAGES)
            try:
                WebDriverWait(driver, 10).until(
                    EC.staleness_of(driver.find_elements(By.CSS_SELECTOR, row_selector)[0])
                )
            except: time.sleep(2)
    except Exception as e:
        log.error(f"Selenium error: {e}", exc_info=True)
    finally:
        driver.quit()
    return records


# ─────────────────────────────────────────────────────────────────────────────
# DEMO DATA
# ─────────────────────────────────────────────────────────────────────────────
def generate_demo_records(n=50):
    firstnames = ["James","Maria","Robert","Linda","Michael","Patricia","William","Barbara","David","Elizabeth"]
    lastnames  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Wilson","Anderson","Rodriguez","Martinez"]
    companies  = ["DHI MORTGAGE COM","GOODLEAP LLC","INDEPENDENT BANK","INTERNAL REVENUE SERVICE","STATE OF TEXAS","RCN CAPITAL LLC"]
    doc_types  = [
        ("DEED OF TRUST",False),("FEDERAL TAX LIEN",True),("STATE TAX LIEN",True),
        ("UCC 1 REAL PROPERTY",True),("DEED",False),("AFFIDAVIT",True),
        ("RELEASE OF FTL",True),("JUDGMENT LIEN",True),
    ]
    records = []
    for i in range(n):
        grantor  = (random.choice(companies) if random.random()>0.5 else
                    f"{random.choice(lastnames).upper()} {random.choice(firstnames).upper()}")
        grantee  = (random.choice(companies) if random.random()>0.6 else
                    f"{random.choice(lastnames).upper()} {random.choice(firstnames).upper()}")
        doc_type,_ = random.choice(doc_types)
        doc_num  = f"2024{random.randint(10000000,99999999)}"
        days_ago = random.randint(30,800)
        filed    = date.fromordinal(date.today().toordinal()-days_ago)
        file_date = filed.strftime("%m/%d/%Y")
        lot,block = str(random.randint(1,25)),str(random.randint(1,15))
        rec = PropertyRecord(
            document_number=doc_num, file_date=file_date, grantor=grantor,
            grantee=grantee, doc_type=doc_type,
            legal_description=f"Subdivision: Lot {lot} Block {block}",
            lot=lot, block=block, source_url=SEARCH_URL,
            days_on_record=days_ago, maps_url=make_maps_url(grantor),
        )
        score_record(rec)
        records.append(rec)
    records.sort(key=lambda r: r.seller_score, reverse=True)
    return records


# ─────────────────────────────────────────────────────────────────────────────
# CSV EXPORT
# ─────────────────────────────────────────────────────────────────────────────
def records_to_csv(records) -> str:
    output = io.StringIO()
    fields = ["document_number","file_date","days_on_record","doc_type","grantor","grantee",
              "legal_description","seller_score","tax_delinquent","code_violation",
              "probate_filing","multiple_liens","divorce_bankruptcy","maps_url"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for r in records:
        writer.writerow({f: getattr(r,f,"") for f in fields})
    return output.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────
def build_dashboard(records, new_lead_doc_numbers=None):
    if new_lead_doc_numbers is None:
        new_lead_doc_numbers = set()

    now_str   = datetime.utcnow().strftime("%-m/%-d/%Y, %-I:%M:%S %p")
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    total     = len(records)

    # Type counts
    def count_type(keywords):
        return sum(1 for r in records if any(k in r.get("doc_type","").upper() for k in keywords))

    # Build rows JSON for JS
    rows_data = []
    for r in records:
        doc_type = r.get("doc_type","") if isinstance(r, dict) else r.doc_type
        grantor  = r.get("grantor","") if isinstance(r, dict) else r.grantor
        grantee  = r.get("grantee","") if isinstance(r, dict) else r.grantee
        doc_num  = r.get("document_number","") if isinstance(r, dict) else r.document_number
        filed    = r.get("file_date","") if isinstance(r, dict) else r.file_date
        legal    = r.get("legal_description","") if isinstance(r, dict) else r.legal_description
        score    = r.get("seller_score",0) if isinstance(r, dict) else r.seller_score
        days     = r.get("days_on_record",0) if isinstance(r, dict) else r.days_on_record
        maps_url = r.get("maps_url","") if isinstance(r, dict) else r.maps_url
        address  = r.get("property_address","") if isinstance(r, dict) else r.property_address
        tax_d    = r.get("tax_delinquent",False) if isinstance(r, dict) else r.tax_delinquent
        code_v   = r.get("code_violation",False) if isinstance(r, dict) else r.code_violation
        prob     = r.get("probate_filing",False) if isinstance(r, dict) else r.probate_filing
        multi    = r.get("multiple_liens",False) if isinstance(r, dict) else r.multiple_liens
        divorce  = r.get("divorce_bankruptcy",False) if isinstance(r, dict) else r.divorce_bankruptcy

        signals = []
        if tax_d:  signals.append("Tax lien")
        if code_v: signals.append("Code violation")
        if prob:   signals.append("Probate / estate")
        if multi:  signals.append("Judgment lien")
        if divorce:signals.append("Lis pendens")

        # Abbreviate doc type
        type_map = {
            "FEDERAL TAX LIEN":"TAX","STATE TAX LIEN":"TAX","RELEASE OF FTL":"TAX",
            "JUDGMENT LIEN":"JUD","JUDGMENT":"JUD","LIS PENDENS":"LIS",
            "NOTICE OF DEFAULT":"NOD","MECHANIC LIEN":"MECH","DEED OF TRUST":"DOT",
            "UCC":"UCC","PROBATE":"PRO","AFFIDAVIT":"AFF","DEED":"DEED",
            "ASSIGNMENT":"ASGN","QUITCLAIM":"QCD","EASEMENT":"ESM",
        }
        code = "OTH"
        for k,v in type_map.items():
            if k in doc_type.upper():
                code = v
                break

        is_new = doc_num in new_lead_doc_numbers
        this_week = days <= 7

        rows_data.append({
            "score": score, "doc_type": doc_type, "code": code,
            "doc_num": doc_num, "filed": filed, "days": days,
            "grantor": grantor, "grantee": grantee,
            "address": address or legal[:60],
            "legal": legal, "maps_url": maps_url,
            "signals": signals, "is_new": is_new, "this_week": this_week,
            "tax_d": tax_d, "code_v": code_v, "prob": prob,
            "multi": multi, "divorce": divorce,
        })

    # Type category counts for header stats
    tax_count  = sum(1 for r in rows_data if r["code"]=="TAX")
    jud_count  = sum(1 for r in rows_data if r["code"]=="JUD")
    lis_count  = sum(1 for r in rows_data if r["code"]=="LIS")
    nod_count  = sum(1 for r in rows_data if r["code"]=="NOD")
    new_count  = sum(1 for r in rows_data if r["is_new"])
    week_count = sum(1 for r in rows_data if r["this_week"])

    rows_json = json.dumps(rows_data)

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>{COUNTY_NAME} Motivated Sellers</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@400;500&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
:root{{
  --bg:#0e0e0e;--surface:#161616;--border:#2a2a2a;
  --text:#f0f0f0;--muted:#666;--accent:#f5a623;
  --purple:#a855f7;--orange:#f97316;--red:#ef4444;--green:#22c55e;--blue:#3b82f6;
}}
body{{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;display:flex;flex-direction:column;min-height:100vh;}}

/* TOP BAR */
.topbar{{display:flex;align-items:center;justify-content:space-between;padding:.5rem 1.5rem;border-bottom:1px solid var(--border);background:#111;}}
.brand{{font-family:'Bebas Neue',sans-serif;font-size:1.1rem;letter-spacing:.15em;color:var(--text);}}
.brand span{{color:var(--accent);}}
.updated{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);}}
.updated::before{{content:'● ';color:var(--green);}}
.export-btn{{background:var(--accent);color:#000;border:none;border-radius:5px;font-family:'DM Mono',monospace;font-size:11px;font-weight:500;padding:.4rem 1rem;cursor:pointer;display:flex;align-items:center;gap:5px;}}
.export-btn:hover{{background:#e09510;}}

/* HERO HEADER */
.hero{{padding:1.5rem 1.5rem 1rem;border-bottom:1px solid var(--border);}}
.hero h1{{font-family:'Bebas Neue',sans-serif;font-size:clamp(2.5rem,6vw,4rem);line-height:.95;color:var(--accent);letter-spacing:.05em;}}
.hero h2{{font-family:'Bebas Neue',sans-serif;font-size:clamp(2.5rem,6vw,4rem);line-height:.95;color:var(--text);letter-spacing:.05em;margin-bottom:.75rem;}}
.hero-meta{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);}}
.total-badge{{float:right;text-align:right;}}
.total-num{{font-family:'Bebas Neue',sans-serif;font-size:3rem;color:var(--accent);line-height:1;}}
.total-lbl{{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:.15em;text-transform:uppercase;}}

/* STATS BAR */
.stats-bar{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));border-bottom:1px solid var(--border);}}
.stat-cell{{padding:.9rem 1.5rem;border-right:1px solid var(--border);}}
.stat-cell:last-child{{border-right:none;}}
.stat-num{{font-family:'Bebas Neue',sans-serif;font-size:2.2rem;line-height:1;}}
.stat-lbl{{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase;margin-top:2px;}}
.purple{{color:var(--purple);}}.orange{{color:var(--orange);}}.red{{color:var(--red);}}.blue{{color:var(--blue);}}.green{{color:var(--green);}}.accent{{color:var(--accent);}}

/* FILTER ROW */
.filter-row{{display:flex;align-items:center;gap:.5rem;padding:.75rem 1.5rem;border-bottom:1px solid var(--border);flex-wrap:wrap;background:#111;}}
.search-input{{background:#1a1a1a;border:1px solid var(--border);border-radius:5px;color:var(--text);font-size:12px;padding:.4rem .8rem;width:200px;outline:none;}}
.search-input:focus{{border-color:var(--accent);}}
.search-input::placeholder{{color:var(--muted);}}
select.type-sel{{background:#1a1a1a;border:1px solid var(--border);border-radius:5px;color:var(--text);font-size:12px;padding:.4rem .8rem;outline:none;cursor:pointer;}}
select.score-sel{{background:#1a1a1a;border:1px solid var(--border);border-radius:5px;color:var(--text);font-size:12px;padding:.4rem .8rem;outline:none;cursor:pointer;}}
.chip{{background:#1a1a1a;border:1px solid var(--border);border-radius:20px;color:var(--muted);cursor:pointer;font-size:11px;padding:.3rem .85rem;white-space:nowrap;transition:all .15s;}}
.chip:hover,.chip.active{{background:rgba(245,166,35,.12);border-color:var(--accent);color:var(--accent);}}
.chip.new-chip{{border-color:var(--purple);color:var(--purple);}}
.chip.new-chip.active{{background:rgba(168,85,247,.15);}}

/* MAIN LAYOUT */
.main{{display:flex;flex:1;overflow:hidden;}}
.table-pane{{flex:1;overflow:auto;}}
.detail-pane{{width:320px;border-left:1px solid var(--border);background:#111;overflow-y:auto;flex-shrink:0;display:none;}}
.detail-pane.open{{display:block;}}

/* TABLE */
table{{width:100%;border-collapse:collapse;}}
thead th{{background:#111;border-bottom:1px solid var(--border);color:var(--muted);font-family:'DM Mono',monospace;font-size:10px;font-weight:500;padding:.6rem 1rem;text-align:left;text-transform:uppercase;letter-spacing:.08em;white-space:nowrap;position:sticky;top:0;z-index:10;}}
tbody tr{{border-bottom:1px solid #1a1a1a;cursor:pointer;transition:background .1s;}}
tbody tr:hover{{background:#1a1a1a;}}
tbody tr.selected{{background:#1f1a0e;border-left:2px solid var(--accent);}}
tbody tr.hidden{{display:none;}}
td{{padding:.55rem 1rem;vertical-align:middle;}}

/* SCORE CIRCLE */
.sc{{width:36px;height:36px;border-radius:50%;border:2px solid;display:flex;align-items:center;justify-content:center;font-family:'Bebas Neue',sans-serif;font-size:16px;margin:0 auto;}}
.sc.hot{{border-color:var(--accent);color:var(--accent);}}
.sc.warm{{border-color:var(--orange);color:var(--orange);}}
.sc.cold{{border-color:var(--muted);color:var(--muted);}}

/* TYPE BADGE */
.type-badge{{border-radius:4px;font-family:'DM Mono',monospace;font-size:10px;font-weight:500;padding:3px 8px;white-space:nowrap;display:inline-block;}}
.tb-TAX{{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.3);}}
.tb-JUD{{background:rgba(168,85,247,.15);color:#d8b4fe;border:1px solid rgba(168,85,247,.3);}}
.tb-LIS{{background:rgba(249,115,22,.15);color:#fdba74;border:1px solid rgba(249,115,22,.3);}}
.tb-NOD{{background:rgba(239,68,68,.2);color:#fca5a5;border:1px solid rgba(239,68,68,.4);}}
.tb-MECH{{background:rgba(59,130,246,.15);color:#93c5fd;border:1px solid rgba(59,130,246,.3);}}
.tb-DOT{{background:rgba(34,197,94,.12);color:#86efac;border:1px solid rgba(34,197,94,.25);}}
.tb-OTH,.tb-UCC,.tb-ASGN,.tb-AFF,.tb-DEED,.tb-QCD,.tb-ESM,.tb-PRO{{background:rgba(255,255,255,.06);color:#aaa;border:1px solid rgba(255,255,255,.12);}}

.code-cell{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);}}
.filed-cell{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);white-space:nowrap;}}
.owner-cell{{font-weight:500;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.grantor-cell{{color:#aaa;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.addr-cell{{color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;}}
.amt-cell{{color:var(--muted);font-family:'DM Mono',monospace;font-size:11px;}}
.new-tag{{display:inline-block;background:rgba(168,85,247,.18);color:#c4b5fd;border:1px solid rgba(168,85,247,.35);border-radius:3px;font-size:9px;font-family:'DM Mono',monospace;padding:1px 5px;margin-left:4px;vertical-align:middle;}}
.week-tag{{display:inline-block;background:rgba(245,166,35,.15);color:var(--accent);border:1px solid rgba(245,166,35,.3);border-radius:3px;font-size:9px;font-family:'DM Mono',monospace;padding:1px 5px;margin-left:4px;vertical-align:middle;}}

/* DETAIL PANE */
.detail-header{{display:flex;align-items:center;justify-content:space-between;padding:1rem;border-bottom:1px solid var(--border);}}
.detail-title{{font-family:'DM Mono',monospace;font-size:12px;color:var(--accent);letter-spacing:.1em;text-transform:uppercase;}}
.close-btn{{background:none;border:none;color:var(--muted);cursor:pointer;font-size:18px;line-height:1;}}
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
</style>
</head><body>

<!-- TOP BAR -->
<div class="topbar">
  <div class="brand"><span>BEXAR</span>LEADS</div>
  <div class="updated">Updated {now_str}</div>
  <button class="export-btn" onclick="exportCSV()">⬇ EXPORT GHL CSV</button>
</div>

<!-- HERO -->
<div class="hero">
  <div class="total-badge">
    <div class="total-num">{total}</div>
    <div class="total-lbl">Total Leads</div>
  </div>
  <h1>{COUNTY_NAME.upper()}</h1>
  <h2>MOTIVATED SELLERS</h2>
  <div class="hero-meta">bexar.tx.publicsearch.us &nbsp;·&nbsp; Generated {datetime.utcnow().strftime("%Y-%m-%d")}</div>
</div>

<!-- STATS BAR -->
<div class="stats-bar">
  <div class="stat-cell"><div class="stat-num purple">{jud_count}</div><div class="stat-lbl">Judgment</div></div>
  <div class="stat-cell"><div class="stat-num orange">{tax_count}</div><div class="stat-lbl">Tax Lien</div></div>
  <div class="stat-cell"><div class="stat-num red">{lis_count}</div><div class="stat-lbl">Lis Pendens</div></div>
  <div class="stat-cell"><div class="stat-num blue">{nod_count}</div><div class="stat-lbl">Notice of Default</div></div>
  <div class="stat-cell"><div class="stat-num green">{new_count}</div><div class="stat-lbl">New Leads</div></div>
  <div class="stat-cell"><div class="stat-num accent">{week_count}</div><div class="stat-lbl">This Week</div></div>
</div>

<!-- FILTER ROW -->
<div class="filter-row">
  <input class="search-input" type="text" placeholder="Owner, address, doc #..." oninput="applyFilters()">
  <select class="type-sel" onchange="applyFilters()">
    <option value="">All Types</option>
    <option value="TAX">Tax Lien</option>
    <option value="JUD">Judgment</option>
    <option value="LIS">Lis Pendens</option>
    <option value="NOD">Notice of Default</option>
    <option value="MECH">Mechanic Lien</option>
    <option value="PRO">Probate</option>
  </select>
  <select class="score-sel" onchange="applyFilters()">
    <option value="0">Any Score</option>
    <option value="30">30+</option>
    <option value="50">50+</option>
    <option value="70">70+</option>
  </select>
  <button class="chip active" data-chip="all" onclick="setChip('all',this)">All</button>
  <button class="chip" data-chip="LIS" onclick="setChip('LIS',this)">Lis pendens</button>
  <button class="chip" data-chip="NOD" onclick="setChip('NOD',this)">Pre-foreclosure</button>
  <button class="chip" data-chip="JUD" onclick="setChip('JUD',this)">Judgment lien</button>
  <button class="chip" data-chip="TAX" onclick="setChip('TAX',this)">Tax lien</button>
  <button class="chip" data-chip="MECH" onclick="setChip('MECH',this)">Mechanic lien</button>
  <button class="chip" data-chip="PRO" onclick="setChip('PRO',this)">Probate / estate</button>
  <button class="chip new-chip" data-chip="new" onclick="setChip('new',this)">New this week</button>
</div>

<!-- MAIN -->
<div class="main">
  <div class="table-pane">
    <table id="leadsTable">
      <thead>
        <tr>
          <th>Score</th><th>Type</th><th>Code</th><th>Filed</th>
          <th>Property Owner</th><th>Grantor / Plaintiff</th>
          <th>Property Address</th><th>Amount</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>

  <!-- DETAIL PANE -->
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
    <div class="detail-row"><span class="detail-key">Amount</span><span class="detail-val" id="d-amount">—</span></div>
    <div class="detail-row"><span class="detail-key">Property Owner</span><span class="detail-val" id="d-owner">—</span></div>
    <div class="detail-row"><span class="detail-key">Grantor / Plaintiff</span><span class="detail-val" id="d-grantor">—</span></div>
    <div class="detail-row"><span class="detail-key">Legal Desc</span><span class="detail-val" id="d-legal">—</span></div>
    <div class="section-title">Property</div>
    <div class="detail-row"><span class="detail-key">Site Address</span><span class="detail-val" id="d-addr">—</span></div>
    <div class="section-title">Links</div>
    <div class="detail-row"><span class="detail-key">Maps</span><span class="detail-val"><a class="detail-link" id="d-maps" href="#" target="_blank">Search in Google Maps →</a></span></div>
    <div class="section-title">Actions</div>
    <button class="underwrite-btn" onclick="underwrite()">🏠 UNDERWRITE THIS PROPERTY</button>
  </div>
</div>

<script>
const allRows = {rows_json};
let activeChip = 'all';
let selectedIdx = null;

function scoreClass(s){{ return s>=50?'hot':s>=30?'warm':'cold'; }}

function renderTable(rows){{
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = rows.map((r,i) => {{
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
      <td class="amt-cell">—</td>
    </tr>`;
  }}).join('');
}}

function applyFilters(){{
  const q     = document.querySelector('.search-input').value.toLowerCase();
  const type  = document.querySelector('.type-sel').value;
  const score = parseInt(document.querySelector('.score-sel').value)||0;
  const rows  = document.querySelectorAll('#tbody tr');
  rows.forEach((row,i) => {{
    const r = allRows[i];
    if(!r)return;
    const matchQ    = !q || (r.grantor+r.grantee+r.address+r.doc_num).toLowerCase().includes(q);
    const matchType = !type || r.code===type;
    const matchScore= r.score >= score;
    const matchChip = activeChip==='all' || activeChip==='new'
      ? (activeChip==='new' ? r.this_week : true)
      : r.code===activeChip;
    row.classList.toggle('hidden', !(matchQ&&matchType&&matchScore&&matchChip));
  }});
}}

function setChip(val, btn){{
  activeChip = val;
  document.querySelectorAll('.chip').forEach(c=>c.classList.remove('active'));
  btn.classList.add('active');
  applyFilters();
}}

function selectRow(idx){{
  selectedIdx = idx;
  const r = allRows[idx];
  document.querySelectorAll('#tbody tr').forEach(tr=>tr.classList.remove('selected'));
  document.querySelector(`#tbody tr[data-idx="${{idx}}"]`)?.classList.add('selected');
  
  // Populate detail pane
  const sc = scoreClass(r.score);
  const scoreEl = document.getElementById('d-score');
  scoreEl.textContent = r.score;
  scoreEl.style.borderColor = sc==='hot'?'#f5a623':sc==='warm'?'#f97316':'#666';
  scoreEl.style.color = sc==='hot'?'#f5a623':sc==='warm'?'#f97316':'#666';
  
  document.getElementById('d-name').textContent = r.grantor || '—';
  document.getElementById('d-sub').textContent = r.doc_type + (r.code?' · '+r.code:'');
  document.getElementById('d-docnum').textContent = r.doc_num || '—';
  document.getElementById('d-type').textContent = r.doc_type || '—';
  document.getElementById('d-filed').textContent = r.filed || '—';
  document.getElementById('d-days').textContent = r.days ? r.days+' days' : '—';
  document.getElementById('d-amount').textContent = '—';
  document.getElementById('d-owner').textContent = r.grantor || '—';
  document.getElementById('d-grantor').textContent = r.grantee || '—';
  document.getElementById('d-legal').textContent = r.legal || '—';
  document.getElementById('d-addr').textContent = r.address || '—';
  
  const mapsEl = document.getElementById('d-maps');
  if(r.maps_url){{ mapsEl.href=r.maps_url; mapsEl.style.display=''; }}
  else{{ mapsEl.style.display='none'; }}
  
  // Signal chips
  const chipsEl = document.getElementById('d-chips');
  chipsEl.innerHTML = r.signals.map(s=>
    `<span class="detail-chip type-badge tb-${{r.code}}">${{s}}</span>`
  ).join('');
  if(r.is_new) chipsEl.innerHTML += '<span class="detail-chip" style="background:rgba(168,85,247,.2);color:#d8b4fe;border:1px solid rgba(168,85,247,.3);">NEW</span>';
  if(r.this_week) chipsEl.innerHTML += '<span class="detail-chip" style="background:rgba(245,166,35,.15);color:#f5a623;border:1px solid rgba(245,166,35,.3);">New this week</span>';
  
  document.getElementById('detailPane').classList.add('open');
}}

function closeDetail(){{
  document.getElementById('detailPane').classList.remove('open');
  document.querySelectorAll('#tbody tr').forEach(tr=>tr.classList.remove('selected'));
  selectedIdx = null;
}}

function underwrite(){{
  const r = allRows[selectedIdx];
  if(!r) return;
  const q = encodeURIComponent((r.grantor||'')+ ' ' +(r.address||'')+ ' San Antonio TX');
  window.open('https://www.google.com/search?q='+q, '_blank');
}}

function exportCSV(){{
  const fields = ['score','doc_type','code','filed','days','grantor','grantee','address','legal','doc_num','signals'];
  const header = fields.join(',');
  const rows = allRows.map(r => fields.map(f => {{
    const v = f==='signals' ? r.signals.join('|') : (r[f]||'');
    return '"'+String(v).replace(/"/g,'""')+'"';
  }}).join(','));
  const csv = [header,...rows].join('\\n');
  const blob = new Blob([csv],{{type:'text/csv'}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href=url; a.download='bexar-leads-{today_str}.csv'; a.click();
  URL.revokeObjectURL(url);
}}

// Initial render
renderTable(allRows);
applyFilters();
</script>
</body></html>"""


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load previous doc numbers for new lead detection
    prev_doc_numbers = load_prev_doc_numbers()
    log.info(f"Loaded {len(prev_doc_numbers)} previously seen records.")

    # Scrape
    log.info(f"Starting scrape of {COUNTY_NAME}...")
    try:
        records = scrape_bexar_selenium()
    except Exception as e:
        log.error(f"Scrape failed: {e}")
        records = []

    if not records:
        log.warning("No live records — using demo data.")
        records = generate_demo_records(50)
    else:
        records.sort(key=lambda r: r.seller_score, reverse=True)
        log.info(f"SUCCESS — {len(records)} real records!")

    # Find new leads (score 30+, not seen before)
    new_leads = find_new_leads(records, prev_doc_numbers)
    new_lead_doc_numbers = {r.document_number for r in new_leads}
    log.info(f"New leads (30+): {len(new_leads)}")

    # Save current doc numbers for next run
    save_doc_numbers(records)

    # Save JSON
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "county": COUNTY_NAME,
            "generated_at": datetime.utcnow().isoformat(),
            "total_records": len(records),
            "live_data": any(r.source_url and "publicsearch" in r.source_url for r in records),
            "new_leads_count": len(new_leads),
            "records": [asdict(r) for r in records],
        }, f, indent=2)
    log.info("JSON saved.")

    # Build dashboard
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(build_dashboard(records, new_lead_doc_numbers))
    log.info("Dashboard saved.")

    # Send Slack notifications
    slack_daily_summary(records, new_leads)
    if new_leads:
        slack_new_lead_alerts(new_leads)

    hot  = sum(1 for r in records if r.seller_score>=50)
    warm = sum(1 for r in records if 25<=r.seller_score<50)
    log.info(f"Hot: {hot}  Warm: {warm}  Cold: {len(records)-hot-warm}")
    log.info(f"Slack notifications sent.")


if __name__ == "__main__":
    main()
