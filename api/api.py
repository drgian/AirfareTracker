"""FlightFare API: sign-in by emailed link, per-user trips, and their price history."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import formataddr

import jwt
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

VERSION       = "1.2.0"
DATABASE_URL  = os.environ.get("DATABASE_URL", "dbname=flighttracker")
APP_URL       = os.environ.get("APP_URL", "https://flightfare.io/")
SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
MAIL_FROM     = os.environ.get("MAIL_FROM", SMTP_USER)
LOGIN_TTL     = timedelta(minutes=30)
SESSION_TTL   = timedelta(days=60)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_JWKS   = jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs", cache_keys=True, lifespan=3600)
# Set on the dev copy only: restricts every sign-in and request to these accounts
PRIVATE_TO    = {e.strip().lower() for e in os.environ.get("PRIVATE_TO", "").split(",") if e.strip()}
MAX_TRIPS     = 25
MAX_FLIGHTS   = 4

# code -> [code, name, city, country, size rank]; scheduled-service airports from OurAirports
AIRPORTS = {a[0]: a for a in json.load(open(os.path.join(os.path.dirname(__file__), "airports.json"), encoding="utf-8"))}

app = FastAPI(title="FlightFare API", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://flightfare.io", "https://www.flightfare.io"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

def db():
    with psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True) as conn:
        yield conn

def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

# Per-IP limit on sign-in emails; one process, so memory is enough
_recent = defaultdict(deque)
def rate_limit(key: str, limit: int, window: int = 3600):
    q, now = _recent[key], time.monotonic()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many requests. Please try again later.")
    q.append(now)

EMAILS = {
    "login":  ("Your FlightFare sign-in link", "Click the link below to sign in to FlightFare:"),
    "signup": ("Confirm your FlightFare account", "Welcome to FlightFare! Click the link below to confirm your email and finish creating your account:"),
    "reset":  ("Reset your FlightFare password", "Click the link below to choose a new FlightFare password:"),
}

def send_login_email(to: str, link: str, purpose: str = "login"):
    subject, intro = EMAILS[purpose]
    body = (
        f"{intro}\n\n{link}\n\n"
        "The link works once and expires in 30 minutes.\n"
        "If you didn't ask for this, you can ignore this email.\n"
    )
    msg = MIMEText(body, "plain")
    msg["From"] = formataddr(("FlightFare", MAIL_FROM))
    msg["To"] = to
    msg["Subject"] = subject
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.sendmail(MAIL_FROM, [to], msg.as_string())

# ── Passwords ─────────────────────────────────────────────────────────────────
SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest_ = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest_).decode()

def check_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    _, salt, want = stored.split("$")
    got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), **SCRYPT)
    return hmac.compare_digest(got, base64.b64decode(want))

def valid_password(password: str):
    if len(password) < 8:
        raise HTTPException(400, "Use a password with at least 8 characters.")
    if len(password) > 200:
        raise HTTPException(400, "That password is too long.")

def new_session(conn, user_id: int) -> str:
    session = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s,%s,%s)",
                 (digest(session), user_id, utcnow() + SESSION_TTL))
    conn.execute("UPDATE users SET last_login_at=%s WHERE id=%s", (utcnow(), user_id))
    return session

def email_link(conn, user_id: int, email: str, purpose: str, pending_hash: str | None = None):
    recent = conn.execute(
        "SELECT COUNT(*) AS n FROM login_tokens WHERE user_id=%s AND created_at > %s",
        (user_id, utcnow() - timedelta(hours=1))).fetchone()["n"]
    if recent >= 5:
        raise HTTPException(429, "Too many emails. Please wait a bit and try again.")
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO login_tokens (token_hash, user_id, expires_at, purpose, pending_password_hash) "
                 "VALUES (%s,%s,%s,%s,%s)", (digest(token), user_id, utcnow() + LOGIN_TTL, purpose, pending_hash))
    # Token rides in the URL fragment so it never reaches server logs or Referer headers
    key = "reset" if purpose == "reset" else "login"
    try:
        send_login_email(email, f"{APP_URL}#{key}={token}", purpose)
    except Exception:
        raise HTTPException(502, "We couldn't send the email. Please try again.")

# ── Auth ──────────────────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")

class EmailIn(BaseModel):
    email: str = Field(max_length=254)
    mode: str = "signin"   # "signin" (sign-in link) or "reset" (password reset link); existing accounts only

class TokenIn(BaseModel):
    token: str = Field(max_length=200)

class SignupIn(BaseModel):
    email: str = Field(max_length=254)

class PasswordIn(BaseModel):
    password: str = Field(max_length=500)

class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=500)

class ResetIn(BaseModel):
    token: str = Field(max_length=200)
    password: str = Field(max_length=500)

PRIVATE_MSG = "This test site is private."

def allowed(email: str):
    if PRIVATE_TO and email.lower() not in PRIVATE_TO:
        raise HTTPException(403, PRIVATE_MSG)

def clean_email(raw: str) -> str:
    email = raw.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Please enter a valid email address.")
    allowed(email)
    return email

def client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for") or request.client.host

@app.post("/auth/signup")
def signup(body: SignupIn, request: Request, conn=Depends(db)):
    email = clean_email(body.email)
    rate_limit("ip:" + client_ip(request), 20)
    user = conn.execute("SELECT id, password_hash FROM users WHERE email=%s", (email,)).fetchone()
    if user and user["password_hash"]:
        raise HTTPException(409, "There's already an account with that email. Sign in instead.")
    if not user:
        user = conn.execute("INSERT INTO users (email) VALUES (%s) RETURNING id", (email,)).fetchone()
    # The password is chosen right after the owner of the address clicks the emailed link
    email_link(conn, user["id"], email, "signup")
    return {"ok": True}


@app.post("/auth/login")
def login(body: LoginIn, request: Request, conn=Depends(db)):
    email = clean_email(body.email)
    rate_limit("login-ip:" + client_ip(request), 30, 900)
    rate_limit("login-email:" + email, 10, 900)
    user = conn.execute("SELECT id, email, password_hash FROM users WHERE email=%s", (email,)).fetchone()
    if user and not user["password_hash"]:
        raise HTTPException(400, "This account doesn't have a password yet. Use \"Forgot password\" to set one.")
    if not user or not check_password(body.password, user["password_hash"]):
        raise HTTPException(401, "That email and password don't match.")
    return {"session": new_session(conn, user["id"]), "email": user["email"]}

class GoogleIn(BaseModel):
    credential: str = Field(max_length=5000)

@app.post("/auth/google")
def google_login(body: GoogleIn, request: Request, conn=Depends(db)):
    """Sign in with a Google ID token; links to an existing account with the same email."""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google sign-in isn't available yet.")
    rate_limit("google-ip:" + client_ip(request), 30, 900)
    try:
        key = GOOGLE_JWKS.get_signing_key_from_jwt(body.credential)
        claims = jwt.decode(body.credential, key.key, algorithms=["RS256"], audience=GOOGLE_CLIENT_ID,
                            issuer=["https://accounts.google.com", "accounts.google.com"])
    except Exception:
        raise HTTPException(401, "Google sign-in didn't work. Please try again.")
    if not claims.get("email") or not claims.get("email_verified"):
        raise HTTPException(401, "Your Google account's email address isn't verified.")
    email, sub = claims["email"].strip().lower(), claims["sub"]
    allowed(email)
    user = (conn.execute("SELECT id FROM users WHERE google_sub=%s", (sub,)).fetchone()
            or conn.execute("SELECT id FROM users WHERE email=%s", (email,)).fetchone())
    if user:
        # Google has verified this address, so it proves ownership of the matching account
        conn.execute("UPDATE users SET google_sub=COALESCE(google_sub, %s), "
                     "email_verified_at=COALESCE(email_verified_at, %s) WHERE id=%s", (sub, utcnow(), user["id"]))
    else:
        user = conn.execute("INSERT INTO users (email, google_sub, email_verified_at) VALUES (%s,%s,%s) RETURNING id",
                            (email, sub, utcnow())).fetchone()
    return {"session": new_session(conn, user["id"]), "email": email}

@app.post("/auth/reset")
def reset_password(body: ResetIn, conn=Depends(db)):
    valid_password(body.password)
    row = conn.execute(
        "UPDATE login_tokens SET used_at=%s WHERE token_hash=%s AND purpose='reset' AND used_at IS NULL "
        "AND expires_at > %s RETURNING user_id", (utcnow(), digest(body.token), utcnow())).fetchone()
    if not row:
        raise HTTPException(400, "This reset link has expired or was already used. Request a new one.")
    email = conn.execute("UPDATE users SET password_hash=%s, email_verified_at=COALESCE(email_verified_at, %s) "
                         "WHERE id=%s RETURNING email", (hash_password(body.password), utcnow(), row["user_id"])).fetchone()["email"]
    conn.execute("DELETE FROM sessions WHERE user_id=%s", (row["user_id"],))
    return {"session": new_session(conn, row["user_id"]), "email": email}

@app.post("/auth/request")
def request_login(body: EmailIn, request: Request, conn=Depends(db)):
    """Email a one-time link: mode 'signin' (sign-in link) or 'reset' (set a new password)."""
    email = clean_email(body.email)
    rate_limit("ip:" + client_ip(request), 20)
    found = conn.execute("SELECT id FROM users WHERE email=%s", (email,)).fetchone()
    if not found:
        raise HTTPException(404, "We don't have an account for that email. Check the spelling, or create an account.")
    email_link(conn, found["id"], email, "reset" if body.mode == "reset" else "login")
    return {"ok": True}

@app.post("/auth/verify")
def verify_login(body: TokenIn, conn=Depends(db)):
    row = conn.execute(
        "UPDATE login_tokens SET used_at=%s WHERE token_hash=%s AND purpose IN ('login','signup') "
        "AND used_at IS NULL AND expires_at > %s RETURNING user_id, pending_password_hash",
        (utcnow(), digest(body.token), utcnow())).fetchone()
    if not row:
        raise HTTPException(400, "This link has expired or was already used. Request a new one.")
    u = conn.execute(
        "UPDATE users SET email_verified_at=COALESCE(email_verified_at, %s), "
        "password_hash=COALESCE(%s, password_hash) WHERE id=%s RETURNING email, password_hash IS NULL AS needs_password",
        (utcnow(), row["pending_password_hash"], row["user_id"])).fetchone()
    return {"session": new_session(conn, row["user_id"]), "email": u["email"], "needs_password": u["needs_password"]}

def current_user(authorization: str = Header(default=""), conn=Depends(db)):
    scheme, _, token = authorization.partition(" ")
    if not hmac.compare_digest(scheme.lower(), "bearer") or not token:
        raise HTTPException(401, "Please sign in.")
    user = conn.execute(
        "SELECT u.id, u.email, u.role, u.home_airport, u.password_hash IS NOT NULL AS has_password, "
        "u.google_sub IS NOT NULL AS has_google "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash=%s AND s.expires_at > %s", (digest(token), utcnow())).fetchone()
    if not user:
        raise HTTPException(401, "Your session has expired. Please sign in again.")
    allowed(user["email"])
    user["token_hash"] = digest(token)
    user["is_admin"] = user["role"] == "admin"
    return user

@app.post("/auth/password")
def set_password(body: PasswordIn, user=Depends(current_user), conn=Depends(db)):
    valid_password(body.password)
    conn.execute("UPDATE users SET password_hash=%s WHERE id=%s", (hash_password(body.password), user["id"]))
    return {"ok": True}

@app.post("/auth/logout")
def logout(user=Depends(current_user), conn=Depends(db)):
    conn.execute("DELETE FROM sessions WHERE token_hash=%s", (user["token_hash"],))
    return {"ok": True}

@app.get("/me")
def me(user=Depends(current_user)):
    return {"email": user["email"], "role": user["role"], "is_admin": user["is_admin"], "has_password": user["has_password"],
            "has_google": user["has_google"], "home_airport": user["home_airport"]}

class MeIn(BaseModel):
    home_airport: str = Field(default="", max_length=3)

@app.patch("/me")
def update_me(body: MeIn, user=Depends(current_user), conn=Depends(db)):
    code = body.home_airport.strip().upper()
    if code and code not in AIRPORTS:
        raise HTTPException(400, "Pick your home airport from the list.")
    conn.execute("UPDATE users SET home_airport=%s WHERE id=%s", (code or None, user["id"]))
    return {"home_airport": code or None}

# ── Feedback ──────────────────────────────────────────────────────────────────
class FeedbackIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    email: str = Field(default="", max_length=254)
    page: str = Field(default="", max_length=40)

def optional_user(authorization: str = Header(default=""), conn=Depends(db)):
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return conn.execute("SELECT u.id, u.email FROM sessions s JOIN users u ON u.id = s.user_id "
                        "WHERE s.token_hash=%s AND s.expires_at > %s", (digest(token), utcnow())).fetchone()

@app.post("/feedback")
def send_feedback(body: FeedbackIn, request: Request, user=Depends(optional_user), conn=Depends(db)):
    if PRIVATE_TO and not user:
        raise HTTPException(403, PRIVATE_MSG)
    message = body.message.strip()
    if not message:
        raise HTTPException(400, "Please write a message.")
    rate_limit("feedback:" + client_ip(request), 10)
    email = user["email"] if user else body.email.strip().lower()
    if email and not EMAIL_RE.match(email):
        raise HTTPException(400, "That email address doesn't look right. Leave it blank if you prefer.")
    conn.execute("INSERT INTO feedback (user_id, email, message, page) VALUES (%s,%s,%s,%s)",
                 (user["id"] if user else None, email or None, message, body.page[:40]))
    admins = [r["email"] for r in conn.execute("SELECT email FROM users WHERE role = 'admin'")]
    who = email or "an anonymous visitor"
    for admin in admins:
        try:
            m = MIMEText(f"New feedback from {who}:\n\n{message}\n\nSee all feedback in the Admin tab: {APP_URL}", "plain")
            m["From"] = formataddr(("FlightFare", MAIL_FROM))
            m["To"] = admin
            m["Subject"] = f"FlightFare feedback from {who}"
            if email:
                m["Reply-To"] = email
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(SMTP_USER, SMTP_PASSWORD)
                smtp.sendmail(MAIL_FROM, [admin], m.as_string())
        except Exception:
            pass   # the message is saved either way; the admin tab lists it
    return {"ok": True}

# ── Page views (anonymous daily counts; the static site has no analytics of its own) ──
PAGES = {"home", "app"}

class HitIn(BaseModel):
    page: str = Field(max_length=20)

@app.post("/hit")
def hit(body: HitIn, request: Request, conn=Depends(db)):
    if body.page in PAGES:
        rate_limit("hit:" + client_ip(request), 120)
        conn.execute("INSERT INTO page_views (day, page, views) VALUES (%s,%s,1) "
                     "ON CONFLICT (day, page) DO UPDATE SET views = page_views.views + 1",
                     (utcnow().date(), body.page))
    return {"ok": True}

# ── Admin ─────────────────────────────────────────────────────────────────────
ROLES = ("basic", "researcher", "admin")   # each role includes everything the one before it can see

def admin_user(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admins only.")
    return user

def research_user(user=Depends(current_user)):
    if user["role"] not in ("researcher", "admin"):
        raise HTTPException(403, "Researchers and admins only.")
    return user

def one(conn, sql, *params):
    return conn.execute(sql, params).fetchone()

@app.get("/admin/stats")
def admin_stats(user=Depends(admin_user), conn=Depends(db)):
    now = utcnow()
    day, week = now - timedelta(days=1), now - timedelta(days=7)
    checks = lambda since, source: one(conn,
        "SELECT COUNT(*) FILTER (WHERE price_usd IS NOT NULL) AS ok, COUNT(*) FILTER (WHERE price_usd IS NULL) AS failed "
        "FROM price_history WHERE NOT synthetic AND scraped_at > %s AND source = %s", since, source)
    views = {r["day"].isoformat(): r["views"] for r in conn.execute(
        "SELECT day, SUM(views) AS views FROM page_views WHERE day > %s GROUP BY day ORDER BY day", (now.date() - timedelta(days=14),))}
    return {
        "users": one(conn, "SELECT COUNT(*) AS total, COUNT(email_verified_at) AS confirmed, "
                           "COUNT(*) FILTER (WHERE last_login_at > %s) AS active_7d, "
                           "COUNT(*) FILTER (WHERE created_at > %s) AS new_7d, "
                           "COUNT(password_hash) AS with_password FROM users", week, week),
        "trips": one(conn, "SELECT COUNT(*) AS trips, COUNT(DISTINCT route_id) AS routes, "
                           "COUNT(DISTINCT user_id) AS users_with_trips FROM user_trips WHERE travel_date >= %s", now.date()),
        "checks_24h": {"scheduled": checks(day, "scheduled"), "on_demand": checks(day, "on-demand")},
        "checks_7d":  {"scheduled": checks(week, "scheduled"), "on_demand": checks(week, "on-demand")},
        "problems": [dict(r) for r in conn.execute(
            "SELECT s.status, COUNT(*) AS routes FROM route_status s WHERE s.status <> 'ok' "
            "AND s.route_id IN (SELECT route_id FROM user_trips WHERE travel_date >= %s) GROUP BY s.status", (now.date(),))],
        "queue": one(conn, "SELECT COUNT(*) AS waiting FROM check_requests WHERE finished_at IS NULL AND requested_at > %s",
                     now - timedelta(hours=6))["waiting"],
        "last_scheduled": (one(conn, "SELECT MAX(scraped_at) AS t FROM price_history WHERE source='scheduled' AND NOT synthetic")["t"] or now).isoformat(),
        "last_on_demand": (lambda t: t.isoformat() if t else None)(one(conn, "SELECT MAX(finished_at) AS t FROM check_requests")["t"]),
        "views_14d": views,
        "views_today": views.get(now.date().isoformat(), 0),
    }

@app.get("/admin/activity")
def admin_activity(user=Depends(admin_user), conn=Depends(db)):
    checks = conn.execute(
        "SELECT p.scraped_at, p.trip_id, p.price_usd, p.notes, p.flights, p.source, "
        "(SELECT label FROM user_trips t WHERE t.route_id = p.trip_id ORDER BY id LIMIT 1) AS label "
        "FROM price_history p WHERE NOT p.synthetic ORDER BY p.scraped_at DESC LIMIT 25").fetchall()
    signups = conn.execute("SELECT email, created_at, email_verified_at IS NOT NULL AS confirmed "
                           "FROM users ORDER BY created_at DESC LIMIT 10").fetchall()
    for r in checks + signups:
        for k in ("scraped_at", "created_at"):
            if r.get(k):
                r[k] = r[k].isoformat()
    return {"checks": checks, "signups": signups}

@app.get("/admin/users")
def admin_users(user=Depends(admin_user), conn=Depends(db)):
    rows = conn.execute(
        "SELECT u.id, u.email, u.role, u.role = 'admin' AS is_admin, u.created_at, u.last_login_at, u.email_verified_at IS NOT NULL AS confirmed, "
        "u.password_hash IS NOT NULL AS has_password, COUNT(t.id) AS trips "
        "FROM users u LEFT JOIN user_trips t ON t.user_id = u.id GROUP BY u.id ORDER BY u.created_at").fetchall()
    for r in rows:
        for k in ("created_at", "last_login_at"):
            r[k] = r[k].isoformat() if r[k] else None
    return rows

@app.get("/admin/users/{user_id}")
def admin_user_detail(user_id: int, user=Depends(admin_user), conn=Depends(db)):
    u = conn.execute(
        "SELECT id, email, role, role = 'admin' AS is_admin, created_at, last_login_at, email_verified_at, password_hash IS NOT NULL AS has_password "
        "FROM users WHERE id=%s", (user_id,)).fetchone()
    if not u:
        raise HTTPException(404, "User not found.")
    trips = conn.execute(
        "SELECT t.id, t.label, t.origin, t.destination, t.travel_date, t.return_date, t.airline, t.outbound_flights, t.cabin_class, "
        "t.preference, t.alert_below, t.created_at, s.status, s.checked_at, "
        "(SELECT COUNT(*) FROM price_history p WHERE p.trip_id = t.route_id AND p.price_usd IS NOT NULL AND NOT p.synthetic) AS checks, "
        "(SELECT price_usd FROM price_history p WHERE p.trip_id = t.route_id AND p.price_usd IS NOT NULL ORDER BY scraped_at DESC LIMIT 1) AS latest, "
        "(SELECT MIN(price_usd) FROM price_history p WHERE p.trip_id = t.route_id AND p.price_usd IS NOT NULL) AS lowest, "
        "(SELECT MAX(price_usd) FROM price_history p WHERE p.trip_id = t.route_id AND p.price_usd IS NOT NULL) AS highest "
        "FROM user_trips t LEFT JOIN route_status s ON s.route_id = t.route_id WHERE t.user_id=%s ORDER BY t.travel_date",
        (user_id,)).fetchall()
    sessions = conn.execute("SELECT COUNT(*) AS active FROM sessions WHERE user_id=%s AND expires_at > %s",
                            (user_id, utcnow())).fetchone()["active"]
    def iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v
    return {**{k: iso(v) for k, v in u.items()}, "active_sessions": sessions,
            "trips": [{k: iso(v) for k, v in t.items()} for t in trips]}

@app.get("/admin/feedback")
def admin_feedback(user=Depends(admin_user), conn=Depends(db)):
    rows = conn.execute("SELECT id, created_at, email, message, page FROM feedback ORDER BY created_at DESC LIMIT 100").fetchall()
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
    return rows

class RoleIn(BaseModel):
    role: str

@app.post("/admin/users/{user_id}/role")
def set_role(user_id: int, body: RoleIn, user=Depends(admin_user), conn=Depends(db)):
    if body.role not in ROLES:
        raise HTTPException(400, "Role must be basic, researcher, or admin.")
    if user_id == user["id"] and body.role != "admin":
        raise HTTPException(400, "You can't remove your own admin access.")
    row = conn.execute("UPDATE users SET role=%s, is_admin=%s WHERE id=%s RETURNING id, email, role",
                       (body.role, body.role == "admin", user_id)).fetchone()
    if not row:
        raise HTTPException(404, "User not found.")
    return row

# ── Analysis (researchers and admins) ─────────────────────────────────────────
# Real checks only: gap fill-ins are marked synthetic and excluded. Each price is compared with its own
# route's average so trips with very different fares can be pooled ("rel" = +0.05 means 5% above average).
ANALYSIS_BASE = """
WITH trips AS (
    SELECT DISTINCT ON (route_id) route_id, origin, destination, travel_date FROM user_trips ORDER BY route_id, id
), p AS (
    SELECT ph.trip_id AS route_id, ph.price_usd AS price, ph.scraped_at, t.travel_date, t.origin, t.destination,
           (ph.scraped_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/New_York' AS local_ts
    FROM price_history ph JOIN trips t ON t.route_id = ph.trip_id
    WHERE ph.price_usd IS NOT NULL AND NOT ph.synthetic
      AND (%(scope)s = 'all' OR ph.trip_id IN (SELECT route_id FROM user_trips WHERE user_id = %(uid)s))
), r AS (
    SELECT route_id, avg(price) AS mean FROM p GROUP BY route_id HAVING count(*) >= 3
), rel AS (
    SELECT p.*, p.price / r.mean - 1 AS rel FROM p JOIN r USING (route_id)
)
"""

@app.get("/analysis")
def analysis(scope: str = "all", user=Depends(research_user), conn=Depends(db)):
    if scope not in ("all", "mine"):
        raise HTTPException(400, "Scope must be all or mine.")
    args = {"scope": scope, "uid": user["id"]}
    q = lambda sql: conn.execute(ANALYSIS_BASE + sql, args).fetchall()
    summary = q("SELECT COUNT(*) AS checks, COUNT(DISTINCT route_id) AS routes, MIN(scraped_at) AS since, "
                "(SELECT COUNT(*) FROM rel) AS usable FROM p")[0]
    by_dow = q("SELECT EXTRACT(ISODOW FROM local_ts)::int AS dow, AVG(rel) AS rel, STDDEV_SAMP(rel) AS sd, COUNT(*) AS n, "
               "COUNT(DISTINCT route_id) AS routes FROM rel GROUP BY 1 ORDER BY 1")
    by_time = q("SELECT CASE WHEN EXTRACT(HOUR FROM local_ts) BETWEEN 5 AND 11 THEN 'Morning' "
                "WHEN EXTRACT(HOUR FROM local_ts) BETWEEN 12 AND 16 THEN 'Afternoon' "
                "WHEN EXTRACT(HOUR FROM local_ts) BETWEEN 17 AND 22 THEN 'Evening' ELSE 'Night' END AS slot, "
                "AVG(rel) AS rel, STDDEV_SAMP(rel) AS sd, COUNT(*) AS n FROM rel GROUP BY 1")
    by_days_out = q("SELECT b.label, b.lo, AVG(rel.rel) AS rel, STDDEV_SAMP(rel.rel) AS sd, COUNT(*) AS n FROM rel JOIN (VALUES "
                    "('0–7 days',0,7),('1–2 weeks',8,14),('2–4 weeks',15,30),('1–2 months',31,60),"
                    "('2–3 months',61,90),('3–6 months',91,180),('6+ months',181,100000)) AS b(label, lo, hi) "
                    "ON (rel.travel_date - rel.local_ts::date) BETWEEN b.lo AND b.hi GROUP BY b.label, b.lo ORDER BY b.lo")
    changes = q(", pairs AS (SELECT price, LAG(price) OVER (PARTITION BY route_id ORDER BY scraped_at) AS prev FROM p) "
                "SELECT COUNT(prev) AS pairs, COUNT(*) FILTER (WHERE price <> prev) AS changed, "
                "COUNT(*) FILTER (WHERE price > prev) AS ups, COUNT(*) FILTER (WHERE price < prev) AS downs, "
                "AVG(ABS(price - prev)) FILTER (WHERE price <> prev) AS avg_move, "
                "AVG(ABS(price - prev) / prev) FILTER (WHERE price <> prev) AS avg_move_pct, "
                "MIN(price - prev) AS biggest_drop, MAX(price - prev) AS biggest_rise FROM pairs")[0]
    routes = q("SELECT origin, destination, travel_date, COUNT(*) AS checks, MIN(price) AS low, MAX(price) AS high, "
               "(ARRAY_AGG(price ORDER BY scraped_at DESC))[1] AS latest, COALESCE(STDDEV_SAMP(price) / AVG(price), 0) AS volatility "
               "FROM p GROUP BY route_id, origin, destination, travel_date ORDER BY checks DESC, origin")
    summary["since"] = summary["since"].isoformat() if summary["since"] else None
    for r in routes:
        r["travel_date"] = r["travel_date"].isoformat()
    return {"scope": scope, "summary": summary, "by_dow": by_dow, "by_time": by_time,
            "by_days_out": by_days_out, "changes": changes, "routes": routes}

# ── Trips ─────────────────────────────────────────────────────────────────────
FLIGHT_RE  = re.compile(r"([A-Z0-9]{2})\s*(\d{1,4})")
# Each airline sells its own cabins, so the choice of airline decides which cabins are on offer
AIRLINES   = {"DL": "Delta Air Lines", "AA": "American Airlines", "UA": "United Airlines"}
CABINS     = {
    "DL": {"main_basic": "Main Basic", "main_classic": "Main Classic", "main_extra": "Main Extra",
           "comfort": "Comfort+", "first": "First"},
    "AA": {"basic": "Basic Economy", "main": "Main Cabin", "main_extra": "Main Cabin Extra",
           "business": "Business"},
    "UA": {"basic": "Basic Economy", "main": "United Economy", "economy_plus": "Economy Plus",
           "business": "Business"},
}
DEFAULT_CABIN = {"DL": "main_classic", "AA": "main", "UA": "main"}

class TripIn(BaseModel):
    origin: str
    destination: str
    travel_date: date
    return_date: date
    airline: str = "DL"
    outbound_flights: str = Field(default="", max_length=60)
    cabin_class: str = "main_classic"
    preference: str = "cheapest"
    label: str = Field(default="", max_length=40)
    alert_below: int | None = Field(default=None, ge=1, le=100000)

class TripPatch(BaseModel):
    label: str | None = Field(default=None, max_length=40)
    alert_below: int | None = Field(default=None, ge=0, le=100000)

def normalize_flights(raw: str) -> str:
    """'dl 2547, af66' -> 'DL2547 / AF66'; raises on anything that isn't a flight designator."""
    text = raw.strip().upper()
    if not text:
        return ""
    flights = FLIGHT_RE.findall(text)
    leftover = FLIGHT_RE.sub("", text).translate(str.maketrans("", "", " /,-+&"))
    bad = leftover or not flights or len(flights) > MAX_FLIGHTS or any(
        not re.search("[A-Z]", carrier) or int(num) == 0 for carrier, num in flights)
    if bad:
        raise HTTPException(400, "Enter flight numbers like DL2547 / DL66 (up to 4), or leave it blank.")
    return " / ".join(f"{c}{int(n)}" for c, n in flights)

def trip_out(conn, t):
    history = conn.execute(
        "SELECT scraped_at, price_usd AS price, stops, depart_time, arrive_time, notes, flights "
        "FROM price_history WHERE trip_id=%s AND price_usd IS NOT NULL ORDER BY scraped_at",
        (t["route_id"],)).fetchall()
    status = conn.execute("SELECT status, detail, checked_at FROM route_status WHERE route_id=%s",
                          (t["route_id"],)).fetchone()
    pending = conn.execute(
        "SELECT requested_at, started_at FROM check_requests WHERE route_id=%s AND finished_at IS NULL "
        "AND requested_at > %s ORDER BY id DESC LIMIT 1", (t["route_id"], utcnow() - timedelta(hours=6))).fetchone()
    for h in history:
        h["scraped_at"] = h["scraped_at"].isoformat()
    t = dict(t)
    for k in ("travel_date", "return_date"):
        t[k] = t[k].isoformat() if t[k] else None
    t.pop("created_at", None)
    t.pop("user_id", None)
    # Trips archive themselves once the departure date has passed
    t["archived"] = bool(t.get("archived_at")) or date.fromisoformat(t["travel_date"]) <= date.today()
    t["auto_archived"] = not t.get("archived_at") and t["archived"]
    t["archived_at"] = t["archived_at"].isoformat() if t.get("archived_at") else None
    t["history"] = history
    t["status"] = status["status"] if status else None
    t["status_detail"] = status["detail"] if status else None
    t["check_pending"] = bool(pending)
    t["check_started"] = bool(pending and pending["started_at"])
    return t

@app.get("/trips")
def list_trips(user=Depends(current_user), conn=Depends(db)):
    trips = conn.execute("SELECT * FROM user_trips WHERE user_id=%s ORDER BY travel_date, id",
                         (user["id"],)).fetchall()
    return [trip_out(conn, t) for t in trips]

@app.get("/trips/{trip_id}")
def get_trip(trip_id: int, user=Depends(current_user), conn=Depends(db)):
    t = conn.execute("SELECT * FROM user_trips WHERE id=%s AND user_id=%s", (trip_id, user["id"])).fetchone()
    if not t:
        raise HTTPException(404, "Trip not found.")
    return trip_out(conn, t)

@app.post("/trips", status_code=201)
def add_trip(body: TripIn, user=Depends(current_user), conn=Depends(db)):
    origin, dest = body.origin.strip().upper(), body.destination.strip().upper()
    for code in (origin, dest):
        if code not in AIRPORTS:
            raise HTTPException(400, f"We don't recognize \"{code[:10]}\" as an airport. Pick one from the list.")
    if origin == dest:
        raise HTTPException(400, "The origin and destination must be different airports.")
    if body.travel_date <= date.today():
        raise HTTPException(400, "The departure date must be in the future.")
    if body.return_date <= body.travel_date:
        raise HTTPException(400, "The return date must be after the departure date.")
    airline = body.airline.strip().upper() or "DL"
    if airline not in AIRLINES:
        raise HTTPException(400, "Choose Delta, American or United.")
    if body.travel_date > date.today() + timedelta(days=330):
        raise HTTPException(400, "Airlines only sell flights about 11 months ahead.")
    flights = normalize_flights(body.outbound_flights)
    if body.cabin_class not in CABINS[airline]:
        raise HTTPException(400, f"{AIRLINES[airline]} doesn't sell that cabin.")
    if body.preference not in ("cheapest", "fastest"):
        raise HTTPException(400, "Choose cheapest or fastest.")
    preference = "cheapest" if flights else body.preference   # pinned flights make the choice moot
    if conn.execute("SELECT COUNT(*) AS n FROM user_trips WHERE user_id=%s",
                    (user["id"],)).fetchone()["n"] >= MAX_TRIPS:
        raise HTTPException(400, f"You can track up to {MAX_TRIPS} trips.")

    # Identical searches share one route, so everyone tracking it shares its history
    same = conn.execute(
        "SELECT route_id FROM user_trips WHERE origin=%s AND destination=%s AND travel_date=%s "
        "AND return_date=%s AND airline=%s AND outbound_flights=%s AND cabin_class=%s AND preference=%s LIMIT 1",
        (origin, dest, body.travel_date, body.return_date, airline, flights, body.cabin_class,
         preference)).fetchone()
    if same:
        route_id = same["route_id"]
    else:
        key = f"{flights}|{body.cabin_class}" + ("|fastest" if preference == "fastest" else "")
        suffix = hashlib.sha1(key.encode()).hexdigest()[:6]
        # Delta routes keep their original ids so their price history carries over
        prefix = "" if airline == "DL" else airline.lower() + "-"
        route_id = f"{prefix}{origin}-{dest}-{body.travel_date}-{body.return_date}-{suffix}".lower()
    label = body.label.strip() or f"{dest} {body.travel_date:%b} {body.travel_date.day}"
    try:
        t = conn.execute(
            "INSERT INTO user_trips (user_id, route_id, label, origin, destination, travel_date, "
            "return_date, airline, outbound_flights, cabin_class, preference, alert_below) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (user["id"], route_id, label, origin, dest, body.travel_date, body.return_date,
             airline, flights, body.cabin_class, preference, body.alert_below)).fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "You're already tracking this trip.")
    # The scraper worker on the home PC picks this up and prices the trip within a minute or two
    priced = conn.execute("SELECT 1 FROM price_history WHERE trip_id=%s AND price_usd IS NOT NULL LIMIT 1",
                          (route_id,)).fetchone()
    queued = conn.execute("SELECT 1 FROM check_requests WHERE route_id=%s AND finished_at IS NULL",
                          (route_id,)).fetchone()
    if not priced and not queued:
        conn.execute("INSERT INTO check_requests (route_id) VALUES (%s)", (route_id,))
    return trip_out(conn, t)

