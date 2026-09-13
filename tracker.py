#!/usr/bin/env python3
"""Flight price tracker — Delta.com → GitHub Pages dashboard."""

import asyncio, json, re, smtplib, sqlite3, subprocess, sys, os
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright

# playwright-stealth 2.x exposes Stealth(); 1.x exposed stealth_async()
try:
    from playwright_stealth import Stealth
    async def apply_stealth(page):
        await Stealth().apply_stealth_async(page)
    HAS_STEALTH = True
except ImportError:
    try:
        from playwright_stealth import stealth_async as apply_stealth
        HAS_STEALTH = True
    except ImportError:
        HAS_STEALTH = False
        print("Warning: playwright-stealth not installed; WAF bypass disabled")

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
DB_FILE   = BASE_DIR / "prices.db"
CFG_FILE  = BASE_DIR / "config.json"
WL_FILE   = BASE_DIR / "watchlist.json"
REPO_DIR  = Path.home() / "airfaretracker-repo"
PROFILE   = str(Path.home() / ".config" / "delta-tracker-profile")
DASHBOARD = "https://drgian.github.io/AirfareTracker/flight_tracker.html"

# ── Config ────────────────────────────────────────────────────────────────────
def load_cfg():
    with open(CFG_FILE) as f:
        return json.load(f)

