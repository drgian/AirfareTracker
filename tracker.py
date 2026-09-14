#!/usr/bin/env python3
"""Flight price tracker — Delta.com → GitHub Pages dashboard."""

import asyncio, json, re, smtplib, socket, subprocess, sys, os, time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from playwright.async_api import async_playwright

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).resolve().parent
CFG_FILE  = BASE_DIR / "config.json"
REPO_DIR  = BASE_DIR / "site-repo"
DASHBOARD = "https://drgian.github.io/AirfareTracker/flight_tracker.html"

# ── Config ────────────────────────────────────────────────────────────────────
def load_cfg():
    with open(CFG_FILE) as f:
        return json.load(f)

# ── Database ──────────────────────────────────────────────────────────────────
SCHEMA = """CREATE TABLE IF NOT EXISTS price_history (
    id          SERIAL PRIMARY KEY,
    trip_id     TEXT NOT NULL,
    scraped_at  TIMESTAMP NOT NULL,
    price_usd   DOUBLE PRECISION,
    airline     TEXT,
    stops       TEXT,
    depart_time TEXT,
    arrive_time TEXT,
    notes       TEXT,
    flights     TEXT
);
CREATE INDEX IF NOT EXISTS price_history_trip_time ON price_history (trip_id, scraped_at);
-- synthetic rows are gap fill-ins: shown on the dashboard, ignored by alerts and the daily limit
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS synthetic BOOLEAN NOT NULL DEFAULT FALSE;"""

def open_tunnel(cfg):
    """When the scraper runs off the AWS box, reach its Postgres through an SSH tunnel."""
    t = cfg.get("ssh_tunnel")
    if not t:
        return None
    port = t.get("local_port", 55432)
    proc = subprocess.Popen(
        ["ssh", "-i", t["key"], "-N", "-o", "ExitOnForwardFailure=yes",
         "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
         "-L", f"{port}:localhost:5432", f"{t['user']}@{t['host']}"])
    for _ in range(40):
        if proc.poll() is not None:
            sys.exit("SSH tunnel to the database failed to start")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return proc
        except OSError:
            time.sleep(0.5)
    proc.terminate()
    sys.exit("SSH tunnel to the database timed out")

def open_db(cfg):
    conn = psycopg.connect(cfg.get("database_url", "dbname=flighttracker"),
                           row_factory=dict_row, autocommit=True)
    conn.execute(SCHEMA)
    return conn

# ── Email (SMTP) ──────────────────────────────────────────────────────────────
def send_email(cfg, to_list, subject, body_text):
    host = cfg.get("smtp_host", "smtp.gmail.com")
    port = cfg.get("smtp_port", 587)
    user = cfg.get("smtp_user", "")
    pwd  = cfg.get("smtp_password", "")
    if not user or not pwd:
        print("  Email skipped — add smtp_user/smtp_password to config.json")
        return
    msg = MIMEMultipart()
    msg["From"]    = user
    msg["To"]      = ", ".join(to_list)
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))
    try:
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(user, pwd)
            s.sendmail(user, to_list, msg.as_string())
        print(f"  Email → {', '.join(to_list)}: sent")
    except Exception as e:
        print(f"  Email failed: {e}")

def fire_alerts(cfg, trip, new_price, prev_price):
    emails = cfg.get("alert_emails", [])
    if isinstance(emails, str):
        emails = [emails]
    if not emails:
        return
    origin    = trip["origin"]
    dest      = trip["destination"]
    dates     = f"{trip['travel_date']} – {trip.get('return_date','')}"
    # config.json's alert_below predates multi-trip support and belongs to its own route
    threshold = trip.get("alert_below")
    if threshold is None and (origin, dest) == (cfg.get("origin"), cfg.get("destination")):
        threshold = cfg.get("alert_below")

    if cfg.get("alert_on_change") and prev_price and new_price != prev_price:
        delta = new_price - prev_price
        sign  = "+" if delta > 0 else ""
        arrow = "↑" if delta > 0 else "↓"
        subj  = f"Flight Price {arrow} {origin}→{dest}: now ${new_price:,.0f} ({sign}${delta:,.0f})"
        body  = "\n".join([
            f"Route: {origin} → {dest}  |  {dates}", "",
            f"Previous price: ${prev_price:,.0f}",
            f"Current price:  ${new_price:,.0f}",
            f"Change:         {sign}${delta:,.0f}", "",
            f"Dashboard: {DASHBOARD}"
        ])
        send_email(cfg, emails, subj, body)

    if threshold and new_price < threshold:
        subj = f"Price Alert: {origin}→{dest} now ${new_price:,.0f} (below ${threshold:,.0f})"
        body = "\n".join([
            f"Route: {origin} → {dest}  |  {dates}", "",
            f"Current price: ${new_price:,.0f}",
            f"Alert threshold: ${threshold:,.0f}", "",
            f"Dashboard: {DASHBOARD}"
        ])
        send_email(cfg, emails, subj, body)