@app.patch("/trips/{trip_id}")
def update_trip(trip_id: int, body: TripPatch, user=Depends(current_user), conn=Depends(db)):
    fields = body.model_dump(exclude_unset=True)
    if "label" in fields:
        fields["label"] = (fields["label"] or "").strip()
        if not fields["label"]:
            raise HTTPException(400, "The name can't be empty.")
    if fields.get("alert_below") == 0:
        fields["alert_below"] = None
    if not fields:
        raise HTTPException(400, "Nothing to update.")
    sets = ", ".join(f"{k}=%s" for k in fields)
    t = conn.execute(f"UPDATE user_trips SET {sets} WHERE id=%s AND user_id=%s RETURNING *",
                     (*fields.values(), trip_id, user["id"])).fetchone()
    if not t:
        raise HTTPException(404, "Trip not found.")
    return trip_out(conn, t)

@app.post("/trips/{trip_id}/archive")
def archive_trip(trip_id: int, user=Depends(current_user), conn=Depends(db)):
    t = conn.execute("UPDATE user_trips SET archived_at=COALESCE(archived_at, %s) WHERE id=%s AND user_id=%s RETURNING *",
                     (utcnow(), trip_id, user["id"])).fetchone()
    if not t:
        raise HTTPException(404, "Trip not found.")
    return trip_out(conn, t)

