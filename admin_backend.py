"""
DAVAI Admin Backend
SQLite-based admin API — mounted into proxy.py under /admin/api/*
"""
import sqlite3
import json
import os
import hashlib
import hmac
import secrets
import time
import re
from datetime import datetime, timezone

ADMIN_DB_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "admin.db")
MOBILE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "davai-mobile", "backend", "data", "davai.db")

# ── DB init ───────────────────────────────────────────────────────────────────

def _db():
    os.makedirs(os.path.dirname(ADMIN_DB_PATH), exist_ok=True)
    con = sqlite3.connect(ADMIN_DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS admin_accounts (
            id          TEXT PRIMARY KEY,
            username    TEXT UNIQUE NOT NULL,
            pw_hash     TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token       TEXT PRIMARY KEY,
            admin_id    TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            email       TEXT UNIQUE NOT NULL,
            pw_hash     TEXT NOT NULL,
            plan        TEXT NOT NULL DEFAULT 'free',
            program     TEXT NOT NULL DEFAULT 'IB',
            school      TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS payments (
            id           TEXT PRIMARY KEY,
            user_id      TEXT,
            plan_id      TEXT NOT NULL,
            sender_name  TEXT NOT NULL,
            sender_ref   TEXT,
            status       TEXT NOT NULL DEFAULT 'pending',
            created_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS pro_codes (
            id          TEXT PRIMARY KEY,
            code        TEXT UNIQUE NOT NULL,
            plan        TEXT NOT NULL DEFAULT 'pro',
            used_at     TEXT,
            used_by     TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    con.commit()
    return con

_CON = None
def db():
    global _CON
    if _CON is None:
        _CON = _db()
    return _CON

# ── Mobile app DB (read/write) ────────────────────────────────────────────────
_MCON = None
def mdb():
    global _MCON
    if os.path.exists(MOBILE_DB_PATH):
        if _MCON is None:
            _MCON = sqlite3.connect(MOBILE_DB_PATH, check_same_thread=False)
            _MCON.row_factory = sqlite3.Row
            _MCON.execute("PRAGMA journal_mode=WAL")
    return _MCON

def _has_mobile_db():
    return os.path.exists(MOBILE_DB_PATH)

# ── Password helpers ──────────────────────────────────────────────────────────

def _hash_pw(password: str) -> str:
    salt = secrets.token_hex(16)
    h    = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return salt + ":" + h.hex()

def _verify_pw(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
        return hmac.compare_digest(check.hex(), h)
    except Exception:
        return False

# ── Session helpers ───────────────────────────────────────────────────────────

def _create_session(admin_id: str) -> str:
    token = secrets.token_urlsafe(40)
    expires = datetime.now(timezone.utc).replace(tzinfo=None)
    expires_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    # 7-day expiry
    import datetime as dt
    exp = (datetime.utcnow() + dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    db().execute(
        "INSERT INTO admin_sessions (token, admin_id, created_at, expires_at) VALUES (?,?,datetime('now'),?)",
        (token, admin_id, exp)
    )
    db().commit()
    return token

def verify_session(token: str):
    if not token:
        return None
    row = db().execute(
        "SELECT a.id, a.username FROM admin_sessions s JOIN admin_accounts a ON a.id=s.admin_id "
        "WHERE s.token=? AND s.expires_at > datetime('now')",
        (token,)
    ).fetchone()
    return row

def _clean_sessions():
    db().execute("DELETE FROM admin_sessions WHERE expires_at <= datetime('now')")
    db().commit()

# ── Setup check ───────────────────────────────────────────────────────────────

def is_setup_done() -> bool:
    row = db().execute("SELECT COUNT(*) as c FROM admin_accounts").fetchone()
    return row["c"] > 0

# ── Route dispatch ────────────────────────────────────────────────────────────

def handle(method: str, path: str, body: dict, token: str) -> tuple:
    """Returns (status_code, response_dict)"""

    # ── Public ──────────────────────────────────────────────
    if path == "/admin/api/setup-status" and method == "GET":
        return 200, {"done": is_setup_done()}

    if path == "/admin/api/setup" and method == "POST":
        return _setup(body)

    if path == "/admin/api/login" and method == "POST":
        return _login(body)

    # ── Auth required ────────────────────────────────────────
    admin = verify_session(token)
    if not admin:
        return 401, {"message": "Unauthorized"}

    if path == "/admin/api/me" and method == "GET":
        return 200, {"id": admin["id"], "username": admin["username"]}

    if path == "/admin/api/stats" and method == "GET":
        return _stats()

    # Users
    if path == "/admin/api/users" and method == "GET":
        return _users_list()
    if path == "/admin/api/users" and method == "POST":
        return _user_create(body)
    if re.match(r"^/admin/api/users/[\w-]+$", path):
        uid = path.split("/")[-1]
        if method == "PATCH":  return _user_update(uid, body)
        if method == "DELETE": return _user_delete(uid)

    # Payments
    if path == "/admin/api/payments" and method == "GET":
        return _payments_list(body)
    if re.match(r"^/admin/api/payments/[\w-]+/approve$", path):
        pid = path.split("/")[-2]
        if method == "POST": return _payment_approve(pid)
    if re.match(r"^/admin/api/payments/[\w-]+/reject$", path):
        pid = path.split("/")[-2]
        if method == "POST": return _payment_reject(pid)

    # Codes
    if path == "/admin/api/codes" and method == "GET":
        return _codes_list()
    if path == "/admin/api/codes/generate" and method == "POST":
        return _code_generate(body)
    if re.match(r"^/admin/api/codes/[\w-]+$", path):
        cid = path.split("/")[-1]
        if method == "DELETE": return _code_delete(cid)

    # Logs
    if path == "/admin/api/logs" and method == "GET":
        return _logs()

    # Change password
    if path == "/admin/api/change-password" and method == "POST":
        return _change_password(admin["id"], body)

    # AI Config
    if path == "/admin/api/ai-config" and method == "GET":
        return _ai_config_get()
    if path == "/admin/api/ai-config" and method == "POST":
        return _ai_config_set(body)

    # Site Config
    if path == "/admin/api/site-config" and method == "GET":
        return _site_config_get()
    if path == "/admin/api/site-config" and method == "POST":
        return _site_config_set(body)

    return 404, {"message": "Not found"}

# ── Handlers ──────────────────────────────────────────────────────────────────

def _setup(body):
    if is_setup_done():
        return 409, {"message": "Admin account already exists."}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or len(password) < 8:
        return 400, {"message": "Username required and password must be 8+ chars."}
    uid = secrets.token_urlsafe(12)
    db().execute(
        "INSERT INTO admin_accounts (id, username, pw_hash) VALUES (?,?,?)",
        (uid, username, _hash_pw(password))
    )
    db().commit()
    return 201, {"ok": True}

def _login(body):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    row = db().execute("SELECT * FROM admin_accounts WHERE username=?", (username,)).fetchone()
    if not row or not _verify_pw(password, row["pw_hash"]):
        return 401, {"message": "Invalid username or password."}
    token = _create_session(row["id"])
    _clean_sessions()
    return 200, {"token": token}

def _stats():
    c     = db()
    m     = mdb()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    # Admin DB counts
    a_total   = c.execute("SELECT COUNT(*) as n FROM users").fetchone()["n"]
    a_pro     = c.execute("SELECT COUNT(*) as n FROM users WHERE plan != 'free'").fetchone()["n"]
    a_today   = c.execute("SELECT COUNT(*) as n FROM users WHERE created_at >= ?", (today,)).fetchone()["n"]
    a_pending = c.execute("SELECT COUNT(*) as n FROM payments WHERE status='pending'").fetchone()["n"]
    a_recent  = [dict(r) for r in c.execute("SELECT id,name,email,plan,program,created_at FROM users ORDER BY created_at DESC LIMIT 5").fetchall()]
    a_ppay    = [dict(r) for r in c.execute("SELECT * FROM payments WHERE status='pending' ORDER BY created_at DESC LIMIT 5").fetchall()]

    # Mobile DB counts (if available)
    m_total = m_pro = m_today = m_pending = 0
    m_recent = []
    m_ppay   = []
    if m:
        m_total   = m.execute("SELECT COUNT(*) as n FROM users").fetchone()["n"]
        m_pro     = m.execute("SELECT COUNT(*) as n FROM users WHERE plan != 'free'").fetchone()["n"]
        m_today   = m.execute("SELECT COUNT(*) as n FROM users WHERE created_at >= ?", (today,)).fetchone()["n"]
        m_pending = m.execute("SELECT COUNT(*) as n FROM payments WHERE status='pending'").fetchone()["n"]
        m_recent  = [dict(r) for r in m.execute("SELECT id,name,email,plan,program,created_at FROM users ORDER BY created_at DESC LIMIT 5").fetchall()]
        m_ppay    = [dict(r) for r in m.execute("SELECT * FROM payments WHERE status='pending' ORDER BY created_at DESC LIMIT 5").fetchall()]

    # Merge — label source
    for r in a_recent: r["source"] = "web"
    for r in m_recent: r["source"] = "app"
    for r in a_ppay:   r["source"] = "web"
    for r in m_ppay:   r["source"] = "app"

    recent_users = sorted(a_recent + m_recent, key=lambda x: x.get("created_at",""), reverse=True)[:8]
    pending_pay  = sorted(a_ppay  + m_ppay,   key=lambda x: x.get("created_at",""), reverse=True)[:8]

    return 200, {
        "total_users":       a_total + m_total,
        "pro_users":         a_pro   + m_pro,
        "pending_payments":  a_pending + m_pending,
        "today_signups":     a_today + m_today,
        "recent_users":      recent_users,
        "pending_pay":       pending_pay,
        "has_mobile_db":     bool(m),
    }

def _users_list():
    rows = [dict(r) for r in db().execute(
        "SELECT id,name,email,plan,program,school,created_at FROM users ORDER BY created_at DESC"
    ).fetchall()]
    for r in rows: r["source"] = "web"

    m = mdb()
    if m:
        mrows = [dict(r) for r in m.execute(
            "SELECT id,name,email,plan,program,school,created_at FROM users ORDER BY created_at DESC"
        ).fetchall()]
        for r in mrows: r["source"] = "app"
        rows = sorted(rows + mrows, key=lambda x: x.get("created_at",""), reverse=True)

    return 200, rows

def _user_create(body):
    name    = (body.get("name") or "").strip()
    email   = (body.get("email") or "").strip().lower()
    password= body.get("password") or secrets.token_urlsafe(10)
    plan    = body.get("plan") or "free"
    program = body.get("program") or "IB"
    if not name or not email:
        return 400, {"message": "name and email required"}
    existing = db().execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        return 409, {"message": "Email already exists"}
    uid = secrets.token_urlsafe(16)
    db().execute(
        "INSERT INTO users (id,name,email,pw_hash,plan,program) VALUES (?,?,?,?,?,?)",
        (uid, name, email, _hash_pw(password), plan, program)
    )
    db().commit()
    row = dict(db().execute("SELECT id,name,email,plan,program,created_at FROM users WHERE id=?", (uid,)).fetchone())
    return 201, row

def _user_update(uid, body):
    row = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return 404, {"message": "User not found"}
    name     = (body.get("name")    or row["name"]).strip()
    email    = (body.get("email")   or row["email"]).strip().lower()
    plan     = body.get("plan")     or row["plan"]
    program  = body.get("program")  or row["program"]
    password = body.get("password")
    if password:
        db().execute(
            "UPDATE users SET name=?,email=?,plan=?,program=?,pw_hash=? WHERE id=?",
            (name, email, plan, program, _hash_pw(password), uid)
        )
    else:
        db().execute(
            "UPDATE users SET name=?,email=?,plan=?,program=? WHERE id=?",
            (name, email, plan, program, uid)
        )
    db().commit()
    updated = dict(db().execute("SELECT id,name,email,plan,program,created_at FROM users WHERE id=?", (uid,)).fetchone())
    return 200, updated

def _user_delete(uid):
    r = db().execute("DELETE FROM users WHERE id=?", (uid,))
    db().commit()
    if r.rowcount == 0:
        return 404, {"message": "Not found"}
    return 200, {"ok": True}

def _payments_list(body):
    status = body.get("status","") if isinstance(body, dict) else ""
    q      = "SELECT * FROM payments" + (" WHERE status=?" if status else "") + " ORDER BY created_at DESC"
    args   = (status,) if status else ()

    rows = [dict(r) for r in db().execute(q, args).fetchall()]
    for r in rows: r["source"] = "web"

    m = mdb()
    if m:
        # check if payments table exists in mobile DB
        tbl = m.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='payments'").fetchone()
        if tbl:
            mrows = [dict(r) for r in m.execute(q, args).fetchall()]
            for r in mrows: r["source"] = "app"
            rows = sorted(rows + mrows, key=lambda x: x.get("created_at",""), reverse=True)

    return 200, rows

def _payment_approve(pid):
    # Try admin DB first, then mobile DB
    p = db().execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    if p:
        db().execute("UPDATE payments SET status='approved' WHERE id=?", (pid,))
        if p["user_id"]:
            db().execute("UPDATE users SET plan=? WHERE id=?", (p["plan_id"], p["user_id"]))
        db().commit()
        return 200, {"ok": True}

    m = mdb()
    if m:
        tbl = m.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='payments'").fetchone()
        if tbl:
            mp = m.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
            if mp:
                m.execute("UPDATE payments SET status='approved' WHERE id=?", (pid,))
                if mp["user_id"]:
                    m.execute("UPDATE users SET plan=? WHERE id=?", (mp["plan_id"], mp["user_id"]))
                m.commit()
                return 200, {"ok": True}

    return 404, {"message": "Payment not found"}

def _payment_reject(pid):
    r = db().execute("UPDATE payments SET status='rejected' WHERE id=?", (pid,))
    db().commit()
    if r.rowcount > 0:
        return 200, {"ok": True}

    m = mdb()
    if m:
        tbl = m.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='payments'").fetchone()
        if tbl:
            mr = m.execute("UPDATE payments SET status='rejected' WHERE id=?", (pid,))
            m.commit()
            if mr.rowcount > 0:
                return 200, {"ok": True}

    return 404, {"message": "Not found"}

def _codes_list():
    rows = [dict(r) for r in db().execute(
        "SELECT * FROM pro_codes ORDER BY created_at DESC"
    ).fetchall()]
    return 200, rows

def _code_generate(body):
    plan = body.get("plan") or "pro"
    code = "DAVAI-" + secrets.token_urlsafe(12).upper()[:16]
    cid  = secrets.token_urlsafe(12)
    db().execute(
        "INSERT INTO pro_codes (id,code,plan) VALUES (?,?,?)",
        (cid, code, plan)
    )
    db().commit()
    return 201, {"code": code, "plan": plan}

def _code_delete(cid):
    r = db().execute("DELETE FROM pro_codes WHERE id=?", (cid,))
    db().commit()
    if r.rowcount == 0:
        return 404, {"message": "Not found"}
    return 200, {"ok": True}

def _logs():
    # Return dummy rate-limit snapshot (real data lives in proxy.py memory)
    # proxy.py will inject real data via RATE_LOG global
    try:
        rows = RATE_LOG  # injected by proxy.py
    except NameError:
        rows = []
    return 200, rows

# ── AI Config ─────────────────────────────────────────────────────────────────

def _ensure_ai_config_table():
    db().execute("""
        CREATE TABLE IF NOT EXISTS ai_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    db().commit()

DEFAULTS = {
    "base_url":      "https://app.claude.gg/v1",
    "active_model":  "claude-sonnet-4-5",
    "max_tokens":    "800",
    "free_limit":    "15",
    "pro_limit":     "60",
    "promax_limit":  "150",
    "hourly_limit":  "20",
}

def _ai_config_get():
    _ensure_ai_config_table()
    rows = db().execute("SELECT key, value FROM ai_config").fetchall()
    cfg  = {r["key"]: r["value"] for r in rows}
    key  = cfg.get("api_key", "")
    return 200, {
        "base_url":      cfg.get("base_url",     DEFAULTS["base_url"]),
        "active_model":  cfg.get("active_model", DEFAULTS["active_model"]),
        "api_key_set":   bool(key),
        "api_key_hint":  (key[:6] + "..." + key[-4:]) if len(key) > 12 else ("*" * len(key) if key else ""),
        "max_tokens":    int(cfg.get("max_tokens",   DEFAULTS["max_tokens"])),
        "free_limit":    int(cfg.get("free_limit",   DEFAULTS["free_limit"])),
        "pro_limit":     int(cfg.get("pro_limit",    DEFAULTS["pro_limit"])),
        "promax_limit":  int(cfg.get("promax_limit", DEFAULTS["promax_limit"])),
        "hourly_limit":  int(cfg.get("hourly_limit", DEFAULTS["hourly_limit"])),
        "models": [
            {"id": "claude-haiku-5-5",    "label": "Claude Haiku 5.5 — fast, cheap"},
            {"id": "claude-sonnet-4-5",   "label": "Claude Sonnet 4.5 — balanced"},
            {"id": "claude-sonnet-4-6",   "label": "Claude Sonnet 4.6 — latest"},
            {"id": "claude-sonnet-5-5",   "label": "Claude Sonnet 5.5 — powerful"},
            {"id": "claude-opus-5-5",     "label": "Claude Opus 5.5 — best"},
            {"id": "claude-fable-5-1",    "label": "Claude Fable 5.1 — creative"},
        ],
    }

def _ai_config_set(body):
    _ensure_ai_config_table()
    updates = {}
    str_keys = ["base_url", "api_key", "active_model"]
    int_keys = ["max_tokens", "free_limit", "pro_limit", "promax_limit", "hourly_limit"]
    for k in str_keys:
        if k in body and str(body[k]).strip():
            val = str(body[k]).strip()
            if k == "base_url": val = val.rstrip("/")
            updates[k] = val
    for k in int_keys:
        if k in body:
            try:
                v = int(body[k])
                if v > 0: updates[k] = str(v)
            except (ValueError, TypeError):
                pass
    if not updates:
        return 400, {"message": "Nothing to update."}
    for k, v in updates.items():
        db().execute("INSERT INTO ai_config (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    db().commit()
    _sync_env_file(updates)
    return 200, {"ok": True}

def _sync_env_file(updates: dict):
    """Write updated keys back to davai-mobile/backend/.env"""
    import os
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "davai-mobile", "backend", ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r") as f:
            lines = f.readlines()
        key_map = {
            "api_key":      "ANTHROPIC_API_KEY",
            "base_url":     "ANTHROPIC_BASE_URL",
            "active_model": "ACTIVE_MODEL",
        }
        env_keys = {key_map[k]: v for k, v in updates.items() if k in key_map}
        new_lines = []
        written   = set()
        for line in lines:
            found = False
            for env_key, val in env_keys.items():
                if line.startswith(env_key + "="):
                    new_lines.append(f"{env_key}={val}\n")
                    written.add(env_key)
                    found = True
                    break
            if not found:
                new_lines.append(line)
        # Add any keys not already present
        for env_key, val in env_keys.items():
            if env_key not in written:
                new_lines.append(f"{env_key}={val}\n")
        with open(env_path, "w") as f:
            f.writelines(new_lines)
    except Exception as e:
        print(f"[admin] .env sync failed: {e}")

def ai_config_read():
    """Called by Node backend via HTTP to get current config."""
    _ensure_ai_config_table()
    rows = db().execute("SELECT key, value FROM ai_config").fetchall()
    cfg  = {r["key"]: r["value"] for r in rows}
    return {
        "api_key":      cfg.get("api_key",      ""),
        "base_url":     cfg.get("base_url",      ""),
        "active_model": cfg.get("active_model",  DEFAULTS["active_model"]),
        "max_tokens":   int(cfg.get("max_tokens",  DEFAULTS["max_tokens"])),
        "free_limit":   int(cfg.get("free_limit",  DEFAULTS["free_limit"])),
        "pro_limit":    int(cfg.get("pro_limit",   DEFAULTS["pro_limit"])),
        "promax_limit": int(cfg.get("promax_limit",DEFAULTS["promax_limit"])),
        "hourly_limit": int(cfg.get("hourly_limit",DEFAULTS["hourly_limit"])),
    }

# ── Site Config ────────────────────────────────────────────────────────────────

def _ensure_site_config_table():
    db().execute("""
        CREATE TABLE IF NOT EXISTS site_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    db().commit()

SITE_DEFAULTS = {
    "site_title":           "DAVAI",
    "site_tagline":         "Free AI study companion for IB Diploma and IELTS students.",
    "hero_subtitle":        "Structured criteria guidance, essay outlines, homework tracking, and vocabulary. Designed for independent learning without commercial subscriptions.",
    "btn_primary_bg":       "#ffffff",
    "btn_primary_color":    "#000000",
    "btn_primary_text":     "Test Study Assistant",
    "btn_secondary_bg":     "rgba(255,255,255,0.08)",
    "btn_secondary_color":  "#ffffff",
    "bg_base":              "#000000",
    "bg_surface":           "#0e0e11",
    "text_main":            "#ffffff",
    "text_muted":           "#a1a1aa",
    "accent_color":         "#ffffff",
    "navbar_brand_bg":      "#ffffff",
    "navbar_brand_color":   "#000000",
    "footer_text":          "DAVAI © 2026",
    "cta_title":            "Begin Your Study Session",
    "cta_subtitle":         "Access the homework tracker, revision tools, and vocabulary assistant.",
    "features_title":       "Everything you need to study smarter",
    "card_bg":              "#0e0e11",
    "card_border":          "rgba(255,255,255,0.06)",
}

def _site_config_get():
    _ensure_site_config_table()
    rows = db().execute("SELECT key, value FROM site_config").fetchall()
    cfg  = {r["key"]: r["value"] for r in rows}
    result = {}
    for k, default in SITE_DEFAULTS.items():
        result[k] = cfg.get(k, default)
    return 200, result

def _site_config_set(body):
    _ensure_site_config_table()
    if not isinstance(body, dict):
        return 400, {"message": "Invalid body"}
    if body.get("_reset"):
        db().execute("DELETE FROM site_config")
        db().commit()
        return 200, {"ok": True, "updated": 0}
    updated = 0
    for k, v in body.items():
        if k in SITE_DEFAULTS and str(v).strip():
            db().execute(
                "INSERT INTO site_config (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, str(v).strip())
            )
            updated += 1
    db().commit()
    return 200, {"ok": True, "updated": updated}

def site_config_read():
    _ensure_site_config_table()
    rows = db().execute("SELECT key, value FROM site_config").fetchall()
    cfg  = {r["key"]: r["value"] for r in rows}
    result = {}
    for k, default in SITE_DEFAULTS.items():
        result[k] = cfg.get(k, default)
    return result

def _change_password(admin_id, body):
    cur  = body.get("current") or ""
    new_ = body.get("new_password") or ""
    if len(new_) < 8:
        return 400, {"message": "New password must be 8+ characters."}
    row = db().execute("SELECT * FROM admin_accounts WHERE id=?", (admin_id,)).fetchone()
    if not row or not _verify_pw(cur, row["pw_hash"]):
        return 401, {"message": "Current password is incorrect."}
    db().execute("UPDATE admin_accounts SET pw_hash=? WHERE id=?", (_hash_pw(new_), admin_id))
    db().commit()
    return 200, {"ok": True}

# ── Payment submit (called from mobile app) ───────────────────────────────────

def submit_payment(user_id, plan_id, sender_name, sender_ref):
    if not plan_id or not sender_name:
        return 400, {"message": "plan_id and sender_name required"}
    pid = secrets.token_urlsafe(16)
    db().execute(
        "INSERT INTO payments (id,user_id,plan_id,sender_name,sender_ref) VALUES (?,?,?,?,?)",
        (pid, user_id or None, plan_id, sender_name.strip(), (sender_ref or "").strip() or None)
    )
    db().commit()
    return 201, {"ok": True, "paymentId": pid}

def get_payment_status(pid):
    row = db().execute("SELECT status,plan_id FROM payments WHERE id=?", (pid,)).fetchone()
    if not row:
        return 404, {"message": "Not found"}
    return 200, dict(row)

# ── Redeem code (called from mobile app) ─────────────────────────────────────

def redeem_code(user_id, code):
    row = db().execute("SELECT * FROM pro_codes WHERE code=? AND used_at IS NULL", (code,)).fetchone()
    if not row:
        return 400, {"message": "Invalid or already used code."}
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    db().execute("UPDATE pro_codes SET used_at=?, used_by=? WHERE id=?", (now, user_id, row["id"]))
    if user_id:
        db().execute("UPDATE users SET plan=? WHERE id=?", (row["plan"], user_id))
    db().commit()
    return 200, {"ok": True, "plan": row["plan"]}
