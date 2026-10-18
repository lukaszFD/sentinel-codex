"""
Sentinel Codex — News Radar config page (module "news").

Small server-rendered web UI for the data that drives the n8n "News Radar"
workflow (database knowledge_hub, schema news): settings, sources, keywords,
digest times and the versioned Gemini prompt, plus read-only views of the
collected items, source health, sent digests and the change log.

Connects as the module's least-privilege editor role
(modules/news/db/10_news.sql) — never as postgres or the n8n role.

Every write transaction sets `news.actor` to the logged-in
user, so the audit trigger records who changed what.

Configuration (environment):
    NEWS_UI_SECRET_KEY        Flask session key, >= 32 chars (required)
    NEWS_UI_USERS             JSON object {"user": "password-or-werkzeug-hash"}
    NEWS_UI_DB_HOST / NEWS_UI_DB_PORT / NEWS_UI_DB_NAME
    NEWS_UI_DB_USER / NEWS_UI_DB_PASSWORD
    NEWS_UI_COOKIE_SECURE     "true" (default) / "false" for local http testing
    NEWS_UI_SESSION_HOURS     session lifetime, default 8
"""

import difflib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, time as dtime
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

log = logging.getLogger("codex-news-ui")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TZ = ZoneInfo("Europe/Warsaw")  # same zone as news.tz()


# ============================================================
# Configuration
# ============================================================

def _env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _load_users():
    raw = _env("NEWS_UI_USERS", required=True)
    try:
        users = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("NEWS_UI_USERS must be a JSON object {\"user\": \"password\"}") from exc
    if not isinstance(users, dict) or not users:
        raise RuntimeError("NEWS_UI_USERS must contain at least one user")
    for name, secret in users.items():
        if not isinstance(secret, str) or len(secret) < 12:
            raise RuntimeError(f"NEWS_UI_USERS: password for '{name}' must be a string of >= 12 characters")
    return users


SECRET_KEY = _env("NEWS_UI_SECRET_KEY", required=True)
if len(SECRET_KEY) < 32:
    raise RuntimeError("NEWS_UI_SECRET_KEY must be at least 32 characters")

USERS = _load_users()

DB_PARAMS = {
    "host": _env("NEWS_UI_DB_HOST", "postgres_db"),
    "port": int(_env("NEWS_UI_DB_PORT", "5432")),
    "dbname": _env("NEWS_UI_DB_NAME", "knowledge_hub"),
    "user": _env("NEWS_UI_DB_USER", required=True),
    "password": _env("NEWS_UI_DB_PASSWORD", required=True),
    "connect_timeout": 5,
    "application_name": "codex-news-ui",
    "options": "-c search_path=news",
}

app = Flask(__name__)
# One proxy hop: nginx-proxy. Only trusted for X-Forwarded-For/-Proto.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_NAME="news_cfg_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=_env("NEWS_UI_COOKIE_SECURE", "true").lower() != "false",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=int(_env("NEWS_UI_SESSION_HOURS", "8"))),
    MAX_CONTENT_LENGTH=512 * 1024,
)


# ============================================================
# Domain rules (kept in one place, used by validation + templates)
# ============================================================

REQUIRED_PLACEHOLDERS = ["[[ITEMS_JSON]]"]
PLACEHOLDER_RE = re.compile(r"\[\[([A-Za-z0-9_]+)\]\]")
VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,20}$")
TERM_RE = re.compile(r"^[^\s*][^*]{0,78}\*?$")

SOURCE_TYPES = {
    "google_news": "Google News search",
    "hn_algolia": "Hacker News search",
    "rss": "RSS / Atom feed",
    "cisa_kev": "CISA KEV (JSON)",
}
QUERY_TYPES = {"google_news", "hn_algolia"}
TOPICS = {"cyber": "Cyber", "ai": "AI", "both": "Both"}
LANGS = {"en": "English", "pl": "Polish"}

ITEM_STATUS = {
    "alert": ("waiting for digest", "warn"),
    "sent": ("sent", "ok"),
    "scored": ("below threshold", ""),
    "pending_ai": ("waiting for AI", "info"),
    "filtered": ("filtered (keywords)", ""),
    "duplicate": ("duplicate", ""),
    "stale": ("too old", ""),
    "ai_error": ("AI failed", "bad"),
}