@app.post("/trips/{trip_id}/restore")
def restore_trip(trip_id: int, user=Depends(current_user), conn=Depends(db)):
    t = conn.execute("SELECT * FROM user_trips WHERE id=%s AND user_id=%s", (trip_id, user["id"])).fetchone()
    if not t:
        raise HTTPException(404, "Trip not found.")
    if t["travel_date"] <= date.today():
        raise HTTPException(400, "This trip's departure date has passed, so it can't be tracked again.")
    t = conn.execute("UPDATE user_trips SET archived_at=NULL WHERE id=%s RETURNING *", (trip_id,)).fetchone()
    return trip_out(conn, t)

@app.delete("/trips/{trip_id}")
def delete_trip(trip_id: int, user=Depends(current_user), conn=Depends(db)):
    if not conn.execute("DELETE FROM user_trips WHERE id=%s AND user_id=%s RETURNING id",
                        (trip_id, user["id"])).fetchone():
        raise HTTPException(404, "Trip not found.")
    return {"ok": True}

@app.get("/airlines")
def airlines():
    """What the trip form offers: each airline and the cabins it sells."""
    return {"airlines": [{"code": c, "name": n, "default_cabin": DEFAULT_CABIN[c],
                          "cabins": [{"value": k, "label": v} for k, v in CABINS[c].items()]}
                         for c, n in AIRLINES.items()]}

@app.get("/health")
def health(conn=Depends(db)):
    conn.execute("SELECT 1")
    return {"ok": True, "version": VERSION, "private": bool(PRIVATE_TO)}
