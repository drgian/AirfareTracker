"""Live-site check after promotion. Uses Resend's test inbox; cleans up after itself."""
import asyncio, hashlib, secrets, subprocess, sys
from playwright.async_api import async_playwright

URL, EMAIL = "https://flightfare.io/", "delivered@resend.dev"
KEY = "/Users/giandiloreto/Downloads/BigDaddyOnTheAir.pem"
fails = 0

def sql(q):
    return subprocess.run(["ssh", "-i", KEY, "ubuntu@3.20.7.57", f'psql -d flighttracker -qtAc "{q}"'],
                          capture_output=True, text=True, check=True).stdout.strip()

def token(purpose):
    raw = secrets.token_urlsafe(32)
    sql(f"INSERT INTO login_tokens (token_hash, user_id, expires_at, purpose) SELECT '{hashlib.sha256(raw.encode()).hexdigest()}', id, "
        f"(now() AT TIME ZONE 'utc') + interval '30 minutes', '{purpose}' FROM users WHERE email='{EMAIL}'")
    return raw

def check(c, m):
    global fails
    print(("PASS " if c else "FAIL ") + m, flush=True)
    fails += not c

async def pick(pg, host, typed, code):
    box = pg.locator(f"{host} input[role=combobox]")
    await box.fill(""); await box.type(typed, delay=40)
    await pg.locator(f"{host} li[role=option]").filter(has_text=code).first.click()

async def choose_dates(pg, a, b):
    await pg.click("#datebtn")
    day = lambda v: pg.locator(f"#rangecal .day[data-v='{v}']")
    for _ in range(14):
        if await day(a).is_visible() and await day(b).is_visible():
            break
        await pg.click("#rangecal [aria-label='Next month']")
    await day(a).click(); await day(b).click()

async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch(channel="chrome", headless=True)
        pg = await (await br.new_context(viewport={"width": 1180, "height": 900})).new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("dialog", lambda d: asyncio.ensure_future(d.accept()))

        await pg.goto(URL, wait_until="networkidle")
        check(await pg.is_visible(".delta-only") and await pg.is_visible("#feedback-open"), "live home page: Delta-only badge and feedback button")
        check(await pg.locator("#g-signup iframe").count() == 1, "Continue with Google button present")
        await pg.fill("#signup-email", EMAIL)
        await pg.click("#signup-form button[type=submit]")
        await pg.wait_for_selector("#v-sent:not([hidden])")
        await pg.goto(URL + "#login=" + token("signup"), wait_until="networkidle")
        await pg.wait_for_selector("#v-reset:not([hidden])")
        await pg.fill("#reset-password", "livepass123")
        await pick(pg, "#home-combo-signup", "grand rap", "GRR")
        await pg.click("#reset-form button[type=submit]")
        await pg.wait_for_selector("text=No trips yet")
        check(sql(f"SELECT home_airport FROM users WHERE email='{EMAIL}'") == "GRR", "sign-up with password and home airport")
        check("null" not in (await pg.inner_text("#v-app")).split(), "no stray 'null' text on an empty dashboard")

        await pg.click("#add-open")
        await pg.wait_for_function("document.getElementById('trip-dialog').open")
        check("GRR" in await pg.locator(".combo[data-for=origin] input[role=combobox]").input_value(), "From pre-filled with home airport")
        await pick(pg, ".combo[data-for=destination]", "minneap", "MSP")
        await choose_dates(pg, "2026-12-10", "2026-12-14")
        await pg.locator(".apick:has(.alogo.AA)").click()          # pick American; its cabins replace Delta's
        cabins = await pg.locator("#cabin-select option").all_inner_texts()
        check(cabins == ["Basic Economy", "Main Cabin", "Main Cabin Extra", "Business"], f"American cabins: {cabins}")
        await pg.locator(".apick:has(.alogo.DL)").click()          # back to Delta for the rest of the run
        await pg.locator("#trip-form [name=preference]").select_option("fastest")
        await pg.locator("#trip-form [name=label]").fill("Live check")
        await pg.click("#trip-submit")
        await pg.wait_for_selector("text=Checking Delta for the current fare", timeout=15000)
        await pg.wait_for_selector("#chart", timeout=300000)
        check("$" in await pg.inner_text(".stats"), "worker priced the new trip live and the chart appeared")

        await pg.click(".settings >> text=Archive")
        await pg.wait_for_selector(".tab >> text=Archived (1)")
        await pg.click(".arch-item >> text=View history")
        await pg.click("text=Restore tracking")
        await pg.wait_for_selector(".tab >> text=Live check")
        check(True, "archive, view history, and restore work live")
        # Delete from the My trips card: a capture-phase stopPropagation here once swallowed the click (fixed 1.1.2)
        await pg.click(".tab >> text=My trips")
        await pg.locator(".ov-card").filter(has_text="Live check").locator("button:has-text('Delete')").click()
        await pg.wait_for_selector("text=No trips yet")
        check(True, "delete works live from the My trips card")

        await pg.click("#logout"); await pg.click("#signin-top")
        await pg.fill("#signin-email", EMAIL); await pg.fill("#signin-password", "livepass123")
        await pg.click("#signin-form button[type=submit]")
        await pg.wait_for_selector("#v-app:not([hidden])")
        check(await pg.is_hidden("#admin-open"), "sign back in with password; regular user sees no Admin")
        await pg.click("#feedback-open"); await pg.click("#fb-send"); await pg.wait_for_timeout(300)
        check("write a message" in await pg.inner_text("#fb-err"), "feedback form validates (not sent)")
        await pg.click("#fb-cancel")

        sql(f"UPDATE users SET role='researcher' WHERE email='{EMAIL}'")
        await pg.reload(wait_until="networkidle"); await pg.wait_for_timeout(800)
        check(await pg.is_visible("#analysis-open") and await pg.is_hidden("#admin-open"), "researcher sees Analysis but not Admin")
        await pg.click("#analysis-open"); await pg.wait_for_selector("text=Price by day of the week")
        await pg.wait_for_timeout(600)
        check(await pg.evaluate("analysisCharts.length") == 3, "Analysis renders its 3 charts with ±1σ whiskers")
        check(not errs, f"no JavaScript errors {errs}")
        await br.close()

try:
    asyncio.run(main())
finally:
    routes = sql(f"SELECT string_agg(quote_literal(route_id), ',') FROM user_trips t JOIN users u ON u.id=t.user_id WHERE u.email='{EMAIL}'")
    print("cleanup user:", sql(f"DELETE FROM users WHERE email='{EMAIL}' RETURNING id"))
    for table, col in (("price_history", "trip_id"), ("route_status", "route_id"), ("check_requests", "route_id")):
        sql(f"DELETE FROM {table} WHERE {col} LIKE 'grr-msp-2026-12-10-2026-12-14-%' AND {col} NOT IN (SELECT route_id FROM user_trips)")
    print("cleanup routes: done")
print("failures:", fails)
sys.exit(1 if fails else 0)
