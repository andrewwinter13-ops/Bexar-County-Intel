"""
Motivated Seller Lead Scraper
=============================
Scrapes public county records and scores leads based on distress signals.

CONFIGURATION:
  - Set COUNTY_NAME, BASE_URL, and field selectors below to match your county's website.
  - Run: python src/scraper.py
  - Output: /data/output.json and /dashboard/index.html

Author: Production-ready template — adapt selectors to your county's HTML structure.
"""

import json
import time
import re
import os
import logging
import random
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these to match your county
# ─────────────────────────────────────────────────────────────────────────────

COUNTY_NAME = "Bexar County"
BASE_URL    = "https://bexar.tx.publicsearch.us/results?department=RP&recordType=OR&dateFrom=01%2F01%2F2024&dateTo=12%2F31%2F2024"
MAX_PAGES       = 20          # Safety cap on pagination
DELAY_SECONDS   = 1.5         # Polite delay between requests
REQUEST_TIMEOUT = 30          # Seconds before request times out

OUTPUT_DIR    = Path("data")
DASHBOARD_DIR = Path("dashboard")
OUTPUT_FILE   = OUTPUT_DIR / "output.json"
DASHBOARD_FILE = DASHBOARD_DIR / "index.html"

# HTTP headers — mimic a real browser to avoid 403 blocks
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ─────────────────────────────────────────────────────────────────────────────
# SCORING WEIGHTS — distress signal → score contribution
# ─────────────────────────────────────────────────────────────────────────────

SCORE_WEIGHTS = {
    "tax_delinquent": 30,
    "code_violation": 25,
    "probate_filing": 20,
    "multiple_liens": 15,
    "divorce_bankruptcy": 10,
}

# Keywords used to detect distress signals in legal descriptions / document types
DISTRESS_KEYWORDS = {
    "tax_delinquent":    ["tax lien", "delinquent tax", "tax sale", "irs lien"],
    "code_violation":    ["code violation", "notice of violation", "abatement", "nuisance"],
    "probate_filing":    ["probate", "estate of", "successor trustee", "letters testamentary"],
    "multiple_liens":    ["mechanic lien", "judgment lien", "lis pendens", "notice of default"],
    "divorce_bankruptcy":["dissolution", "bankruptcy", "chapter 7", "chapter 13", "quitclaim"],
}

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PropertyRecord:
    """Represents one scraped county property record."""
    document_number: str = ""
    file_date:       str = ""
    grantor:         str = ""   # Seller name
    grantee:         str = ""   # Buyer name
    legal_description: str = ""
    property_address:  str = ""

    # Distress signals (populated by scorer)
    tax_delinquent:    bool = False
    code_violation:    bool = False
    probate_filing:    bool = False
    multiple_liens:    bool = False
    divorce_bankruptcy: bool = False

    seller_score: int = 0
    scraped_at:   str = field(default_factory=lambda: datetime.utcnow().isoformat())
    source_url:   str = ""

# ─────────────────────────────────────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────────────────────────────────────

