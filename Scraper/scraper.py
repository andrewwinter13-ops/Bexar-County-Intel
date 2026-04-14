@@ -1,12 +1,17 @@
"""
Motivated Seller Lead Scraper - Bexar County, TX
Features: Clean headers, Days on Record, CSV Export, Warm Leads Map
Features: Selenium scrape, scoring, CSV export, map, Slack notifications
Slack sends:
  1. Daily summary — total leads pulled, how many are 30+, dashboard link
  2. New lead alerts — any new grantor with score 30+ not seen in previous run
"""

import json, time, logging, random, csv, io, urllib.parse
import json, time, logging, random, csv, io, urllib.parse, os
from datetime import datetime, date
from pathlib import Path
from dataclasses import dataclass, field, asdict
from urllib.request import urlopen, Request
from urllib.error import URLError

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
@@ -15,33 +20,43 @@
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
@@ -54,6 +69,9 @@
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class PropertyRecord:
    document_number:    str = ""
@@ -77,21 +95,22 @@ class PropertyRecord:
    source_url:         str = ""


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def calc_days(file_date_str: str) -> int:
    if not file_date_str:
        return 0
    for fmt in ["%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"]:
        try:
            filed = datetime.strptime(file_date_str.strip(), fmt).date()
            return (date.today() - filed).days
        except:
            continue
            return (date.today() - datetime.strptime(file_date_str.strip(), fmt).date()).days
        except: continue
    return 0


def make_maps_url(name: str) -> str:
    query = urllib.parse.quote(f"{name}, San Antonio, TX")
    return f"https://www.google.com/maps/search/?api=1&query={query}"
    q = urllib.parse.quote(f"{name}, San Antonio, TX")
    return f"https://www.google.com/maps/search/?api=1&query={q}"


def score_record(rec):
@@ -114,6 +133,132 @@ def score_record(rec):
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
@@ -137,8 +282,7 @@ def wait_for_records(driver, timeout=PAGE_LOAD_WAIT):
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
            if driver.find_elements(By.CSS_SELECTOR, sel):
                return sel
        except TimeoutException:
            continue
        except TimeoutException: continue
    return None


@@ -148,8 +292,7 @@ def extract_rows(driver, selector):
        for row in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 3:
                    continue
                if len(cells) < 3: continue
                def cell(i, d=""):
                    try: return cells[i].text.strip() or d
                    except: return d
@@ -227,6 +370,9 @@ def scrape_bexar_selenium():
    return records


# ─────────────────────────────────────────────────────────────────────────────
# DEMO DATA
# ─────────────────────────────────────────────────────────────────────────────
def generate_demo_records(n=50):
    firstnames = ["James","Maria","Robert","Linda","Michael","Patricia","William","Barbara","David","Elizabeth"]
    lastnames  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Wilson","Anderson","Rodriguez","Martinez"]
@@ -261,6 +407,9 @@ def generate_demo_records(n=50):
    return records


