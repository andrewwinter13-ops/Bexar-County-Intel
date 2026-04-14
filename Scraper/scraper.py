"""
Motivated Seller Lead Scraper - Bexar County, TX
Features: Google Maps links, Days on Record, CSV Export, Foreclosure Map
"""

import json, time, logging, random, csv, io
from datetime import datetime, date
from pathlib import Path
from dataclasses import dataclass, field, asdict

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

COUNTY_NAME  = "Bexar County, TX"
SEARCH_URL   = (
    "https://bexar.tx.publicsearch.us/results"
    "?department=RP&recordType=OR"
    "&dateFrom=01%2F01%2F2024&dateTo=12%2F31%2F2025"
)
MAX_PAGES      = 20
PAGE_LOAD_WAIT = 15
BETWEEN_PAGES  = 3

OUTPUT_DIR     = Path("Scraper")
OUTPUT_FILE    = OUTPUT_DIR / "output.json"
DASHBOARD_FILE = OUTPUT_DIR / "index.html"

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


@dataclass
class PropertyRecord:
    document_number:    str = ""
    file_date:          str = ""
    grantor:            str = ""
    grantee:            str = ""
    legal_description:  str = ""
    property_address:   str = ""
    doc_type:           str = ""
    book_volume:        str = ""
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


def calc_days_on_record(file_date_str: str) -> int:
    """Calculate how many days since the document was filed."""
    if not file_date_str:
        return 0
    formats = ["%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y"]
    for fmt in formats:
        try:
            filed = datetime.strptime(file_date_str.strip(), fmt).date()
            return (date.today() - filed).days
        except:
            continue
    return 0


def make_maps_url(address: str) -> str:
    """Generate a Google Maps URL for an address."""
    if not address or len(address) < 5:
        return ""
    import urllib.parse
    query = urllib.parse.quote(address + ", San Antonio, TX")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


def score_record(rec):
    searchable = " ".join([
        rec.grantor, rec.grantee, rec.legal_description,
        rec.property_address, rec.doc_type
    ]).lower()
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

    # Boost score for older filings — more motivated seller
    days = rec.days_on_record
    if days > 365:
        score += 15
    elif days > 180:
        score += 10
    elif days > 90:
        score += 5

    rec.seller_score = min(score, 100)


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
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=opts)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver


def wait_for_records(driver, timeout=PAGE_LOAD_WAIT):
    wait = WebDriverWait(driver, timeout)
    selectors = [
        "table tbody tr", "tr.result-row",
        "[class*='ResultRow']", "[class*='result-row']",
        "[class*='tableRow']", "[data-testid*='row']",
    ]
    for sel in selectors:
        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
            rows = driver.find_elements(By.CSS_SELECTOR, sel)
            if rows:
                return sel
        except TimeoutException:
            continue
    return None


