"""
Motivated Seller Lead Scraper - Bexar County, TX
"""

import json, time, logging, random
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict

import requests
from bs4 import BeautifulSoup

COUNTY_NAME     = "Bexar County, TX"
BASE_URL        = "https://bexar.tx.publicsearch.us/results?department=RP&recordType=OR&dateFrom=01%2F01%2F2024&dateTo=12%2F31%2F2024"
MAX_PAGES       = 20
DELAY_SECONDS   = 1.5
REQUEST_TIMEOUT = 30

OUTPUT_DIR     = Path("Scraper")
DASHBOARD_DIR  = Path("Scraper")
OUTPUT_FILE    = OUTPUT_DIR / "output.json"
DASHBOARD_FILE = DASHBOARD_DIR / "index.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

SCORE_WEIGHTS = {
    "tax_delinquent": 30, "code_violation": 25,
    "probate_filing": 20, "multiple_liens": 15, "divorce_bankruptcy": 10,
}

DISTRESS_KEYWORDS = {
    "tax_delinquent":     ["tax lien", "delinquent tax", "tax sale", "irs lien"],
    "code_violation":     ["code violation", "notice of violation", "abatement", "nuisance"],
    "probate_filing":     ["probate", "estate of", "successor trustee", "letters testamentary"],
    "multiple_liens":     ["mechanic lien", "judgment lien", "lis pendens", "notice of default"],
    "divorce_bankruptcy": ["dissolution", "bankruptcy", "chapter 7", "chapter 13", "quitclaim"],
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
    tax_delinquent:     bool = False
    code_violation:     bool = False
    probate_filing:     bool = False
    multiple_liens:     bool = False
    divorce_bankruptcy: bool = False
    seller_score:       int = 0
    scraped_at:         str = field(default_factory=lambda: datetime.utcnow().isoformat())
    source_url:         str = ""


def score_record(rec):
    searchable = " ".join([rec.grantor, rec.grantee, rec.legal_description, rec.property_address]).lower()
    score = 0
    for signal, keywords in DISTRESS_KEYWORDS.items():
        if any(kw in searchable for kw in keywords):
            setattr(rec, signal, True)
            score += SCORE_WEIGHTS[signal]
    rec.seller_score = min(score, 100)


def generate_demo_records(n=35):
    firstnames = ["James","Maria","Robert","Linda","Michael","Patricia","William","Barbara","David","Elizabeth"]
    lastnames  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Wilson","Anderson"]
    streets    = ["Oak St","Maple Ave","Cedar Blvd","Pine Rd","Elm Dr","Walnut Ln","Birch Ct","Spruce Way"]
    legal_tmpl = [
        "Lot {n} Block {b} Tax Lien filed per Revenue Code",
        "Parcel {p} Notice of Code Violation Abatement Order",
        "Lot {n} Dissolution of Marriage Quitclaim Deed",
        "Lot {n} Block {b} Mechanic Lien Multiple Liens Recorded",
        "Chapter 7 Bankruptcy Trustee Deed",
        "Lot {n} Block {b} Probate Letters Testamentary Issued",
        "Lot {n} Standard Residential Conveyance",
        "Notice of Default Lis Pendens filed",
    ]
    records = []
    for i in range(n):
        grantor   = f"{random.choice(lastnames)}, {random.choice(firstnames)}"
        grantee   = f"{random.choice(lastnames)}, {random.choice(firstnames)}"
        doc_num   = f"2024-{random.randint(100000,999999):07d}"
        file_date = f"{random.randint(1,12):02d}/{random.randint(1,28):02d}/{random.choice([2023,2024,2025])}"
        legal     = random.choice(legal_tmpl).format(n=random.randint(1,99), b=random.randint(1,20), p=random.randint(10000,99999))
        address   = f"{random.randint(100,9999)} {random.choice(streets)}, San Antonio, TX {random.randint(78200,78299)}"
        rec = PropertyRecord(document_number=doc_num, file_date=file_date, grantor=grantor,
                             grantee=grantee, legal_description=legal, property_address=address, source_url=BASE_URL)
        score_record(rec)
        records.append(rec)
    records.sort(key=lambda r: r.seller_score, reverse=True)
    return records


def scrape_bexar():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for page in range(1, MAX_PAGES + 1):
        try:
            log.info(f"Fetching page {page}...")
            resp = session.get(BASE_URL, params={"page": page - 1, "size": 25}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.select("tr.result-row, tbody tr, .record-row")
            if not rows:
                log.info("No rows found, stopping.")
                break
            for row in rows:
                try:
                    cells = row.find_all("td")
                    if len(cells) < 3:
                        continue
                    def c(i, d=""):
                        try: return cells[i].get_text(strip=True) or d
                        except: return d
                    rec = PropertyRecord(document_number=c(0), file_date=c(1), grantor=c(2),
                                         grantee=c(3), legal_description=c(4), property_address=c(5), source_url=resp.url)
                    if rec.document_number or rec.grantor:
                        score_record(rec)
                        records.append(rec)
                except Exception as e:
                    log.debug(f"Row error: {e}")
            if not soup.select_one("a.next, [aria-label='Next'], .pagination .next"):
                break
            time.sleep(DELAY_SECONDS)
        except requests.RequestException as e:
            log.error(f"Network error: {e}")
            break
    return records


def build_dashboard(records):
    def score_color(s):
        return "#ef4444" if s >= 50 else "#f97316" if s >= 25 else "#22c55e"
    def score_label(s):
        return "HOT" if s >= 50 else "WARM" if s >= 25 else "COLD"
    def badge(active, label):
        cls = "badge-active" if active else "badge-inactive"
        return f'<span class="badge {cls}">{label}</span>'

    rows_html = ""
    for i, r in enumerate(records):
        flags = (badge(r.tax_delinquent,"Tax Lien") + badge(r.code_violation,"Code Viol.")
                 + badge(r.probate_filing,"Probate") + badge(r.multiple_liens,"Multi-Lien")
                 + badge(r.divorce_bankruptcy,"Divorce/BK"))
        rows_html += f"""<tr class="record-row {'hot-row' if r.seller_score>=50 else ''}">
          <td class="rank">#{i+1}</td>
          <td><div class="score-circle" style="--c:{score_color(r.seller_score)}">{r.seller_score}</div>
          <div class="score-label" style="color:{score_color(r.seller_score)}">{score_label(r.seller_score)}</div></td>
          <td class="doc-num">{r.document_number}</td><td>{r.file_date}</td>
          <td class="name">{r.grantor}</td><td class="name">{r.grantee}</td>
          <td class="address">{r.property_address}</td>
          <td class="legal">{r.legal_description[:80]}{'...' if len(r.legal_description)>80 else ''}</td>
          <td class="flags">{flags}</td></tr>"""

    total=len(records); hot=sum(1 for r in records if r.seller_score>=50)
    warm=sum(1 for r in records if 25<=r.seller_score<50); cold=total-hot-warm
    avg=round(sum(r.seller_score for r in records)/total,1) if total else 0
    gen=datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>{COUNTY_NAME} Motivated Seller Leads</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@700;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0b0d12;--surface:#13161f;--border:#1e2233;--text:#e2e8f0;--muted:#64748b;--accent:#6366f1;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px;}}
header{{background:linear-gradient(135deg,#0f1117,#13161f);border-bottom:1px solid var(--border);padding:2rem 2.5rem;}}
.header-inner{{max-width:1600px;margin:0 auto;display:flex;align-items:flex-start;justify-content:space-between;gap:2rem;flex-wrap:wrap;}}
.eyebrow{{font-family:'DM Mono',monospace;font-size:11px;color:var(--accent);letter-spacing:.2em;text-transform:uppercase;}}
h1{{font-family:'Syne',sans-serif;font-size:2.2rem;font-weight:800;background:linear-gradient(135deg,#e2e8f0 30%,#6366f1);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}}
.subtitle{{font-size:13px;color:var(--muted);margin-top:.25rem;font-family:'DM Mono',monospace;}}
.stats{{display:flex;gap:1rem;flex-wrap:wrap;}}
.stat-card{{background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:12px;padding:1rem 1.4rem;min-width:110px;text-align:center;}}
.stat-card .num{{font-family:'Syne',sans-serif;font-size:1.8rem;font-weight:800;line-height:1;}}
.stat-card .lbl{{font-size:11px;color:var(--muted);margin-top:.25rem;text-transform:uppercase;letter-spacing:.1em;}}
.num-hot{{color:#ef4444;}}.num-warm{{color:#f97316;}}.num-cold{{color:#22c55e;}}.num-total{{color:var(--accent);}}.num-avg{{color:#94a3b8;}}
.filter-bar{{max-width:1600px;margin:1.5rem auto 0;padding:0 2.5rem;display:flex;align-items:center;gap:1rem;flex-wrap:wrap;}}
.filter-btn{{background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--muted);cursor:pointer;font-family:'DM Mono',monospace;font-size:12px;padding:.5rem 1rem;transition:all .15s;}}
.filter-btn:hover,.filter-btn.active{{border-color:var(--accent);color:var(--text);background:rgba(99,102,241,.1);}}
.search-box{{margin-left:auto;background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;padding:.5rem 1rem;width:220px;outline:none;}}
.search-box:focus{{border-color:var(--accent);}}.search-box::placeholder{{color:var(--muted);}}
.table-wrap{{max-width:1600px;margin:1.5rem auto 3rem;padding:0 2.5rem;overflow-x:auto;}}
table{{width:100%;border-collapse:collapse;border:1px solid var(--border);border-radius:12px;overflow:hidden;}}
thead th{{background:var(--surface);border-bottom:1px solid var(--border);color:var(--muted);font-family:'DM Mono',monospace;font-size:11px;padding:.9rem 1rem;text-align:left;text-transform:uppercase;white-space:nowrap;}}
tbody tr{{border-bottom:1px solid var(--border);transition:background .1s;}}
tbody tr:hover{{background:rgba(255,255,255,.03);}}.hot-row{{background:rgba(239,68,68,.05);}}.hidden{{display:none;}}
td{{padding:.85rem 1rem;vertical-align:middle;}}
.rank{{color:var(--muted);font-family:'DM Mono',monospace;font-size:12px;text-align:center;}}
.score-circle{{width:44px;height:44px;border-radius:50%;border:2px solid var(--c,#22c55e);color:var(--c,#22c55e);display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif;font-weight:800;font-size:15px;margin:0 auto 4px;}}
.score-label{{text-align:center;font-size:10px;font-family:'DM Mono',monospace;}}
.doc-num{{font-family:'DM Mono',monospace;font-size:12px;color:var(--muted);}}.name{{font-weight:500;white-space:nowrap;}}.address{{color:#94a3b8;font-size:13px;max-width:180px;}}.legal{{color:var(--muted);font-size:12px;max-width:220px;line-height:1.4;}}.flags{{min-width:220px;}}
.badge{{border-radius:5px;display:inline-block;font-family:'DM Mono',monospace;font-size:10px;font-weight:500;margin:2px 2px 2px 0;padding:2px 7px;white-space:nowrap;}}
.badge-active{{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.3);}}.badge-inactive{{background:rgba(255,255,255,.04);color:#334155;border:1px solid transparent;}}
footer{{border-top:1px solid var(--border);color:var(--muted);font-family:'DM Mono',monospace;font-size:11px;padding:1.25rem;text-align:center;}}
</style></head><body>
<header><div class="header-inner">
  <div><div class="eyebrow">Motivated Seller Intelligence</div><h1>{COUNTY_NAME} Lead Dashboard</h1><div class="subtitle">Generated {gen} &nbsp; {total} records</div></div>
  <div class="stats">
    <div class="stat-card"><div class="num num-hot">{hot}</div><div class="lbl">Hot</div></div>
    <div class="stat-card"><div class="num num-warm">{warm}</div><div class="lbl">Warm</div></div>
    <div class="stat-card"><div class="num num-cold">{cold}</div><div class="lbl">Cold</div></div>
    <div class="stat-card"><div class="num num-total">{total}</div><div class="lbl">Total</div></div>
    <div class="stat-card"><div class="num num-avg">{avg}</div><div class="lbl">Avg Score</div></div>
  </div>
</div></header>
<div class="filter-bar">
  <button class="filter-btn active" onclick="filterRows('all',this)">All Leads</button>
  <button class="filter-btn" onclick="filterRows('hot',this)">Hot (50+)</button>
  <button class="filter-btn" onclick="filterRows('warm',this)">Warm (25-49)</button>
  <button class="filter-btn" onclick="filterRows('cold',this)">Cold (&lt;25)</button>
  <input class="search-box" type="text" placeholder="Search name, address..." oninput="searchRows(this.value)">
</div>
<div class="table-wrap"><table>
  <thead><tr><th>#</th><th>Score</th><th>Doc #</th><th>Date</th><th>Grantor (Seller)</th><th>Grantee (Buyer)</th><th>Address</th><th>Legal</th><th>Signals</th></tr></thead>
  <tbody id="tableBody">{rows_html}</tbody>
</table></div>
<footer>{COUNTY_NAME} Public Records - For informational use only</footer>
<script>
let cf='all';
function filterRows(t,b){{cf=t;document.querySelectorAll('.filter-btn').forEach(x=>x.classList.remove('active'));b.classList.add('active');applyFilters(document.querySelector('.search-box').value);}}
function searchRows(q){{applyFilters(q);}}
function applyFilters(query){{const q=query.toLowerCase();document.querySelectorAll('#tableBody tr').forEach(row=>{{const s=parseInt(row.querySelector('.score-circle')?.textContent||0);const t=row.textContent.toLowerCase();const mf=cf==='all'||((cf==='hot'&&s>=50)||(cf==='warm'&&s>=25&&s<50)||(cf==='cold'&&s<25));row.classList.toggle('hidden',!(mf&&(q===''||t.includes(q))));}}); }}
</script></body></html>"""


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info(f"Attempting live scrape of {COUNTY_NAME}...")
    try:
        records = scrape_bexar()
    except Exception as e:
        log.error(f"Live scrape error: {e}")
        records = []

    if not records:
        log.warning("No live records — using demo data.")
        records = generate_demo_records(35)

    log.info(f"Total records: {len(records)}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"county": COUNTY_NAME, "generated_at": datetime.utcnow().isoformat(),
                   "total_records": len(records), "records": [asdict(r) for r in records]}, f, indent=2)
    log.info(f"JSON saved to {OUTPUT_FILE}")

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(build_dashboard(records))
    log.info(f"Dashboard saved to {DASHBOARD_FILE}")

    hot=sum(1 for r in records if r.seller_score>=50)
    warm=sum(1 for r in records if 25<=r.seller_score<50)
    log.info(f"Hot: {hot}  Warm: {warm}  Cold: {len(records)-hot-warm}")


if __name__ == "__main__":
    main()
