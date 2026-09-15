"""FlightFare watchdog: runs every 5 minutes on the AWS server and emails when the price checker needs attention.

Checks
- Worker offline: the Windows worker keeps a database connection open through its SSH tunnel, which reaches
  Postgres over TCP from 127.0.0.1 (the API uses the local socket instead). No such connection for 10+ minutes
  means the PC is off, asleep, offline, or the worker stopped.
- Missed scheduled check: no scheduled prices saved within 45 minutes after 8 AM or 6 PM Eastern.
One email when a problem starts and one when it clears; state lives in the watchdog_state table.
"""
import os
import smtplib
from datetime import datetime, time, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import formataddr
from zoneinfo import ZoneInfo

import psycopg

EASTERN = ZoneInfo("America/New_York")
TO = [a.strip() for a in os.environ.get("WATCHDOG_TO", "").split(",") if a.strip()]
OFFLINE_AFTER = timedelta(minutes=10)
SCHEDULE = [time(8, 0), time(18, 0)]
GRACE = timedelta(minutes=45)

def send(subject, body):
    msg = MIMEText(body + "\n\nFlightFare watchdog", "plain", "utf-8")
    msg["From"] = formataddr(("FlightFare watchdog", os.environ["MAIL_FROM"]))
    msg["To"] = ", ".join(TO)
    msg["Subject"] = subject
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"]), timeout=20) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.sendmail(os.environ["MAIL_FROM"], TO, msg.as_string())
    print(f"sent: {subject}")

def main():
    now = datetime.now(timezone.utc)
    with psycopg.connect(os.environ.get("DATABASE_URL", "dbname=flighttracker"), autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS watchdog_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        state = dict(conn.execute("SELECT key, value FROM watchdog_state").fetchall())
        def save(key, value):
            conn.execute("INSERT INTO watchdog_state VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                         (key, value))

        # ── worker connection ──
        online = conn.execute("SELECT COUNT(*) FROM pg_stat_activity WHERE client_addr = '127.0.0.1' "
                              "AND datname = current_database() AND pid <> pg_backend_pid()").fetchone()[0] > 0
        if online:
            save("worker_last_seen", now.isoformat())
            if state.get("worker_alert") == "open":
                down_since = state.get("worker_down_since", "")
                send("FlightFare: price checker is back online",
                     f"The Windows price checker reconnected at {now.astimezone(EASTERN):%-I:%M %p ET}"
                     + (f" (it dropped around {datetime.fromisoformat(down_since).astimezone(EASTERN):%-I:%M %p ET})." if down_since else "."))
                save("worker_alert", "closed")
        else:
            last_seen = datetime.fromisoformat(state.get("worker_last_seen", now.isoformat()))
            if "worker_last_seen" not in state:
                save("worker_last_seen", now.isoformat())
            if now - last_seen >= OFFLINE_AFTER and state.get("worker_alert") != "open":
                send("FlightFare: price checker is offline",
                     f"The Windows price checker hasn't been connected since "
                     f"{last_seen.astimezone(EASTERN):%-I:%M %p ET} ({int((now - last_seen).total_seconds() // 60)} minutes).\n\n"
                     "Until it reconnects, new trips won't get their first price and the 8 AM / 6 PM checks can't save prices.\n\n"
                     "Things to check on the PC: is it on, awake, signed in, and online? Signing in usually brings it back "
                     "within a few minutes. You'll get another email when it reconnects.")
                save("worker_alert", "open")
                save("worker_down_since", last_seen.isoformat())

        # ── scheduled checks ──
        local = now.astimezone(EASTERN)
        for at in SCHEDULE:
            slot = datetime.combine(local.date(), at, EASTERN)
            if not (slot + GRACE <= local < slot + GRACE + timedelta(hours=3)):
                continue
            key = f"missed_{slot:%Y-%m-%d_%H%M}"
            if key in state:
                continue
            saved = conn.execute("SELECT COUNT(*) FROM price_history WHERE source='scheduled' AND NOT synthetic "
                                 "AND price_usd IS NOT NULL AND scraped_at >= %s",
                                 (slot.astimezone(timezone.utc).replace(tzinfo=None),)).fetchone()[0]
            if saved:
                save(key, "ok")
            else:
                send(f"FlightFare: the {slot:%-I %p} price check didn't save any prices",
                     f"No prices were saved by the {slot:%-I:%M %p} scheduled check today ({slot:%A %b %-d}).\n\n"
                     "The Windows PC may be offline or signed out, or Delta may have blocked the search. "
                     "The next scheduled check will try again.")
                save(key, "alerted")

if __name__ == "__main__":
    if not TO:
        raise SystemExit("WATCHDOG_TO is not set")
    main()