# ── Scraper ───────────────────────────────────────────────────────────────────
# Delta's bot protection rejects browsers launched by automation tools, so we start an
# ordinary Chrome and attach to it over the DevTools protocol instead.
CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
]
CDP_PORT = 9333
CABIN_TO_BRAND = {"main_basic": "BMAIN", "main_classic": "CMAIN", "main_extra": "EMAIN",
                  "comfort": "CDCP", "comfort_classic": "CDCP", "first": "CFIRST"}
BRAND_NAMES = {"BMAIN": "Main Basic", "CMAIN": "Main Classic", "EMAIN": "Main Extra",
               "CDCP": "Comfort Classic", "EDCP": "Comfort Extra",
               "CFIRST": "First Classic", "EFIRST": "First Extra"}

def chrome_path(cfg):
    for p in [cfg.get("chrome_path")] + CHROME_PATHS:
        if p and Path(p).exists():
            return p
    sys.exit("Google Chrome not found; set chrome_path in config.json")

def parse_offers(data, trip):
    """Cheapest fare in the trip's cabin, limited to its pinned outbound flights if any."""
    brand  = CABIN_TO_BRAND.get(trip.get("cabin_class", "main_classic"), "CMAIN")
    pinned = re.findall(r"DL\s*(\d+)", trip.get("outbound_flights", ""))
    best = None
    for offer_set in data["data"]["gqlSearchOffers"]["gqlOffersSets"]:
        t = offer_set["trips"][0]
        nums = [str(s["marketingCarrier"]["carrierNum"]) for s in t["flightSegment"]]
        if pinned and nums != pinned:
            continue
        for offer in offer_set["offers"]:
            props = offer["additionalOfferProperties"]
            if props.get("dominantSegmentBrandId") != brand or props.get("soldOut"):
                continue
            amt = re.search(r'"roundedCurrencyAmt": (\d+)', json.dumps(offer))
            if not amt:
                continue
            price = float(amt.group(1))
            if best is None or price < best["price"]:
                best = {
                    "price":       price,
                    "airline":     "Delta",
                    "fare_class":  BRAND_NAMES.get(brand, brand),
                    "stops":       str(t.get("stopCnt", "")),
                    "depart_time": t["scheduledDepartureLocalTs"][11:16],
                    "arrive_time": t["scheduledArrivalLocalTs"][11:16],
                    "flights":     " / ".join(f"DL{n}" for n in nums),
                }
    return best

async def pick_airport(page, which, code):
    btn = page.locator(f"[id$='-{which}-button'] >> visible=true").first
    if f", {code}," in (await btn.get_attribute("aria-label") or ""):
        return
    await btn.click()
    await page.wait_for_timeout(1000)
    await page.keyboard.type(code, delay=120)
    option = page.locator("li[role=option] >> visible=true").filter(has_text=re.compile(rf"^\s*{code}\b"))
    await option.first.click()
    await page.wait_for_timeout(800)

async def pick_dates(page, dates):
    await page.locator("[id^='date-picker-trigger'] >> visible=true").first.click()
    await page.wait_for_timeout(1200)
    clear = page.locator("[role=dialog] button:has-text('Clear') >> visible=true")
    if await clear.count():
        await clear.first.click()
        await page.wait_for_timeout(500)
    prev = page.locator("[role=dialog] button[aria-label^='Previous month']:not([disabled])")
    for _ in range(14):
        if not await prev.count():
            break
        await prev.first.click()
        await page.wait_for_timeout(300)
    for d in dates:
        cell = page.locator(f"[role=dialog] button[aria-label^='{d:%B} {d.day}, {d.year}'] >> visible=true")
        for _ in range(14):
            if await cell.count():
                break
            await page.locator("[role=dialog] button[aria-label^='Next month']:not([disabled])").first.click()
            await page.wait_for_timeout(400)
        await cell.first.click()
        await page.wait_for_timeout(600)
    await page.locator("[role=dialog] button:has-text('Done') >> visible=true").first.click()
    await page.wait_for_timeout(800)