# ─────────────────────────────────────────────────────────────────────────────
# CSV EXPORT
# ─────────────────────────────────────────────────────────────────────────────
def records_to_csv(records) -> str:
    output = io.StringIO()
    fields = ["document_number","file_date","days_on_record","doc_type","grantor","grantee",
@@ -273,21 +422,21 @@ def records_to_csv(records) -> str:
    return output.getvalue()


def build_dashboard(records):
# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────
def build_dashboard(records, new_lead_doc_numbers: set):
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
        sigs=[]
        if r.tax_delinquent: sigs.append("Tax Lien")
        if r.code_violation: sigs.append("Code Viol.")
        if r.probate_filing: sigs.append("Probate")
@@ -297,14 +446,16 @@ def active_signals(r):

    rows_html = ""
    for i, r in enumerate(records):
        is_new = r.document_number in new_lead_doc_numbers
        new_badge = '<span class="new-badge">NEW</span>' if is_new else ""
        maps_link = (f'<a href="{r.maps_url}" target="_blank" class="maps-link">📍 Search Maps</a>'
                     if r.maps_url else "—")
        rows_html += f"""<tr class="record-row {'hot-row' if r.seller_score>=50 else ''}" data-score="{r.seller_score}" data-text="{(r.grantor+r.grantee+r.doc_type).lower().replace('"','')}">
          <td class="rank">#{i+1}</td>
          <td class="rank">#{i+1}{new_badge}</td>
          <td><div class="score-circle" style="--c:{score_color(r.seller_score)}">{r.seller_score}</div>
          <div class="slbl" style="color:{score_color(r.seller_score)}">{score_label(r.seller_score)}</div></td>
          <td class="mono">{r.document_number}</td>
          <td class="nowrap">{r.file_date}</td>
          <td class="mono sm">{r.document_number}</td>
          <td class="nowrap sm">{r.file_date}</td>
          <td>{days_label(r.days_on_record)}</td>
          <td class="nowrap bold">{r.doc_type}</td>
          <td class="name" title="{r.grantor}">{r.grantor}</td>
@@ -313,25 +464,23 @@ def active_signals(r):
          <td>{maps_link}</td>
          <td>{active_signals(r)}</td></tr>"""

    total = len(records)
    hot   = sum(1 for r in records if r.seller_score>=50)
    warm  = sum(1 for r in records if 25<=r.seller_score<50)
    cold  = total-hot-warm
    avg   = round(sum(r.seller_score for r in records)/total,1) if total else 0
    gen   = datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")
    is_live = any(r.source_url and "publicsearch" in r.source_url for r in records)
    total  = len(records)
    hot    = sum(1 for r in records if r.seller_score>=50)
    warm   = sum(1 for r in records if 25<=r.seller_score<50)
    cold   = total-hot-warm
    above30= sum(1 for r in records if r.seller_score>=30)
    avg    = round(sum(r.seller_score for r in records)/total,1) if total else 0
    gen    = datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")
    is_live= any(r.source_url and "publicsearch" in r.source_url for r in records)
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
@@ -360,7 +509,7 @@ def active_signals(r):
.sc{{background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:10px;padding:.75rem 1.2rem;min-width:90px;text-align:center;}}
.sc .n{{font-family:'Syne',sans-serif;font-size:1.6rem;font-weight:800;line-height:1;}}
.sc .l{{font-size:10px;color:var(--muted);margin-top:.2rem;text-transform:uppercase;letter-spacing:.1em;}}
.nh{{color:#ef4444;}}.nw{{color:#f97316;}}.nc{{color:#22c55e;}}.nt{{color:var(--accent);}}.na{{color:#94a3b8;}}
.nh{{color:#ef4444;}}.nw{{color:#f97316;}}.nc{{color:#22c55e;}}.nt{{color:var(--accent);}}.na{{color:#94a3b8;}}.n30{{color:#a78bfa;}}
.toolbar{{max-width:1800px;margin:1rem auto 0;padding:0 2rem;display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;}}
.fb{{background:var(--surface);border:1px solid var(--border);border-radius:7px;color:var(--muted);cursor:pointer;font-family:'DM Mono',monospace;font-size:11px;padding:.4rem .9rem;transition:all .15s;}}
.fb:hover,.fb.active{{border-color:var(--accent);color:var(--text);background:rgba(99,102,241,.1);}}
@@ -380,13 +529,12 @@ def active_signals(r):
tbody tr{{border-bottom:1px solid var(--border);transition:background .1s;}}
tbody tr:hover{{background:rgba(255,255,255,.025);}}.hot-row{{background:rgba(239,68,68,.04);}}.hidden{{display:none;}}
td{{padding:.6rem .8rem;vertical-align:middle;}}
.rank{{color:var(--muted);font-family:'DM Mono',monospace;font-size:11px;text-align:center;}}
.rank{{color:var(--muted);font-family:'DM Mono',monospace;font-size:11px;text-align:center;position:relative;}}
.new-badge{{display:inline-block;background:rgba(167,139,250,.2);color:#c4b5fd;border:1px solid rgba(167,139,250,.4);border-radius:3px;font-family:'DM Mono',monospace;font-size:8px;padding:1px 4px;margin-left:4px;vertical-align:middle;}}
.score-circle{{width:40px;height:40px;border-radius:50%;border:2px solid var(--c,#22c55e);color:var(--c,#22c55e);display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif;font-weight:800;font-size:14px;margin:0 auto 3px;}}
.slbl{{text-align:center;font-size:9px;font-family:'DM Mono',monospace;}}
.mono{{font-family:'DM Mono',monospace;color:var(--muted);}}
.sm{{font-size:11px;}}
.nowrap{{white-space:nowrap;}}
.bold{{font-weight:500;}}
.sm{{font-size:11px;}}.nowrap{{white-space:nowrap;}}.bold{{font-weight:500;}}
.name{{white-space:nowrap;max-width:140px;overflow:hidden;text-overflow:ellipsis;}}
.days{{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);}}
.hot-age{{color:#ef4444;font-weight:500;}}.warm-age{{color:#f97316;}}
@@ -404,26 +552,29 @@ def active_signals(r):
    <div class="db">{data_note}</div>
  </div>
  <div class="stats">
    <div class="sc"><div class="n nh">{hot}</div><div class="l">Hot</div></div>
    <div class="sc"><div class="n nw">{warm}</div><div class="l">Warm</div></div>
    <div class="sc"><div class="n nh">{hot}</div><div class="l">Hot 50+</div></div>
    <div class="sc"><div class="n nw">{warm}</div><div class="l">Warm 25+</div></div>
    <div class="sc"><div class="n n30">{above30}</div><div class="l">Score 30+</div></div>
    <div class="sc"><div class="n nc">{cold}</div><div class="l">Cold</div></div>
    <div class="sc"><div class="n nt">{total}</div><div class="l">Total</div></div>
    <div class="sc"><div class="n na">{avg}</div><div class="l">Avg</div></div>
    <div class="sc"><div class="n na">{avg}</div><div class="l">Avg Score</div></div>
  </div>
</div></header>

<div class="toolbar">
  <button class="fb active" onclick="filt('all',this)">All</button>
  <button class="fb" onclick="filt('hot',this)">🔥 Hot (50+)</button>
  <button class="fb" onclick="filt('warm',this)">⚠️ Warm (25-49)</button>
  <button class="fb" onclick="filt('cold',this)">✓ Cold (&lt;25)</button>
  <button class="fb" onclick="filt('30',this)">⭐ Score 30+</button>
  <button class="fb" onclick="filt('new',this)">🆕 New Today</button>
  <button class="fb" onclick="filt('cold',this)">✓ Cold</button>
  <input class="sb" type="text" placeholder="Search name, doc type..." oninput="search(this.value)">
  <button class="map-btn" onclick="toggleMap()">🗺️ Warm Leads Map</button>
  <button class="exp-btn" onclick="exportCSV()">⬇ Export CSV</button>
</div>

<div class="map-wrap" id="mapWrap">
  <div class="map-note">Showing {len(map_data)} warm + hot leads plotted across San Antonio &nbsp;·&nbsp; Click any pin for details</div>
  <div class="map-note">Warm + hot leads across San Antonio &nbsp;·&nbsp; Click any pin for details</div>
  <div id="map"></div>
</div>

@@ -438,20 +589,37 @@ def active_signals(r):
<footer>{COUNTY_NAME} Public Records &nbsp;·&nbsp; bexar.tx.publicsearch.us &nbsp;·&nbsp; For informational use only</footer>

<script>
const newDocs = new Set({json.dumps(list(new_lead_doc_numbers))});
let cf='all';
function filt(t,b){{cf=t;document.querySelectorAll('.fb').forEach(x=>x.classList.remove('active'));b.classList.add('active');apply(document.querySelector('.sb').value);}}
function search(q){{apply(q);}}
function apply(query){{const q=query.toLowerCase();document.querySelectorAll('#tb tr').forEach(row=>{{const s=parseInt(row.dataset.score||0);const t=row.dataset.text||'';const mf=cf==='all'||((cf==='hot'&&s>=50)||(cf==='warm'&&s>=25&&s<50)||(cf==='cold'&&s<25));row.classList.toggle('hidden',!(mf&&(q===''||t.includes(q))));}}); }}
function apply(query){{
  const q=query.toLowerCase();
  document.querySelectorAll('#tb tr').forEach(row=>{{
    const s=parseInt(row.dataset.score||0);
    const t=row.dataset.text||'';
    const docNum=row.querySelector('.rank')?.textContent||'';
    const isNew=Array.from(newDocs).some(d=>docNum.includes(d));
    const mf=cf==='all'||(cf==='hot'&&s>=50)||(cf==='warm'&&s>=25&&s<50)||
              (cf==='30'&&s>=30)||(cf==='new'&&isNew)||(cf==='cold'&&s<25);
    row.classList.toggle('hidden',!(mf&&(q===''||t.includes(q))));
  }});
}}

const csvData=`{csv_data}`;
function exportCSV(){{const blob=new Blob([csvData],{{type:'text/csv'}});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='bexar-leads-{today_str}.csv';a.click();URL.revokeObjectURL(url);}}
function exportCSV(){{
  const blob=new Blob([csvData],{{type:'text/csv'}});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url;a.download='bexar-leads-{today_str}.csv';a.click();
  URL.revokeObjectURL(url);
}}

const markers={markers_json};
let mapLoaded=false,mapVisible=false;
function toggleMap(){{
  const w=document.getElementById('mapWrap');
  mapVisible=!mapVisible;
  w.classList.toggle('show',mapVisible);
  mapVisible=!mapVisible;w.classList.toggle('show',mapVisible);
  if(mapVisible&&!mapLoaded){{mapLoaded=true;loadMap();}}
}}
function loadMap(){{
@@ -462,20 +630,26 @@ def active_signals(r):
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{attribution:'© OpenStreetMap'}}).addTo(map);
    markers.forEach(m=>{{
      const color=m.score>=50?'#ef4444':'#f97316';
      const r=m.score>=50?10:7;
      L.circleMarker([m.lat,m.lng],{{radius:r,fillColor:color,color:'#fff',weight:1.5,opacity:1,fillOpacity:0.85}})
       .addTo(map)
       .bindPopup(`<b>${{m.name}}</b><br><b>${{m.doc_type}}</b><br>Filed: ${{m.date}} (${{m.days}} days ago)<br>Score: <b>${{m.score}}</b><br><a href="${{m.maps_url}}" target="_blank">🔍 Search in Google Maps</a>`);
      L.circleMarker([m.lat,m.lng],{{radius:m.score>=50?10:7,fillColor:color,color:'#fff',weight:1.5,opacity:1,fillOpacity:0.85}})
       .addTo(map).bindPopup(`<b>${{m.name}}</b><br><b>${{m.doc_type}}</b><br>Filed: ${{m.date}} (${{m.days}} days ago)<br>Score: <b>${{m.score}}</b><br><a href="${{m.maps_url}}" target="_blank">🔍 Search in Google Maps</a>`);
    }});
  }};
  document.head.appendChild(ls);
}}
</script>
</body></html>"""
</script></body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load previous doc numbers for new lead detection
    prev_doc_numbers = load_prev_doc_numbers()
    log.info(f"Loaded {len(prev_doc_numbers)} previously seen records.")

    # Scrape
    log.info(f"Starting scrape of {COUNTY_NAME}...")
    try:
        records = scrape_bexar_selenium()
@@ -490,23 +664,40 @@ def main():
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
    log.info(f"JSON saved")
    log.info("JSON saved.")

    # Build dashboard
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(build_dashboard(records))
    log.info(f"Dashboard saved")
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
