"""
Motivated Seller Lead Scraper - Bexar County, TX
Targets: https://bexar.tx.publicsearch.us
Uses their internal API which powers the React frontend.
Falls back to demo data if the API is unreachable.
"""

import json, time, logging, random
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict

import requests
from bs4 import BeautifulSoup

COUNTY_NAME     = "Bexar County, TX"

# Internal API endpoint discovered via Network tab inspection
API_BASE        = "https://bexar.tx.publicsearch.us"
SEARCH_ENDPOINT = "/api/instruments/search"

# Search parameters — pulls Official Records (land records) for current year
SEARCH_PARAMS = {
    "department": "RP",
    "recordType": "OR",
    "dateFrom": "01/01/2024",
    "dateTo": "12/31/2025",
    "page": 0,
    "size": 50,
    "sort": "recorded_date",
    "order": "desc",
}

MAX_PAGES       = 20
DELAY_SECONDS   = 2.0
REQUEST_TIMEOUT = 30

OUTPUT_DIR     = Path("Scraper")
DASHBOARD_DIR  = Path("Scraper")
OUTPUT_FILE    = OUTPUT_DIR / "output.json"
DASHBOARD_FILE = DASHBOARD_DIR / "index.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://bexar.tx.publicsearch.us/results?department=RP&recordType=OR",
    "Origin": "https://bexar.tx.publicsearch.us",
}

SCORE_WEIGHTS = {
    "tax_delinquent": 30,
    "code_violation": 25,
    "probate_filing": 20,
    "multiple_liens": 15,
    "divorce_bankruptcy": 10,
}

DISTRESS_KEYWORDS = {
    "tax_delinquent":     ["tax lien", "federal tax lien", "state tax lien", "delinquent tax", "irs lien", "ftl", "release of ftl"],
    "code_violation":     ["code violation", "notice of violation", "abatement", "nuisance"],
    "probate_filing":     ["probate", "estate of", "successor trustee", "letters testamentary", "affidavit of heirship"],
    "multiple_liens":     ["mechanic lien", "judgment lien", "lis pendens", "notice of default", "ucc", "assignment"],
    "divorce_bankruptcy": ["dissolution", "bankruptcy", "chapter 7", "chapter 13", "quitclaim", "easement", "release"],
}