async def search_trip(page, trip):
    print(f"  Searching {trip['origin']}→{trip['destination']} "
          f"({trip['travel_date']} – {trip.get('return_date', '')})...")
    offers = []
    async def on_response(resp):
        if "rm-offer-gql" in resp.url:
            try:
                body = await resp.json()
            except Exception:
                return
            if (body.get("data") or {}).get("gqlSearchOffers"):
                offers.append(body)
    page.on("response", on_response)
    try:
        await page.goto("https://www.delta.com/", timeout=90000, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        cookie = page.locator("#onetrust-accept-btn-handler >> visible=true")
        if await cookie.count():
            await cookie.first.click()
        await pick_airport(page, "origin", trip["origin"])
        await pick_airport(page, "destination", trip["destination"])
        dates = [datetime.strptime(trip["travel_date"], "%Y-%m-%d")]
        if trip.get("return_date"):
            dates.append(datetime.strptime(trip["return_date"], "%Y-%m-%d"))
        await pick_dates(page, dates)
        await page.locator("#findFilghtsCta >> visible=true").first.click()
        for _ in range(60):
            if offers or await page.title() == "Access Denied":
                break
            await page.wait_for_timeout(1000)
        if not offers:
            print(f"  No fares returned (page: {await page.title()!r})")
            await page.screenshot(path=str(BASE_DIR / f"debug_{trip['id']}.png"))
            return None
        result = parse_offers(offers[0], trip)
        if result:
            print(f"  → ${result['price']:,.0f} {result['fare_class']}  {result['flights']}  "
                  f"{result['depart_time']}→{result['arrive_time']}")
        else:
            print("  Fares returned, but none in the requested cabin/flights.")
        return result
    except Exception as e:
        print(f"  Scrape error: {str(e).splitlines()[0]}")
        await page.screenshot(path=str(BASE_DIR / f"debug_{trip['id']}.png"))
        return None
    finally:
        page.remove_listener("response", on_response)

async def scrape_trips(cfg, trips):
    """Search every trip in one Chrome session; returns {trip_id: result or None}."""
    chrome = subprocess.Popen(
        [chrome_path(cfg), f"--remote-debugging-port={CDP_PORT}",
         f"--user-data-dir={BASE_DIR / 'chrome-profile'}",
         "--no-first-run", "--no-default-browser-check", "--window-size=1280,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results = {}
    try:
        async with async_playwright() as p:
            browser = None
            for _ in range(30):
                try:
                    browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
                    break
                except Exception:
                    await asyncio.sleep(0.5)
            if browser is None:
                sys.exit("Could not attach to Chrome")
            ctx  = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            for trip in trips:
                print(f"\n── {trip.get('label', trip['id'])} ──")
                for attempt in range(2):
                    results[trip["id"]] = await search_trip(page, trip)
                    if results[trip["id"]]:
                        break
                    if attempt == 0:
                        print("  Retrying...")
            await browser.close()
    finally:
        chrome.terminate()
    return results

# ── HTML generation ───────────────────────────────────────────────────────────
def generate_html(conn, trips):
    def trip_rows(trip_id):
        rows = conn.execute(
            "SELECT scraped_at, price_usd AS price, airline, stops, "
            "depart_time, arrive_time, notes, flights "
            "FROM price_history WHERE trip_id=%s AND price_usd IS NOT NULL ORDER BY scraped_at",
            (trip_id,),
        ).fetchall()
        for r in rows:
            r["scraped_at"] = r["scraped_at"].isoformat()
        return rows

    def subtitle(t):
        parts = [
            f"{t['origin']} → {t['destination']}",
            f"{t['travel_date']} – {t.get('return_date','')}",
            t.get("cabin_class", "").replace("_", " ").title(),
        ]
        if t.get("outbound_flights"):
            parts.append(f"Out {t['outbound_flights']}")
        if t.get("return_flights"):
            parts.append(f"Back {t['return_flights']}")
        return "  ·  ".join(p for p in parts if p.strip())

    tabs_html   = ""
    panels_html = ""

    for idx, trip in enumerate(trips):
        active = "active" if idx == 0 else ""
        label  = trip.get("label", trip["id"])
        tabs_html += (
            f'<button class="tab-btn {active}" onclick="showTab(\'panel-{idx}\')" '
            f'data-panel="panel-{idx}" data-trip="{trip["id"]}" id="btn-{idx}">'
            f'{label}</button>\n'
        )
        data      = trip_rows(trip["id"])
        data_json = json.dumps(data)

        # JavaScript template — use {{ }} to escape braces for Python f-string
        panels_html += f"""
<div class="tab-panel {active}" id="panel-{idx}" data-trip="{trip['id']}">
  <p class="subtitle">{subtitle(trip)}</p>
  <div class="stats" id="stats-{idx}"></div>
  <div class="chart-wrap"><canvas id="chart-{idx}" height="220"></canvas></div>
  <details class="history-details">
    <summary class="history-summary"><span class="toggle-icon">▶</span> Price history — <span id="hist-count-{idx}"></span> checks</summary>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Date checked</th><th>Price</th><th>Fare class</th><th>Flights</th><th>Departs</th><th>Arrives</th><th></th></tr></thead>
        <tbody id="tbody-{idx}"></tbody>
      </table>
    </div>
  </details>
</div>
<script>
(function(){{
  const DATA={data_json};
  const i={idx};
  const DAYS=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const prices=DATA.map(d=>d.price).filter(p=>p!=null&&p>0);
  if(!prices.length){{
    document.getElementById('tbody-'+i).innerHTML='<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:32px">⏳ Waiting for first price check.</td></tr>';
    document.getElementById('chart-'+i).parentElement.style.display='none';
    return;
  }}
  const minP=Math.min(...prices),maxP=Math.max(...prices);
  const avgP=prices.reduce((a,b)=>a+b,0)/prices.length;
  const priced=DATA.filter(d=>d.price);
  const latest=priced[priced.length-1];
  const prev=priced.slice(-2)[0];
  const delta=latest&&prev&&prev!==latest?latest.price-prev.price:null;
  const fmt=v=>v!=null?'$'+v.toLocaleString():'—';
  const chg=delta!=null?(delta>0?'<span style="color:var(--red)">▲ $'+Math.abs(delta).toLocaleString()+'</span>':'<span style="color:var(--green)">▼ $'+Math.abs(delta).toLocaleString()+'</span>'):'—';
  document.getElementById('stats-'+i).innerHTML=`
    <div class="stat"><div class="stat-label">Latest</div><div class="stat-value blue">${{fmt(latest.price)}}</div></div>
    <div class="stat"><div class="stat-label">Change</div><div class="stat-value">${{chg}}</div></div>
    <div class="stat"><div class="stat-label">Lowest seen</div><div class="stat-value green">${{fmt(minP)}}</div></div>
    <div class="stat"><div class="stat-label">Highest seen</div><div class="stat-value red">${{fmt(maxP)}}</div></div>
    <div class="stat"><div class="stat-label">Avg</div><div class="stat-value">${{avgP!=null?'$'+Math.round(avgP).toLocaleString():'—'}}</div></div>
  `;
  const labels=priced.map(d=>{{
    const dt=new Date(d.scraped_at+'Z');
    const date=DAYS[dt.getDay()]+' '+dt.toLocaleDateString('en-US',{{month:'numeric',day:'numeric'}});
    const h=dt.getHours(),m=String(dt.getMinutes()).padStart(2,'0'),ampm=h>=12?'pm':'am';
    return date+' '+(h%12||12)+':'+m+ampm;
  }});
  const vals=priced.map(d=>d.price);
  const isDark=window.matchMedia('(prefers-color-scheme:dark)').matches;
  const gc=isDark?'rgba(255,255,255,.07)':'rgba(0,0,0,.06)';
  const tc=isDark?'#94a3b8':'#64748b';
  new Chart(document.getElementById('chart-'+i),{{
    type:'line',
    data:{{labels,datasets:[{{
      label:'Price',data:vals,
      borderColor:'#0d6efd',backgroundColor:'rgba(13,110,253,.08)',
      borderWidth:2.5,pointRadius:5,tension:.3,fill:true,
      pointBackgroundColor:vals.map(v=>v===minP?'#16a34a':v===maxP?'#dc2626':'#0d6efd'),
    }}]}},
    options:{{responsive:true,plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:c=>` ${{c.parsed.y}}`}}}}}},
      scales:{{
        x:{{grid:{{color:gc}},ticks:{{color:tc,maxTicksLimit:10}}}},
        y:{{grid:{{color:gc}},ticks:{{color:tc,callback:v=>'$'+v}},suggestedMin:minP*.92,suggestedMax:maxP*1.08}}
      }}
    }}
  }});
  const sorted=[...DATA].reverse();
  document.getElementById('tbody-'+i).innerHTML=sorted.map(d=>{{
    const badge=d.price===minP?'<span class="badge badge-low">Low</span>':d.price===maxP?'<span class="badge badge-high">High</span>':d.price>avgP?'<span class="badge badge-mid">↑</span>':'';
    const dt=new Date(d.scraped_at+'Z');
    const ds=dt.toLocaleDateString('en-US',{{month:'short',day:'numeric',year:'numeric'}});
    const ts=dt.toLocaleTimeString('en-US',{{hour:'numeric',minute:'2-digit'}});
    const note=d.notes||'Main Classic';
    return `<tr>
      <td><b>${{DAYS[dt.getDay()]}}</b> ${{ds}} <span style="color:var(--muted);font-size:.8em">${{ts}}</span></td>
      <td class="price-cell">${{d.price?'$'+d.price.toLocaleString():'—'}}</td>
      <td style="color:var(--muted);font-size:.8rem">${{note}}</td>
      <td style="color:var(--muted);font-size:.8rem;white-space:nowrap">${{d.flights||'—'}}</td>
      <td>${{d.depart_time??'—'}}</td>
      <td>${{d.arrive_time??'—'}}</td>
      <td>${{badge}}</td>
    </tr>`;
  }}).join('');
  const hc=document.getElementById('hist-count-'+i);if(hc)hc.textContent=DATA.length;
}})();
</script>
"""

    now_utc = utcnow().strftime("%Y-%m-%d %H:%M UTC")

    return f"""<title>Flight Price Tracker</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>✈</text></svg>">
<style>
:root{{--bg:#f8fafc;--card:#fff;--border:#e2e8f0;--text:#0f172a;--muted:#64748b;
      --blue:#0d6efd;--green:#16a34a;--red:#dc2626;}}
@media(prefers-color-scheme:dark){{:root:not([data-theme=light]){{
  --bg:#0f172a;--card:#1e293b;--border:#334155;--text:#f1f5f9;--muted:#94a3b8;}}}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);padding:20px 16px;max-width:960px;margin:0 auto}}
.header-row{{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}}
h1{{font-size:1.3rem;font-weight:700}}
.add-btn{{background:var(--blue);color:#fff;border-radius:8px;padding:7px 15px;font-size:.84rem;font-weight:600;text-decoration:none;white-space:nowrap}}
.add-btn:hover{{opacity:.85}}
.tabs{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:20px;border-bottom:2px solid var(--border);padding-bottom:0}}
.tab-btn{{padding:8px 18px;border:none;background:none;color:var(--muted);font-size:.9rem;font-weight:600;cursor:pointer;border-bottom:3px solid transparent;margin-bottom:-2px;transition:color .15s;display:flex;align-items:center;gap:6px}}
.tab-btn.active,.tab-btn:hover{{color:var(--blue)}}
.tab-btn.active{{border-bottom-color:var(--blue)}}
.pending-dot{{display:inline-block;width:7px;height:7px;border-radius:50%;background:#f59e0b}}
.tab-panel{{display:none}}.tab-panel.active{{display:block}}
.sync-note{{font-size:.78rem;color:var(--muted);margin:-10px 0 14px}}
.subtitle{{color:var(--muted);font-size:.85rem;margin-bottom:16px}}
.stats{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}}
.stat{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 18px;min-width:110px}}
.stat-label{{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}}
.stat-value{{font-size:1.5rem;font-weight:700;margin-top:2px}}
.stat-value.blue{{color:var(--blue)}}.stat-value.green{{color:var(--green)}}.stat-value.red{{color:var(--red)}}
.chart-wrap{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px;overflow-x:auto}}
canvas{{display:block;width:100%!important}}
.table-wrap{{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:8px}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}
th{{text-align:left;padding:8px 12px;background:var(--bg);color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;border-bottom:2px solid var(--border)}}
td{{padding:9px 12px;border-bottom:1px solid var(--border)}}tr:last-child td{{border:none}}
.price-cell{{font-weight:700}}
.badge{{display:inline-block;padding:2px 7px;border-radius:10px;font-size:.72rem;font-weight:600}}
.badge-low{{background:#dcfce7;color:#14532d}}.badge-high{{background:#fee2e2;color:#7f1d1d}}.badge-mid{{background:#fef3c7;color:#78350f}}
.updated{{font-size:.75rem;color:var(--muted);margin-top:12px}}
.empty-card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:32px;text-align:center;color:var(--muted);font-size:.9rem}}
.history-details{{margin-bottom:8px}}
.history-summary{{cursor:pointer;list-style:none;display:flex;align-items:center;gap:6px;padding:6px 2px;color:var(--muted);font-size:.82rem;font-weight:600;user-select:none;letter-spacing:.02em}}
.history-summary::-webkit-details-marker{{display:none}}
.history-summary:hover{{color:var(--text)}}
.toggle-icon{{font-size:.7em;transition:transform .18s;display:inline-block}}
details[open] .history-summary .toggle-icon{{transform:rotate(90deg)}}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>

<div class="header-row">
  <h1>✈ Flight Price Tracker</h1>
  <a class="add-btn" href="flight_manage.html">Manage Trips</a>
</div>
<div class="tabs" id="tabs-row">
{tabs_html}</div>
<p class="sync-note" id="sync-note" hidden></p>

{panels_html}

<p class="updated">Last updated: {now_utc}</p>

<script>
function showTab(id){{
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.toggle('active',p.id===id));
  document.querySelectorAll('[data-panel]').forEach(b=>b.classList.toggle('active',b.dataset.panel===id));
}}
(function(){{
  const rendered=new Set(Array.from(document.querySelectorAll('.tab-panel[data-trip]')).map(p=>p.dataset.trip));
  fetch('watchlist.json?t='+Date.now(),{{cache:'no-store'}}).then(r=>r.ok?r.json():null).then(wl=>{{
    if(!wl||!Array.isArray(wl.trips)) return;
    const live=new Set(wl.trips.map(t=>t.id));
    const tabsEl=document.getElementById('tabs-row');
    const updatedEl=document.querySelector('p.updated');
    let added=0,removed=0;
    document.querySelectorAll('[data-trip]').forEach(el=>{{
      if(!live.has(el.dataset.trip)){{el.remove();removed++;}}
    }});
    wl.trips.forEach(t=>{{
      if(rendered.has(t.id)) return;
      const pid='pending-'+t.id;
      const btn=document.createElement('button');
      btn.className='tab-btn';btn.dataset.panel=pid;btn.dataset.trip=t.id;
      btn.onclick=()=>showTab(pid);
      btn.textContent=t.label||(t.origin+'→'+t.destination);
      const dot=document.createElement('span');dot.className='pending-dot';dot.title='Waiting for first price check';
      btn.appendChild(dot);
      tabsEl.appendChild(btn);
      const panel=document.createElement('div');
      panel.className='tab-panel';panel.id=pid;panel.dataset.trip=t.id;
      const bits=[t.origin+' → '+t.destination,t.travel_date+(t.return_date?' – '+t.return_date:'')];
      if(t.outbound_flights) bits.push('Out '+t.outbound_flights);
      if(t.return_flights) bits.push('Back '+t.return_flights);
      const sub=document.createElement('p');sub.className='subtitle';sub.textContent=bits.join('  ·  ');
      const card=document.createElement('div');card.className='empty-card';
      card.textContent='⏳ Added to the watchlist — first price on the next tracker run.';
      panel.appendChild(sub);panel.appendChild(card);
      updatedEl.insertAdjacentElement('beforebegin',panel);
      added++;
    }});
    if(!document.querySelector('.tab-panel.active')){{
      const first=document.querySelector('.tab-panel');
      if(first) showTab(first.id);
    }}
    if(added||removed){{
      const n=document.getElementById('sync-note');
      n.hidden=false;
      n.textContent='Watchlist updated: '+(added?added+' new':'')+(added&&removed?', ':'')+(removed?removed+' removed':'')+'.';
    }}
  }}).catch(()=>{{}});
}})();
</script>
"""

# ── GitHub push ───────────────────────────────────────────────────────────────
def sync_repo(cfg):
    """Clone or update the gh-pages checkout so we see trips added on the Manage page."""
    genv = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if not REPO_DIR.exists():
        token = cfg.get("github_token", "")
        repo_url = f"https://{token}@github.com/drgian/AirfareTracker.git"
        print("Cloning AirfareTracker gh-pages...")
        subprocess.run(["git", "clone", "-b", "gh-pages", repo_url, str(REPO_DIR)],
                       check=True, env=genv)
    else:
        subprocess.run(["git", "-C", str(REPO_DIR), "pull", "--rebase", "origin", "gh-pages"],
                       check=True, env=genv)

def push_to_github(cfg, html_content):
    (REPO_DIR / "flight_tracker.html").write_text(html_content, encoding="utf-8")

    git  = ["git", "-C", str(REPO_DIR)]
    genv = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    subprocess.run(git + ["config", "user.email", "tracker@local"], check=True)
    subprocess.run(git + ["config", "user.name", "Flight Tracker"],  check=True)
    subprocess.run(git + ["add", "flight_tracker.html"],             check=True)

    diff = subprocess.run(git + ["diff", "--cached", "--quiet"], capture_output=True)
    if diff.returncode == 0:
        print("  No dashboard changes to push.")
        return

    now = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    subprocess.run(git + ["commit", "-m", f"Update prices {now}"], check=True, env=genv)
    subprocess.run(git + ["push", "origin", "gh-pages"],            check=True, env=genv)
    print("  Dashboard pushed.")

# ── Main ──────────────────────────────────────────────────────────────────────
WATCHLIST_URL = "https://raw.githubusercontent.com/drgian/AirfareTracker/gh-pages/watchlist.json"

async def run(cfg):
    conn = open_db(cfg)
    sync_repo(cfg)
    with open(REPO_DIR / "watchlist.json") as f:
        trips = json.load(f).get("trips", [])

    max_per_day = cfg.get("max_runs_per_day", 2)
    today = utcnow().date()
    due = []
    for trip in trips:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM price_history WHERE trip_id=%s AND scraped_at::date=%s "
            "AND price_usd IS NOT NULL AND NOT synthetic",
            (trip["id"], today),
        ).fetchone()["n"]
        if n >= max_per_day:
            print(f"{trip.get('label', trip['id'])}: already {n} price(s) today, skipping.")
        else:
            due.append(trip)

    results = await scrape_trips(cfg, due) if due else {}

    any_scraped = False
    for trip in due:
        prev = conn.execute(
            "SELECT price_usd FROM price_history WHERE trip_id=%s AND price_usd IS NOT NULL "
            "AND NOT synthetic ORDER BY scraped_at DESC LIMIT 1",
            (trip["id"],),
        ).fetchone()
        result = results.get(trip["id"])
        now = utcnow()
        if result:
            conn.execute(
                "INSERT INTO price_history "
                "(trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (trip["id"], now, result["price"], result["airline"], result["stops"],
                 result["depart_time"], result["arrive_time"], result["fare_class"], result["flights"]),
            )
            any_scraped = True
            fire_alerts(cfg, trip, result["price"], prev["price_usd"] if prev else None)
        else:
            conn.execute(
                "INSERT INTO price_history (trip_id, scraped_at, notes) VALUES (%s,%s,'scrape failed')",
                (trip["id"], now),
            )

    if any_scraped:
        print("\n── Updating dashboard ──")
        push_to_github(cfg, generate_html(conn, trips))
    conn.close()

async def main():
    cfg = load_cfg()
    if "--dry-run" in sys.argv:
        wl = subprocess.run(["curl", "-sf", WATCHLIST_URL], capture_output=True, text=True, check=True)
        trips = json.loads(wl.stdout).get("trips", [])
        await scrape_trips(cfg, trips)
        print("\nDry run: nothing saved, emailed or published.")
        return
    tunnel = open_tunnel(cfg)
    try:
        await run(cfg)
    finally:
        if tunnel:
            tunnel.terminate()

if __name__ == "__main__":
    asyncio.run(main())