# Business description of every settings key, in workflow order.
SETTING_HELP = {
    "enabled": {
        "min": 0, "max": 1, "switch": True, "stage": 1,
        "label": "News Radar enabled",
        "what": "Master switch. When off, no source is fetched. Items already collected are still "
                "scored and sent at the next digest time.",
        "raise": "On — sources are checked on their own schedule.",
        "lower": "Off — nothing new is collected.",
    },
    "max_item_age_hours": {
        "min": 1, "max": 720, "stage": 1,
        "label": "Ignore news older than (hours)",
        "what": "News published longer ago than this when it is first seen is stored but never sent. "
                "Protects you from a flood of old articles when a new source is added.",
        "raise": "Older articles can still make it into a digest.",
        "lower": "Only fresh news. Too low and articles from slow feeds are dropped.",
    },
    "duplicate_window_hours": {
        "min": 1, "max": 720, "stage": 1,
        "label": "Same headline counts as duplicate within (hours)",
        "what": "The same story usually comes from several sources. A headline already seen within "
                "this window is not sent again.",
        "raise": "Fewer repeats of a story that keeps coming back.",
        "lower": "A story that returns after a while gets through again.",
    },
    "keyword_min_score": {
        "min": -50, "max": 50, "stage": 2,
        "label": "Minimum keyword score",
        "what": "Each item gets the sum of the weights of the keywords it contains plus the source bonus. "
                "Items below this number are dropped without asking the AI.",
        "raise": "Fewer items reach the AI — lower Gemini usage, more chance to miss something.",
        "lower": "More items reach the AI. 0 or less means almost everything is scored.",
    },
    "ai_enabled": {
        "min": 0, "max": 1, "switch": True, "stage": 3,
        "label": "Use AI (Gemini) to score news",
        "what": "Gemini scores every item that passed the keyword filter from 1 to 5 and writes a short "
                "Polish summary. Without AI the keyword score alone decides.",
        "raise": "On — better selection and summaries in the digest.",
        "lower": "Off — no Gemini calls; items with a high keyword score are sent without a summary.",
    },
    "ai_batch_size": {
        "min": 1, "max": 50, "stage": 3,
        "label": "Items per AI call",
        "what": "How many items go to Gemini in one request. The workflow makes one request per run "
                "(every 15 minutes).",
        "raise": "Faster backlog processing, longer requests.",
        "lower": "Smaller requests; a busy news day may build a backlog.",
    },
    "ai_max_attempts": {
        "min": 1, "max": 10, "stage": 3,
        "label": "AI attempts per item",
        "what": "If Gemini fails or answers badly, the item is retried on the next runs. After this "
                "many attempts it is marked as failed and skipped.",
        "raise": "More patience during a Gemini outage.",
        "lower": "Problem items are given up sooner.",
    },
    "digest_min_ai_score": {
        "min": 1, "max": 5, "stage": 4,
        "label": "Send news with AI score from",
        "what": "Items the AI scored at least this high go into the next digest e-mail.",
        "raise": "4 — only important news; 5 — only urgent news. Shorter e-mails.",
        "lower": "3 — also news worth a look. 2 or 1 — nearly everything.",
    },
    "fallback_min_keyword_score": {
        "min": -50, "max": 50, "stage": 4,
        "label": "Without AI: send from keyword score",
        "what": "Used only when AI is off: items with at least this keyword score go into the digest.",
        "raise": "Fewer items without AI.",
        "lower": "More items without AI.",
    },
    "digest_max_items": {
        "min": 1, "max": 200, "stage": 4,
        "label": "Max news per e-mail",
        "what": "The best items go first. The rest wait for the next digest.",
        "raise": "Longer e-mails, nothing waits.",
        "lower": "Shorter e-mails; on a busy day lower-scored items arrive later.",
    },
    "retention_days": {
        "min": 1, "max": 3650, "stage": 5,
        "label": "Keep collected news for (days)",
        "what": "Items and digest history older than this are deleted.",
        "raise": "Longer history on the Items page.",
        "lower": "Smaller database.",
    },
}

STAGES = {
    1: ("Collecting", "Sources are fetched on their own interval. New articles are de-duplicated."),
    2: ("Keyword filter", "Keywords and source bonus decide what is worth asking the AI about."),
    3: ("AI scoring", "Gemini scores each item 1–5 and writes a Polish summary."),
    4: ("Digest e-mail", "Sent at the times on the Schedule page."),
    5: ("Housekeeping", "Old data is deleted automatically."),
}

AUDITED_TABLES = ["settings", "sources", "keywords", "digest_slots", "prompts"]


# ============================================================
# Database helpers
# ============================================================

@contextmanager
def db(readonly=False):
    """One connection + one transaction per call; commits on success."""
    with psycopg.connect(**DB_PARAMS, row_factory=dict_row) as conn:
        with conn.transaction():
            if readonly:
                conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SELECT set_config('news.actor', %s, true)",
                         (session.get("user") or "anonymous",))
            yield conn


