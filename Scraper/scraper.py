"""
Motivated Seller Lead Scraper - Bexar County, TX
Features: Clean headers, Days on Record, CSV Export, Warm Leads Map
"""

import json, time, logging, random, csv, io, urllib.parse
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


def calc_days(file_date_str: str) -> int:
    if not file_date_str:
        return 0
    for fmt in ["%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"]:
        try:
            filed = datetime.strptime(file_date_str.strip(), fmt).date()
            return (date.today() - filed).days
        except:
            continue
    return 0


def make_maps_url(name: str) -> str:
    query = urllib.parse.quote(f"{name}, San Antonio, TX")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


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
        except TimeoutException:
            continue
    return None


def extract_rows(driver, selector):
    records = []
    try:
        for row in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 3:
                    continue
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


def build_dashboard(records):
    def score_color(s):
        return "#ef4444" if s>=50 else "#f97316" if s>=25 else "#22c55e"
    def score_label(s):
        return "HOT" if s>=50 else "WARM" if s>=25 else "COLD"
    def badge(active, label):
        cls = "badge-active" if active else "badge-inactive"
        return f'<span class="badge {cls}">{label}</span>'
    def days_label(d):
        if d==0: return "—"
        if d>365: return f'<span class="days hot-age">{d}d</span>'
        if d>180: return f'<span class="days warm-age">{d}d</span>'
        return f'<span class="days">{d}d</span>'
    def active_signals(r):
        sigs = []
        if r.tax_delinquent: sigs.append("Tax Lien")
        if r.code_violation: sigs.append("Code Viol.")
        if r.probate_filing: sigs.append("Probate")
        if r.multiple_liens: sigs.append("Multi-Lien")
        if r.divorce_bankruptcy: sigs.append("Divorce/BK")
        return " ".join(f'<span class="badge badge-active">{s}</span>' for s in sigs) or "—"

    rows_html = ""
    for i, r in enumerate(records):
        maps_link = (f'<a href="{r.maps_url}" target="_blank" class="maps-link">📍 Search Maps</a>'
                     if r.maps_url else "—")
        rows_html += f"""<tr class="record-row {'hot-row' if r.seller_score>=50 else ''}" data-score="{r.seller_score}" data-text="{(r.grantor+r.grantee+r.doc_type).lower().replace('"','')}">
          <td class="rank">#{i+1}</td>
          <td><div class="score-circle" style="--c:{score_color(r.seller_score)}">{r.seller_score}</div>
          <div class="slbl" style="color:{score_color(r.seller_score)}">{score_label(r.seller_score)}</div></td>
          <td class="mono">{r.document_number}</td>
          <td class="nowrap">{r.file_date}</td>
          <td>{days_label(r.days_on_record)}</td>
          <td class="nowrap bold">{r.doc_type}</td>
          <td class="name" title="{r.grantor}">{r.grantor}</td>
          <td class="name" title="{r.grantee}">{r.grantee}</td>
          <td class="mono sm">{r.legal_description[:40]}{'…' if len(r.legal_description)>40 else ''}</td>
          <td>{maps_link}</td>
          <td>{active_signals(r)}</td></tr>"""

    total = len(records)
    hot   = sum(1 for r in records if r.seller_score>=50)
    warm  = sum(1 for r in records if 25<=r.seller_score<50)
    cold  = total-hot-warm
    avg   = round(sum(r.seller_score for r in records)/total,1) if total else 0
    gen   = datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")
    is_live = any(r.source_url and "publicsearch" in r.source_url for r in records)
    data_note = "LIVE DATA" if is_live else "DEMO DATA"
    csv_data  = records_to_csv(records).replace("\\","\\\\").replace("`","'")
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    # Warm + hot leads for map — use Bexar County centroid with slight random offset
    # since we only have names not addresses, we spread pins across SA
    import random as _rnd
    _rnd.seed(42)
    warm_leads = [r for r in records if r.seller_score >= 25]
    map_data = []
    for r in warm_leads:
        # Spread pins across San Antonio bounding box
        lat = 29.3 + _rnd.uniform(0, 0.35)
        lng = -98.65 + _rnd.uniform(0, 0.45)
        map_data.append({
            "name": r.grantor, "score": r.seller_score,
            "doc_type": r.doc_type, "date": r.file_date,
            "days": r.days_on_record, "maps_url": r.maps_url,
            "lat": round(lat,5), "lng": round(lng,5),
        })
    markers_json = json.dumps(map_data)

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>{COUNTY_NAME} Motivated Seller Leads</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@700;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0b0d12;--surface:#13161f;--border:#1e2233;--text:#e2e8f0;--muted:#64748b;--accent:#6366f1;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;}}
header{{background:linear-gradient(135deg,#0f1117,#13161f);border-bottom:1px solid var(--border);padding:1.5rem 2rem;}}
.hi{{max-width:1800px;margin:0 auto;display:flex;align-items:flex-start;justify-content:space-between;gap:2rem;flex-wrap:wrap;}}
.eyebrow{{font-family:'DM Mono',monospace;font-size:10px;color:var(--accent);letter-spacing:.2em;text-transform:uppercase;}}
h1{{font-family:'Syne',sans-serif;font-size:1.9rem;font-weight:800;background:linear-gradient(135deg,#e2e8f0 30%,#6366f1);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;line-height:1.2;}}
.sub{{font-size:12px;color:var(--muted);margin-top:.2rem;font-family:'DM Mono',monospace;}}
.db{{display:inline-block;font-family:'DM Mono',monospace;font-size:10px;padding:2px 8px;border-radius:4px;margin-top:5px;background:{'rgba(34,197,94,.15)' if is_live else 'rgba(234,179,8,.15)'};color:{'#86efac' if is_live else '#fde047'};border:1px solid {'rgba(34,197,94,.3)' if is_live else 'rgba(234,179,8,.3)'};}}
.stats{{display:flex;gap:.75rem;flex-wrap:wrap;}}
.sc{{background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:10px;padding:.75rem 1.2rem;min-width:90px;text-align:center;}}
.sc .n{{font-family:'Syne',sans-serif;font-size:1.6rem;font-weight:800;line-height:1;}}
.sc .l{{font-size:10px;color:var(--muted);margin-top:.2rem;text-transform:uppercase;letter-spacing:.1em;}}
.nh{{color:#ef4444;}}.nw{{color:#f97316;}}.nc{{color:#22c55e;}}.nt{{color:var(--accent);}}.na{{color:#94a3b8;}}
.toolbar{{max-width:1800px;margin:1rem auto 0;padding:0 2rem;display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;}}
.fb{{background:var(--surface);border:1px solid var(--border);border-radius:7px;color:var(--muted);cursor:pointer;font-family:'DM Mono',monospace;font-size:11px;padding:.4rem .9rem;transition:all .15s;}}
.fb:hover,.fb.active{{border-color:var(--accent);color:var(--text);background:rgba(99,102,241,.1);}}
.sb{{background:var(--surface);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:12px;padding:.4rem .9rem;width:180px;outline:none;}}
.sb:focus{{border-color:var(--accent);}}.sb::placeholder{{color:var(--muted);}}
.map-btn{{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);border-radius:7px;color:#86efac;cursor:pointer;font-family:'DM Mono',monospace;font-size:11px;padding:.4rem .9rem;}}
.map-btn:hover{{background:rgba(34,197,94,.2);}}
.exp-btn{{margin-left:auto;background:rgba(99,102,241,.12);border:1px solid rgba(99,102,241,.35);border-radius:7px;color:#a5b4fc;cursor:pointer;font-family:'DM Mono',monospace;font-size:11px;padding:.4rem .9rem;}}
.exp-btn:hover{{background:rgba(99,102,241,.22);}}
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
.score-circle{{width:40px;height:40px;border-radius:50%;border:2px solid var(--c,#22c55e);color:var(--c,#22c55e);display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif;font-weight:800;font-size:14px;margin:0 auto 3px;}}
.slbl{{text-align:center;font-size:9px;font-family:'DM Mono',monospace;}}
.mono{{font-family:'DM Mono',monospace;color:var(--muted);}}
.sm{{font-size:11px;}}
.nowrap{{white-space:nowrap;}}
.bold{{font-weight:500;}}
.name{{white-space:nowrap;max-width:140px;overflow:hidden;text-overflow:ellipsis;}}
.days{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);}}
.hot-age{{color:#ef4444;font-weight:500;}}.warm-age{{color:#f97316;}}
.maps-link{{color:#60a5fa;text-decoration:none;font-size:11px;white-space:nowrap;}}
.maps-link:hover{{color:#93c5fd;}}
.badge{{border-radius:4px;display:inline-block;font-family:'DM Mono',monospace;font-size:9px;font-weight:500;margin:1px;padding:2px 6px;white-space:nowrap;}}
.badge-active{{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.3);}}
footer{{border-top:1px solid var(--border);color:var(--muted);font-family:'DM Mono',monospace;font-size:10px;padding:1rem 2rem;text-align:center;}}
</style></head><body>
<header><div class="hi">
  <div>
    <div class="eyebrow">Motivated Seller Intelligence</div>
    <h1>{COUNTY_NAME}<br>Lead Dashboard</h1>
    <div class="sub">Generated {gen} &nbsp;·&nbsp; {total} records &nbsp;·&nbsp; bexar.tx.publicsearch.us</div>
    <div class="db">{data_note}</div>
  </div>
  <div class="stats">
    <div class="sc"><div class="n nh">{hot}</div><div class="l">Hot</div></div>
    <div class="sc"><div class="n nw">{warm}</div><div class="l">Warm</div></div>
    <div class="sc"><div class="n nc">{cold}</div><div class="l">Cold</div></div>
    <div class="sc"><div class="n nt">{total}</div><div class="l">Total</div></div>
    <div class="sc"><div class="n na">{avg}</div><div class="l">Avg</div></div>
  </div>
</div></header>

<div class="toolbar">
  <button class="fb active" onclick="filt('all',this)">All</button>
  <button class="fb" onclick="filt('hot',this)">🔥 Hot (50+)</button>
  <button class="fb" onclick="filt('warm',this)">⚠️ Warm (25-49)</button>
  <button class="fb" onclick="filt('cold',this)">✓ Cold (&lt;25)</button>
  <input class="sb" type="text" placeholder="Search name, doc type..." oninput="search(this.value)">
  <button class="map-btn" onclick="toggleMap()">🗺️ Warm Leads Map</button>
  <button class="exp-btn" onclick="exportCSV()">⬇ Export CSV</button>
</div>

<div class="map-wrap" id="mapWrap">
  <div class="map-note">Showing {len(map_data)} warm + hot leads plotted across San Antonio &nbsp;·&nbsp; Click any pin for details</div>
  <div id="map"></div>
</div>

<div class="tw"><table>
  <thead><tr>
    <th>#</th><th>Score</th><th>Doc #</th><th>Filed</th><th>Age</th>
    <th>Doc Type</th><th>Grantor (Seller)</th><th>Grantee (Buyer)</th>
    <th>Legal Description</th><th>Maps</th><th>Signals</th>
  </tr></thead>
  <tbody id="tb">{rows_html}</tbody>
</table></div>
<footer>{COUNTY_NAME} Public Records &nbsp;·&nbsp; bexar.tx.publicsearch.us &nbsp;·&nbsp; For informational use only</footer>

<script>
let cf='all';
function filt(t,b){{cf=t;document.querySelectorAll('.fb').forEach(x=>x.classList.remove('active'));b.classList.add('active');apply(document.querySelector('.sb').value);}}
function search(q){{apply(q);}}
function apply(query){{const q=query.toLowerCase();document.querySelectorAll('#tb tr').forEach(row=>{{const s=parseInt(row.dataset.score||0);const t=row.dataset.text||'';const mf=cf==='all'||((cf==='hot'&&s>=50)||(cf==='warm'&&s>=25&&s<50)||(cf==='cold'&&s<25));row.classList.toggle('hidden',!(mf&&(q===''||t.includes(q))));}}); }}

const csvData=`{csv_data}`;
function exportCSV(){{const blob=new Blob([csvData],{{type:'text/csv'}});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='bexar-leads-{today_str}.csv';a.click();URL.revokeObjectURL(url);}}

const markers={markers_json};
let mapLoaded=false,mapVisible=false;
function toggleMap(){{
  const w=document.getElementById('mapWrap');
  mapVisible=!mapVisible;
  w.classList.toggle('show',mapVisible);
  if(mapVisible&&!mapLoaded){{mapLoaded=true;loadMap();}}
}}
function loadMap(){{
  const lc=document.createElement('link');lc.rel='stylesheet';lc.href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';document.head.appendChild(lc);
  const ls=document.createElement('script');ls.src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
  ls.onload=function(){{
    const map=L.map('map').setView([29.4241,-98.4936],11);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{attribution:'© OpenStreetMap'}}).addTo(map);
    markers.forEach(m=>{{
      const color=m.score>=50?'#ef4444':'#f97316';
      const r=m.score>=50?10:7;
      L.circleMarker([m.lat,m.lng],{{radius:r,fillColor:color,color:'#fff',weight:1.5,opacity:1,fillOpacity:0.85}})
       .addTo(map)
       .bindPopup(`<b>${{m.name}}</b><br><b>${{m.doc_type}}</b><br>Filed: ${{m.date}} (${{m.days}} days ago)<br>Score: <b>${{m.score}}</b><br><a href="${{m.maps_url}}" target="_blank">🔍 Search in Google Maps</a>`);
    }});
  }};
  document.head.appendChild(ls);
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
    log.info(f"JSON saved")

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(build_dashboard(records))
    log.info(f"Dashboard saved")

    hot  = sum(1 for r in records if r.seller_score>=50)
    warm = sum(1 for r in records if 25<=r.seller_score<50)
    log.info(f"Hot: {hot}  Warm: {warm}  Cold: {len(records)-hot-warm}")


if __name__ == "__main__":
    main()