def extract_rows(driver, selector):
    records = []
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, selector)
        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 3:
                    continue
                def cell(i, default=""):
                    try: return cells[i].text.strip() or default
                    except: return default

                file_date = cell(3)
                address   = cell(5) if len(cells) > 5 else ""

                rec = PropertyRecord(
                    grantor=cell(0), grantee=cell(1), doc_type=cell(2),
                    file_date=file_date, document_number=cell(4),
                    legal_description=cell(5), lot=cell(6), block=cell(7),
                    source_url=driver.current_url,
                    days_on_record=calc_days_on_record(file_date),
                    maps_url=make_maps_url(address),
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
    next_selectors = [
        "button[aria-label='Next page']", "button[aria-label='next']",
        "a.next", "[class*='next']:not([disabled])",
        "[class*='pagination'] button:last-child", "li.next a",
    ]
    for sel in next_selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn.is_enabled() and btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                return True
        except:
            continue
    try:
        for btn in driver.find_elements(By.TAG_NAME, "button"):
            if btn.text.strip().lower() in ["next", "›", "»", "next page"]:
                if btn.is_enabled():
                    driver.execute_script("arguments[0].click();", btn)
                    return True
    except:
        pass
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
            if not page_records:
                break
            if not click_next_page(driver):
                log.info("No next page.")
                break
            time.sleep(BETWEEN_PAGES)
            try:
                WebDriverWait(driver, 10).until(
                    EC.staleness_of(driver.find_elements(By.CSS_SELECTOR, row_selector)[0])
                )
            except:
                time.sleep(2)
    except Exception as e:
        log.error(f"Selenium error: {e}", exc_info=True)
    finally:
        driver.quit()
    return records


def generate_demo_records(n=50):
    firstnames = ["James","Maria","Robert","Linda","Michael","Patricia","William","Barbara","David","Elizabeth"]
    lastnames  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Wilson","Anderson","Rodriguez","Martinez"]
    companies  = ["DHI MORTGAGE COM...","GOODLEAP LLC","INDEPENDENT BANK","INTERNAL REVENUE SERVICE","STATE OF TEXAS","RCN CAPITAL LLC"]
    streets    = ["Rocky Point","Elm Crest","Oak Meadow","Cedar Hills","Pine Valley","Sunset Ridge","River Walk","Mission Hills"]
    doc_types  = [
        ("DEED OF TRUST", False), ("FEDERAL TAX LIEN", True),
        ("STATE TAX LIEN", True), ("UCC 1 REAL PROPERTY", True),
        ("DEED", False), ("AFFIDAVIT", True),
        ("RELEASE OF FTL", True), ("JUDGMENT LIEN", True),
    ]
    records = []
    for i in range(n):
        grantor  = (random.choice(companies) if random.random() > 0.5 else
                    f"{random.choice(lastnames).upper()} {random.choice(firstnames).upper()}")
        grantee  = (random.choice(companies) if random.random() > 0.6 else
                    f"{random.choice(lastnames).upper()} {random.choice(firstnames).upper()}")
        doc_type, _ = random.choice(doc_types)
        doc_num  = f"2024{random.randint(10000000,99999999)}"
        days_ago = random.randint(30, 800)
        filed    = date.today().__class__.fromordinal(date.today().toordinal() - days_ago)
        file_date = filed.strftime("%m/%d/%Y")
        lot   = str(random.randint(1,25))
        block = str(random.randint(1,15))
        address = f"{random.randint(100,9999)} {random.choice(streets)}, San Antonio TX 782{random.randint(10,99)}"
        rec = PropertyRecord(
            document_number=doc_num, file_date=file_date, grantor=grantor,
            grantee=grantee, doc_type=doc_type,
            legal_description=f"Subdivision: Lot {lot} Block {block}",
            property_address=address, lot=lot, block=block,
            source_url=SEARCH_URL,
            days_on_record=days_ago,
            maps_url=make_maps_url(address),
        )
        score_record(rec)
        records.append(rec)
    records.sort(key=lambda r: r.seller_score, reverse=True)
    return records


def records_to_csv(records) -> str:
    """Convert records to CSV string for download."""
    output = io.StringIO()
    fields = ["document_number","file_date","days_on_record","doc_type","grantor","grantee",
              "legal_description","property_address","seller_score",
              "tax_delinquent","code_violation","probate_filing","multiple_liens","divorce_bankruptcy","maps_url"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for r in records:
        writer.writerow({f: getattr(r, f, "") for f in fields})
    return output.getvalue()


def build_dashboard(records):
    def score_color(s):
        return "#ef4444" if s >= 50 else "#f97316" if s >= 25 else "#22c55e"
    def score_label(s):
        return "HOT" if s >= 50 else "WARM" if s >= 25 else "COLD"
    def badge(active, label):
        cls = "badge-active" if active else "badge-inactive"
        return f'<span class="badge {cls}">{label}</span>'
    def days_label(d):
        if d == 0: return ""
        if d > 365: return f'<span class="days-old hot-age">{d}d ↑</span>'
        if d > 180: return f'<span class="days-old warm-age">{d}d</span>'
        return f'<span class="days-old">{d}d</span>'
    def maps_link(url, address):
        if not url:
            return address[:40] if address else "—"
        short = address[:35] + "…" if len(address) > 35 else address
        return f'<a href="{url}" target="_blank" class="maps-link" title="{address}">📍 {short}</a>'

    # Build map markers for hot/warm leads
    map_markers = []
    for r in records:
        if r.seller_score >= 25 and r.property_address:
            map_markers.append({
                "name": r.grantor,
                "address": r.property_address,
                "score": r.seller_score,
                "doc_type": r.doc_type,
                "date": r.file_date,
                "days": r.days_on_record,
                "maps_url": r.maps_url,
            })

    rows_html = ""
    for i, r in enumerate(records):
        flags = (badge(r.tax_delinquent,"Tax Lien") + badge(r.code_violation,"Code Viol.")
                 + badge(r.probate_filing,"Probate") + badge(r.multiple_liens,"Multi-Lien")
                 + badge(r.divorce_bankruptcy,"Divorce/BK"))
        rows_html += f"""<tr class="record-row {'hot-row' if r.seller_score>=50 else ''}" data-score="{r.seller_score}" data-text="{(r.grantor+r.grantee+r.doc_type+r.property_address).lower().replace('"','')}">
          <td class="rank">#{i+1}</td>
          <td><div class="score-circle" style="--c:{score_color(r.seller_score)}">{r.seller_score}</div>
          <div class="score-label" style="color:{score_color(r.seller_score)}">{score_label(r.seller_score)}</div></td>
          <td class="doc-num">{r.document_number}</td>
          <td style="white-space:nowrap;font-size:12px">{r.file_date}</td>
          <td>{days_label(r.days_on_record)}</td>
          <td class="doc-type">{r.doc_type}</td>
          <td class="name" title="{r.grantor}">{r.grantor}</td>
          <td class="name" title="{r.grantee}">{r.grantee}</td>
          <td class="address">{maps_link(r.maps_url, r.property_address or r.legal_description)}</td>
          <td class="flags">{flags}</td></tr>"""

    total = len(records)
    hot   = sum(1 for r in records if r.seller_score >= 50)
    warm  = sum(1 for r in records if 25 <= r.seller_score < 50)
    cold  = total - hot - warm
    avg   = round(sum(r.seller_score for r in records) / total, 1) if total else 0
    gen   = datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")
    is_live = any(r.source_url and "publicsearch" in r.source_url for r in records)
    data_note = "LIVE DATA" if is_live else "DEMO DATA"
    csv_data  = records_to_csv(records).replace("`","").replace("\\","\\\\")
    markers_json = json.dumps(map_markers[:200])  # limit for performance

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>{COUNTY_NAME} Motivated Seller Leads</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@700;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0b0d12;--surface:#13161f;--border:#1e2233;--text:#e2e8f0;--muted:#64748b;--accent:#6366f1;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px;}}
header{{background:linear-gradient(135deg,#0f1117,#13161f);border-bottom:1px solid var(--border);padding:2rem 2.5rem;}}
.header-inner{{max-width:1800px;margin:0 auto;display:flex;align-items:flex-start;justify-content:space-between;gap:2rem;flex-wrap:wrap;}}
.eyebrow{{font-family:'DM Mono',monospace;font-size:11px;color:var(--accent);letter-spacing:.2em;text-transform:uppercase;}}
h1{{font-family:'Syne',sans-serif;font-size:2.2rem;font-weight:800;background:linear-gradient(135deg,#e2e8f0 30%,#6366f1);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}}
.subtitle{{font-size:13px;color:var(--muted);margin-top:.25rem;font-family:'DM Mono',monospace;}}
.data-badge{{display:inline-block;font-family:'DM Mono',monospace;font-size:10px;padding:2px 8px;border-radius:4px;margin-top:6px;background:{'rgba(34,197,94,.15)' if is_live else 'rgba(234,179,8,.15)'};color:{'#86efac' if is_live else '#fde047'};border:1px solid {'rgba(34,197,94,.3)' if is_live else 'rgba(234,179,8,.3)'};}}
.stats{{display:flex;gap:1rem;flex-wrap:wrap;}}
.stat-card{{background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:12px;padding:1rem 1.4rem;min-width:110px;text-align:center;}}
.stat-card .num{{font-family:'Syne',sans-serif;font-size:1.8rem;font-weight:800;line-height:1;}}
.stat-card .lbl{{font-size:11px;color:var(--muted);margin-top:.25rem;text-transform:uppercase;letter-spacing:.1em;}}
.num-hot{{color:#ef4444;}}.num-warm{{color:#f97316;}}.num-cold{{color:#22c55e;}}.num-total{{color:var(--accent);}}.num-avg{{color:#94a3b8;}}
.toolbar{{max-width:1800px;margin:1.5rem auto 0;padding:0 2.5rem;display:flex;align-items:center;gap:1rem;flex-wrap:wrap;}}
.filter-btn{{background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--muted);cursor:pointer;font-family:'DM Mono',monospace;font-size:12px;padding:.5rem 1rem;transition:all .15s;}}
.filter-btn:hover,.filter-btn.active{{border-color:var(--accent);color:var(--text);background:rgba(99,102,241,.1);}}
.search-box{{background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;padding:.5rem 1rem;width:200px;outline:none;}}
.search-box:focus{{border-color:var(--accent);}}.search-box::placeholder{{color:var(--muted);}}
.export-btn{{margin-left:auto;background:rgba(99,102,241,.15);border:1px solid rgba(99,102,241,.4);border-radius:8px;color:#a5b4fc;cursor:pointer;font-family:'DM Mono',monospace;font-size:12px;padding:.5rem 1.2rem;transition:all .15s;}}
.export-btn:hover{{background:rgba(99,102,241,.25);}}
.map-btn{{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);border-radius:8px;color:#86efac;cursor:pointer;font-family:'DM Mono',monospace;font-size:12px;padding:.5rem 1.2rem;transition:all .15s;}}
.map-btn:hover{{background:rgba(34,197,94,.2);}}
.map-section{{max-width:1800px;margin:1.5rem auto 0;padding:0 2.5rem;display:none;}}
.map-section.visible{{display:block;}}
#map{{width:100%;height:500px;border-radius:12px;border:1px solid var(--border);}}
.table-wrap{{max-width:1800px;margin:1.5rem auto 3rem;padding:0 2.5rem;overflow-x:auto;}}
table{{width:100%;border-collapse:collapse;border:1px solid var(--border);border-radius:12px;overflow:hidden;}}
thead th{{background:var(--surface);border-bottom:1px solid var(--border);color:var(--muted);font-family:'DM Mono',monospace;font-size:11px;padding:.9rem 1rem;text-align:left;text-transform:uppercase;white-space:nowrap;}}
tbody tr{{border-bottom:1px solid var(--border);transition:background .1s;}}
tbody tr:hover{{background:rgba(255,255,255,.03);}}.hot-row{{background:rgba(239,68,68,.05);}}.hidden{{display:none;}}
td{{padding:.75rem 1rem;vertical-align:middle;}}
.rank{{color:var(--muted);font-family:'DM Mono',monospace;font-size:12px;text-align:center;}}
.score-circle{{width:44px;height:44px;border-radius:50%;border:2px solid var(--c,#22c55e);color:var(--c,#22c55e);display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif;font-weight:800;font-size:15px;margin:0 auto 4px;}}
.score-label{{text-align:center;font-size:10px;font-family:'DM Mono',monospace;}}
.doc-num{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);}}
.doc-type{{font-size:12px;font-weight:500;white-space:nowrap;}}
.name{{font-weight:500;white-space:nowrap;max-width:150px;overflow:hidden;text-overflow:ellipsis;}}
.address{{max-width:200px;}}
.maps-link{{color:#60a5fa;text-decoration:none;font-size:12px;}}
.maps-link:hover{{color:#93c5fd;text-decoration:underline;}}
.flags{{min-width:200px;}}
.badge{{border-radius:5px;display:inline-block;font-family:'DM Mono',monospace;font-size:10px;font-weight:500;margin:2px 2px 2px 0;padding:2px 7px;white-space:nowrap;}}
.badge-active{{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.3);}}.badge-inactive{{background:rgba(255,255,255,.04);color:#334155;border:1px solid transparent;}}
.days-old{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);white-space:nowrap;}}
.hot-age{{color:#ef4444;font-weight:500;}}.warm-age{{color:#f97316;}}
footer{{border-top:1px solid var(--border);color:var(--muted);font-family:'DM Mono',monospace;font-size:11px;padding:1.25rem;text-align:center;}}
</style></head><body>
<header><div class="header-inner">
  <div>
    <div class="eyebrow">Motivated Seller Intelligence</div>
    <h1>{COUNTY_NAME} Lead Dashboard</h1>
    <div class="subtitle">Generated {gen} &nbsp;·&nbsp; {total} records &nbsp;·&nbsp; bexar.tx.publicsearch.us</div>
    <div class="data-badge">{data_note}</div>
  </div>
  <div class="stats">
    <div class="stat-card"><div class="num num-hot">{hot}</div><div class="lbl">Hot</div></div>
    <div class="stat-card"><div class="num num-warm">{warm}</div><div class="lbl">Warm</div></div>
    <div class="stat-card"><div class="num num-cold">{cold}</div><div class="lbl">Cold</div></div>
    <div class="stat-card"><div class="num num-total">{total}</div><div class="lbl">Total</div></div>
    <div class="stat-card"><div class="num num-avg">{avg}</div><div class="lbl">Avg Score</div></div>
  </div>
</div></header>

<div class="toolbar">
  <button class="filter-btn active" onclick="filterRows('all',this)">All Leads</button>
  <button class="filter-btn" onclick="filterRows('hot',this)">Hot (50+)</button>
  <button class="filter-btn" onclick="filterRows('warm',this)">Warm (25-49)</button>
  <button class="filter-btn" onclick="filterRows('cold',this)">Cold (&lt;25)</button>
  <input class="search-box" type="text" placeholder="Search name, doc type..." oninput="searchRows(this.value)">
  <button class="map-btn" onclick="toggleMap()">🗺 Foreclosure Map</button>
  <button class="export-btn" onclick="exportCSV()">⬇ Export CSV</button>
</div>

<div class="map-section" id="mapSection">
  <div id="map"></div>
</div>

<div class="table-wrap"><table>
  <thead><tr>
    <th>#</th><th>Score</th><th>Doc #</th><th>Date</th><th>Age</th><th>Doc Type</th>
    <th>Grantor (Seller)</th><th>Grantee (Buyer)</th><th>Address</th><th>Signals</th>
  </tr></thead>
  <tbody id="tableBody">{rows_html}</tbody>
</table></div>
<footer>{COUNTY_NAME} Public Records &nbsp;·&nbsp; bexar.tx.publicsearch.us &nbsp;·&nbsp; For informational use only</footer>

<script>
// ── Filter & Search ──────────────────────────────────────────────────────────
let cf='all';
function filterRows(t,b){{cf=t;document.querySelectorAll('.filter-btn').forEach(x=>x.classList.remove('active'));b.classList.add('active');applyFilters(document.querySelector('.search-box').value);}}
function searchRows(q){{applyFilters(q);}}
function applyFilters(query){{
  const q=query.toLowerCase();
  document.querySelectorAll('#tableBody tr').forEach(row=>{{
    const s=parseInt(row.dataset.score||0);
    const t=row.dataset.text||'';
    const mf=cf==='all'||((cf==='hot'&&s>=50)||(cf==='warm'&&s>=25&&s<50)||(cf==='cold'&&s<25));
    row.classList.toggle('hidden',!(mf&&(q===''||t.includes(q))));
  }});
}}

// ── CSV Export ───────────────────────────────────────────────────────────────
const csvData = `{csv_data.replace(chr(96), "'")}`;
function exportCSV(){{
  const blob = new Blob([csvData], {{type:'text/csv'}});
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url;
  a.download = 'bexar-county-leads-{datetime.utcnow().strftime("%Y-%m-%d")}.csv';
  a.click();
  URL.revokeObjectURL(url);
}}

// ── Foreclosure Map ───────────────────────────────────────────────────────────
const markers = {markers_json};
let mapLoaded = false;
let mapVisible = false;

function toggleMap(){{
  const sec = document.getElementById('mapSection');
  mapVisible = !mapVisible;
  sec.classList.toggle('visible', mapVisible);
  if(mapVisible && !mapLoaded){{
    mapLoaded = true;
    loadMap();
  }}
}}

function loadMap(){{
  const mapDiv = document.getElementById('map');
  // Use OpenStreetMap via Leaflet (free, no API key needed)
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
  document.head.appendChild(link);

  const script = document.createElement('script');
  script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
  script.onload = function(){{
    const map = L.map('map').setView([29.4241, -98.4936], 11);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{
      attribution:'© OpenStreetMap contributors'
    }}).addTo(map);

    // Geocode and plot markers using Nominatim
    markers.forEach((m, i) => {{
      setTimeout(() => {{
        const addr = encodeURIComponent(m.address + ', San Antonio, TX');
        fetch(`https://nominatim.openstreetmap.org/search?q=${{addr}}&format=json&limit=1`)
          .then(r => r.json())
          .then(data => {{
            if(!data.length) return;
            const lat = parseFloat(data[0].lat);
            const lng = parseFloat(data[0].lon);
            const color = m.score >= 50 ? '#ef4444' : m.score >= 25 ? '#f97316' : '#22c55e';
            const circle = L.circleMarker([lat,lng], {{
              radius: m.score >= 50 ? 10 : 7,
              fillColor: color, color: '#fff',
              weight: 1.5, opacity: 1, fillOpacity: 0.85
            }}).addTo(map);
            circle.bindPopup(`
              <b>${{m.name}}</b><br>
              ${{m.doc_type}}<br>
              ${{m.address}}<br>
              Filed: ${{m.date}} (${{m.days}}d ago)<br>
              Score: <b>${{m.score}}</b><br>
              <a href="${{m.maps_url}}" target="_blank">Open in Google Maps</a>
            `);
          }})
          .catch(()=>{{}});
      }}, i * 300); // stagger requests to avoid rate limiting
    }});
  }};
  document.head.appendChild(script);
}}
</script>
</body></html>"""


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

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

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "county": COUNTY_NAME,
            "generated_at": datetime.utcnow().isoformat(),
            "total_records": len(records),
            "live_data": any(r.source_url and "publicsearch" in r.source_url for r in records),
            "records": [asdict(r) for r in records],
        }, f, indent=2)
    log.info(f"JSON → {OUTPUT_FILE}")

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(build_dashboard(records))
    log.info(f"Dashboard → {DASHBOARD_FILE}")

    hot  = sum(1 for r in records if r.seller_score >= 50)
    warm = sum(1 for r in records if 25 <= r.seller_score < 50)
    log.info(f"Hot: {hot}  Warm: {warm}  Cold: {len(records)-hot-warm}")


if __name__ == "__main__":
    main()