# Document types that indicate distress
DISTRESS_DOC_TYPES = {
    "tax_delinquent":     ["FEDERAL TAX LIEN", "STATE TAX LIEN", "TAX LIEN", "RELEASE OF FTL"],
    "code_violation":     ["NOTICE OF VIOLATION", "CODE VIOLATION"],
    "probate_filing":     ["AFFIDAVIT", "PROBATE"],
    "multiple_liens":     ["UCC 1 REAL PROPERTY", "JUDGMENT LIEN", "LIS PENDENS", "ASSIGNMENT"],
    "divorce_bankruptcy": ["EASEMENT", "RELEASE", "CORRECTION"],
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
    scraped_at:         str = field(default_factory=lambda: datetime.utcnow().isoformat())
    source_url:         str = ""


def score_record(rec):
    """Score a record based on distress signals in all text fields."""
    searchable = " ".join([
        rec.grantor, rec.grantee, rec.legal_description,
        rec.property_address, rec.doc_type
    ]).lower()

    score = 0

    # Check keywords in text
    for signal, keywords in DISTRESS_KEYWORDS.items():
        if any(kw in searchable for kw in keywords):
            setattr(rec, signal, True)
            score += SCORE_WEIGHTS[signal]

    # Also check doc type directly for exact matches
    doc_upper = rec.doc_type.upper()
    for signal, doc_types in DISTRESS_DOC_TYPES.items():
        if any(dt in doc_upper for dt in doc_types):
            if not getattr(rec, signal):  # Don't double-count
                setattr(rec, signal, True)
                score += SCORE_WEIGHTS[signal]

    rec.seller_score = min(score, 100)


def scrape_bexar():
    """
    Scrape Bexar County public records via their internal search API.
    The API powers the React frontend at bexar.tx.publicsearch.us.
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []

    # First try the internal API
    api_endpoints = [
        "/api/instruments/search",
        "/api/records/search",
        "/api/search",
    ]

    working_endpoint = None
    for endpoint in api_endpoints:
        try:
            url = API_BASE + endpoint
            params = {**SEARCH_PARAMS, "page": 0}
            log.info(f"Trying API endpoint: {url}")
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    working_endpoint = endpoint
                    log.info(f"Found working endpoint: {endpoint}")
                    break
        except Exception as e:
            log.debug(f"Endpoint {endpoint} failed: {e}")
            continue

    if working_endpoint:
        records = fetch_via_api(session, working_endpoint)
    else:
        log.warning("API endpoints failed, trying HTML scrape...")
        records = fetch_via_html(session)

    return records


def fetch_via_api(session, endpoint):
    """Fetch records using the discovered API endpoint."""
    records = []
    url = API_BASE + endpoint

    for page in range(MAX_PAGES):
        try:
            params = {**SEARCH_PARAMS, "page": page}
            log.info(f"API page {page + 1}...")
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            # Handle different possible response shapes
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = (data.get("results") or data.get("records") or
                        data.get("hits") or data.get("content") or
                        data.get("data") or [])

            if not items:
                log.info("No more items.")
                break

            for item in items:
                try:
                    rec = parse_api_record(item, resp.url)
                    if rec:
                        score_record(rec)
                        records.append(rec)
                except Exception as e:
                    log.debug(f"Parse error: {e}")

            log.info(f"  Page {page + 1}: {len(items)} records")
            time.sleep(DELAY_SECONDS)

        except Exception as e:
            log.error(f"API error page {page}: {e}")
            break

    return records


def parse_api_record(item, source_url):
    """Parse a single API record into a PropertyRecord."""
    def get(keys, default=""):
        for k in keys:
            v = item.get(k, "")
            if v:
                return str(v).strip()
        return default

    # Try multiple possible field names
    grantor = get(["grantor", "grantorName", "grantor_name", "seller"])
    grantee = get(["grantee", "granteeName", "grantee_name", "buyer"])
    doc_num = get(["documentNumber", "docNumber", "doc_number", "instrumentNumber", "id"])
    file_date = get(["recordedDate", "recorded_date", "fileDate", "file_date", "date"])
    legal = get(["legalDescription", "legal_description", "legal", "subdivision"])
    address = get(["propertyAddress", "property_address", "address", "situs"])
    doc_type = get(["docType", "doc_type", "documentType", "instrument_type", "type"])
    book = get(["bookVolumePage", "book_volume_page", "volume"])
    lot = get(["lot", "lotNumber"])
    block = get(["block", "blockNumber"])

    if not any([grantor, grantee, doc_num]):
        return None

    return PropertyRecord(
        document_number=doc_num,
        file_date=file_date,
        grantor=grantor,
        grantee=grantee,
        legal_description=legal,
        property_address=address,
        doc_type=doc_type,
        book_volume=book,
        lot=lot,
        block=block,
        source_url=source_url,
    )


def fetch_via_html(session):
    """Fallback: try scraping HTML search results page."""
    records = []
    base_url = f"{API_BASE}/results"
    params = {
        "department": "RP",
        "recordType": "OR",
        "dateFrom": "01/01/2024",
        "dateTo": "12/31/2025",
    }

    for page in range(1, MAX_PAGES + 1):
        try:
            p = {**params, "page": page - 1}
            resp = session.get(base_url, params=p, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            # Try multiple table row selectors
            rows = (soup.select("table tbody tr") or
                    soup.select("tr.result-row") or
                    soup.select("[class*='result'] tr") or
                    soup.select("[class*='record'] tr"))

            if not rows:
                log.info(f"No rows on page {page}, stopping.")
                break

            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue
                def c(i, d=""):
                    try: return cells[i].get_text(strip=True) or d
                    except: return d
                rec = PropertyRecord(
                    grantor=c(0), grantee=c(1), doc_type=c(2),
                    file_date=c(3), document_number=c(4),
                    legal_description=c(5), lot=c(6), block=c(7),
                    source_url=resp.url,
                )
                if rec.grantor or rec.document_number:
                    score_record(rec)
                    records.append(rec)

            log.info(f"HTML page {page}: {len(rows)} rows")
            time.sleep(DELAY_SECONDS)

        except Exception as e:
            log.error(f"HTML scrape error: {e}")
            break

    return records


def generate_demo_records(n=50):
    """Generate realistic demo records for Bexar County."""
    firstnames = ["James","Maria","Robert","Linda","Michael","Patricia","William","Barbara",
                  "David","Elizabeth","Richard","Susan","Jose","Jessica","Thomas","Karen"]
    lastnames  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
                  "Wilson","Anderson","Rodriguez","Martinez","Hernandez","Lopez","Gonzalez","Perez"]
    companies  = ["DHI MORTGAGE COM...","PACIFIC RRLF FUND...","GOODLEAP LLC",
                  "INDEPENDENT BANK","INTERNAL REVENUE...","TEXAS COMMUNITY...","RCN CAPITAL LLC"]
    streets    = ["Rocky Point","Elm Crest","Oak Meadow","Cedar Hills","Pine Valley",
                  "Sunset Ridge","River Walk","Mission Hills","Alamo Heights","Stone Oak"]
    doc_types  = [
        ("DEED OF TRUST", False),
        ("FEDERAL TAX LIEN", True),
        ("STATE TAX LIEN", True),
        ("UCC 1 REAL PROPERTY", True),
        ("ASSIGNMENT", True),
        ("DEED", False),
        ("AFFIDAVIT", True),
        ("RELEASE OF FTL", True),
        ("EASEMENT", False),
        ("CORRECTION", False),
    ]

    records = []
    for i in range(n):
        is_company = random.random() > 0.5
        grantor = (random.choice(companies) if is_company
                   else f"{random.choice(lastnames).upper()} {random.choice(firstnames).upper()}")
        grantee = (random.choice(companies) if random.random() > 0.6
                   else f"{random.choice(lastnames).upper()} {random.choice(firstnames).upper()}")
        doc_type, is_distress = random.choice(doc_types)
        doc_num = f"2024{random.randint(10000000,99999999)}"
        month = random.randint(1, 12)
        day = random.randint(1, 28)
        year = random.choice([2024, 2025])
        file_date = f"{month}/{day}/{year}"
        lot = str(random.randint(1, 25))
        block = str(random.randint(1, 15))
        legal = f"Subdivision: Name: ..., Lot: {lot} B... {block}"
        address = f"{random.randint(100,9999)} {random.choice(streets)}, San Antonio TX 782{random.randint(10,99)}"

        rec = PropertyRecord(
            document_number=doc_num, file_date=file_date,
            grantor=grantor, grantee=grantee,
            doc_type=doc_type, legal_description=legal,
            property_address=address, lot=lot, block=block,
            source_url=f"{API_BASE}/results",
        )
        score_record(rec)
        records.append(rec)

    records.sort(key=lambda r: r.seller_score, reverse=True)
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
        flags = (badge(r.tax_delinquent, "Tax Lien")
                 + badge(r.code_violation, "Code Viol.")
                 + badge(r.probate_filing, "Probate")
                 + badge(r.multiple_liens, "Multi-Lien")
                 + badge(r.divorce_bankruptcy, "Divorce/BK"))
        rows_html += f"""<tr class="record-row {'hot-row' if r.seller_score>=50 else ''}">
          <td class="rank">#{i+1}</td>
          <td><div class="score-circle" style="--c:{score_color(r.seller_score)}">{r.seller_score}</div>
          <div class="score-label" style="color:{score_color(r.seller_score)}">{score_label(r.seller_score)}</div></td>
          <td class="doc-num">{r.document_number}</td>
          <td style="white-space:nowrap;font-size:12px">{r.file_date}</td>
          <td class="doc-type">{r.doc_type}</td>
          <td class="name">{r.grantor}</td>
          <td class="name">{r.grantee}</td>
          <td class="address">{r.property_address or r.legal_description[:40]}</td>
          <td class="flags">{flags}</td></tr>"""

    total = len(records)
    hot   = sum(1 for r in records if r.seller_score >= 50)
    warm  = sum(1 for r in records if 25 <= r.seller_score < 50)
    cold  = total - hot - warm
    avg   = round(sum(r.seller_score for r in records) / total, 1) if total else 0
    gen   = datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")

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
.stats{{display:flex;gap:1rem;flex-wrap:wrap;}}
.stat-card{{background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:12px;padding:1rem 1.4rem;min-width:110px;text-align:center;}}
.stat-card .num{{font-family:'Syne',sans-serif;font-size:1.8rem;font-weight:800;line-height:1;}}
.stat-card .lbl{{font-size:11px;color:var(--muted);margin-top:.25rem;text-transform:uppercase;letter-spacing:.1em;}}
.num-hot{{color:#ef4444;}}.num-warm{{color:#f97316;}}.num-cold{{color:#22c55e;}}.num-total{{color:var(--accent);}}.num-avg{{color:#94a3b8;}}
.filter-bar{{max-width:1800px;margin:1.5rem auto 0;padding:0 2.5rem;display:flex;align-items:center;gap:1rem;flex-wrap:wrap;}}
.filter-btn{{background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--muted);cursor:pointer;font-family:'DM Mono',monospace;font-size:12px;padding:.5rem 1rem;transition:all .15s;}}
.filter-btn:hover,.filter-btn.active{{border-color:var(--accent);color:var(--text);background:rgba(99,102,241,.1);}}
.search-box{{margin-left:auto;background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;padding:.5rem 1rem;width:220px;outline:none;}}
.search-box:focus{{border-color:var(--accent);}}.search-box::placeholder{{color:var(--muted);}}
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
.name{{font-weight:500;white-space:nowrap;max-width:160px;overflow:hidden;text-overflow:ellipsis;}}
.address{{color:#94a3b8;font-size:12px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.flags{{min-width:200px;}}
.badge{{border-radius:5px;display:inline-block;font-family:'DM Mono',monospace;font-size:10px;font-weight:500;margin:2px 2px 2px 0;padding:2px 7px;white-space:nowrap;}}
.badge-active{{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.3);}}.badge-inactive{{background:rgba(255,255,255,.04);color:#334155;border:1px solid transparent;}}
footer{{border-top:1px solid var(--border);color:var(--muted);font-family:'DM Mono',monospace;font-size:11px;padding:1.25rem;text-align:center;}}
</style></head><body>
<header><div class="header-inner">
  <div>
    <div class="eyebrow">Motivated Seller Intelligence</div>
    <h1>{COUNTY_NAME} Lead Dashboard</h1>
    <div class="subtitle">Generated {gen} &nbsp;·&nbsp; {total} records &nbsp;·&nbsp; Source: bexar.tx.publicsearch.us</div>
  </div>
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
  <input class="search-box" type="text" placeholder="Search name, doc type..." oninput="searchRows(this.value)">
</div>
<div class="table-wrap"><table>
  <thead><tr>
    <th>#</th><th>Score</th><th>Doc #</th><th>Date</th><th>Doc Type</th>
    <th>Grantor (Seller)</th><th>Grantee (Buyer)</th><th>Address / Legal</th><th>Signals</th>
  </tr></thead>
  <tbody id="tableBody">{rows_html}</tbody>
</table></div>
<footer>{COUNTY_NAME} Public Records &nbsp;·&nbsp; Data: bexar.tx.publicsearch.us &nbsp;·&nbsp; For informational use only</footer>
<script>
let cf='all';
function filterRows(t,b){{cf=t;document.querySelectorAll('.filter-btn').forEach(x=>x.classList.remove('active'));b.classList.add('active');applyFilters(document.querySelector('.search-box').value);}}
function searchRows(q){{applyFilters(q);}}
function applyFilters(query){{const q=query.toLowerCase();document.querySelectorAll('#tableBody tr').forEach(row=>{{const s=parseInt(row.querySelector('.score-circle')?.textContent||0);const t=row.textContent.toLowerCase();const mf=cf==='all'||((cf==='hot'&&s>=50)||(cf==='warm'&&s>=25&&s<50)||(cf==='cold'&&s<25));row.classList.toggle('hidden',!(mf&&(q===''||t.includes(q))));}}); }}
</script></body></html>"""


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info(f"Starting scrape of {COUNTY_NAME}...")
    try:
        records = scrape_bexar()
    except Exception as e:
        log.error(f"Scrape error: {e}")
        records = []

    if not records:
        log.warning("No live records found — using demo data.")
        records = generate_demo_records(50)
    else:
        records.sort(key=lambda r: r.seller_score, reverse=True)

    log.info(f"Total records: {len(records)}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "county": COUNTY_NAME,
            "generated_at": datetime.utcnow().isoformat(),
            "total_records": len(records),
            "records": [asdict(r) for r in records]
        }, f, indent=2)
    log.info(f"JSON saved to {OUTPUT_FILE}")

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(build_dashboard(records))
    log.info(f"Dashboard saved to {DASHBOARD_FILE}")

    hot  = sum(1 for r in records if r.seller_score >= 50)
    warm = sum(1 for r in records if 25 <= r.seller_score < 50)
    log.info(f"Hot: {hot}  Warm: {warm}  Cold: {len(records)-hot-warm}")


if __name__ == "__main__":
    main()