class CountyScraper:
    """
    Fetches and parses public grantor/grantee records from the county recorder website.

    HOW TO ADAPT:
      1. Inspect the county website with browser DevTools (F12 → Network).
      2. Find the request that loads the records table (look for XHR/Fetch calls or
         a plain HTML page with a <table>).
      3. Update `_fetch_page()` with the correct URL / POST params.
      4. Update `_parse_records()` with the correct CSS selectors for your table.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.records: list[PropertyRecord] = []

    # ── Public API ─────────────────────────────────────────────────────────

    def run(self) -> list[PropertyRecord]:
        """Entry point — iterates pages and returns scored records."""
        log.info(f"Starting scrape of {COUNTY_NAME} records…")
        page = 1

        while page <= MAX_PAGES:
            log.info(f"  Fetching page {page}…")
            try:
                soup, next_exists = self._fetch_page(page)
                page_records = self._parse_records(soup, page)
                self.records.extend(page_records)
                log.info(f"  → {len(page_records)} records on page {page}")
            except requests.RequestException as exc:
                log.error(f"  Network error on page {page}: {exc}")
                break
            except Exception as exc:
                log.error(f"  Parse error on page {page}: {exc}", exc_info=True)
                break

            if not next_exists or not page_records:
                log.info("  No more pages.")
                break

            page += 1
            time.sleep(DELAY_SECONDS + random.uniform(0, 0.5))   # polite delay

        log.info(f"Scrape complete. Total raw records: {len(self.records)}")
        self._score_all()
        self.records.sort(key=lambda r: r.seller_score, reverse=True)
        return self.records

    # ── Fetching ───────────────────────────────────────────────────────────

    def _fetch_page(self, page: int) -> tuple[BeautifulSoup, bool]:
        """
        Download one page of records.

        ⚙️  ADAPT THIS METHOD to match your county's pagination scheme.
        Common patterns:
          • Query string:  ?page=2   or  ?PageNum=2
          • POST body:     {"page": 2, "pageSize": 25}
          • ASP.NET ViewState: requires extracting __VIEWSTATE and __EVENTVALIDATION

        Returns (BeautifulSoup, has_next_page).
        """
        params = {"page": page}   # ← adjust to your county's param name

        response = self.session.get(
            BASE_URL,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # Detect whether a "next page" link exists
        # ⚙️  ADAPT: find the actual next-page element on your county's site
        next_btn = soup.select_one("a.next-page, [aria-label='Next page'], .pagination .next")
        has_next = next_btn is not None and "disabled" not in next_btn.get("class", [])

        return soup, has_next

    # ── Parsing ────────────────────────────────────────────────────────────

    def _parse_records(self, soup: BeautifulSoup, page: int) -> list[PropertyRecord]:
        """
        Extract PropertyRecord objects from the page's HTML.

        ⚙️  ADAPT THIS METHOD — replace CSS selectors with ones that match
        your county recorder's actual table/list structure.

        Example for a standard <table> layout:
          rows = soup.select("table#recordsTable tbody tr")
        """
        records: list[PropertyRecord] = []

        # ── Generic table row extraction ──────────────────────────────────
        # Tries multiple common table selectors; first match wins.
        row_selectors = [
            "table.records-table tbody tr",
            "table#searchResults tbody tr",
            "table.dataTable tbody tr",
            ".record-row",
            "tr[data-record-id]",
        ]

        rows = []
        for sel in row_selectors:
            rows = soup.select(sel)
            if rows:
                break

        if not rows:
            log.warning(f"  No rows matched on page {page}. Check CSS selectors.")
            return records

        for row in rows:
            try:
                rec = self._extract_record(row, page)
                if rec:
                    records.append(rec)
            except Exception as exc:
                # Log bad rows but continue processing the rest
                log.debug(f"  Skipping malformed row: {exc}")

        return records

    def _extract_record(self, row, page: int) -> Optional[PropertyRecord]:
        """
        Parse a single table row into a PropertyRecord.

        ⚙️  ADAPT column indices (td_text calls) to match your county's column order.
        Alternatively, match by header name if the site uses <th> column headers.
        """
        cells = row.find_all("td")
        if len(cells) < 4:
            return None   # Skip header rows or short rows

        def td_text(idx: int, default: str = "") -> str:
            """Safely extract and clean text from a table cell."""
            try:
                return cells[idx].get_text(separator=" ", strip=True) or default
            except IndexError:
                return default

        # ── Field mapping — ⚙️ adjust column indices ─────────────────────
        rec = PropertyRecord(
            document_number  = td_text(0),
            file_date        = td_text(1),
            grantor          = td_text(2),
            grantee          = td_text(3),
            legal_description= td_text(4),
            property_address = td_text(5),
            source_url       = f"{BASE_URL}?page={page}",
        )

        # Skip completely empty records
        if not any([rec.document_number, rec.grantor, rec.grantee]):
            return None

        return rec

    # ── Scoring ────────────────────────────────────────────────────────────

    def _score_all(self) -> None:
        """Apply distress-signal scoring to every record."""
        for rec in self.records:
            self._score_record(rec)

    def _score_record(self, rec: PropertyRecord) -> None:
        """
        Score one record by searching for distress keywords in text fields.
        Populates boolean flags and computes seller_score (0–100).
        """
        # Combine all text fields into one lowercase blob for keyword scanning
        searchable = " ".join([
            rec.grantor, rec.grantee, rec.legal_description,
            rec.property_address, rec.document_number,
        ]).lower()

        score = 0

        for signal, keywords in DISTRESS_KEYWORDS.items():
            if any(kw in searchable for kw in keywords):
                setattr(rec, signal, True)
                score += SCORE_WEIGHTS[signal]

        # Cap at 100
        rec.seller_score = min(score, 100)

# ─────────────────────────────────────────────────────────────────────────────
# DEMO DATA GENERATOR  (used when live scraping is not possible)
# ─────────────────────────────────────────────────────────────────────────────

def generate_demo_records(n: int = 35) -> list[PropertyRecord]:
    """
    Generate realistic-looking demo records so the pipeline and dashboard
    can be tested without live internet access.

    Remove this function and its call in main() when deploying against a
    real county website.
    """
    import random

    firstnames = ["James","Maria","Robert","Linda","Michael","Patricia","William","Barbara",
                  "David","Elizabeth","Richard","Susan","Joseph","Jessica","Thomas","Karen"]
    lastnames  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
                  "Wilson","Anderson","Taylor","Thomas","Jackson","White","Harris","Martin"]
    streets    = ["Oak St","Maple Ave","Cedar Blvd","Pine Rd","Elm Dr","Walnut Ln",
                  "Birch Ct","Spruce Way","Ash Pl","Hickory Rd"]
    doc_types  = ["Grant Deed","Quitclaim Deed","Notice of Default","Lis Pendens",
                  "Tax Lien","Mechanic Lien","Probate Deed","Trustee Deed",
                  "Judgment Lien","Deed of Trust"]
    legal_tmpl = [
        "Lot {n}, Block {b}, Tract {t} Estate of {name}",
        "Parcel {p} Tax Lien filed per Revenue Code §{c}",
        "Lot {n} Dissolution of Marriage – Quitclaim",
        "Notice of Code Violation – Abatement Order #{p}",
        "Lot {n} Block {b} Mechanic Lien – Multiple Liens Recorded",
        "Chapter 7 Bankruptcy – Trustee Deed to {name}",
        "Lot {n} Standard Residential Conveyance",
        "Lot {n} Block {b} Probate – Letters Testamentary Issued",
    ]

    records = []
    used_docs = set()

    for i in range(n):
        grantor  = f"{random.choice(lastnames)}, {random.choice(firstnames)}"
        grantee  = f"{random.choice(lastnames)}, {random.choice(firstnames)}"
        doc_num  = f"2024-{random.randint(100000,999999):07d}"
        while doc_num in used_docs:
            doc_num = f"2024-{random.randint(100000,999999):07d}"
        used_docs.add(doc_num)

        month = random.randint(1,12)
        day   = random.randint(1,28)
        year  = random.choice([2023, 2024, 2025])
        file_date = f"{month:02d}/{day:02d}/{year}"

        legal = random.choice(legal_tmpl).format(
            n=random.randint(1,99), b=random.randint(1,20),
            t=random.randint(1000,9999), p=random.randint(10000,99999),
            c=random.randint(100,999), name=grantor,
        )
        address = (f"{random.randint(100,9999)} {random.choice(streets)}, "
                   f"{COUNTY_NAME.replace(' County','')}, CA "
                   f"{random.randint(91900,92199)}")

        rec = PropertyRecord(
            document_number=doc_num,
            file_date=file_date,
            grantor=grantor,
            grantee=grantee,
            legal_description=legal,
            property_address=address,
            source_url=BASE_URL,
        )

        # Run the scorer (reuse the same logic)
        searchable = " ".join([
            rec.grantor, rec.grantee, rec.legal_description, rec.property_address
        ]).lower()
        score = 0
        for signal, keywords in DISTRESS_KEYWORDS.items():
            if any(kw in searchable for kw in keywords):
                setattr(rec, signal, True)
                score += SCORE_WEIGHTS[signal]
        rec.seller_score = min(score, 100)

        records.append(rec)

    records.sort(key=lambda r: r.seller_score, reverse=True)
    return records

# ─────────────────────────────────────────────────────────────────────────────
# HTML DASHBOARD GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

def build_dashboard(records: list[PropertyRecord]) -> str:
    """Render a self-contained HTML dashboard from the scored records list."""

    def score_color(s: int) -> str:
        if s >= 50: return "#ef4444"   # red — hot lead
        if s >= 25: return "#f97316"   # orange — warm lead
        return "#22c55e"               # green — cold lead

    def score_label(s: int) -> str:
        if s >= 50: return "🔥 HOT"
        if s >= 25: return "⚠️ WARM"
        return "✓ COLD"

    def badge(active: bool, label: str) -> str:
        cls = "badge-active" if active else "badge-inactive"
        return f'<span class="badge {cls}">{label}</span>'

    rows_html = ""
    for i, r in enumerate(records):
        flags = (
            badge(r.tax_delinquent,    "Tax Delinquent")
            + badge(r.code_violation,  "Code Violation")
            + badge(r.probate_filing,  "Probate")
            + badge(r.multiple_liens,  "Multi-Lien")
            + badge(r.divorce_bankruptcy, "Divorce/BK")
        )
        rows_html += f"""
        <tr class="record-row {'hot-row' if r.seller_score>=50 else ''}">
          <td class="rank">#{i+1}</td>
          <td>
            <div class="score-circle" style="--c:{score_color(r.seller_score)}">
              {r.seller_score}
            </div>
            <div class="score-label" style="color:{score_color(r.seller_score)}">
              {score_label(r.seller_score)}
            </div>
          </td>
          <td class="doc-num">{r.document_number}</td>
          <td>{r.file_date}</td>
          <td class="name">{r.grantor}</td>
          <td class="name">{r.grantee}</td>
          <td class="address">{r.property_address}</td>
          <td class="legal">{r.legal_description[:80]}{'…' if len(r.legal_description)>80 else ''}</td>
          <td class="flags">{flags}</td>
        </tr>"""

    total       = len(records)
    hot_count   = sum(1 for r in records if r.seller_score >= 50)
    warm_count  = sum(1 for r in records if 25 <= r.seller_score < 50)
    cold_count  = total - hot_count - warm_count
    avg_score   = round(sum(r.seller_score for r in records) / total, 1) if total else 0
    generated   = datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{COUNTY_NAME} — Motivated Seller Leads</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@700;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:       #0b0d12;
    --surface:  #13161f;
    --border:   #1e2233;
    --text:     #e2e8f0;
    --muted:    #64748b;
    --accent:   #6366f1;
    --red:      #ef4444;
    --orange:   #f97316;
    --green:    #22c55e;
    --hot-bg:   rgba(239,68,68,0.05);
  }}

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    font-size: 14px;
    min-height: 100vh;
  }}

  /* ── HEADER ── */
  header {{
    background: linear-gradient(135deg, #0f1117 0%, #13161f 50%, #0a0c14 100%);
    border-bottom: 1px solid var(--border);
    padding: 2rem 2.5rem 1.5rem;
    position: relative;
    overflow: hidden;
  }}
  header::before {{
    content: '';
    position: absolute;
    top: -80px; right: -80px;
    width: 320px; height: 320px;
    background: radial-gradient(circle, rgba(99,102,241,0.15) 0%, transparent 70%);
    pointer-events: none;
  }}
  .header-inner {{
    max-width: 1600px;
    margin: 0 auto;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 2rem;
    flex-wrap: wrap;
  }}
  .header-brand {{ display: flex; flex-direction: column; gap: 0.25rem; }}
  .eyebrow {{
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    color: var(--accent);
    letter-spacing: 0.2em;
    text-transform: uppercase;
  }}
  h1 {{
    font-family: 'Syne', sans-serif;
    font-size: clamp(1.6rem, 3vw, 2.4rem);
    font-weight: 800;
    line-height: 1.1;
    background: linear-gradient(135deg, #e2e8f0 30%, #6366f1);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }}
  .subtitle {{
    font-size: 13px;
    color: var(--muted);
    margin-top: 0.25rem;
    font-family: 'DM Mono', monospace;
  }}

  /* ── STAT CARDS ── */
  .stats {{
    display: flex;
    gap: 1rem;
    flex-wrap: wrap;
  }}
  .stat-card {{
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1rem 1.4rem;
    min-width: 110px;
    text-align: center;
  }}
  .stat-card .num {{
    font-family: 'Syne', sans-serif;
    font-size: 1.8rem;
    font-weight: 800;
    line-height: 1;
  }}
  .stat-card .lbl {{
    font-size: 11px;
    color: var(--muted);
    margin-top: 0.25rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }}
  .num-hot    {{ color: var(--red); }}
  .num-warm   {{ color: var(--orange); }}
  .num-cold   {{ color: var(--green); }}
  .num-total  {{ color: var(--accent); }}
  .num-avg    {{ color: #94a3b8; }}

  /* ── FILTER BAR ── */
  .filter-bar {{
    max-width: 1600px;
    margin: 1.5rem auto 0;
    padding: 0 2.5rem;
    display: flex;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
  }}
  .filter-btn {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--muted);
    cursor: pointer;
    font-family: 'DM Mono', monospace;
    font-size: 12px;
    padding: 0.5rem 1rem;
    transition: all 0.15s;
  }}
  .filter-btn:hover, .filter-btn.active {{
    border-color: var(--accent);
    color: var(--text);
    background: rgba(99,102,241,0.1);
  }}
  .search-box {{
    margin-left: auto;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 13px;
    padding: 0.5rem 1rem;
    width: 220px;
    outline: none;
    transition: border-color 0.15s;
  }}
  .search-box:focus {{ border-color: var(--accent); }}
  .search-box::placeholder {{ color: var(--muted); }}

  /* ── TABLE ── */
  .table-wrap {{
    max-width: 1600px;
    margin: 1.5rem auto 3rem;
    padding: 0 2.5rem;
    overflow-x: auto;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid var(--border);
  }}
  thead th {{
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    color: var(--muted);
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.12em;
    padding: 0.9rem 1rem;
    text-align: left;
    text-transform: uppercase;
    white-space: nowrap;
    cursor: pointer;
    user-select: none;
  }}
  thead th:hover {{ color: var(--text); }}
  thead th::after {{ content: ' ↕'; opacity: 0.4; font-size: 10px; }}

  tbody tr {{ border-bottom: 1px solid var(--border); transition: background 0.1s; }}
  tbody tr:last-child {{ border-bottom: none; }}
  tbody tr:hover {{ background: rgba(255,255,255,0.03); }}
  tbody tr.hot-row {{ background: var(--hot-bg); }}
  tbody tr.hidden {{ display: none; }}

  td {{
    padding: 0.85rem 1rem;
    vertical-align: middle;
  }}
  .rank {{
    color: var(--muted);
    font-family: 'DM Mono', monospace;
    font-size: 12px;
    text-align: center;
    min-width: 2.5rem;
  }}

  /* Score circle */
  .score-circle {{
    width: 44px; height: 44px;
    border-radius: 50%;
    border: 2px solid var(--c, #22c55e);
    color: var(--c, #22c55e);
    display: flex; align-items: center; justify-content: center;
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: 15px;
    margin: 0 auto 4px;
  }}
  .score-label {{
    text-align: center;
    font-size: 10px;
    font-family: 'DM Mono', monospace;
    font-weight: 500;
  }}

  .doc-num  {{ font-family: 'DM Mono', monospace; font-size: 12px; color: var(--muted); }}
  .name     {{ font-weight: 500; white-space: nowrap; }}
  .address  {{ color: #94a3b8; font-size: 13px; max-width: 180px; }}
  .legal    {{ color: var(--muted); font-size: 12px; max-width: 220px; line-height: 1.4; }}
  .flags    {{ min-width: 220px; }}

  /* Badges */
  .badge {{
    border-radius: 5px;
    display: inline-block;
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    font-weight: 500;
    margin: 2px 2px 2px 0;
    padding: 2px 7px;
    white-space: nowrap;
  }}
  .badge-active  {{ background: rgba(239,68,68,0.15); color: #fca5a5; border: 1px solid rgba(239,68,68,0.3); }}
  .badge-inactive {{ background: rgba(255,255,255,0.04); color: #334155; border: 1px solid transparent; }}

  /* ── FOOTER ── */
  footer {{
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    padding: 1.25rem 2.5rem;
    text-align: center;
  }}

  @media (max-width: 768px) {{
    header, .filter-bar, .table-wrap {{ padding-left: 1rem; padding-right: 1rem; }}
    .stats {{ gap: 0.5rem; }}
    .stat-card {{ min-width: 80px; padding: 0.75rem 1rem; }}
    .search-box {{ width: 100%; margin-left: 0; }}
  }}
</style>
</head>
<body>

<header>
  <div class="header-inner">
    <div class="header-brand">
      <div class="eyebrow">🏠 Motivated Seller Intelligence</div>
      <h1>{COUNTY_NAME}<br>Lead Dashboard</h1>
      <div class="subtitle">Generated {generated} &nbsp;·&nbsp; {total} records indexed</div>
    </div>
    <div class="stats">
      <div class="stat-card"><div class="num num-hot">{hot_count}</div><div class="lbl">🔥 Hot</div></div>
      <div class="stat-card"><div class="num num-warm">{warm_count}</div><div class="lbl">⚠️ Warm</div></div>
      <div class="stat-card"><div class="num num-cold">{cold_count}</div><div class="lbl">✓ Cold</div></div>
      <div class="stat-card"><div class="num num-total">{total}</div><div class="lbl">Total</div></div>
      <div class="stat-card"><div class="num num-avg">{avg_score}</div><div class="lbl">Avg Score</div></div>
    </div>
  </div>
</header>

<div class="filter-bar">
  <button class="filter-btn active" onclick="filterRows('all',this)">All Leads</button>
  <button class="filter-btn" onclick="filterRows('hot',this)">🔥 Hot (50+)</button>
  <button class="filter-btn" onclick="filterRows('warm',this)">⚠️ Warm (25–49)</button>
  <button class="filter-btn" onclick="filterRows('cold',this)">✓ Cold (&lt;25)</button>
  <input class="search-box" type="text" placeholder="🔍  Search name, address…" oninput="searchRows(this.value)">
</div>

<div class="table-wrap">
  <table id="leadsTable">
    <thead>
      <tr>
        <th>#</th>
        <th>Score</th>
        <th>Doc #</th>
        <th>File Date</th>
        <th>Grantor (Seller)</th>
        <th>Grantee (Buyer)</th>
        <th>Address</th>
        <th>Legal Description</th>
        <th>Distress Signals</th>
      </tr>
    </thead>
    <tbody id="tableBody">
      {rows_html}
    </tbody>
  </table>
</div>

<footer>
  {COUNTY_NAME} Public Records &nbsp;·&nbsp; Data sourced from {BASE_URL}
  &nbsp;·&nbsp; Refresh to reload &nbsp;·&nbsp; For informational use only
</footer>

<script>
  let currentFilter = 'all';

  function filterRows(type, btn) {{
    currentFilter = type;
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    applyFilters(document.querySelector('.search-box').value);
  }}

  function searchRows(query) {{
    applyFilters(query);
  }}

  function applyFilters(query) {{
    const q = query.toLowerCase().trim();
    document.querySelectorAll('#tableBody .record-row').forEach(row => {{
      const score = parseInt(row.querySelector('.score-circle').textContent);
      const text  = row.textContent.toLowerCase();

      const matchFilter =
        currentFilter === 'all'  ? true :
        currentFilter === 'hot'  ? score >= 50 :
        currentFilter === 'warm' ? (score >= 25 && score < 50) :
        currentFilter === 'cold' ? score < 25 : true;

      const matchSearch = q === '' || text.includes(q);

      row.classList.toggle('hidden', !(matchFilter && matchSearch));
    }});
  }}
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # Ensure output directories exist
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)

    # ── SCRAPE (or use demo data) ─────────────────────────────────────────
    # When deploying against a real county website, replace the demo block
    # with:  records = CountyScraper().run()
    #
    # Demo mode is active because BASE_URL is a placeholder.
    False = "YOUR_COUNTY" in BASE_URL or not BASE_URL.startswith("http")

    if False:
        log.warning("BASE_URL is a placeholder — generating demo data.")
        log.warning("Set BASE_URL to your county recorder URL and update CSS selectors.")
        records = generate_demo_records(35)
    else:
        records = CountyScraper().run()

    log.info(f"Total scored records: {len(records)}")

    # ── SAVE JSON ─────────────────────────────────────────────────────────
    output = {
        "county": COUNTY_NAME,
        "generated_at": datetime.utcnow().isoformat(),
        "total_records": len(records),
        "records": [asdict(r) for r in records],
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    log.info(f"JSON saved → {OUTPUT_FILE}")

    # ── BUILD DASHBOARD ───────────────────────────────────────────────────
    html = build_dashboard(records)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Dashboard saved → {DASHBOARD_FILE}")

    # ── SUMMARY ──────────────────────────────────────────────────────────
    hot  = sum(1 for r in records if r.seller_score >= 50)
    warm = sum(1 for r in records if 25 <= r.seller_score < 50)
    log.info(f"  🔥 Hot leads  (50+): {hot}")
    log.info(f"  ⚠️  Warm leads (25–49): {warm}")
    log.info(f"  ✓  Cold leads (<25): {len(records)-hot-warm}")

if __name__ == "__main__":
    main()
