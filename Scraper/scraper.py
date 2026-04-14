"""
Motivated Seller Lead Scraper - Bexar County, TX
Scrapes Bexar County and writes combined dashboard with Harris County data.
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

# ─── CONFIG ──────────────────────────────────────────────────────────────────
COUNTY_NAME      = "Bexar County, TX"
SEARCH_URL       = ("https://bexar.tx.publicsearch.us/results"
                    "?department=RP&recordType=OR"
                    "&dateFrom=01%2F01%2F2024&dateTo=12%2F31%2F2025")
DASHBOARD_URL    = "https://andrewwinter13-ops.github.io/Bexar-County-Intel/Scraper/index.html"
MAX_PAGES        = 20
PAGE_LOAD_WAIT   = 15
BETWEEN_PAGES    = 3
NOTIFY_MIN_SCORE = 30

OUTPUT_DIR     = Path("Scraper")
OUTPUT_FILE    = OUTPUT_DIR / "output.json"
DASHBOARD_FILE = OUTPUT_DIR / "index.html"
PREV_DOCS_FILE = OUTPUT_DIR / "prev_doc_numbers.json"
SLACK_WEBHOOK  = os.environ.get("SLACK_WEBHOOK_URL", "")

# ─── SCORING ─────────────────────────────────────────────────────────────────
SCORE_WEIGHTS = {
    "tax_delinquent": 30, "code_violation": 25,
    "probate_filing": 20, "multiple_liens": 30, "divorce_bankruptcy": 10,
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


# ─── DATA MODEL ──────────────────────────────────────────────────────────────
@dataclass
class PropertyRecord:
    document_number:    str  = ""
    file_date:          str  = ""
    grantor:            str  = ""
    grantee:            str  = ""
    legal_description:  str  = ""
    property_address:   str  = ""
    doc_type:           str  = ""
    lot:                str  = ""
    block:              str  = ""
    county:             str  = "Bexar"
    tax_delinquent:     bool = False
    code_violation:     bool = False
    probate_filing:     bool = False
    multiple_liens:     bool = False
    divorce_bankruptcy: bool = False
    seller_score:       int  = 0
    days_on_record:     int  = 0
    maps_url:           str  = ""
    scraped_at:         str  = field(default_factory=lambda: datetime.utcnow().isoformat())
    source_url:         str  = ""


# ─── HELPERS ─────────────────────────────────────────────────────────────────
def calc_days(s: str) -> int:
    if not s: return 0
    for fmt in ["%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"]:
        try: return (date.today() - datetime.strptime(s.strip(), fmt).date()).days
        except: continue
    return 0

def make_maps_url(name: str, city: str = "San Antonio") -> str:
    q = urllib.parse.quote(f"{name}, {city}, TX")
    return f"https://www.google.com/maps/search/?api=1&query={q}"

def score_record(rec):
    blob = " ".join([rec.grantor, rec.grantee, rec.legal_description, rec.doc_type]).lower()
    score = 0
    for sig, kws in DISTRESS_KEYWORDS.items():
        if any(kw in blob for kw in kws):
            setattr(rec, sig, True); score += SCORE_WEIGHTS[sig]
    for sig, dts in DISTRESS_DOC_TYPES.items():
        if any(dt in rec.doc_type.upper() for dt in dts):
            if not getattr(rec, sig): setattr(rec, sig, True); score += SCORE_WEIGHTS[sig]
    d = rec.days_on_record
    if d > 365: score += 15
    elif d > 180: score += 10
    elif d > 90:  score += 5
    rec.seller_score = min(score, 100)


# ─── SLACK ───────────────────────────────────────────────────────────────────
def slack_send(msg):
    if not SLACK_WEBHOOK: return
    try:
        req = Request(SLACK_WEBHOOK, json.dumps(msg).encode(), {"Content-Type":"application/json"})
        urlopen(req, timeout=10)
    except URLError as e: log.error(f"Slack: {e}")

def slack_daily_summary(bexar_records, harris_records, new_leads):
    total_b = len(bexar_records); total_h = len(harris_records)
    above30 = sum(1 for r in bexar_records+harris_records if r.seller_score>=30)
    hot     = sum(1 for r in bexar_records+harris_records if r.seller_score>=50)
    slack_send({"blocks":[
        {"type":"header","text":{"type":"plain_text","text":f"Lead Scraper — {datetime.utcnow().strftime('%B %d, %Y')}"}},
        {"type":"section","fields":[
            {"type":"mrkdwn","text":f"*Bexar County:*\n{total_b} records"},
            {"type":"mrkdwn","text":f"*Harris County:*\n{total_h} records"},
            {"type":"mrkdwn","text":f"*Score 30+:*\n{above30}"},
            {"type":"mrkdwn","text":f"*Hot (50+):*\n{hot}"},
            {"type":"mrkdwn","text":f"*New Today:*\n{len(new_leads)}"},
        ]},
        {"type":"divider"},
        {"type":"section","text":{"type":"mrkdwn","text":f"<{DASHBOARD_URL}|Open Combined Dashboard>"}}
    ]})

def slack_new_alerts(new_leads):
    if not new_leads: return
    lines = []
    for r in new_leads[:10]:
        sigs = [s for s,f in [("Tax Lien",r.tax_delinquent),("Code Viol.",r.code_violation),
                               ("Probate",r.probate_filing),("Multi-Lien",r.multiple_liens),
                               ("Divorce/BK",r.divorce_bankruptcy)] if f]
        emoji = "🔥" if r.seller_score>=50 else "⚠️"
        lines.append(f"{emoji} *{r.grantor}* [{r.county}] — Score: *{r.seller_score}*\n"
                     f"   {r.doc_type} · Filed: {r.file_date} ({r.days_on_record}d ago)\n"
                     f"   {' · '.join(sigs) or r.doc_type}\n"
                     f"   <{r.maps_url}|Search Maps>")
    overflow = f"\n_...and {len(new_leads)-10} more_" if len(new_leads)>10 else ""
    slack_send({"blocks":[
        {"type":"header","text":{"type":"plain_text","text":f"New Leads (30+): {len(new_leads)}"}},
        {"type":"section","text":{"type":"mrkdwn","text":"\n\n".join(lines)+overflow}},
        {"type":"section","text":{"type":"mrkdwn","text":f"<{DASHBOARD_URL}|Open Dashboard>"}}
    ]})


# ─── TRACKING ────────────────────────────────────────────────────────────────
def load_prev() -> set:
    try:
        if PREV_DOCS_FILE.exists():
            return set(json.loads(PREV_DOCS_FILE.read_text()))
    except: pass
    return set()

def save_prev(records):
    PREV_DOCS_FILE.write_text(json.dumps([r.document_number for r in records if r.document_number]))

def find_new(records, prev) -> list:
    return [r for r in records if r.seller_score>=NOTIFY_MIN_SCORE and r.document_number not in prev]


# ─── SELENIUM ────────────────────────────────────────────────────────────────
def make_driver():
    opts = Options()
    for arg in ["--headless","--no-sandbox","--disable-dev-shm-usage",
                "--disable-gpu","--window-size=1920,1080",
                "--disable-blink-features=AutomationControlled"]:
        opts.add_argument(arg)
    opts.add_experimental_option("excludeSwitches",["enable-automation"])
    opts.add_experimental_option("useAutomationExtension",False)
    opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36")
    d = webdriver.Chrome(options=opts)
    d.execute_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    return d

def wait_rows(driver):
    wait = WebDriverWait(driver, PAGE_LOAD_WAIT)
    for sel in ["table tbody tr","tr.result-row","[class*='ResultRow']","[class*='tableRow']"]:
        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR,sel)))
            if driver.find_elements(By.CSS_SELECTOR,sel): return sel
        except TimeoutException: continue
    return None

def extract(driver, sel, county="Bexar", city="San Antonio"):
    recs = []
    for row in driver.find_elements(By.CSS_SELECTOR,sel):
        try:
            cells = row.find_elements(By.TAG_NAME,"td")
            if len(cells)<3: continue
            def c(i,d=""):
                try: return cells[i].text.strip() or d
                except: return d
            fd=c(3); g=c(0)
            rec = PropertyRecord(grantor=g,grantee=c(1),doc_type=c(2),
                file_date=fd,document_number=c(4),legal_description=c(5),
                lot=c(6),block=c(7),county=county,source_url=driver.current_url,
                days_on_record=calc_days(fd),maps_url=make_maps_url(g,city))
            if rec.grantor or rec.document_number:
                score_record(rec); recs.append(rec)
        except Exception as e: log.debug(f"Row: {e}")
    return recs

def next_page(driver):
    for sel in ["button[aria-label='Next page']","a.next","[class*='pagination'] button:last-child"]:
        try:
            b = driver.find_element(By.CSS_SELECTOR,sel)
            if b.is_enabled() and b.is_displayed():
                driver.execute_script("arguments[0].click();",b); return True
        except: continue
    for b in driver.find_elements(By.TAG_NAME,"button"):
        if b.text.strip().lower() in ["next","›","»"] and b.is_enabled():
            driver.execute_script("arguments[0].click();",b); return True
    return False

def scrape_county(url, county, city):
    driver = make_driver(); records = []
    try:
        log.info(f"Scraping {county} — {url}")
        driver.get(url); time.sleep(5)
        sel = wait_rows(driver)
        if not sel:
            log.warning(f"No rows for {county}.")
            Path(f"Scraper/debug_{county.lower()}.html").write_text(driver.page_source)
            return []
        for pg in range(1, MAX_PAGES+1):
            log.info(f"  {county} page {pg}..."); time.sleep(2)
            pr = extract(driver, sel, county, city)
            records.extend(pr)
            log.info(f"  {len(pr)} records (total {len(records)})")
            if not pr: break
            if not next_page(driver): break
            time.sleep(BETWEEN_PAGES)
            try: WebDriverWait(driver,10).until(EC.staleness_of(driver.find_elements(By.CSS_SELECTOR,sel)[0]))
            except: time.sleep(2)
    except Exception as e: log.error(f"{county} error: {e}",exc_info=True)
    finally: driver.quit()
    return records


# ─── DEMO DATA ───────────────────────────────────────────────────────────────
def demo(n=50, county="Bexar", city="San Antonio"):
    fnames = ["James","Maria","Robert","Linda","Michael","Patricia","William","Barbara","David","Elizabeth"]
    lnames = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Wilson","Anderson","Rodriguez","Martinez"]
    cos    = ["INTERNAL REVENUE SERVICE","STATE OF TEXAS","GOODLEAP LLC","INDEPENDENT BANK","RCN CAPITAL LLC"]
    dtypes = [("DEED OF TRUST",False),("FEDERAL TAX LIEN",True),("STATE TAX LIEN",True),
              ("JUDGMENT LIEN",True),("DEED",False),("AFFIDAVIT",True),("RELEASE OF FTL",True)]
    recs = []
    for _ in range(n):
        g  = (random.choice(cos) if random.random()>.5 else
              f"{random.choice(lnames).upper()} {random.choice(fnames).upper()}")
        ge = (random.choice(cos) if random.random()>.6 else
              f"{random.choice(lnames).upper()} {random.choice(fnames).upper()}")
        dt,_ = random.choice(dtypes)
        days  = random.randint(30,800)
        fd    = date.fromordinal(date.today().toordinal()-days).strftime("%m/%d/%Y")
        rec   = PropertyRecord(
            document_number=f"2024{random.randint(10000000,99999999)}",
            file_date=fd,grantor=g,grantee=ge,doc_type=dt,county=county,
            legal_description=f"Lot {random.randint(1,25)} Block {random.randint(1,15)}",
            source_url=SEARCH_URL,days_on_record=days,maps_url=make_maps_url(g,city))
        score_record(rec); recs.append(rec)
    return recs


# ─── CSV ─────────────────────────────────────────────────────────────────────
def to_csv(records):
    buf = io.StringIO()
    fields = ["county","document_number","file_date","days_on_record","doc_type","grantor","grantee",
              "legal_description","seller_score","tax_delinquent","code_violation",
              "probate_filing","multiple_liens","divorce_bankruptcy","maps_url"]
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in records: w.writerow({f:getattr(r,f,"") for f in fields})
    return buf.getvalue()


# ─── COMBINED DASHBOARD ───────────────────────────────────────────────────────
def build_dashboard(all_records, new_docs):
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
    def county_badge(county):
        if county=="Bexar":
            return '<span class="county-badge bexar">Bexar</span>'
        return '<span class="county-badge harris">Harris</span>'

    rows = ""
    for i,r in enumerate(all_records):
        nb = '<span class="new-badge">NEW</span>' if r.document_number in new_docs else ""
        ml = (f'<a href="{r.maps_url}" target="_blank" class="maps-link">Search Maps</a>'
              if r.maps_url else "—")
        rows += (f'<tr class="record-row {"hot-row" if r.seller_score>=50 else ""}" '
                 f'data-score="{r.seller_score}" data-county="{r.county}" '
                 f'data-text="{(r.grantor+r.grantee+r.doc_type+r.county).lower().replace(chr(34),"")}"> '
                 f'<td class="rank">#{i+1}{nb}</td>'
                 f'<td>{county_badge(r.county)}</td>'
                 f'<td><div class="score-circle" style="--c:{sc(r.seller_score)}">{r.seller_score}</div>'
                 f'<div class="slbl" style="color:{sc(r.seller_score)}">{sl(r.seller_score)}</div></td>'
                 f'<td class="mono sm">{r.document_number}</td>'
                 f'<td class="nowrap sm">{r.file_date}</td>'
                 f'<td>{dl(r.days_on_record)}</td>'
                 f'<td class="nowrap bold">{r.doc_type}</td>'
                 f'<td class="name" title="{r.grantor}">{r.grantor}</td>'
                 f'<td class="name" title="{r.grantee}">{r.grantee}</td>'
                 f'<td class="mono sm">{r.legal_description[:35]}{"…" if len(r.legal_description)>35 else ""}</td>'
                 f'<td>{ml}</td>'
                 f'<td>{sigs(r)}</td></tr>')

    bexar   = [r for r in all_records if r.county=="Bexar"]
    harris  = [r for r in all_records if r.county=="Harris"]
    total   = len(all_records)
    hot     = sum(1 for r in all_records if r.seller_score>=50)
    warm    = sum(1 for r in all_records if 25<=r.seller_score<50)
    cold    = total-hot-warm
    above30 = sum(1 for r in all_records if r.seller_score>=30)
    avg     = round(sum(r.seller_score for r in all_records)/total,1) if total else 0
    gen     = datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")
    live    = any(r.source_url and ("publicsearch" in r.source_url or "cclerk" in r.source_url) for r in all_records)
    dnote   = "LIVE DATA" if live else "DEMO DATA"
    csv_d   = to_csv(all_records).replace("\\","\\\\").replace("`","'")
    today   = datetime.utcnow().strftime("%Y-%m-%d")

    import random as _r; _r.seed(42)
    map_data = []
    for r in [x for x in all_records if x.seller_score>=25]:
        if r.county=="Bexar":
            lat=round(29.3+_r.uniform(0,.35),5); lng=round(-98.65+_r.uniform(0,.45),5)
        else:
            lat=round(29.6+_r.uniform(0,.35),5); lng=round(-95.65+_r.uniform(0,.45),5)
        map_data.append({"name":r.grantor,"score":r.seller_score,"doc_type":r.doc_type,
                         "date":r.file_date,"days":r.days_on_record,"maps_url":r.maps_url,
                         "county":r.county,"lat":lat,"lng":lng})
    mj = json.dumps(map_data)
    nd = json.dumps(list(new_docs))

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>TX Motivated Seller Leads — Bexar & Harris</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@700;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0b0d12;--surface:#13161f;--border:#1e2233;--text:#e2e8f0;--muted:#64748b;--accent:#6366f1;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;}}
header{{background:linear-gradient(135deg,#0f1117,#13161f);border-bottom:1px solid var(--border);padding:1.5rem 2rem;}}
.hi{{max-width:1800px;margin:0 auto;display:flex;align-items:flex-start;justify-content:space-between;gap:2rem;flex-wrap:wrap;}}
.eyebrow{{font-family:'DM Mono',monospace;font-size:10px;color:var(--accent);letter-spacing:.2em;text-transform:uppercase;}}
h1{{font-family:'Syne',sans-serif;font-size:1.9rem;font-weight:800;background:linear-gradient(135deg,#e2e8f0 30%,#6366f1);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}}
.sub{{font-size:12px;color:var(--muted);margin-top:.2rem;font-family:'DM Mono',monospace;}}
.db{{display:inline-block;font-family:'DM Mono',monospace;font-size:10px;padding:2px 8px;border-radius:4px;margin-top:5px;
  background:{"rgba(34,197,94,.15)" if live else "rgba(234,179,8,.15)"};
  color:{"#86efac" if live else "#fde047"};
  border:1px solid {"rgba(34,197,94,.3)" if live else "rgba(234,179,8,.3)"};}}
.county-split{{display:flex;gap:1.5rem;margin-top:.5rem;flex-wrap:wrap;}}
.county-stat{{font-family:'DM Mono',monospace;font-size:11px;}}
.county-stat .cn{{font-weight:500;}}
.bexar-dot{{color:#6366f1;}}.harris-dot{{color:#f97316;}}
.stats{{display:flex;gap:.75rem;flex-wrap:wrap;}}
.sc{{background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:10px;padding:.75rem 1.2rem;min-width:90px;text-align:center;}}
.sc .n{{font-family:'Syne',sans-serif;font-size:1.6rem;font-weight:800;line-height:1;}}
.sc .l{{font-size:10px;color:var(--muted);margin-top:.2rem;text-transform:uppercase;letter-spacing:.1em;}}
.nh{{color:#ef4444;}}.nw{{color:#f97316;}}.nc{{color:#22c55e;}}.nt{{color:var(--accent);}}.na{{color:#94a3b8;}}.n30{{color:#a78bfa;}}
.toolbar{{max-width:1800px;margin:1rem auto 0;padding:0 2rem;display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;}}
.fb{{background:var(--surface);border:1px solid var(--border);border-radius:7px;color:var(--muted);cursor:pointer;font-family:'DM Mono',monospace;font-size:11px;padding:.4rem .9rem;transition:all .15s;}}
.fb:hover,.fb.active{{border-color:var(--accent);color:var(--text);background:rgba(99,102,241,.1);}}
.fb.bexar-btn.active{{border-color:#6366f1;background:rgba(99,102,241,.15);color:#a5b4fc;}}
.fb.harris-btn.active{{border-color:#f97316;background:rgba(249,115,22,.15);color:#fdba74;}}
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
.new-badge{{display:inline-block;background:rgba(167,139,250,.2);color:#c4b5fd;border:1px solid rgba(167,139,250,.4);border-radius:3px;font-family:'DM Mono',monospace;font-size:8px;padding:1px 4px;margin-left:4px;vertical-align:middle;}}
.county-badge{{border-radius:4px;font-family:'DM Mono',monospace;font-size:10px;font-weight:500;padding:2px 7px;white-space:nowrap;display:inline-block;}}
.county-badge.bexar{{background:rgba(99,102,241,.15);color:#a5b4fc;border:1px solid rgba(99,102,241,.3);}}
.county-badge.harris{{background:rgba(249,115,22,.15);color:#fdba74;border:1px solid rgba(249,115,22,.3);}}
.score-circle{{width:40px;height:40px;border-radius:50%;border:2px solid var(--c,#22c55e);color:var(--c,#22c55e);display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif;font-weight:800;font-size:14px;margin:0 auto 3px;}}
.slbl{{text-align:center;font-size:9px;font-family:'DM Mono',monospace;}}
.mono{{font-family:'DM Mono',monospace;color:var(--muted);}}.sm{{font-size:11px;}}.nowrap{{white-space:nowrap;}}.bold{{font-weight:500;}}
.name{{white-space:nowrap;max-width:130px;overflow:hidden;text-overflow:ellipsis;}}
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
    <h1>Texas Multi-County Lead Dashboard</h1>
    <div class="sub">Generated {gen} &nbsp;·&nbsp; {total} total records</div>
    <div class="county-split">
      <div class="county-stat"><span class="bexar-dot">●</span> <span class="cn">Bexar County</span> — {len(bexar)} records</div>
      <div class="county-stat"><span class="harris-dot">●</span> <span class="cn">Harris County</span> — {len(harris)} records</div>
    </div>
    <div class="db">{dnote}</div>
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
  <button class="fb bexar-btn" onclick="filtCounty('Bexar',this)">● Bexar</button>
  <button class="fb harris-btn" onclick="filtCounty('Harris',this)">● Harris</button>
  <button class="fb" onclick="filt('hot',this)">🔥 Hot (50+)</button>
  <button class="fb" onclick="filt('warm',this)">⚠️ Warm (25-49)</button>
  <button class="fb" onclick="filt('30',this)">⭐ Score 30+</button>
  <button class="fb" onclick="filt('new',this)">🆕 New Today</button>
  <button class="fb" onclick="filt('cold',this)">✓ Cold</button>
  <input class="sb" type="text" placeholder="Search name, doc type..." oninput="apply(this.value)">
  <button class="map-btn" onclick="toggleMap()">🗺️ Leads Map</button>
  <button class="exp-btn" onclick="exportCSV()">⬇ Export CSV</button>
</div>

<div class="map-wrap" id="mapWrap">
  <div class="map-note">
    <span style="color:#a5b4fc">● Bexar County</span> &nbsp;·&nbsp;
    <span style="color:#fdba74">● Harris County</span> &nbsp;·&nbsp;
    Warm + hot leads · Click any pin for details
  </div>
  <div id="map"></div>
</div>

<div class="tw"><table>
  <thead><tr>
    <th>#</th><th>County</th><th>Score</th><th>Doc #</th><th>Filed</th><th>Age</th>
    <th>Doc Type</th><th>Grantor (Seller)</th><th>Grantee (Buyer)</th>
    <th>Legal</th><th>Maps</th><th>Signals</th>
  </tr></thead>
  <tbody id="tb">{rows}</tbody>
</table></div>
<footer>Bexar County · bexar.tx.publicsearch.us &nbsp;|&nbsp; Harris County · cclerk.hctx.net &nbsp;·&nbsp; For informational use only</footer>

<script>
const newDocs=new Set({nd});
let cf='all', cc='all';

function filt(t,b){{
  cf=t; cc='all';
  document.querySelectorAll('.fb').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  apply(document.querySelector('.sb').value);
}}

function filtCounty(county,b){{
  cc=county; cf='all';
  document.querySelectorAll('.fb').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  apply(document.querySelector('.sb').value);
}}

function apply(query){{
  const q=query.toLowerCase();
  document.querySelectorAll('#tb tr').forEach(row=>{{
    const s=parseInt(row.dataset.score||0);
    const t=row.dataset.text||'';
    const co=row.dataset.county||'';
    const rk=row.querySelector('.rank')?.textContent||'';
    const isNew=Array.from(newDocs).some(d=>rk.includes(d));
    const matchCounty = cc==='all' || co===cc;
    const matchFilter = cf==='all'||(cf==='hot'&&s>=50)||(cf==='warm'&&s>=25&&s<50)||
                        (cf==='30'&&s>=30)||(cf==='new'&&isNew)||(cf==='cold'&&s<25);
    row.classList.toggle('hidden',!(matchCounty&&matchFilter&&(q===''||t.includes(q))));
  }});
}}

const csvData=`{csv_d}`;
function exportCSV(){{
  const blob=new Blob([csvData],{{type:'text/csv'}});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url;a.download='tx-leads-{today}.csv';a.click();
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
    const map=L.map('map').setView([29.8,-97.5],7);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{attribution:'© OpenStreetMap'}}).addTo(map);
    markers.forEach(m=>{{
      const isBexar=m.county==='Bexar';
      const color=m.score>=50?'#ef4444':(isBexar?'#6366f1':'#f97316');
      L.circleMarker([m.lat,m.lng],{{radius:m.score>=50?10:7,fillColor:color,color:'#fff',weight:1.5,opacity:1,fillOpacity:0.85}})
       .addTo(map).bindPopup('<b>'+m.name+'</b> ['+m.county+']<br>'+m.doc_type+'<br>Filed: '+m.date+' ('+m.days+'d ago)<br>Score: <b>'+m.score+'</b><br><a href="'+m.maps_url+'" target="_blank">Search in Google Maps</a>');
    }});
  }};document.head.appendChild(ls);
}}
</script></body></html>"""


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    prev = load_prev()
    log.info(f"Previously seen: {len(prev)}")

    # Scrape Bexar County
    log.info("=== Scraping Bexar County ===")
    try:
        bexar_records = scrape_county(
            "https://bexar.tx.publicsearch.us/results?department=RP&recordType=OR&dateFrom=01%2F01%2F2024&dateTo=12%2F31%2F2025",
            "Bexar", "San Antonio"
        )
    except Exception as e:
        log.error(f"Bexar failed: {e}"); bexar_records = []

    if not bexar_records:
        log.warning("Using Bexar demo data.")
        bexar_records = demo(50, "Bexar", "San Antonio")

    # Load Harris County data if it exists
    log.info("=== Loading Harris County data ===")
    harris_records = []
    harris_file = Path("Harris/output.json")
    if harris_file.exists():
        try:
            harris_data = json.loads(harris_file.read_text())
            for r in harris_data.get("records", []):
                rec = PropertyRecord(**{k:v for k,v in r.items() if k in PropertyRecord.__dataclass_fields__})
                rec.county = "Harris"
                harris_records.append(rec)
            log.info(f"Loaded {len(harris_records)} Harris records from file.")
        except Exception as e:
            log.error(f"Harris load error: {e}")
    else:
        log.warning("No Harris data yet — using demo.")
        harris_records = demo(50, "Harris", "Houston")

    # Combine and sort
    all_records = bexar_records + harris_records
    all_records.sort(key=lambda r: r.seller_score, reverse=True)
    log.info(f"Total combined: {len(all_records)}")

    # New leads detection
    new_leads = find_new(all_records, prev)
    new_docs  = {r.document_number for r in new_leads}
    log.info(f"New 30+ leads: {len(new_leads)}")
    save_prev(all_records)

    # Save Bexar JSON
    with open(OUTPUT_FILE,"w",encoding="utf-8") as f:
        json.dump({"county":COUNTY_NAME,"generated_at":datetime.utcnow().isoformat(),
                   "total_records":len(bexar_records),"new_leads_count":len(new_leads),
                   "live_data":any(r.source_url and "publicsearch" in r.source_url for r in bexar_records),
                   "records":[asdict(r) for r in bexar_records]},f,indent=2)
    log.info("Bexar JSON saved.")

    # Build combined dashboard
    with open(DASHBOARD_FILE,"w",encoding="utf-8") as f:
        f.write(build_dashboard(all_records, new_docs))
    log.info("Combined dashboard saved.")

    # Slack
    slack_daily_summary(bexar_records, harris_records, new_leads)
    if new_leads: slack_new_alerts(new_leads)

    hot  = sum(1 for r in all_records if r.seller_score>=50)
    warm = sum(1 for r in all_records if 25<=r.seller_score<50)
    log.info(f"Hot:{hot} Warm:{warm} Cold:{len(all_records)-hot-warm}")


if __name__ == "__main__":
    main()