# ── Database ──────────────────────────────────────────────────────────────────
def open_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS price_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        trip_id     TEXT    NOT NULL DEFAULT 'grr-bon-2026-11-28',
        scraped_at  TEXT    NOT NULL,
        price_usd   REAL,
        airline     TEXT,
        stops       TEXT,
        depart_time TEXT,
        arrive_time TEXT,
        notes       TEXT,
        flights     TEXT
    )""")
    # migration: add trip_id to older DBs
    try:
        conn.execute("ALTER TABLE price_history ADD COLUMN trip_id TEXT DEFAULT 'grr-bon-2026-11-28'")
    except Exception:
        pass
    conn.commit()
    return conn

# ── pick_best ─────────────────────────────────────────────────────────────────
def pick_best(results):
    if not results:
        return None
    classic = [r for r in results if r.get("fare_class") == "Main Classic"]
    pool = classic if classic else ([r for r in results if r.get("fare_class") != "First"] or results)
    best = min(pool, key=lambda r: r["price"])
    best.setdefault("airline", "Delta")
    return best

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
async def scrape_trip(trip):
    origin      = trip["origin"]
    dest        = trip["destination"]
    travel_date = trip["travel_date"]
    return_date = trip.get("return_date", "")
    outbound    = trip.get("outbound_flights", "")

    print(f"  Scraping {origin}→{dest} ({travel_date})...")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            PROFILE,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if HAS_STEALTH:
            await apply_stealth(page)

        result = None
        try:
            await page.goto(
                "https://www.delta.com/us/en/flight-search/round-trip",
                timeout=90000, wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(4000)

            try:
                await page.wait_for_selector("#fromAirportName", timeout=30000)
            except Exception:
                shot = BASE_DIR / f"debug_{trip['id']}.png"
                await page.screenshot(path=str(shot), full_page=True)
                title = await page.title()
                body  = (await page.inner_text("body"))[:400].replace("\n", " | ")
                print(f"  Search form never appeared. Title: {title!r}")
                print(f"  Page text: {body}")
                print(f"  Screenshot: {shot}")
                raise

            # Origin
            await page.click("#fromAirportName")
            await page.fill("#fromAirportName", origin)
            await page.wait_for_timeout(1500)
            try:
                await page.click(f"[data-code='{origin}']", timeout=4000)
            except Exception:
                await page.keyboard.press("ArrowDown")
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(500)

            # Destination
            await page.click("#toAirportName")
            await page.fill("#toAirportName", dest)
            await page.wait_for_timeout(1500)
            try:
                await page.click(f"[data-code='{dest}']", timeout=4000)
            except Exception:
                await page.keyboard.press("ArrowDown")
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(500)

            # Departure date
            dep_str = datetime.strptime(travel_date, "%Y-%m-%d").strftime("%m/%d/%Y")
            await page.fill("#departureDate", dep_str)
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(400)

            # Return date
            if return_date:
                ret_str = datetime.strptime(return_date, "%Y-%m-%d").strftime("%m/%d/%Y")
                await page.fill("#returnDate", ret_str)
                await page.keyboard.press("Tab")
                await page.wait_for_timeout(400)

            # Search
            await page.click("#btn-search")
            print("  Waiting for results...")
            await page.wait_for_timeout(20000)

            text = await page.inner_text("body")

            price      = None
            fare_class = "Main"
            stops      = None
            depart_t   = None
            arrive_t   = None
            flights    = None

            # Price extraction
            m = re.search(r'From[\s\S]{0,20}?\$([0-9,]+)[\s\S]{0,30}?Round Trip', text)
            if m:
                price = float(m.group(1).replace(",", ""))
            if not price:
                m2 = re.search(r'\$([0-9,]+)\s*/\s*person', text, re.IGNORECASE)
                if m2:
                    price = float(m2.group(1).replace(",", ""))

            # Fare class
            if "Main Classic" in text:
                fare_class = "Main Classic"
            elif "Main Select" in text:
                fare_class = "Main Select"

            # Stops
            if re.search(r'nonstop', text, re.IGNORECASE):
                stops = "0"
            elif re.search(r'1\s*stop', text, re.IGNORECASE):
                stops = "1"

            # Flight numbers — use watchlist outbound if set
            if outbound:
                flights = outbound
                for fn in re.findall(r'DL\d{3,4}', outbound):
                    idx = text.find(fn)
                    if idx >= 0:
                        snip = text[max(0, idx - 200):idx + 300]
                        times = re.findall(r'\b(\d{1,2}:\d{2})\b', snip)
                        if len(times) >= 2:
                            depart_t = times[0]
                            arrive_t = times[1]
                            break
            else:
                fl = re.findall(r'\bDL\s*(\d{3,4})\b', text)
                if fl:
                    seen = list(dict.fromkeys(fl))[:2]
                    flights = " / ".join(f"DL{n}" for n in seen)

            # Time fallback
            if not depart_t:
                times = re.findall(r'\b(\d{1,2}:\d{2})\b', text)
                if len(times) >= 2:
                    depart_t = times[0]
                    arrive_t = times[1]

            if price:
                result = {
                    "price":       price,
                    "airline":     "Delta",
                    "fare_class":  fare_class,
                    "stops":       stops,
                    "depart_time": depart_t,
                    "arrive_time": arrive_t,
                    "flights":     flights,
                }
                print(f"  → ${price:,.0f} ({fare_class})")
            else:
                print("  No price found.")
                print(f"  Page text (first 400 chars):\n{text[:400]}")

        except Exception as e:
            print(f"  Scrape error: {e}")
        finally:
            await ctx.close()

    return result

# ── HTML generation ───────────────────────────────────────────────────────────
def generate_html(conn, trips):
    def trip_rows(trip_id):
        rows = conn.execute(
            "SELECT scraped_at, price_usd as price, airline, stops, "
            "depart_time, arrive_time, notes, flights "
            "FROM price_history WHERE trip_id=? ORDER BY scraped_at",
            (trip_id,),
        ).fetchall()
        return [dict(r) for r in rows]

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
def push_to_github(cfg, html_content):
    token   = cfg.get("github_token", "")
    repo_url = (
        f"https://{token}@github.com/drgian/AirfareTracker.git"
        if token else
        "https://github.com/drgian/AirfareTracker.git"
    )

    if not REPO_DIR.exists():
        print("  Cloning AirfareTracker gh-pages...")
        subprocess.run(
            ["git", "clone", "-b", "gh-pages", repo_url, str(REPO_DIR)],
            check=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

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
async def main():
    cfg = load_cfg()
    conn = open_db()

    if WL_FILE.exists():
        with open(WL_FILE) as f:
            trips = json.load(f).get("trips", [])
    else:
        trips = [{
            "id":           f"{cfg['origin'].lower()}-{cfg['destination'].lower()}-{cfg['travel_date']}",
            "origin":       cfg["origin"],
            "destination":  cfg["destination"],
            "travel_date":  cfg["travel_date"],
            "return_date":  cfg.get("return_date", ""),
            "cabin_class":  cfg.get("cabin_class", "main_classic"),
        }]

    max_per_day = cfg.get("max_runs_per_day", 2)
    today       = datetime.now().strftime("%Y-%m-%d")
    any_scraped = False

    for trip in trips:
        trip_id = trip["id"]
        print(f"\n── {trip.get('label', trip_id)} ──")

        runs_today = conn.execute(
            "SELECT COUNT(*) FROM price_history "
            "WHERE trip_id=? AND scraped_at LIKE ? AND price_usd IS NOT NULL",
            (trip_id, today + "%"),
        ).fetchone()[0]

        if runs_today >= max_per_day:
            print(f"  Already {runs_today} price(s) today — skipping.")
            continue

        prev = conn.execute(
            "SELECT price_usd FROM price_history "
            "WHERE trip_id=? AND price_usd IS NOT NULL ORDER BY scraped_at DESC LIMIT 1",
            (trip_id,),
        ).fetchone()
        prev_price = prev[0] if prev else None

        result = await scrape_trip(trip)

        now_str = utcnow().isoformat()
        if result:
            conn.execute(
                "INSERT INTO price_history "
                "(trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (trip_id, now_str, result["price"], result.get("airline"),
                 result.get("stops"), result.get("depart_time"), result.get("arrive_time"),
                 result.get("fare_class", "Main"), result.get("flights")),
            )
            conn.commit()
            any_scraped = True
            fire_alerts(cfg, trip, result["price"], prev_price)
        else:
            conn.execute(
                "INSERT INTO price_history (trip_id, scraped_at, price_usd, notes) VALUES (?,?,NULL,'scrape failed')",
                (trip_id, now_str),
            )
            conn.commit()

    if any_scraped:
        print("\n── Updating dashboard ──")
        html = generate_html(conn, trips)
        push_to_github(cfg, html)

    conn.close()

if __name__ == "__main__":
    asyncio.run(main())
