#!/usr/bin/env python3
"""Flight price tracker — Delta.com → GitHub Pages dashboard."""

import asyncio, contextlib, json, random, re, shutil, smtplib, socket, subprocess, sys, os, time, traceback

try:
    import jwt              # only needed for push notifications
except ImportError:
    jwt = None
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from playwright.async_api import async_playwright

VERSION = "1.8.0"

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).resolve().parent
CFG_FILE  = BASE_DIR / "config.json"
REPO_DIR  = BASE_DIR / "site-repo"
DASHBOARD = "https://flightfare.io/flight_tracker.html"
APP_URL   = "https://flightfare.io/"
# Task Scheduler may not see PATH changes made by installers until a reboot
# The legacy static dashboard needs git; the website doesn't. No git, no dashboard - the checks still run.
_WIN_GIT = r"C:\Program Files\Git\cmd\git.exe"
GIT = shutil.which("git") or (_WIN_GIT if os.name == "nt" and os.path.exists(_WIN_GIT) else None)

# ── Config ────────────────────────────────────────────────────────────────────
def load_cfg():
    with open(CFG_FILE, encoding="utf-8-sig") as f:
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
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS synthetic BOOLEAN NOT NULL DEFAULT FALSE;
-- on-demand checks (first price for a newly added trip) don't count toward the twice-daily limit
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'scheduled';
CREATE TABLE IF NOT EXISTS route_status (
    route_id   TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    detail     TEXT,
    checked_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS check_requests (
    id           SERIAL PRIMARY KEY,
    route_id     TEXT NOT NULL,
    requested_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    started_at   TIMESTAMP,
    finished_at  TIMESTAMP
);
CREATE INDEX IF NOT EXISTS check_requests_open ON check_requests (route_id) WHERE finished_at IS NULL;
-- Price changes wait here until the day's summary email goes out; threshold alerts still send immediately
CREATE TABLE IF NOT EXISTS price_alerts (
    id          SERIAL PRIMARY KEY,
    email       TEXT NOT NULL,
    route_id    TEXT NOT NULL,
    label       TEXT,
    route       TEXT NOT NULL,
    prev_price  DOUBLE PRECISION,
    new_price   DOUBLE PRECISION NOT NULL,
    link        TEXT NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    sent_at     TIMESTAMP
);
CREATE INDEX IF NOT EXISTS price_alerts_unsent ON price_alerts (email) WHERE sent_at IS NULL;"""

def open_tunnel(cfg):
    """When the scraper runs off the AWS box, reach its Postgres through an SSH tunnel."""
    t = cfg.get("ssh_tunnel")
    if not t:
        return None
    port = t.get("local_port", 55432)
    proc = subprocess.Popen(
        ["ssh", "-i", str(BASE_DIR / t["key"]), "-N", "-o", "ExitOnForwardFailure=yes",
         "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
         "-L", f"{port}:localhost:5432", f"{t['user']}@{t['host']}"],
        # Detached from our stdout so a leftover tunnel can't hold the log file open
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if proc.poll() is not None:
            raise RuntimeError("SSH tunnel to the database failed to start")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return proc
        except OSError:
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("SSH tunnel to the database timed out")

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
    sender = cfg.get("smtp_from", user)
    msg = MIMEMultipart()
    msg["From"]    = formataddr(("FlightFare", sender))
    msg["To"]      = ", ".join(to_list)
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))
    try:
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(user, pwd)
            s.sendmail(sender, to_list, msg.as_string())
    except Exception as e:
        print(f"  Email failed: {e}")
        return
    print(f"  Email → {', '.join(to_list)}: sent")

def alert_recipients(cfg, conn, trip, legacy):
    """{email: (alert threshold or None, dashboard link)} for everyone following this route."""
    out = {}
    if legacy:
        emails = cfg.get("alert_emails", [])
        # config.json's alert_below predates multi-trip support and belongs to its own route
        own = (trip["origin"], trip["destination"]) == (cfg.get("origin"), cfg.get("destination"))
        for e in ([emails] if isinstance(emails, str) else emails):
            out[e.lower()] = (cfg.get("alert_below") if own else None, DASHBOARD)
    for r in conn.execute("SELECT u.email, t.alert_below FROM user_trips t JOIN users u ON u.id = t.user_id "
                          "WHERE t.route_id=%s AND t.archived_at IS NULL AND u.disabled_at IS NULL", (trip["id"],)):
        old = out.get(r["email"], (None, APP_URL))[0]
        out[r["email"]] = (r["alert_below"] if r["alert_below"] is not None else old, APP_URL)
    return out

DIGEST_AFTER_HOUR = 12     # the day's summary goes out with the afternoon/evening check
DIGEST_MAX_WAIT    = timedelta(hours=26)

def fire_alerts(cfg, conn, recipients, trip, new_price, prev_price):
    """Queue the change for the daily summary; email at once only when a target price is newly met."""
    origin, dest = trip["origin"], trip["destination"]
    airline = AIRLINE_NAMES.get(trip.get("airline", "DL"), trip.get("airline", ""))
    route = f"{airline} {origin} → {dest}  |  {trip['travel_date']} – {trip.get('return_date', '')}"
    for email, (threshold, link) in recipients.items():
        if prev_price and new_price != prev_price:
            conn.execute("INSERT INTO price_alerts (email, route_id, label, route, prev_price, new_price, link) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (email, trip["id"], trip.get("label"), route, prev_price, new_price, link))
        # Only when it crosses the line, so sitting below the target doesn't email every check
        crossed = threshold and new_price < threshold and (prev_price is None or prev_price >= threshold)
        if crossed:
            send_email(cfg, [email],
                       f"Price Alert: {origin}→{dest} now ${new_price:,.0f} (below ${threshold:,.0f})",
                       "\n".join([f"Route: {route}", "",
                                  f"Current price: ${new_price:,.0f}",
                                  f"Alert threshold: ${threshold:,.0f}",
                                  f"Previous price: ${prev_price:,.0f}" if prev_price else "", "",
                                  f"Dashboard: {link}"]))
            push_to_email(cfg, conn, email,
                          f"{origin} → {dest} is ${new_price:,.0f}",
                          f"Below your ${threshold:,.0f} target"
                          + (f", down from ${prev_price:,.0f}" if prev_price else ""),
                          thread=trip["id"])

def send_digests(cfg, conn, force=False):
    """One summary email per person per day, covering every price change since the last one."""
    local_hour = datetime.now(EASTERN).hour
    rows = conn.execute("SELECT * FROM price_alerts WHERE sent_at IS NULL ORDER BY email, id").fetchall()
    if not rows:
        return 0
    oldest = min(r["created_at"] for r in rows)
    if not force and local_hour < DIGEST_AFTER_HOUR and utcnow() - oldest < DIGEST_MAX_WAIT:
        print(f"{len(rows)} price change(s) held for today's summary")
        return 0
    by_email = {}
    for r in rows:
        by_email.setdefault(r["email"], []).append(r)
    sent = 0
    for email, items in by_email.items():
        # A trip that moved several times is one line: where it started, where it ended up
        trips_moved = {}
        for r in items:
            t = trips_moved.setdefault(r["route_id"], {"first": r, "last": r, "moves": 0})
            t["last"] = r
            t["moves"] += 1
        summaries = []
        for t in trips_moved.values():
            start, end = t["first"]["prev_price"], t["last"]["new_price"]
            summaries.append((end - start, t, start, end))
        summaries.sort(key=lambda x: x[0])
        down = sum(1 for d, *_ in summaries if d < 0)
        up = sum(1 for d, *_ in summaries if d > 0)
        bits = [b for b in (f"{down} down" if down else "", f"{up} up" if up else "") if b]
        n = len(summaries)
        subject = f"FlightFare: {n} trip{'s' if n > 1 else ''} changed price ({', '.join(bits) or 'no net change'})"
        lines = ["Here's what moved since your last summary:", ""]
        for delta, t, start, end in summaries:
            r = t["last"]
            sign = "+" if delta > 0 else "-"
            moved = f"   ({t['moves']} changes today)" if t["moves"] > 1 else ""
            lines += [f"{r['label'] or r['route_id']}  ({r['route']})",
                      f"   ${start:,.0f} → ${end:,.0f}   {sign}${abs(delta):,.0f}{moved}", ""]
        lines += [f"Dashboard: {items[0]['link']}"]
        claimed = [r["id"] for r in conn.execute(
            "UPDATE price_alerts SET sent_at=%s WHERE id = ANY(%s) AND sent_at IS NULL RETURNING id",
            (utcnow(), [r["id"] for r in items]))]
        if not claimed:
            continue                      # another run already sent these
        try:
            send_email(cfg, [email], subject, "\n".join(lines))
        except Exception as e:
            conn.execute("UPDATE price_alerts SET sent_at=NULL WHERE id = ANY(%s)", (claimed,))
            print(f"  couldn't send the summary to {email}, will retry: {e}")
            continue
        biggest = summaries[0]
        push_to_email(cfg, conn, email, subject.replace("FlightFare: ", ""),
                      f"Biggest move: {biggest[1]['last']['label'] or biggest[1]['last']['route_id']} "
                      f"${biggest[2]:,.0f} → ${biggest[3]:,.0f}", thread="digest")
        sent += 1
    return sent

# ── Scraper ───────────────────────────────────────────────────────────────────
# Delta's bot protection rejects browsers launched by automation tools, so we start an
# ordinary Chrome and attach to it over the DevTools protocol instead.
CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
EASTERN  = ZoneInfo("America/New_York")
CDP_PORT = 9333
SEARCH_GAP = 12        # seconds between searches; airlines return nothing if you rush them
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

def flight_designators(text):
    return [f"{c}{int(n)}" for c, n in re.findall(r"([A-Z0-9]{2})\s*(\d{1,4})", (text or "").upper())]

def parse_offers(data, trip):
    """(result, status, detail): cheapest fare in the trip's cabin, on its pinned outbound flights if any."""
    brand  = CABIN_TO_BRAND.get(trip.get("cabin_class", "main_classic"), "CMAIN")
    pinned = flight_designators(trip.get("outbound_flights", ""))
    sets   = data["data"]["gqlSearchOffers"].get("gqlOffersSets") or []
    if not sets:
        return None, "no_flights", "Delta has no flights on this route for these dates."
    fastest = not pinned and trip.get("preference") == "fastest"
    matched, candidates = False, []
    for offer_set in sets:
        t = offer_set["trips"][0]
        flights = [f'{seg["marketingCarrier"]["carrierCode"]}{int(seg["marketingCarrier"]["carrierNum"])}'
                   for seg in t["flightSegment"]]
        if pinned and flights != pinned:
            continue
        matched = True
        tt = t.get("totalTripTime") or {}
        minutes = tt.get("dayCnt", 0) * 1440 + tt.get("hourCnt", 0) * 60 + tt.get("minuteCnt", 0)
        for offer in offer_set["offers"]:
            props = offer["additionalOfferProperties"]
            if props.get("dominantSegmentBrandId") != brand or props.get("soldOut"):
                continue
            amt = re.search(r'"roundedCurrencyAmt": (\d+)', json.dumps(offer))
            if not amt:
                continue
            candidates.append((minutes, float(amt.group(1)), t, flights))
    best = None
    if candidates:
        # Fastest = shortest total travel time, ties to the cheaper fare; otherwise cheapest fare
        minutes, price, t, flights = min(candidates, key=(lambda c: (c[0], c[1])) if fastest else (lambda c: (c[1], c[0])))
        best = {
            "price":       price,
            "airline":     "Delta",
            "fare_class":  BRAND_NAMES.get(brand, brand),
            "stops":       str(t.get("stopCnt", "")),
            "depart_time": t["scheduledDepartureLocalTs"][11:16],
            "arrive_time": t["scheduledArrivalLocalTs"][11:16],
            "flights":     " / ".join(flights),
        }
    if pinned and not matched:
        return None, "flights_not_found", f"Delta didn't offer {' / '.join(pinned)} on these dates."
    if not best:
        where = "these flights" if pinned else "this route"
        return None, "cabin_unavailable", f"{BRAND_NAMES.get(brand, brand)} isn't available on {where} for these dates."
    return best, "ok", None

AIRLINE_NAMES = {"DL": "Delta", "AA": "American", "UA": "United", "WN": "Southwest"}
# Each airline's own product names for the cabins we offer
AA_CABIN = {"basic": "BASIC_ECONOMY", "main": "COACH", "main_extra": "COACH_PLUS", "business": "BUSINESS"}
AA_CABIN_NAMES = {"basic": "Basic Economy", "main": "Main Cabin", "main_extra": "Main Cabin Extra", "business": "Business"}
UA_CABIN = {"basic": {"ECO-BASIC"}, "main": {"ECONOMY"}, "economy_plus": {"ECONOMY-MERCH-EPLUS"},
            "business": {"MIN-BUSINESS-OR-FIRST", "BUSINESS", "FIRST", "PREMIUM-PLUS"}}
UA_CABIN_NAMES = {"basic": "Basic Economy", "main": "United Economy", "economy_plus": "Economy Plus", "business": "Business"}

# Southwest's own codes for the fares it now calls Basic / Choice / Choice Preferred / Choice Extra
WN_CABIN = {"basic": "WGA", "choice": "PLU", "choice_preferred": "ANY", "choice_extra": "BUS"}
WN_CABIN_NAMES = {"basic": "Basic", "choice": "Choice", "choice_preferred": "Choice Preferred",
                  "choice_extra": "Choice Extra"}

def best_offer(candidates, fastest):
    """candidates: (minutes, price, extras). Fastest = shortest trip, ties to the cheaper fare; else cheapest."""
    return min(candidates, key=(lambda c: (c[0], c[1])) if fastest else (lambda c: (c[1], c[0])))

async def search_american(page, trip):
    """American publishes the whole fare table inside its results page."""
    cabin = trip.get("cabin_class", "main")
    product = AA_CABIN.get(cabin)
    cabin_name = AA_CABIN_NAMES.get(cabin, cabin)
    if not product:
        return None, "cabin_unavailable", f"American doesn't sell {cabin_name}."
    pinned = flight_designators(trip.get("outbound_flights", ""))
    us = lambda d: f"{d[5:7]}/{d[8:10]}/{d[0:4]}"
    await page.goto("https://www.aa.com/", timeout=90000, wait_until="domcontentloaded")
    await page.wait_for_timeout(3500)
    for sel, val in ((r"#reservationFlightSearchForm\.originAirport", trip["origin"]),
                     (r"#reservationFlightSearchForm\.destinationAirport", trip["destination"])):
        await page.fill(sel, "")
        await page.fill(sel, val)
        await page.wait_for_timeout(700)
    await page.fill("#aa-leavingOn", us(trip["travel_date"]))
    await page.fill("#aa-returningFrom", us(trip["return_date"]))
    await page.keyboard.press("Escape")
    await page.click(r"#flightSearchForm\.button\.reSubmit")
    for _ in range(40):
        await page.wait_for_timeout(2000)
        if "choose-flights" in page.url and re.search(r"\$\s?\d", await page.inner_text("body")):
            break
    html = await page.content()
    m = re.search(r'<script id="ng-state" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        return None, "error", f"American returned no fares (page: {await page.title()})"
    result = json.loads(m.group(1)).get("SearchData", {}).get("itineraryResult") or {}
    slices = result.get("slices") or []
    if not slices:
        return None, "no_flights", "American has no flights on this route for these dates."
    matched, candidates = False, []
    for sl in slices:
        flights = [f"{seg['flight']['carrierCode']}{int(seg['flight']['flightNumber'])}"
                   for seg in sl.get("segments", []) if seg.get("flight")]
        if pinned and flights != pinned:
            continue
        matched = True
        for pd in sl.get("pricingDetail", []):
            # allPassengerDisplayTotal is the round-trip total with taxes; slicePricing is one leg only
            amt = (pd.get("allPassengerDisplayTotal") or {}).get("amount")
            if pd.get("productType") != product or not pd.get("productAvailable") or not amt:
                continue
            candidates.append((sl.get("durationInMinutes") or 0, float(amt), sl, flights))
    if pinned and not matched:
        return None, "flights_not_found", f"American didn't offer {' / '.join(pinned)} on these dates."
    if not candidates:
        where = "these flights" if pinned else "this route"
        return None, "cabin_unavailable", f"{cabin_name} isn't available on {where} for these dates."
    _, price, sl, flights = best_offer(candidates, not pinned and trip.get("preference") == "fastest")
    return {"price": price, "airline": "American", "fare_class": cabin_name,
            "stops": str(sl.get("stops", "")), "depart_time": (sl.get("departureDateTime") or "")[11:16],
            "arrive_time": (sl.get("arrivalDateTime") or "")[11:16], "flights": " / ".join(flights)}, "ok", None

async def search_united(page, trip):
    """United's results page can be linked to directly; fares arrive as a stream of JSON chunks."""
    cabin = trip.get("cabin_class", "main")
    wanted = UA_CABIN.get(cabin)
    cabin_name = UA_CABIN_NAMES.get(cabin, cabin)
    if not wanted:
        return None, "cabin_unavailable", f"United doesn't sell {cabin_name}."
    pinned = flight_designators(trip.get("outbound_flights", ""))
    stream = {}
    # United's results page holds on to search state, so a second search in the same tab comes back empty:
    # give every search its own tab
    tab = await page.context.new_page()
    async def on_response(resp):
        if "FetchSSE" in resp.url or "FetchFlights" in resp.url:
            try:
                stream["body"] = await resp.text()
            except Exception:
                pass
    tab.on("response", on_response)
    try:
        url = (f"https://www.united.com/en/us/fsr/choose-flights?f={trip['origin']}&t={trip['destination']}"
               f"&d={trip['travel_date']}&r={trip['return_date']}"
               "&sc=7%2C7&px=1&taxng=1&newHP=True&clm=7&st=bestmatches&tqp=R")
        await tab.goto(url, timeout=90000, wait_until="domcontentloaded")
        for _ in range(40):
            await tab.wait_for_timeout(2000)
            if stream.get("body") and re.search(r"\$\s?[\d,]{3,}", await tab.inner_text("body")):
                break
        if not stream.get("body"):
            body = await tab.inner_text("body")
            if re.search(r"can't process|restart your search|no flights", body, re.I):
                return None, "no_flights", "United has no flights on this route for these dates."
            return None, "error", f"United returned no fares (page: {await tab.title()})"
    finally:
        tab.remove_listener("response", on_response)
        await tab.close()
    matched, candidates = False, []
    for chunk in re.findall(r"^data:\s*(.+)$", stream["body"], re.M):
        try:
            flight = (json.loads(chunk) or {}).get("flight")
        except Exception:
            continue
        if not flight:
            continue
        legs = [flight] + list(flight.get("connections") or [])
        flights = [f"{leg.get('marketingCarrier', 'UA')}{int(leg.get('flightNumber'))}" for leg in legs
                   if leg.get("flightNumber")]
        if pinned and flights != pinned:
            continue
        matched = True
        for prod in flight.get("products", []):
            if prod.get("productType") not in wanted:
                continue
            price = next((pr.get("amount") for pr in prod.get("prices", [])
                          if pr.get("pricingType") == "Fare" and pr.get("amount")), None)
            if price:
                candidates.append((flight.get("travelMinutesTotal") or 0, float(price), flight, legs, flights))
    if not matched and not candidates and not pinned:
        return None, "no_flights", "United has no flights on this route for these dates."
    if pinned and not matched:
        return None, "flights_not_found", f"United didn't offer {' / '.join(pinned)} on these dates."
    if not candidates:
        where = "these flights" if pinned else "this route"
        return None, "cabin_unavailable", f"{cabin_name} isn't available on {where} for these dates."
    _, price, flight, legs, flights = best_offer(candidates, not pinned and trip.get("preference") == "fastest")
    return {"price": price, "airline": "United", "fare_class": cabin_name,
            "stops": str(len(legs) - 1), "depart_time": (flight.get("departDateTime") or "")[11:16],
            "arrive_time": (legs[-1].get("destinationDateTime") or "")[11:16],
            "flights": " / ".join(flights)}, "ok", None

async def search_southwest(page, trip):
    """Southwest prices each direction on its own, so a round trip is the two halves added together."""
    cabin = trip.get("cabin_class", "choice")
    family = WN_CABIN.get(cabin)
    cabin_name = WN_CABIN_NAMES.get(cabin, cabin)
    if not family:
        return None, "cabin_unavailable", f"Southwest doesn't sell {cabin_name}."
    pinned = flight_designators(trip.get("outbound_flights", ""))
    payload = {}
    tab = await page.context.new_page()
    async def on_response(resp):
        if "air-booking/page/air/booking/shopping" in resp.url:
            try:
                payload["body"] = await resp.text()
            except Exception:
                pass
    tab.on("response", on_response)
    try:
        url = ("https://www.southwest.com/air/booking/select-depart.html?adultPassengersCount=1"
               f"&departureDate={trip['travel_date']}&departureTimeOfDay=ALL_DAY"
               f"&destinationAirportCode={trip['destination']}&fareType=USD"
               f"&originationAirportCode={trip['origin']}&passengerType=ADULT"
               f"&returnDate={trip['return_date']}&returnTimeOfDay=ALL_DAY&tripType=roundtrip")
        await tab.goto(url, timeout=90000, wait_until="domcontentloaded")
        for _ in range(40):
            await tab.wait_for_timeout(2000)
            if payload.get("body"):
                break
        if not payload.get("body"):
            body = await tab.inner_text("body")
            if re.search(r"no flights|not available|doesn't fly", body, re.I):
                return None, "no_flights", "Southwest has no flights on this route for these dates."
            return None, "error", f"Southwest returned no fares (page: {await tab.title()})"
    finally:
        tab.remove_listener("response", on_response)
        await tab.close()

    try:
        bounds = json.loads(payload["body"])["data"]["searchResults"]["airProducts"]
    except Exception:
        return None, "error", "Southwest's fare data wasn't in the shape we expect."
    if len(bounds) < 2:
        return None, "no_flights", "Southwest has no flights on this route for these dates."

    def options(bound, want_pinned):
        """(minutes, price, flights, depart, arrive, stops) for every flight that sells this fare."""
        out = []
        for det in bound.get("details", []):
            flights = [f"WN{int(n)}" for n in det.get("flightNumbers", [])]
            if want_pinned and pinned and flights != pinned:
                continue
            prod = ((det.get("fareProducts") or {}).get("ADULT") or {}).get(family) or {}
            fare = (prod.get("fare") or {}).get("totalFare") or {}
            if prod.get("availabilityStatus") != "AVAILABLE" or not fare.get("value"):
                continue
            out.append((det.get("totalDuration") or 0, float(fare["value"]), flights,
                        det.get("departureTime", ""), det.get("arrivalTime", ""),
                        max(len(det.get("segments", [])) - 1, 0)))
        return out

    outbound = options(bounds[0], True)
    if pinned and not outbound:
        if not options(bounds[0], False):
            return None, "cabin_unavailable", f"{cabin_name} isn't available on this route for these dates."
        return None, "flights_not_found", f"Southwest didn't offer {' / '.join(pinned)} on these dates."
    ret = options(bounds[1], False)
    if not outbound or not ret:
        return None, "cabin_unavailable", f"{cabin_name} isn't available on this route for these dates."

    fastest = not pinned and trip.get("preference") == "fastest"
    minutes, price, flights, dep, arr, stops = best_offer(
        [(o[0], o[1], o) for o in outbound], fastest)[2]
    back_price = min(r[1] for r in ret)          # the return half is always the cheapest seat going back
    return {"price": price + back_price, "airline": "Southwest", "fare_class": cabin_name,
            "stops": str(stops), "depart_time": dep, "arrive_time": arr,
            "flights": " / ".join(flights)}, "ok", None

class AirportNotServed(Exception):
    pass

async def pick_airport(page, which, code):
    btn = page.locator(f"[id$='-{which}-button'] >> visible=true").first
    if f", {code}," in (await btn.get_attribute("aria-label") or ""):
        return
    await btn.click()
    await page.wait_for_timeout(1000)
    await page.keyboard.type(code, delay=120)
    option = page.locator("li[role=option] >> visible=true").filter(has_text=re.compile(rf"^\s*{code}\b"))
    try:
        await option.first.wait_for(timeout=8000)
    except Exception:
        raise AirportNotServed(code)
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
            title = await page.title()
            print(f"  No fares returned (page: {title!r})")
            await page.screenshot(path=str(BASE_DIR / f"debug_{trip['id']}.png"))
            return None, "error", f"Delta returned no results (page: {title})"
        return parse_offers(offers[0], trip)   # guarded_search prints the outcome
    except AirportNotServed as e:
        print(f"  Delta's search doesn't offer airport {e}")
        return None, "airport_not_served", f"Delta doesn't fly to or from {e}."
    except Exception as e:
        print(f"  Scrape error: {str(e).splitlines()[0]}")
        await page.screenshot(path=str(BASE_DIR / f"debug_{trip['id']}.png"))
        return None, "error", str(e).splitlines()[0][:200]
    finally:
        page.remove_listener("response", on_response)

async def guarded_search(search, page, trip):
    """Run one airline's search, turning anything unexpected into an error result with a screenshot."""
    try:
        result, status, detail = await search(page, trip)
    except AirportNotServed as e:
        print(f"  The airline's search doesn't offer airport {e}")
        return None, "airport_not_served", f"This airline doesn't fly to or from {e}."
    except Exception as e:
        print(f"  Scrape error: {str(e).splitlines()[0]}")
        try:
            await page.screenshot(path=str(BASE_DIR / f"debug_{trip['id']}.png"))
        except Exception:
            pass
        return None, "error", str(e).splitlines()[0][:200]
    if result:
        print(f"  → ${result['price']:,.0f} {result['fare_class']}  {result['flights']}  "
              f"{result['depart_time']}→{result['arrive_time']}")
    else:
        print(f"  {status}: {detail}")
    return result, status, detail

def ensure_display():
    """Chrome needs an X display.

    On a Wayland machine Chrome will quietly attach to the compositor instead, and in that mode it
    never opens its remote-debugging port - so we pin it to X11 and to the display we intend.
    """
    if os.name == "nt":
        return
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = os.environ.get("FALLBACK_DISPLAY", ":99")
        print(f"  no DISPLAY set; using {os.environ['DISPLAY']}")
    os.environ.pop("WAYLAND_DISPLAY", None)
    os.environ["XDG_SESSION_TYPE"] = "x11"

def clear_stale_chrome_locks():
    """A Chrome that died badly leaves lock files behind, and the next launch exits on sight of them."""
    profile = BASE_DIR / "chrome-profile"
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"  couldn't clear {name}: {e}")

async def open_browser(cfg):
    """Start Chrome and attach to it. Returns (process, browser, page, playwright context)."""
    ensure_display()
    clear_stale_chrome_locks()
    prefix = []
    if os.name != "nt" and os.environ.get("INVOCATION_ID") and shutil.which("systemd-run"):
        prefix = ["systemd-run", "--user", "--scope", "--collect", "-q"]
    ozone = [] if os.name == "nt" else ["--ozone-platform=x11"]
    chrome = subprocess.Popen(
        prefix + [chrome_path(cfg), f"--remote-debugging-port={CDP_PORT}", *ozone,
         f"--user-data-dir={BASE_DIR / 'chrome-profile'}",
         "--no-first-run", "--no-default-browser-check", "--window-size=1280,900", "about:blank"],
        stdout=open(BASE_DIR / "chrome.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    p_ctx = await async_playwright().start()
    browser = None
    for _ in range(120):
        try:
            browser = await p_ctx.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
            break
        except Exception:
            await asyncio.sleep(0.5)
    if browser is None:
        chrome.terminate()
        await p_ctx.stop()
        raise RuntimeError("Could not attach to Chrome")
    ctx = browser.contexts[0]
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    return chrome, browser, page, p_ctx

async def close_browser(chrome, browser, p_ctx):
    try:
        if browser:
            cdp = await browser.new_browser_cdp_session()
            await cdp.send("Browser.close")
    except Exception:
        pass
    for closer in (lambda: p_ctx and p_ctx.stop(), lambda: chrome and chrome.terminate()):
        try:
            r = closer()
            if hasattr(r, "__await__"):
                await r
        except Exception:
            pass

async def scrape_trips(cfg, trips):
    """Search every trip in one Chrome session; returns {trip_id: (result, status, detail)}."""
    ensure_display()
    clear_stale_chrome_locks()
    # Under a systemd service, Chrome exits the moment it forks. Run it in its own transient scope
    # so it lives independently of this unit; everywhere else, launch it directly.
    prefix = []
    if os.name != "nt" and os.environ.get("INVOCATION_ID") and shutil.which("systemd-run"):
        prefix = ["systemd-run", "--user", "--scope", "--collect", "-q"]
    ozone = [] if os.name == "nt" else ["--ozone-platform=x11"]
    chrome = subprocess.Popen(
        prefix + [chrome_path(cfg), f"--remote-debugging-port={CDP_PORT}", *ozone,
         f"--user-data-dir={BASE_DIR / 'chrome-profile'}",
         "--no-first-run", "--no-default-browser-check", "--window-size=1280,900", "about:blank"],
        # Chrome's own output goes to a file: when it refuses to start, this is the only place it says why
        stdout=open(BASE_DIR / "chrome.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    results = {}
    try:
        async with async_playwright() as p:
            browser = None
            # Cold starts on a small machine can take well over 15 seconds, so wait a full minute
            for _ in range(120):
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
                airline = trip.get("airline", "DL")
                print(f"\n── {trip.get('label', trip['id'])} ({AIRLINE_NAMES.get(airline, airline)}) ──")
                search = {"AA": search_american, "UA": search_united,
                          "WN": search_southwest}.get(airline, search_trip)
                for attempt in range(2):
                    results[trip["id"]] = await guarded_search(search, page, trip)
                    if results[trip["id"]][1] != "error":
                        break
                    if attempt == 0:
                        print("  Retrying...")
                        await asyncio.sleep(SEARCH_GAP)
                # Searches fired back to back come back empty (United throttles them), so pace them out
                if trip is not trips[-1]:
                    await asyncio.sleep(SEARCH_GAP)
            # Shut Chrome down via DevTools; killing only its main process can leave
            # helpers holding the profile lock, which breaks the next run
            cdp = await browser.new_browser_cdp_session()
            await cdp.send("Browser.close")
    finally:
        chrome.terminate()
    return results

# ── Push notifications to the iOS app ─────────────────────────────────────────
APNS_HOSTS = {"production": "api.push.apple.com", "sandbox": "api.sandbox.push.apple.com"}

def apns_token(cfg):
    """A short-lived JWT signed with the APNs key. Apple allows reuse for up to an hour."""
    now = int(time.time())
    cached = getattr(apns_token, "_cache", None)
    if cached and now - cached[1] < 2400:
        return cached[0]
    key_file = BASE_DIR / cfg.get("apns_key_file", "apns.p8")
    if not key_file.exists():
        return None
    token = jwt.encode({"iss": cfg["apple_team_id"], "iat": now}, key_file.read_text(),
                       algorithm="ES256", headers={"kid": cfg["apns_key_id"]})
    apns_token._cache = (token, now)
    return token

def send_push(cfg, conn, user_ids, title, body, thread=None):
    """Send one notification to every phone belonging to these people. Silent if push isn't configured."""
    if not user_ids or not cfg.get("apns_key_id"):
        return 0
    token = apns_token(cfg)
    if not token:
        print("  push: no APNs key on this machine, skipping")
        return 0
    rows = conn.execute("SELECT id, token, environment FROM devices WHERE user_id = ANY(%s) AND failed_at IS NULL",
                        (list(user_ids),)).fetchall()
    sent = 0
    for d in rows:
        payload = json.dumps({"aps": {"alert": {"title": title, "body": body}, "sound": "default",
                                      "thread-id": thread or "flightfare"}})
        url = f"https://{APNS_HOSTS.get(d['environment'], APNS_HOSTS['production'])}/3/device/{d['token']}"
        try:
            out = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--http2", "-X", "POST",
                 "-H", f"authorization: bearer {token}",
                 "-H", f"apns-topic: {cfg.get('apple_bundle_id', 'io.flightfare.app')}",
                 "-H", "apns-push-type: alert", "-H", "apns-priority: 10",
                 "-d", payload, url, "--max-time", "20"],
                capture_output=True, text=True).stdout.strip()
        except Exception as e:
            print(f"  push failed: {e}")
            continue
        if out == "200":
            sent += 1
        elif out in ("410", "400"):     # the phone uninstalled the app or the token is bad
            conn.execute("UPDATE devices SET failed_at=%s WHERE id=%s", (utcnow(), d["id"]))
            print(f"  push: device no longer reachable ({out}), retired it")
        else:
            print(f"  push: Apple said {out}")
    if sent:
        print(f"  pushed to {sent} device(s)")
    return sent

def push_to_email(cfg, conn, email, title, body, thread=None):
    """Same notification, addressed the way the rest of the alerting is: by email."""
    if not cfg.get("apns_key_id"):
        return 0
    ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE lower(email)=lower(%s)", (email,))]
    return send_push(cfg, conn, ids, title, body, thread)

# ── Research: the same trip priced every week, six months out ─────────────────
RESEARCH_SPREAD_HOURS = 20      # spread the day's searches over this many hours, not in a burst

def wti_price(conn):
    """Today's WTI oil price, fetched once a day from the St. Louis Fed (no key needed).

    The series lags by a few days and skips weekends, so we take the most recent published figure.
    A missing price isn't fatal - the observation is still recorded, just without it.
    """
    today = utcnow().date()
    row = conn.execute("SELECT wti_usd FROM market_prices WHERE day=%s", (today,)).fetchone()
    if row:
        return row["wti_usd"]
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILWTICO"
    csv = ""
    try:
        import urllib.request
        csv = urllib.request.urlopen(url, timeout=25).read().decode()
    except Exception:
        try:      # Python's SSL trust store is unreliable across these machines; curl is not
            csv = subprocess.run(["curl", "-sL", "--max-time", "25", url],
                                 capture_output=True, text=True, check=True).stdout
        except Exception:
            csv = ""
    price = None
    for line in reversed(csv.strip().splitlines()):
        parts = line.split(",")
        if len(parts) == 2 and parts[0][:1].isdigit():
            try:
                price = float(parts[1])
                break
            except ValueError:
                continue          # FRED writes "." on holidays
    if price is None:
        print("  couldn't fetch the oil price; recording without it")
        return None
    conn.execute("INSERT INTO market_prices (day, wti_usd) VALUES (%s,%s) ON CONFLICT (day) DO NOTHING",
                 (today, price))
    print(f"  oil (WTI): ${price:,.2f}")
    return price

def build_research_queue(conn):
    """Each morning, lay out the day's ladder: every week from next week to six months out."""
    today = utcnow().date()
    made = 0
    for r in conn.execute("SELECT * FROM research_routes WHERE active").fetchall():
        # first rung: the next occurrence of the chosen weekday, at least 7 days out
        ahead = (r["weekday"] - (today.weekday())) % 7 or 7
        first = today + timedelta(days=ahead if ahead >= 7 else ahead + 7)
        rungs = [first + timedelta(weeks=w) for w in range(r["weeks"])]
        gap = (RESEARCH_SPREAD_HOURS * 3600) / max(len(rungs), 1)
        start = utcnow()
        for i, dep in enumerate(rungs):
            ret = dep + timedelta(days=r["nights"])
            due = start + timedelta(seconds=gap * i + random.uniform(0, gap * 0.4))
            made += conn.execute(
                "INSERT INTO research_queue (route_id, departure_date, return_date, due_at, for_day) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (route_id, departure_date, for_day) DO NOTHING",
                (r["id"], dep, ret, due, today)).rowcount
    if made:
        print(f"Research: queued {made} searches for today, spread over {RESEARCH_SPREAD_HOURS}h")
    return made

async def run_research(cfg, conn, page):
    """Price whatever research searches are due now. One at a time, paced like everything else."""
    due = conn.execute(
        "SELECT q.*, r.origin, r.destination, r.airline, r.cabin_class, r.preference, r.label "
        "FROM research_queue q JOIN research_routes r ON r.id = q.route_id "
        "WHERE q.finished_at IS NULL AND q.due_at <= %s AND r.active ORDER BY q.due_at LIMIT 5",
        (utcnow(),)).fetchall()
    if not due:
        return 0
    oil = wti_price(conn)
    done = 0
    for item in due:
        conn.execute("UPDATE research_queue SET started_at=%s WHERE id=%s", (utcnow(), item["id"]))
        trip = {"id": f"research-{item['id']}", "label": f"{item['label']} {item['departure_date']}",
                "airline": item["airline"], "origin": item["origin"], "destination": item["destination"],
                "travel_date": item["departure_date"].isoformat(), "return_date": item["return_date"].isoformat(),
                "cabin_class": item["cabin_class"], "preference": item["preference"], "outbound_flights": ""}
        search = {"AA": search_american, "UA": search_united}.get(item["airline"], search_trip)
        print(f"\n── research: {trip['label']} ──")
        for attempt in range(2):        # airlines occasionally return an empty page; one retry, as elsewhere
            result, status, detail = await guarded_search(search, page, trip)
            if status != "error":
                break
            if attempt == 0:
                print("  Retrying...")
                await asyncio.sleep(SEARCH_GAP)
        today = utcnow().date()
        conn.execute(
            "INSERT INTO research_prices (route_id, observed_on, departure_date, return_date, days_out, "
            "depart_dow, observed_dow, price_usd, status, notes, flights, depart_time, arrive_time, stops, oil_usd) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (route_id, observed_on, departure_date) DO UPDATE SET price_usd=EXCLUDED.price_usd, "
            "status=EXCLUDED.status, flights=EXCLUDED.flights, oil_usd=EXCLUDED.oil_usd",
            (item["route_id"], today, item["departure_date"], item["return_date"],
             (item["departure_date"] - today).days, item["departure_date"].weekday(), today.weekday(),
             result["price"] if result else None, status, detail,
             result["flights"] if result else None, result["depart_time"] if result else None,
             result["arrive_time"] if result else None, result["stops"] if result else None, oil))
        conn.execute("UPDATE research_queue SET finished_at=%s WHERE id=%s", (utcnow(), item["id"]))
        done += 1
        await asyncio.sleep(SEARCH_GAP)
    return done

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
  const chg=delta?(delta>0?'<span style="color:var(--red)">▲ $'+Math.abs(delta).toLocaleString()+'</span>':'<span style="color:var(--green)">▼ $'+Math.abs(delta).toLocaleString()+'</span>'):'<span style="color:var(--muted)">$0</span>';
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
        subprocess.run([GIT, "clone", "-b", "gh-pages", repo_url, str(REPO_DIR)],
                       check=True, env=genv)
    else:
        subprocess.run([GIT, "-C", str(REPO_DIR), "pull", "--rebase", "origin", "gh-pages"],
                       check=True, env=genv)

def push_to_github(cfg, html_content):
    (REPO_DIR / "flight_tracker.html").write_text(html_content, encoding="utf-8")

    git  = [GIT, "-C", str(REPO_DIR)]
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

LOCK_FILE = BASE_DIR / "scrape.lock"

@contextlib.contextmanager
def scrape_lock(wait_seconds=1500):
    """Only one Chrome session may use the profile at a time (scheduled run vs. worker)."""
    deadline = time.time() + wait_seconds
    while True:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - LOCK_FILE.stat().st_mtime > 1800:   # left behind by a crashed run
                    LOCK_FILE.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.time() > deadline:
                raise TimeoutError("another price check is still running")
            time.sleep(5)
    try:
        yield
    finally:
        LOCK_FILE.unlink(missing_ok=True)

def user_route_trips(conn):
    """One trip dict per distinct route added on the website, future departures only."""
    return [
        {"id": r["route_id"], "label": r["label"], "origin": r["origin"], "destination": r["destination"],
         "travel_date": r["travel_date"].isoformat(), "return_date": r["return_date"].isoformat(),
         "airline": r.get("airline") or "DL",
         "outbound_flights": r["outbound_flights"], "cabin_class": r["cabin_class"],
         "preference": r["preference"]}
        for r in conn.execute("SELECT DISTINCT ON (t.route_id) t.* FROM user_trips t "
                              "JOIN users u ON u.id = t.user_id "
                              "WHERE t.travel_date > %s AND t.archived_at IS NULL AND u.disabled_at IS NULL "
                              "ORDER BY t.route_id, t.id", (utcnow().date(),))
    ]

def record_result(conn, cfg, trip, outcome, legacy, source="scheduled"):
    """Save one check's outcome, update the route's status, and send alerts. Returns True if priced."""
    result, status, detail = outcome
    now = utcnow()
    prev = conn.execute(
        "SELECT price_usd FROM price_history WHERE trip_id=%s AND price_usd IS NOT NULL "
        "AND NOT synthetic ORDER BY scraped_at DESC LIMIT 1", (trip["id"],)).fetchone()
    conn.execute(
        "INSERT INTO route_status (route_id, status, detail, checked_at) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (route_id) DO UPDATE SET status=EXCLUDED.status, detail=EXCLUDED.detail, "
        "checked_at=EXCLUDED.checked_at", (trip["id"], status, detail, now))
    if not result:
        conn.execute("INSERT INTO price_history (trip_id, scraped_at, notes, source) VALUES (%s,%s,%s,%s)",
                     (trip["id"], now, f"check failed: {status}", source))
        return False
    conn.execute(
        "INSERT INTO price_history "
        "(trip_id, scraped_at, price_usd, airline, stops, depart_time, arrive_time, notes, flights, source) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (trip["id"], now, result["price"], result["airline"], result["stops"], result["depart_time"],
         result["arrive_time"], result["fare_class"], result["flights"], source))
    fire_alerts(cfg, conn, alert_recipients(cfg, conn, trip, legacy), trip,
                result["price"], prev["price_usd"] if prev else None)
    return True

async def run(cfg):
    conn = open_db(cfg)
    trips = []
    if GIT:
        sync_repo(cfg)
        trips = json.loads((REPO_DIR / "watchlist.json").read_text(encoding="utf-8")).get("trips", [])
    else:
        print("git not installed: skipping the legacy dashboard; website trips are unaffected")
    legacy_ids = {t["id"] for t in trips}
    # Old watchlist trips stop once departed, or once every website user tracking that route archived it
    archived_everywhere = {r["route_id"] for r in conn.execute(
        "SELECT route_id FROM user_trips GROUP BY route_id HAVING bool_and(archived_at IS NOT NULL)")}
    scrape_legacy = [t for t in trips if t["travel_date"] > utcnow().date().isoformat() and t["id"] not in archived_everywhere]
    user_routes = [t for t in user_route_trips(conn) if t["id"] not in legacy_ids]

    max_per_day = cfg.get("max_runs_per_day", 2)
    today = utcnow().date()
    due = []
    for trip in scrape_legacy + user_routes:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM price_history WHERE trip_id=%s AND scraped_at::date=%s "
            "AND price_usd IS NOT NULL AND NOT synthetic AND source='scheduled'",
            (trip["id"], today),
        ).fetchone()["n"]
        if n >= max_per_day:
            print(f"{trip.get('label', trip['id'])}: already {n} price(s) today, skipping.")
        else:
            due.append(trip)

    if due:
        with scrape_lock():
            results = await scrape_trips(cfg, due)
    else:
        results = {}

    any_scraped = False
    for trip in due:
        outcome = results.get(trip["id"], (None, "error", "not searched"))
        any_scraped |= record_result(conn, cfg, trip, outcome, trip["id"] in legacy_ids)

    if any_scraped and GIT:
        print("\n── Updating dashboard ──")
        push_to_github(cfg, generate_html(conn, trips))
    sent = send_digests(cfg, conn)
    if sent:
        print(f"Sent {sent} price summary email(s)")
    conn.close()

WORKER_PORT = 55433

async def worker(cfg):
    """Always-on loop: price newly added trips within a minute or two of being added."""
    # Separate tunnel port from the scheduled runs, which may start while the worker is connected
    if cfg.get("ssh_tunnel"):
        old = cfg["ssh_tunnel"].get("local_port", 55432)
        cfg = {**cfg, "ssh_tunnel": {**cfg["ssh_tunnel"], "local_port": WORKER_PORT},
               "database_url": cfg["database_url"].replace(f"port={old}", f"port={WORKER_PORT}")}
    print(f"[{utcnow():%Y-%m-%d %H:%M}Z] worker {VERSION} started", flush=True)
    tunnel = conn = None
    last_digest = last_research = utcnow()
    research_day = utcnow().date() - timedelta(days=1)
    while True:
        try:
            if tunnel is None or tunnel.poll() is not None:
                tunnel, conn = open_tunnel(cfg), None
            if conn is None or conn.closed:
                conn = open_db(cfg)
            wanted = {r["route_id"] for r in conn.execute(
                "SELECT DISTINCT route_id FROM check_requests WHERE finished_at IS NULL AND requested_at > %s",
                (utcnow() - timedelta(hours=6),))}
            if wanted:
                trips = [t for t in user_route_trips(conn) if t["id"] in wanted]
                print(f"[{utcnow():%Y-%m-%d %H:%M}Z] checking {len(trips)} new trip(s)", flush=True)
                conn.execute("UPDATE check_requests SET started_at=%s WHERE finished_at IS NULL "
                             "AND route_id = ANY(%s)", (utcnow(), list(wanted)))
                if trips:
                    with scrape_lock():
                        results = await scrape_trips(cfg, trips)
                    for t in trips:
                        record_result(conn, cfg, t, results.get(t["id"], (None, "error", "not searched")),
                                      legacy=False, source="on-demand")
                conn.execute("UPDATE check_requests SET finished_at=%s WHERE finished_at IS NULL "
                             "AND route_id = ANY(%s)", (utcnow(), list(wanted)))
                sys.stdout.flush()
            if utcnow() - last_digest > timedelta(minutes=10):
                last_digest = utcnow()
                if send_digests(cfg, conn):
                    sys.stdout.flush()
            # Research runs in the gaps: it never delays a user's instant check, which is handled above
            if not wanted and utcnow() - last_research > timedelta(minutes=2):
                last_research = utcnow()
                if utcnow().date() > research_day:
                    research_day = utcnow().date()
                    build_research_queue(conn)
                pending = conn.execute(
                    "SELECT COUNT(*) AS n FROM research_queue WHERE finished_at IS NULL AND due_at <= %s",
                    (utcnow(),)).fetchone()["n"]
                if pending:
                    with scrape_lock():
                        chrome, page, browser, p_ctx = None, None, None, None
                        try:
                            chrome, browser, page, p_ctx = await open_browser(cfg)
                            await run_research(cfg, conn, page)
                        finally:
                            await close_browser(chrome, browser, p_ctx)
                    sys.stdout.flush()
        except Exception:
            traceback.print_exc()
            sys.stdout.flush()
            for closer in (lambda: conn and conn.close(), lambda: tunnel and tunnel.terminate()):
                try:
                    closer()
                except Exception:
                    pass
            tunnel = conn = None
            await asyncio.sleep(30)
            continue
        await asyncio.sleep(15)

async def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"FlightFare tracker {VERSION} · {utcnow():%Y-%m-%d %H:%M}Z", flush=True)
    cfg = load_cfg()
    if "--dry-run" in sys.argv:
        wl = subprocess.run(["curl", "-sf", WATCHLIST_URL], capture_output=True, text=True, check=True)
        trips = json.loads(wl.stdout).get("trips", [])
        await scrape_trips(cfg, trips)
        print("\nDry run: nothing saved, emailed or published.")
        return
    if "--worker" in sys.argv:
        return await worker(cfg)
    tunnel = open_tunnel(cfg)
    try:
        if "--publish" in sys.argv:
            conn = open_db(cfg)
            sync_repo(cfg)
            trips = json.loads((REPO_DIR / "watchlist.json").read_text(encoding="utf-8")).get("trips", [])
            push_to_github(cfg, generate_html(conn, trips))
            conn.close()
        else:
            await run(cfg)
    finally:
        if tunnel:
            tunnel.terminate()

if __name__ == "__main__":
    asyncio.run(main())