def db_error(exc):
    """Human-readable message from a psycopg error (never the full traceback)."""
    diag = getattr(exc, "diag", None)
    if diag is not None and diag.message_primary:
        msg = diag.message_primary
        if diag.message_detail:
            msg += f" ({diag.message_detail})"
        return msg
    return str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__


def fmt_num(value):
    if value is None:
        return ""
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return str(int(value))
        return format(value.normalize(), "f")
    return str(value)


def fmt_dt(value):
    if not value:
        return "—"
    if isinstance(value, datetime) and value.tzinfo:
        value = value.astimezone(TZ)
    return value.strftime("%Y-%m-%d %H:%M")


def text_fingerprint(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode())
        h.update(b"\x00")
    return h.hexdigest()


def int_field(name, lo, hi, label=None):
    raw = (request.form.get(name) or "").strip()
    try:
        val = int(raw)
    except ValueError:
        raise ValueError(f"{label or name}: '{raw}' is not a whole number")
    if not lo <= val <= hi:
        raise ValueError(f"{label or name}: allowed range {lo}–{hi}")
    return val


# ============================================================
# Auth, CSRF, security headers
# ============================================================

_login_failures = {}
_login_lock = threading.Lock()
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_FAILURES = 5


def _throttle_keys(username):
    return (f"ip:{request.remote_addr}", f"user:{username.lower()}")


def _is_throttled(username):
    now = time.time()
    with _login_lock:
        for key in _throttle_keys(username):
            recent = [t for t in _login_failures.get(key, []) if now - t < LOGIN_WINDOW]
            _login_failures[key] = recent
            if len(recent) >= LOGIN_MAX_FAILURES:
                return True
    return False


def _record_failure(username):
    with _login_lock:
        for key in _throttle_keys(username):
            _login_failures.setdefault(key, []).append(time.time())


def _check_password(username, password):
    stored = USERS.get(username)
    if stored is None:
        hmac.compare_digest(password, "x" * 32)
        return False
    if stored.startswith(("pbkdf2:", "scrypt:")):
        return check_password_hash(stored, password)
    return hmac.compare_digest(stored.encode(), password.encode())


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user") or session.get("user") not in USERS:
            session.clear()
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals.update(csrf_token=csrf_token, SOURCE_TYPES=SOURCE_TYPES, TOPICS=TOPICS,
                             LANGS=LANGS, ITEM_STATUS=ITEM_STATUS, QUERY_TYPES=QUERY_TYPES)
app.jinja_env.filters["num"] = fmt_num
app.jinja_env.filters["dt"] = fmt_dt


@app.before_request
def _csrf_protect():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, "Invalid CSRF token — reload the page and try again.")


@app.after_request
def _security_headers(resp):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(400)
@app.errorhandler(404)
def _http_error(err):
    return render_template("error.html", code=err.code, message=err.description), err.code


@app.errorhandler(psycopg.OperationalError)
def _db_down(err):
    log.error("Database unavailable: %s", err)
    return render_template("error.html", code=503,
                           message="No connection to the database (postgres_db)."), 503


def handle_db_errors(redirect_endpoint, keep_args=False):
    """Wraps a POST handler: ValueError and psycopg errors become flash messages.

    keep_args=True passes the handler's keyword arguments on to url_for
    (e.g. source_id for the source edit page).
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ValueError as exc:
                for line in str(exc).splitlines():
                    flash(line, "error")
            except KeyError:
                flash("A required field is missing.", "error")
            except psycopg.errors.UniqueViolation:
                flash("This entry already exists.", "error")
            except psycopg.Error as exc:
                flash(f"Database error: {db_error(exc)}", "error")
            return redirect(url_for(redirect_endpoint, **(kwargs if keep_args else {})))
        return wrapper
    return deco


# ============================================================
# Routes — auth
# ============================================================

@app.route("/healthz")
def healthz():
    if request.args.get("deep") == "1":
        try:
            with psycopg.connect(**DB_PARAMS) as conn:
                conn.execute("SELECT 1 FROM settings LIMIT 1")
        except psycopg.Error:
            return "db unavailable", 503
    return "ok", 200


def _is_safe_next_url(target):
    if not target:
        return False
    normalized = target.replace("\\", "/")
    parsed = urlparse(normalized)
    if parsed.scheme or parsed.netloc:
        return False
    return normalized.startswith("/") and not normalized.startswith("//")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if _is_throttled(username):
            log.warning("Login throttled for user=%r ip=%s", username, request.remote_addr)
            flash("Too many failed attempts. Try again in 15 minutes.", "error")
        elif _check_password(username, password):
            session.clear()
            session.permanent = True
            session["user"] = username
            csrf_token()
            log.info("Login ok user=%r ip=%s", username, request.remote_addr)
            nxt = request.args.get("next", "")
            return redirect(nxt if _is_safe_next_url(nxt) else url_for("dashboard"))
        else:
            _record_failure(username)
            log.warning("Login failed user=%r ip=%s", username, request.remote_addr)
            flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================================
# Routes — overview
# ============================================================

def next_digest(slots, now=None):
    """Next active slot as an aware datetime in TZ, or None."""
    now = now or datetime.now(TZ)
    times = sorted(s["slot_time"] for s in slots if s["is_active"])
    for day in range(2):
        date = (now + timedelta(days=day)).date()
        for t in times:
            at = datetime.combine(date, t, tzinfo=TZ)
            if at > now:
                return at
    return None


@app.route("/")
@login_required
def dashboard():
    with db(readonly=True) as conn:
        status = conn.execute(
            "SELECT status, count(*) AS n FROM items "
            "WHERE fetched_at > now() - interval '24 hours' GROUP BY status").fetchall()
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
        slots = conn.execute("SELECT slot_time, is_active FROM digest_slots").fetchall()
        failing = conn.execute(
            "SELECT s.id, s.name, st.last_error, st.error_streak, st.last_polled_at "
            "FROM sources s JOIN source_state st ON st.source_id = s.id "
            "WHERE s.is_active AND st.last_status = 'error' ORDER BY st.error_streak DESC").fetchall()
        sources = conn.execute(
            "SELECT count(*) FILTER (WHERE is_active) AS active, count(*) AS total FROM sources").fetchone()
        never = conn.execute(
            "SELECT count(*) AS n FROM sources s LEFT JOIN source_state st ON st.source_id = s.id "
            "WHERE s.is_active AND st.source_id IS NULL").fetchone()["n"]
        digests = conn.execute(
            "SELECT * FROM digest_log ORDER BY slot_at DESC LIMIT 8").fetchall()
        queued = conn.execute(
            "SELECT i.id, s.name AS source, i.title, i.url, i.ai_score, i.ai_summary_pl, i.published_at "
            "FROM items i LEFT JOIN sources s ON s.id = i.source_id "
            "WHERE i.status = 'alert' ORDER BY i.ai_score DESC NULLS LAST, i.published_at DESC LIMIT 10").fetchall()
        changes = conn.execute(
            "SELECT changed_at, actor, table_name, operation, row_key FROM config_change_log "
            "ORDER BY changed_at DESC, id DESC LIMIT 8").fetchall()
    counts = {r["status"]: r["n"] for r in status}
    return render_template("dashboard.html", counts=counts, settings=settings,
                           next_at=next_digest(slots), failing=failing, sources=sources, never=never,
                           digests=digests, queued=queued, changes=changes)


# ============================================================
# Routes — settings
# ============================================================

def setting_label(key):
    return SETTING_HELP.get(key, {}).get("label", key)


app.jinja_env.globals["setting_label"] = setting_label


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    if request.method == "POST":
        try:
            with db() as conn:
                rows = conn.execute("SELECT key, value FROM settings FOR UPDATE").fetchall()
                current = {r["key"]: r["value"] for r in rows}
                changed, errors = {}, []
                for key, cur_val in current.items():
                    raw = request.form.get(f"v__{key}")
                    if raw is None:
                        continue
                    raw = raw.strip().replace(",", ".")
                    try:
                        new_val = Decimal(raw)
                    except InvalidOperation:
                        errors.append(f"{setting_label(key)}: '{raw}' is not a number")
                        continue
                    rule = SETTING_HELP.get(key)
                    if not new_val.is_finite() or new_val != new_val.to_integral_value():
                        errors.append(f"{setting_label(key)}: must be a whole number")
                        continue
                    if rule and not rule["min"] <= new_val <= rule["max"]:
                        errors.append(f"{setting_label(key)}: allowed range {rule['min']}–{rule['max']}")
                        continue
                    if new_val != cur_val:
                        if request.form.get(f"o__{key}", "") != fmt_num(cur_val):
                            errors.append(f"{setting_label(key)}: changed by someone else in the meantime "
                                          f"(now {fmt_num(cur_val)}) — reload the page")
                        changed[key] = new_val
                if errors:
                    raise ValueError("\n".join(errors))
                for key, val in changed.items():
                    conn.execute("UPDATE settings SET value = %s WHERE key = %s", (val, key))
            if changed:
                flash("Saved: " + ", ".join(f"{setting_label(k)} = {fmt_num(v)}" for k, v in changed.items()), "ok")
            else:
                flash("No changes.", "info")
        except ValueError as exc:
            for line in str(exc).splitlines():
                flash(line, "error")
        except psycopg.Error as exc:
            flash(f"Database error: {db_error(exc)}", "error")
        return redirect(url_for("settings_page"))

    with db(readonly=True) as conn:
        rows = conn.execute("SELECT key, value, description, updated_at FROM settings").fetchall()
    order = {k: i for i, k in enumerate(SETTING_HELP)}
    stages = {n: {"title": t, "intro": i, "rows": []} for n, (t, i) in STAGES.items()}
    other = []
    for r in sorted(rows, key=lambda r: order.get(r["key"], len(order))):
        r["help"] = SETTING_HELP.get(r["key"])
        (stages[r["help"]["stage"]]["rows"] if r["help"] else other).append(r)
    return render_template("settings.html", stages=stages, other=other)


# ============================================================
# Routes — sources
# ============================================================

def _source_from_form():
    stype = request.form.get("source_type", "")
    if stype not in SOURCE_TYPES:
        raise ValueError("Unknown source type")
    name = (request.form.get("name") or "").strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("Name: 1–80 characters")
    target = (request.form.get("target") or "").strip()
    if stype in QUERY_TYPES:
        if not target:
            raise ValueError("Search query must not be empty")
        endpoint, query = None, target
    else:
        if not re.match(r"^https://[^\s/]+\.[^\s]+$", target):
            raise ValueError("Feed URL must be a full https:// address")
        endpoint, query = target, None
    lang = request.form.get("lang", "en")
    topic = request.form.get("topic", "")
    if lang not in LANGS or topic not in TOPICS:
        raise ValueError("Unknown language or topic")
    return {
        "name": name, "source_type": stype, "endpoint": endpoint, "query": query,
        "lang": lang, "topic": topic,
        "poll_minutes": int_field("poll_minutes", 5, 1440, "Check every (minutes)"),
        "base_score": int_field("base_score", -10, 10, "Source bonus"),
        "note": (request.form.get("note") or "").strip()[:200] or None,
    }


@app.route("/sources", methods=["GET", "POST"])
@login_required
def sources_page():
    if request.method == "POST":
        return _sources_post()
    with db(readonly=True) as conn:
        rows = conn.execute(
            "SELECT s.*, st.last_polled_at, st.last_status, st.last_error, st.last_item_count, st.error_streak, "
            "       (SELECT count(*) FROM items i WHERE i.source_id = s.id "
            "          AND i.fetched_at > now() - interval '7 days') AS items_7d, "
            "       (SELECT count(*) FROM items i WHERE i.source_id = s.id AND i.status = 'sent' "
            "          AND i.fetched_at > now() - interval '7 days') AS sent_7d "
            "FROM sources s LEFT JOIN source_state st ON st.source_id = s.id "
            "ORDER BY s.is_active DESC, s.topic, s.name").fetchall()
    return render_template("sources.html", rows=rows)


@handle_db_errors("sources_page")
def _sources_post():
    action = request.form.get("action")
    with db() as conn:
        if action == "add":
            s = _source_from_form()
            conn.execute(
                "INSERT INTO sources (name, source_type, endpoint, query, lang, topic, poll_minutes, "
                "base_score, note) VALUES (%(name)s, %(source_type)s, %(endpoint)s, %(query)s, %(lang)s, "
                "%(topic)s, %(poll_minutes)s, %(base_score)s, %(note)s)", s)
            flash(f"Added source {s['name']}. It is fetched on the next workflow run.", "ok")
        elif action == "toggle":
            r = conn.execute("UPDATE sources SET is_active = NOT is_active WHERE id = %s "
                             "RETURNING name, is_active", (int(request.form["id"]),)).fetchone()
            if r:
                flash(f"{r['name']}: {'enabled' if r['is_active'] else 'disabled'}.", "ok")
        elif action == "delete":
            r = conn.execute("DELETE FROM sources WHERE id = %s RETURNING name",
                             (int(request.form["id"]),)).fetchone()
            if r:
                flash(f"Deleted {r['name']}. Items it collected stay, without a source.", "ok")
        else:
            abort(400)
    return redirect(url_for("sources_page"))


@app.route("/sources/<int:source_id>", methods=["GET", "POST"])
@login_required
def source_edit(source_id):
    if request.method == "POST":
        return _source_edit_post(source_id=source_id)
    with db(readonly=True) as conn:
        row = conn.execute("SELECT * FROM sources WHERE id = %s", (source_id,)).fetchone()
        if row is None:
            abort(404, "Source does not exist.")
        state = conn.execute("SELECT * FROM source_state WHERE source_id = %s", (source_id,)).fetchone()
        recent = conn.execute(
            "SELECT title, url, status, ai_score, keyword_score, fetched_at FROM items "
            "WHERE source_id = %s ORDER BY fetched_at DESC, id DESC LIMIT 15", (source_id,)).fetchall()
    return render_template("source_edit.html", row=row, state=state, recent=recent)


@handle_db_errors("source_edit", keep_args=True)
def _source_edit_post(source_id):
    s = _source_from_form()
    with db() as conn:
        r = conn.execute(
            "UPDATE sources SET name = %(name)s, source_type = %(source_type)s, endpoint = %(endpoint)s, "
            "query = %(query)s, lang = %(lang)s, topic = %(topic)s, poll_minutes = %(poll_minutes)s, "
            "base_score = %(base_score)s, note = %(note)s WHERE id = %(id)s RETURNING name",
            {**s, "id": source_id}).fetchone()
    if r is None:
        abort(404, "Source does not exist.")
    flash(f"Saved {r['name']}.", "ok")
    return redirect(url_for("sources_page"))


# ============================================================
# Routes — keywords
# ============================================================

def _normalize_term(raw):
    term = " ".join((raw or "").lower().split())
    if len(term) < 2 or not TERM_RE.match(term):
        raise ValueError("Keyword: 2–80 characters; '*' is allowed only at the end (prefix match)")
    return term


@app.route("/keywords", methods=["GET", "POST"])
@login_required
def keywords_page():
    if request.method == "POST":
        return _keywords_post()
    test_text = request.args.get("test", "")
    test = None
    with db(readonly=True) as conn:
        rows = conn.execute(
            "SELECT k.*, (SELECT count(*) FROM items i WHERE k.term = ANY (i.matched_keywords) "
            "             AND i.fetched_at > now() - interval '7 days') AS hits_7d "
            "FROM keywords k ORDER BY k.is_active DESC, k.weight DESC, k.term").fetchall()
        if test_text.strip():
            test = conn.execute("SELECT * FROM keyword_match(%s)", (test_text[:2000],)).fetchone()
        threshold = conn.execute("SELECT value FROM settings WHERE key = 'keyword_min_score'").fetchone()
    return render_template("keywords.html", rows=rows, test_text=test_text, test=test,
                           threshold=threshold["value"] if threshold else None)


@handle_db_errors("keywords_page")
def _keywords_post():
    action = request.form.get("action")
    with db() as conn:
        if action == "add":
            term = _normalize_term(request.form.get("term"))
            weight = int_field("weight", -10, 10, "Weight")
            if weight == 0:
                raise ValueError("Weight: 0 has no effect — use a positive or negative number")
            topic = request.form.get("topic", "")
            if topic not in TOPICS:
                raise ValueError("Unknown topic")
            conn.execute("INSERT INTO keywords (term, weight, topic, note) VALUES (%s, %s, %s, %s)",
                         (term, weight, topic, (request.form.get("note") or "").strip()[:200] or None))
            flash(f"Added keyword '{term}' ({weight:+d}).", "ok")
        elif action == "weight":
            weight = int_field("weight", -10, 10, "Weight")
            if weight == 0:
                raise ValueError("Weight: 0 has no effect — disable the keyword instead")
            conn.execute("UPDATE keywords SET weight = %s WHERE id = %s",
                         (weight, int(request.form["id"])))
            flash("Weight saved.", "ok")
        elif action == "toggle":
            r = conn.execute("UPDATE keywords SET is_active = NOT is_active WHERE id = %s "
                             "RETURNING term, is_active", (int(request.form["id"]),)).fetchone()
            if r:
                flash(f"'{r['term']}': {'enabled' if r['is_active'] else 'disabled'}.", "ok")
        elif action == "delete":
            r = conn.execute("DELETE FROM keywords WHERE id = %s RETURNING term",
                             (int(request.form["id"]),)).fetchone()
            if r:
                flash(f"Deleted '{r['term']}'.", "ok")
        else:
            abort(400)
    return redirect(url_for("keywords_page"))


# ============================================================
# Routes — digest schedule
# ============================================================

@app.route("/schedule", methods=["GET", "POST"])
@login_required
def schedule_page():
    if request.method == "POST":
        return _schedule_post()
    with db(readonly=True) as conn:
        slots = conn.execute("SELECT * FROM digest_slots ORDER BY slot_time").fetchall()
        log_rows = conn.execute("SELECT * FROM digest_log ORDER BY slot_at DESC LIMIT 30").fetchall()
    return render_template("schedule.html", slots=slots, log_rows=log_rows, next_at=next_digest(slots))


@handle_db_errors("schedule_page")
def _schedule_post():
    action = request.form.get("action")
    with db() as conn:
        if action == "add":
            raw = (request.form.get("slot_time") or "").strip()
            try:
                t = dtime.fromisoformat(raw).replace(second=0, microsecond=0)
            except ValueError:
                raise ValueError("Time must be HH:MM")
            conn.execute("INSERT INTO digest_slots (slot_time, note) VALUES (%s, %s)",
                         (t, (request.form.get("note") or "").strip()[:100] or None))
            flash(f"Added digest at {t.strftime('%H:%M')}.", "ok")
        elif action in ("toggle", "delete"):
            t = dtime.fromisoformat(request.form["slot_time"])
            if action == "toggle":
                conn.execute("UPDATE digest_slots SET is_active = NOT is_active WHERE slot_time = %s", (t,))
            else:
                conn.execute("DELETE FROM digest_slots WHERE slot_time = %s", (t,))
            flash(f"{t.strftime('%H:%M')}: {'updated' if action == 'toggle' else 'deleted'}.", "ok")
        else:
            abort(400)
    return redirect(url_for("schedule_page"))


# ============================================================
# Routes — prompt versions
# ============================================================

def _placeholder_report(text):
    found = set(PLACEHOLDER_RE.findall(text or ""))
    return {
        "missing": [p for p in REQUIRED_PLACEHOLDERS if p not in (text or "")],
        "unknown": sorted(found - {"ITEMS_JSON"}),
    }


def _get_prompt(conn, version):
    row = conn.execute("SELECT * FROM prompts WHERE version = %s", (version,)).fetchone()
    if row is None:
        abort(404, f"Prompt version '{version}' does not exist.")
    return row


def _suggest_version(conn, base):
    existing = {r["version"] for r in conn.execute("SELECT version FROM prompts").fetchall()}
    m = re.match(r"^(.*?)(\d+)$", base or "")
    if m:
        prefix, num = m.group(1), int(m.group(2))
        for i in range(num + 1, num + 1000):
            if f"{prefix}{i}" not in existing and VERSION_RE.match(f"{prefix}{i}"):
                return f"{prefix}{i}"
    return ""


@app.route("/prompts")
@login_required
def prompts_page():
    with db(readonly=True) as conn:
        rows = conn.execute(
            "SELECT p.version, p.is_active, p.notes, p.created_at, length(p.system_prompt) AS chars, "
            "       (SELECT count(*) FROM items i WHERE i.prompt_version = p.version) AS used "
            "FROM prompts p ORDER BY p.is_active DESC, p.created_at DESC, p.version DESC").fetchall()
    return render_template("prompts.html", rows=rows)


@app.route("/prompts/new", methods=["GET", "POST"])
@login_required
def prompt_new():
    if request.method == "POST":
        version = request.form.get("version", "").strip()
        text = request.form.get("system_prompt", "").replace("\r\n", "\n")
        notes = request.form.get("notes", "").strip()[:500] or None
        error = None
        if not VERSION_RE.match(version):
            error = "Version name: 1–20 characters, letters, digits, dot, hyphen, underscore only."
        elif not text.strip():
            error = "Prompt text must not be empty."
        else:
            try:
                with db() as conn:
                    conn.execute("INSERT INTO prompts (version, system_prompt, is_active, notes) "
                                 "VALUES (%s, %s, FALSE, %s)", (version, text, notes))
                flash(f"Draft {version} created. It is not active yet.", "ok")
                return redirect(url_for("prompt_edit", version=version))
            except psycopg.errors.UniqueViolation:
                error = f"Version '{version}' already exists."
            except psycopg.Error as exc:
                error = f"Database error: {db_error(exc)}"
        flash(error, "error")
        return render_template("prompt_new.html", version=version, text=text, notes=notes or "")

    source = request.args.get("from", "")
    with db(readonly=True) as conn:
        src = _get_prompt(conn, source) if source else \
            conn.execute("SELECT * FROM prompts WHERE is_active").fetchone()
        base = src["version"] if src else "1.0"
        version = _suggest_version(conn, base)
    return render_template("prompt_new.html", version=version, text=src["system_prompt"] if src else "",
                           notes=f"Based on {base}" if src else "")


@app.route("/prompts/<version>", methods=["GET", "POST"])
@login_required
def prompt_edit(version):
    if request.method == "POST":
        text = request.form.get("system_prompt", "").replace("\r\n", "\n")
        notes = request.form.get("notes", "").strip()[:500] or None
        row = None
        try:
            with db() as conn:
                row = _get_prompt(conn, version)
                if row["is_active"]:
                    text = row["system_prompt"]  # active text is read-only, only the note changes
                if not text.strip():
                    raise ValueError("Prompt text must not be empty.")
                if request.form.get("fingerprint", "") != text_fingerprint(row["system_prompt"], row["notes"]):
                    raise ValueError("This version was changed by someone else in the meantime. "
                                     "Copy your edits and reload the page.")
                conn.execute("UPDATE prompts SET system_prompt = %s, notes = %s WHERE version = %s",
                             (text, notes, version))
            flash(f"Saved version {version}.", "ok")
            return redirect(url_for("prompt_edit", version=version))
        except ValueError as exc:
            flash(str(exc), "error")
        except psycopg.Error as exc:
            flash(f"Database error: {db_error(exc)}", "error")
        if row is None:
            return redirect(url_for("prompts_page"))
        row = {**row, "system_prompt": text, "notes": notes}
        return render_template("prompt_edit.html", row=row, report=_placeholder_report(text),
                               fingerprint=request.form.get("fingerprint", ""), diff=[])

    with db(readonly=True) as conn:
        row = _get_prompt(conn, version)
        active = conn.execute("SELECT * FROM prompts WHERE is_active").fetchone()
    diff = []
    if active and not row["is_active"]:
        for line in difflib.unified_diff(active["system_prompt"].splitlines(), row["system_prompt"].splitlines(),
                                         fromfile=active["version"], tofile=version, lineterm="", n=2):
            kind = "meta" if line.startswith(("---", "+++", "@@")) else \
                "add" if line.startswith("+") else "del" if line.startswith("-") else "ctx"
            diff.append((kind, line))
    return render_template("prompt_edit.html", row=row, report=_placeholder_report(row["system_prompt"]),
                           fingerprint=text_fingerprint(row["system_prompt"], row["notes"]), diff=diff)


@app.route("/prompts/<version>/activate", methods=["POST"])
@login_required
@handle_db_errors("prompts_page")
def prompt_activate(version):
    with db() as conn:
        row = _get_prompt(conn, version)
        if row["is_active"]:
            flash(f"Version {version} is already active.", "info")
            return redirect(url_for("prompts_page"))
        report = _placeholder_report(row["system_prompt"])
        if report["missing"] or report["unknown"]:
            raise ValueError("Not activated — the prompt must contain [[ITEMS_JSON]] and no other placeholders.")
        # Partial unique index forbids two active rows, the deferred trigger forbids zero at COMMIT.
        conn.execute("UPDATE prompts SET is_active = FALSE WHERE is_active")
        conn.execute("UPDATE prompts SET is_active = TRUE WHERE version = %s", (version,))
    flash(f"Active prompt: {version}. The workflow uses it from the next AI call.", "ok")
    return redirect(url_for("prompts_page"))


@app.route("/prompts/<version>/delete", methods=["POST"])
@login_required
@handle_db_errors("prompts_page")
def prompt_delete(version):
    with db() as conn:
        _get_prompt(conn, version)
        conn.execute("DELETE FROM prompts WHERE version = %s", (version,))
    flash(f"Deleted version {version}.", "ok")
    return redirect(url_for("prompts_page"))


# ============================================================
# Routes — items (read-only)
# ============================================================

@app.route("/items")
@login_required
def items_page():
    status = request.args.get("status", "")
    if status not in ITEM_STATUS:
        status = ""
    q = request.args.get("q", "").strip()[:100]
    with db(readonly=True) as conn:
        rows = conn.execute(
            "SELECT i.*, s.name AS source FROM items i LEFT JOIN sources s ON s.id = i.source_id "
            "WHERE (%(status)s = '' OR i.status = %(status)s) "
            "  AND (%(q)s = '' OR i.title ILIKE '%%' || %(q)s || '%%') "
            "ORDER BY i.fetched_at DESC, i.id DESC LIMIT 200", {"status": status, "q": q}).fetchall()
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) AS n FROM items GROUP BY status").fetchall()}
    return render_template("items.html", rows=rows, status=status, q=q, counts=counts)


# ============================================================
# Routes — change log
# ============================================================

def _changes(old, new):
    old, new = old or {}, new or {}
    out = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if a == b:
            continue
        diff = None
        if isinstance(a, str) and isinstance(b, str) and (len(a) > 200 or len(b) > 200):
            diff = [("meta" if l.startswith(("---", "+++", "@@")) else
                     "add" if l.startswith("+") else "del" if l.startswith("-") else "ctx", l)
                    for l in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=1)]
        out.append({"field": key, "old": a, "new": b, "diff": diff})
    return out


@app.route("/audit")
@login_required
def audit_page():
    table = request.args.get("table", "")
    if table not in AUDITED_TABLES:
        table = ""
    with db(readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM config_change_log WHERE (%s = '' OR table_name = %s) "
            "ORDER BY changed_at DESC, id DESC LIMIT 200", (table, table)).fetchall()
    for r in rows:
        r["changes"] = _changes(r["old_data"], r["new_data"]) if r["operation"] == "UPDATE" else []
    return render_template("audit.html", rows=rows, table=table, tables=AUDITED_TABLES)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)
