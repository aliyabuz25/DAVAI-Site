#!/usr/bin/env python3
# DAVAI proxy server — safe rewrite
# Set your key: export ANTHROPIC_API_KEY=sk-ant-...
# Then run:  python3 proxy.py
# IMPORTANT: go to console.anthropic.com → Settings → Billing → Usage limits
# and set a monthly spending cap before sharing this publicly.
"""
DAVAI — Local CORS Proxy
Forwards requests from the browser to api.anthropic.com, adding CORS headers.
Usage: python3 proxy.py
Then serve the site on :5050 separately, or let this proxy also serve static files.

To use live AI: set your Anthropic API key from console.anthropic.com
  export ANTHROPIC_API_KEY=sk-ant-...
  python3 proxy.py

⚠️  IMPORTANT BEFORE GOING PUBLIC:
  1. Go to console.anthropic.com → Settings → Billing → Usage limits
     Set a monthly spending cap (e.g. $5 or $10) so a bad day can't cost hundreds.
  2. Replace the API key below with a real sk-ant-... key from console.anthropic.com
  3. Never put the API key in any frontend JavaScript file — keep it here only.
"""

import http.server
import urllib.request
import urllib.error
import urllib.parse
import json
import os
import ssl
import time
import shutil
import tempfile
import atexit
import ipaddress
from collections import defaultdict
import admin_backend as _admin

STATIC_DIR  = os.path.dirname(os.path.abspath(__file__))

# ── API key — environment only, never hardcoded ──────────────────
# Run: export ANTHROPIC_API_KEY=sk-ant-...  then python3 proxy.py
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# ── On your Mac or online? ───────────────────────────────────────
# On your Mac nothing needs setting: it runs on 127.0.0.1:5050.
# Online hosts (Render, Railway, Fly…) set PORT for you; then it listens on 0.0.0.0.
PORT   = int(os.environ.get("PORT", "5050"))
HOST   = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
ONLINE = HOST not in ("127.0.0.1", "localhost")

# Which websites/apps may use this server, comma-separated, e.g.
#   ALLOWED_ORIGINS=https://davai.tech,https://app.davai.tech,capacitor://localhost,https://localhost
# Empty = any origin (OK on your Mac only).
ALLOWED_ORIGINS = {o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()}

# Online, every visitor arrives through the host's network, so read the student's real
# address from the headers the host adds (Cloudflare/Render set CF-Connecting-IP / True-Client-IP).
TRUST_PROXY = os.environ.get("TRUST_PROXY", "1" if ONLINE else "0") == "1"

# The real AI service. DAVAI_UPSTREAM can point to another Anthropic-compatible
# server (https only), or a local test server.
UPSTREAM = "https://api.anthropic.com"
_up = os.environ.get("DAVAI_UPSTREAM", "").strip().rstrip("/")
if _up.endswith("/v1"):
    _up = _up[:-3]
if _up.startswith("http://127.0.0.1:") or (_up.startswith("https://") and " " not in _up and "@" not in _up):
    UPSTREAM = _up

MAX_BODY = 3_000_000     # bytes; one resized photo plus text fits easily

# ── Accounts (Supabase) ──────────────────────────────────────────
# SUPABASE_URL      e.g. https://abcdefgh.supabase.co
# SUPABASE_ANON_KEY the public "anon" key (used to check logins)
# SUPABASE_SERVICE_KEY  the SECRET "service_role" key — only on the server, used to delete accounts
# REQUIRE_LOGIN     1 = the AI only answers signed-in students (on by default when SUPABASE_URL is set)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
if SUPABASE_URL and not (SUPABASE_URL.startswith("https://") or SUPABASE_URL.startswith("http://127.0.0.1:")):
    SUPABASE_URL = ""
SUPABASE_ANON_KEY    = os.environ.get("SUPABASE_ANON_KEY", "").strip()
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
REQUIRE_LOGIN = os.environ.get("REQUIRE_LOGIN", "1" if SUPABASE_URL else "0") == "1"

# The key is passed to curl from a private file (not on the command line, where other
# programs on the computer could read it in the process list).
_KEY_FILE = None
def _key_header_file():
    global _KEY_FILE
    if _KEY_FILE is None:
        fd, _KEY_FILE = tempfile.mkstemp(prefix="davai-", suffix=".hdr")
        os.chmod(_KEY_FILE, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"x-api-key: {API_KEY}\n")
        atexit.register(lambda: os.path.exists(_KEY_FILE) and os.remove(_KEY_FILE))
    return _KEY_FILE

_CURL   = shutil.which("curl")
_SSL_OK = ssl.create_default_context()   # full certificate checks (used only if curl is missing)

# ── Static file allowlist ────────────────────────────────────────
# Only serve these extensions. Everything else → 403.
ALLOWED_EXTENSIONS = {".html", ".css", ".js", ".json", ".png", ".svg", ".ico", ".webmanifest", ".jpg", ".jpeg", ".gif", ".webp", ".txt", ".mp4", ".webm"}
# Never serve anything under these paths regardless of extension
BLOCKED_PREFIXES = ("davao_20_5/server/", "data/", "uploads/", ".git/", "_unused/", "_backups/", "node_modules/", "docs/")
# Never serve a path that contains any of these folder names anywhere
# (e.g. davao_20_5/data/users.json holds every user's login token)
BLOCKED_SEGMENTS = {"data", "uploads", "server", "node_modules", "_unused", "_backups", "__pycache__"}

# ── AI request limits (protects your API budget) ─────────────────
ALLOWED_PATHS    = {"/v1/messages"}
ALLOWED_MODELS   = {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001", "claude-haiku-4-5", "claude-sonnet-4-6", "claude-sonnet-4-5"}
DEFAULT_MODEL    = "claude-haiku-5-5"
MAX_TOKENS_LIMIT = 2000
MAX_IMAGES       = 1                 # photos per request
MAX_IMAGE_B64    = 2_000_000         # about 1.5 MB photo after resizing in the app

# ── Plans: AI questions per day (rolling 24 hours) ────────────────
# Everyone is Free. A Pro / Pro Max code (made with make_pro_code.py) raises the limit.
PLAN_LIMITS = {"free": 20, "pro": 40, "promax": 60}
PLAN_NAMES  = {"free": "Free", "pro": "Pro", "promax": "Pro Max"}
# Secret used to sign Pro codes. Set it on the server and on the computer that makes codes.
PLAN_SECRET = os.environ.get("DAVAI_PLAN_SECRET", "").encode()

# ── Rate limiting & abuse protection ─────────────────────────────
RATE_LIMIT_PER_HOUR = int(os.environ.get("DAVAI_HOURLY", "20"))   # per device per hour (stops spamming, any plan)
# Overall per-IP limit (catches shared school networks / cleared app data)
RATE_LIMIT_IP_DAY   = 600  # per IP per day across all devices
# Max message length in characters
MAX_MSG_LENGTH      = 6000   # long enough for a pasted IELTS essay or a spoken answer

# In-memory counters — reset when proxy restarts
_dev_hour  = defaultdict(list)   # device_key -> [timestamps in last hour]
_dev_day   = defaultdict(list)   # device_key -> [timestamps today]
_ip_day    = defaultdict(list)   # ip         -> [timestamps today]

# ── Rate-limit snapshot for admin logs ──────────────────────────
def _rate_snapshot():
    now = time.time()
    rows = []
    with _rate_lock:
        for key, ts_list in _dev_day.items():
            valid = [t for t in ts_list if now - t < 86400]
            if not valid: continue
            rows.append({
                "key":      key,
                "count":    len(valid),
                "plan":     "free",
                "reset_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(min(valid) + 86400)),
            })
        for key, ts_list in _ip_day.items():
            valid = [t for t in ts_list if now - t < 86400]
            if not valid: continue
            rows.append({
                "key":      f"ip:{key}",
                "count":    len(valid),
                "plan":     "ip",
                "reset_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(min(valid) + 86400)),
            })
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows[:100]

import re as _re, threading as _threading, hmac as _hmac, hashlib as _hashlib, base64 as _b64, secrets as _secrets, math as _math
_rate_lock   = _threading.Lock()
_last_sweep  = [0.0]
_DEVICE_RE   = _re.compile(r"^[A-Za-z0-9-]{8,64}$")

def _sweep(now):
    """Forget devices/IPs with no requests in the last day (keeps memory small)."""
    if now - _last_sweep[0] < 600:
        return
    _last_sweep[0] = now
    day_ago = now - 86400
    for d in (_dev_hour, _dev_day, _ip_day):
        for k in [k for k, v in d.items() if not v or v[-1] < day_ago]:
            del d[k]

# ── Pro codes: DAVAI-<data>.<signature>, signed with PLAN_SECRET (cannot be faked) ──
def _b64e(b): return _b64.urlsafe_b64encode(b).decode().rstrip("=")
def _b64d(s): return _b64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

# ── Checking a student's login token with Supabase (cached for 5 minutes) ──
_user_cache = {}
_user_lock = _threading.Lock()

def _http(method, url, headers, body=None, timeout=15):
    """Small HTTPS helper. Secret headers go to curl through a private file, never the command line."""
    if _CURL:
        import subprocess
        fd, hp = tempfile.mkstemp(prefix="davai-h-")
        try:
            os.chmod(hp, 0o600)
            with os.fdopen(fd, "w") as f:
                for k, v in headers.items():
                    f.write(f"{k}: {v}\n")
            cmd = [_CURL, "-s", "-X", method, url, "-H", "@" + hp, "--max-time", str(timeout), "-w", "\n%{http_code}"]
            if body is not None:
                cmd += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
            r = subprocess.run(cmd, input=body, capture_output=True, timeout=timeout + 5)
            out = r.stdout.rsplit(b"\n", 1)
            return (int(out[1]) if len(out) == 2 and out[1].strip().isdigit() else 0), out[0]
        except Exception:
            return 0, b""
        finally:
            try: os.remove(hp)
            except OSError: pass
    req = urllib.request.Request(url, data=body, method=method, headers={**headers, **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_OK) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:
        return 0, b""

def verify_user(token):
    """Returns (user_id, email) if the token belongs to a signed-in student, else None."""
    if not SUPABASE_URL or not token or len(token) > 4096:
        return None
    key = _hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _user_lock:
        hit = _user_cache.get(key)
        if hit and hit[2] > now:
            return hit[0], hit[1]
    status, raw = _http("GET", SUPABASE_URL + "/auth/v1/user", {"apikey": SUPABASE_ANON_KEY, "Authorization": "Bearer " + token})
    if status != 200:
        return None
    try:
        j = json.loads(raw)
        uid = str(j.get("id", ""))[:64]
        if not uid:
            return None
    except Exception:
        return None
    with _user_lock:
        if len(_user_cache) > 5000:
            _user_cache.clear()
        _user_cache[key] = (uid, str(j.get("email", ""))[:200], now + 300)
    return uid, str(j.get("email", ""))

def forget_user(uid):
    with _user_lock:
        for k in [k for k, v in _user_cache.items() if v[0] == uid]:
            del _user_cache[k]

def make_code(plan, days):
    if plan not in PLAN_LIMITS or plan == "free":
        raise ValueError("plan must be pro or promax")
    if not PLAN_SECRET:
        raise ValueError("Set DAVAI_PLAN_SECRET first")
    payload = json.dumps({"p": plan, "e": int(time.time() + days * 86400), "i": _secrets.token_hex(4)},
                         separators=(",", ":")).encode()
    sig = _hmac.new(PLAN_SECRET, payload, _hashlib.sha256).digest()[:16]
    return "DAVAI-" + _b64e(payload) + "." + _b64e(sig)

def read_code(code):
    """Returns (plan, code_id, expires) for a valid, unexpired code, else ("free", None, None)."""
    try:
        code = (code or "").strip()
        if not PLAN_SECRET or not code.startswith("DAVAI-") or len(code) > 200:
            return "free", None, None
        data, sig = code[6:].split(".", 1)
        payload = _b64d(data)
        good = _hmac.new(PLAN_SECRET, payload, _hashlib.sha256).digest()[:16]
        if not _hmac.compare_digest(good, _b64d(sig)):
            return "free", None, None
        j = json.loads(payload)
        if j.get("p") not in PLAN_LIMITS or int(j.get("e", 0)) < time.time():
            return "free", None, None
        return j["p"], str(j.get("i", ""))[:16], int(j["e"])
    except Exception:
        return "free", None, None

def _quota_key(ip, device_id, code, uid=None):
    device_id = (device_id or "").strip()
    # Signed in: the limits follow the account (new devices or cleared data don't reset them)
    dev = "user:" + uid if uid else ("dev:" + device_id if _DEVICE_RE.match(device_id) else "ip:" + ip)
    plan, cid, exp = read_code(code)
    # A shared Pro code shares one daily allowance between everyone using it
    day_key = "code:" + cid if cid else dev
    return dev, day_key, plan, exp

def plan_info(ip, device_id="", code="", uid=None):
    """What the student has left, without using a question."""
    with _rate_lock:
        now = time.time()
        dev, day_key, plan, exp = _quota_key(ip, device_id, code, uid)
        used = len([t for t in _dev_day.get(day_key, []) if t > now - 86400])
        limit = PLAN_LIMITS[plan]
        return {"plan": plan, "name": PLAN_NAMES[plan], "limit": limit,
                "remaining": max(0, limit - used), "expires": exp}

def check_rate_limit(ip, device_id="", code="", uid=None):
    with _rate_lock:
        return _check_rate_limit(ip, device_id, code, uid)

def _check_rate_limit(ip, device_id="", code="", uid=None):
    """Returns (allowed, reason, info). Cleans up old timestamps."""
    now      = time.time()
    hour_ago = now - 3600
    day_ago  = now - 86400
    _sweep(now)
    dev, day_key, plan, exp = _quota_key(ip, device_id, code, uid)
    limit = PLAN_LIMITS[plan]

    _dev_hour[dev]     = [t for t in _dev_hour[dev] if t > hour_ago]
    _dev_day[day_key]  = [t for t in _dev_day[day_key] if t > day_ago]
    _ip_day[ip]        = [t for t in _ip_day[ip] if t > day_ago]
    info = {"plan": plan, "name": PLAN_NAMES[plan], "limit": limit,
            "remaining": max(0, limit - len(_dev_day[day_key])), "expires": exp, "_keys": (dev, day_key, ip, now)}

    if len(_dev_day[day_key]) >= limit:
        hours = max(1, _math.ceil((_dev_day[day_key][0] + 86400 - now) / 3600))
        info["daily_limit"] = True
        more = (f" Pro gives {PLAN_LIMITS['pro']} a day and Pro Max {PLAN_LIMITS['promax']}." if plan == "free"
                else f" Pro Max gives {PLAN_LIMITS['promax']} a day." if plan == "pro" else "")
        return False, (f"You've used all {limit} {PLAN_NAMES[plan]} questions for today. "
                       f"More in about {hours} hour{'s' if hours != 1 else ''}.{more}"), info
    if len(_dev_hour[dev]) >= RATE_LIMIT_PER_HOUR:
        return False, f"That's a lot of questions in one hour. Take a short break and try again soon.", info
    if len(_ip_day[ip]) >= RATE_LIMIT_IP_DAY:
        return False, "This network has reached DAVAI's daily limit. Try again tomorrow.", info

    _dev_hour[dev].append(now)
    _dev_day[day_key].append(now)
    _ip_day[ip].append(now)
    info["remaining"] = max(0, limit - len(_dev_day[day_key]))
    return True, "", info

def refund(info):
    """Give the question back if the AI service failed (not the student's fault)."""
    dev, day_key, ip, t = info.get("_keys", (None, None, None, None))
    with _rate_lock:
        for d, k in ((_dev_hour, dev), (_dev_day, day_key), (_ip_day, ip)):
            if k in d and t in d[k]:
                d[k].remove(t)

CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-DAVAI-Device, X-DAVAI-Plan",
    "Access-Control-Expose-Headers": "X-DAVAI-Plan, X-DAVAI-Limit, X-DAVAI-Remaining",
    "Access-Control-Max-Age": "600",
}

# Sent with every GET (HTML page) response so YouTube iframes work
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.gstatic.com https://cdn.jsdelivr.net; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https://img.youtube.com https://i.ytimg.com https://*.googleapis.com https://*.ytimg.com https://upload.wikimedia.org https://cdn.jsdelivr.net; "
        "frame-src https://www.youtube.com https://youtube.com https://www.youtube-nocookie.com https://youtube-nocookie.com; "
        "connect-src 'self' http://localhost:5050 https://api.anthropic.com https://formspree.io; "
        "media-src 'self' blob: https:; "
    ),
}


# ── School calendar (ManageBac, Toddle, Canvas … calendar links) ─────
# The student pastes their private calendar link. The server opens it, reads only the
# deadlines and sends those back. It never stores or prints the link, never returns the
# raw file, and only connects to public internet addresses (no private networks).
import socket as _socket
CAL_MAX_BYTES = 2_000_000
CAL_PER_HOUR  = int(os.environ.get("DAVAI_CAL_HOURLY", "30"))
CAL_ALLOW_LOCAL = os.environ.get("DAVAI_CAL_TEST", "") == "1"     # tests only: allows http://127.0.0.1
_cal_hits = defaultdict(list)
_cal_lock = _threading.Lock()

class CalError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg); self.status = status

CAL_BAD_LINK = "That doesn't look like a calendar link. Copy the whole link (it starts with https:// or webcal://) and try again."

def _cal_clean(raw):
    u = (raw or "").strip()
    if len(u) > 2048 or not u:
        raise CalError(CAL_BAD_LINK)
    if u.lower().startswith("webcal://"):
        u = "https://" + u[9:]
    try:
        p = urllib.parse.urlsplit(u)
        port = p.port
    except ValueError:
        raise CalError(CAL_BAD_LINK)
    host = (p.hostname or "").lower()
    if CAL_ALLOW_LOCAL and p.scheme == "http" and host == "127.0.0.1":
        return u, host, port, True
    if p.scheme != "https" or not host or p.username or p.password or port not in (None, 443):
        raise CalError(CAL_BAD_LINK)
    return u, host, 443, False

def _cal_public_ip(host):
    """Resolve the name and make sure EVERY address is on the public internet."""
    try:
        infos = _socket.getaddrinfo(host, 443, proto=_socket.IPPROTO_TCP)
    except (_socket.gaierror, UnicodeError):
        raise CalError("Couldn't find that website. Check the link and try again.")
    ips = sorted({i[4][0] for i in infos})
    if not ips:
        raise CalError("Couldn't find that website. Check the link and try again.")
    for ip in ips:
        a = ipaddress.ip_address(ip.split("%")[0])
        if not a.is_global or a.is_multicast:
            raise CalError(CAL_BAD_LINK)
    return ips[0]

def _cal_get(url, host, local):
    """One request, no automatic redirects. Returns (status, body, location)."""
    if local or not _CURL:
        if not local:
            _cal_public_ip(host)
        class _NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k): return None
        op = urllib.request.build_opener(_NoRedir, urllib.request.HTTPSHandler(context=_SSL_OK))
        req = urllib.request.Request(url, headers={"User-Agent": "DAVAI-Calendar/1.0", "Accept": "text/calendar, */*"})
        try:
            with op.open(req, timeout=12) as r:
                return r.status, r.read(CAL_MAX_BYTES + 1), ""
        except urllib.error.HTTPError as e:
            return e.code, b"", e.headers.get("Location", "")
        except Exception:
            return 0, b"", ""
    import subprocess
    ip = _cal_public_ip(host)
    pin = f"{host}:443:{('[' + ip + ']') if ':' in ip else ip}"   # connect to the address we just checked
    cmd = [_CURL, "-s", "--proto", "=https", "--max-time", "12", "--max-filesize", str(CAL_MAX_BYTES),
           "--resolve", pin, "-A", "DAVAI-Calendar/1.0", "-H", "Accept: text/calendar, */*",
           "-w", "\n%{http_code}\n%{redirect_url}", "--", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=20)
    except Exception:
        return 0, b"", ""
    parts = r.stdout.rsplit(b"\n", 2)
    if len(parts) != 3 or not parts[1].strip().isdigit():
        return 0, b"", ""
    return int(parts[1]), parts[0], parts[2].decode("utf-8", "replace").strip()

def fetch_calendar(raw_url):
    url, host, _, local = _cal_clean(raw_url)
    for _ in range(4):                       # follow up to 3 redirects, checking each new address
        status, body, loc = _cal_get(url, host, local)
        if status in (301, 302, 303, 307, 308) and loc:
            url, host, _, local = _cal_clean(urllib.parse.urljoin(url, loc))
            continue
        break
    else:
        raise CalError("That calendar link keeps redirecting. Copy a fresh link and try again.")
    if status == 0:
        raise CalError("Couldn't reach your school's calendar. Try again in a minute.", 502)
    if status in (401, 403, 404, 410):
        raise CalError("Your school calendar didn't open. The link may be old. Copy a new link from your school platform.", 400)
    if status != 200:
        raise CalError("Your school's calendar isn't answering right now. Try again later.", 502)
    if len(body) > CAL_MAX_BYTES:
        raise CalError("That calendar is too big to read.", 400)
    text = body.decode("utf-8", "replace")
    if "BEGIN:VCALENDAR" not in text[:4000].upper():
        raise CalError("That link isn't a calendar. Make sure you copied the calendar (subscribe) link.", 400)
    return host, parse_ics(text)

_ICS_UNESC = [("\\n", "\n"), ("\\N", "\n"), ("\\,", ","), ("\\;", ";"), ("\\\\", "\\")]
def _ics_text(v):
    for a, b in _ICS_UNESC: v = v.replace(a, b)
    v = _re.sub(r"<[^>]{0,200}>", " ", v)               # some platforms put HTML in descriptions
    return _re.sub(r"[ \t]+", " ", v).strip()

def _ics_time(params, v):
    """Returns (iso, all_day, date 'YYYY-MM-DD' approx) or None."""
    v = v.strip()
    m = _re.fullmatch(r"(\d{4})(\d{2})(\d{2})", v)
    if m or "VALUE=DATE" in params.upper() and len(v) == 8:
        if not m: return None
        d = f"{m[1]}-{m[2]}-{m[3]}"
        return d, True, d
    m = _re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)", v)
    if not m: return None
    iso = f"{m[1]}-{m[2]}-{m[3]}T{m[4]}:{m[5]}:{m[6]}" + ("Z" if m[7] else "")
    return iso, False, f"{m[1]}-{m[2]}-{m[3]}"

def parse_ics(text, now=None):
    now = now or time.time()
    lo = time.strftime("%Y-%m-%d", time.gmtime(now - 21 * 86400))
    hi = time.strftime("%Y-%m-%d", time.gmtime(now + 200 * 86400))
    text = _re.sub(r"\r?\n[ \t]", "", text)              # unfold long lines
    out, cur = [], None
    for line in text.splitlines():
        u = line.upper()
        if u in ("BEGIN:VEVENT", "BEGIN:VTODO"):
            cur = {"_kind": u[6:]}; continue
        if u in ("END:VEVENT", "END:VTODO"):
            ev, cur = cur, None
            if not ev: continue
            start = ev.get("DUE") if ev["_kind"] == "VTODO" else ev.get("DTSTART")
            if not start or ev.get("STATUS", ("", ""))[1].upper() == "CANCELLED": continue
            t = _ics_time(*start)
            if not t or not (lo <= t[2] <= hi): continue
            end = ev.get("DTEND")
            if t[1] and end:
                e = _ics_time(*end)
                if e and e[1]:                           # all-day event lasting several days = holiday / trip
                    try:
                        days = (time.mktime(time.strptime(e[2], "%Y-%m-%d")) - time.mktime(time.strptime(t[2], "%Y-%m-%d"))) / 86400
                        if days > 1.5: continue
                    except ValueError:
                        pass
            title = _ics_text(ev.get("SUMMARY", ("", ""))[1])[:200]
            if not title: continue
            link = ev.get("URL", ("", ""))[1].strip()
            out.append({
                "uid": (ev.get("UID", ("", ""))[1].strip() or (title + t[0]))[:300],
                "title": title, "start": t[0], "allDay": t[1],
                "desc": _ics_text(ev.get("DESCRIPTION", ("", ""))[1])[:400],
                "cat": _ics_text(ev.get("CATEGORIES", ("", ""))[1])[:80],
                "url": link[:500] if link.lower().startswith("https://") else "",
            })
            if len(out) >= 400: break
            continue
        if cur is None or ":" not in line: continue
        k, v = line.split(":", 1)
        name, _, params = k.partition(";")
        name = name.upper()
        if name in ("SUMMARY", "DTSTART", "DTEND", "DUE", "UID", "DESCRIPTION", "CATEGORIES", "URL", "STATUS") and name not in cur:
            cur[name] = (params, v)
    out.sort(key=lambda e: e["start"])
    return out

def cal_rate_ok(key):
    now = time.time()
    with _cal_lock:
        h = _cal_hits[key] = [t for t in _cal_hits[key] if t > now - 3600]
        if len(h) >= CAL_PER_HOUR: return False
        h.append(now)
        return True

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"   # each answer ends when the connection closes (simple and works for streaming)

    def log_message(self, fmt, *args):
        print(f"[proxy] {self.address_string()} — {fmt % args}")

    def origin_ok(self):
        if not ALLOWED_ORIGINS:
            return True
        return self.headers.get("Origin", "").rstrip("/") in ALLOWED_ORIGINS

    def send_cors(self):
        origin = self.headers.get("Origin", "").rstrip("/")
        if not ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        info = getattr(self, "_plan", None)
        if info:
            self.send_header("X-DAVAI-Plan", info["plan"])
            self.send_header("X-DAVAI-Limit", str(info["limit"]))
            self.send_header("X-DAVAI-Remaining", str(info["remaining"]))

    def client_ip(self):
        if TRUST_PROXY:
            cands = [self.headers.get("CF-Connecting-IP", ""), self.headers.get("True-Client-IP", ""),
                     self.headers.get("X-Forwarded-For", "").split(",")[0]]
            for c in cands:
                c = c.strip()
                try:
                    ipaddress.ip_address(c)
                    return c
                except ValueError:
                    pass
        return self.client_address[0]

    def bearer(self):
        a = self.headers.get("Authorization", "")
        return a[7:].strip() if a[:7].lower() == "bearer " else ""

    def _account_delete(self):
        """Apple requires in-app account deletion. Only the account owner (valid token) can do it."""
        if not self.origin_ok():
            self._forbidden(); return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if 0 < n <= 1024: self.rfile.read(n)
        except ValueError:
            pass
        u = verify_user(self.bearer())
        if not u:
            self._json(401, {"error": "Please log in again, then try deleting your account.", "login_required": True}); return
        if not SUPABASE_SERVICE_KEY:
            self._json(503, {"error": "Account deletion isn't set up on the server yet. Please email davai.salahov@gmail.com."}); return
        status, _ = _http("DELETE", SUPABASE_URL + "/auth/v1/admin/users/" + urllib.parse.quote(u[0]),
                          {"apikey": SUPABASE_SERVICE_KEY, "Authorization": "Bearer " + SUPABASE_SERVICE_KEY})
        if status in (200, 204):
            forget_user(u[0])
            print(f"[proxy] account deleted {u[0]}")
            self._json(200, {"deleted": True}); return
        self._json(502, {"error": "Could not delete the account right now. Please try again."})

    def _school_calendar(self):
        """Read a student's school calendar link and return only the deadlines."""
        if not self.origin_ok():
            self._forbidden(); return
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if not 0 < n <= 4096:
            self._json(400, {"error": "Invalid request."}); return
        try:
            url = json.loads(self.rfile.read(n) or b"{}").get("url", "")
        except (ValueError, AttributeError):
            self._json(400, {"error": "Invalid request."}); return
        uid = None
        if REQUIRE_LOGIN:
            u = verify_user(self.bearer())
            if not u:
                self._json(401, {"error": "Please log in to connect your school.", "login_required": True}); return
            uid = u[0]
        key = "user:" + uid if uid else "dev:" + (self.headers.get("X-DAVAI-Device", "")[:64] or self.client_ip())
        if not cal_rate_ok(key) or not cal_rate_ok("ip:" + self.client_ip()):
            self._json(429, {"error": "Too many calendar updates. Try again in an hour."}); return
        try:
            host, events = fetch_calendar(url if isinstance(url, str) else "")
        except CalError as e:
            self._json(e.status, {"error": str(e)}); return
        print(f"[calendar] {len(events)} items from {host}")      # never print the link itself
        self._json(200, {"events": events, "count": len(events), "host": host})

    def _json(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_cors()
        self.end_headers()
        self.wfile.write(data)

    # ── PATCH / DELETE → admin API only ─────────────────────────────
    def do_PATCH(self):
        path = self.path.split("?")[0]
        if path.startswith("/admin/api/"):
            return self._admin_api("PATCH")
        self._forbidden()

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path.startswith("/admin/api/"):
            return self._admin_api("DELETE")
        self._forbidden()

    # ── Admin API dispatcher ─────────────────────────────────────────
    def _admin_api(self, method):
        path  = self.path.split("?")[0]
        token = self.headers.get("X-Admin-Token","")
        try:
            length = int(self.headers.get("Content-Length","0") or "0")
            raw    = self.rfile.read(length) if length > 0 else b"{}"
            body   = json.loads(raw) if raw.strip() else {}
        except Exception:
            body = {}
        status, resp = _admin.handle(method, path, body, token)
        self._json(status, resp)

    # ── Redirect helper ──────────────────────────────────────────────
    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    # ── Serve admin static file ──────────────────────────────────────
    def _serve_admin_file(self, filename):
        fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin", filename)
        if not os.path.isfile(fp):
            self.send_response(404); self.end_headers()
            self.wfile.write(b"Not found"); return
        with open(fp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ── OPTIONS preflight ────────────────────────────────────────────
    def do_OPTIONS(self):
        if not self.origin_ok():
            self._forbidden(); return
        self.send_response(204)
        self.send_cors()
        self.end_headers()

    # ── POST → forward to upstream API via curl (HTTP/2 support) ────
    def do_POST(self):
        import subprocess, json as _json
        ip = self.client_ip()

        # ── Admin API (POST/PATCH/DELETE) ────────────────────────────
        if self.path.split("?")[0].startswith("/admin/api/"):
            return self._admin_api("POST")

        if self.path.split("?")[0] == "/v1/account/delete":
            return self._account_delete()
        if self.path.split("?")[0] == "/v1/school-calendar":
            return self._school_calendar()

        # ── Only forward the Messages endpoint ──────────────────────
        if self.path.split("?")[0] not in ALLOWED_PATHS:
            self._forbidden(); return

        # ── Only DAVAI's own website/app may use the AI budget ──────
        if not self.origin_ok():
            print(f"[proxy] ⛔ blocked origin {self.headers.get('Origin', '(none)')} ip={ip}")
            self._forbidden(); return

        if not API_KEY:
            self._json(503, {"error": "The AI is not set up on the server yet (no API key)."}); return

        # ── Only signed-in students can use the AI ─────────────────
        uid = None
        if REQUIRE_LOGIN:
            u = verify_user(self.bearer())
            if not u:
                self._json(401, {"error": "Please log in to use the AI.", "login_required": True}); return
            uid = u[0]

        # ── Size check before reading anything ─────────────────────
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0:
            self._json(400, {"error": "Invalid request."}); return
        if length > MAX_BODY:
            self._json(413, {"error": "That request is too big. Try a smaller photo or shorter text."}); return

        # ── Rate limit check ────────────────────────────────────────
        device_id = self.headers.get("X-DAVAI-Device", "").strip()[:64]
        code = self.headers.get("X-DAVAI-Plan", "")[:200]
        allowed, reason, info = check_rate_limit(ip, device_id, code, uid)
        self._plan = info
        if not allowed:
            self._json(429, {"error": reason, "limit_reached": True,
                             "upgrade": bool(info.get("daily_limit")) and info["plan"] != "promax"})
            print(f"[proxy] ⛔ Limited {ip} ({info['plan']}): {reason}")
            return

        body   = self.rfile.read(length)

        # ── Message length check ─────────────────────────────────────
        try:
            parsed_body = _json.loads(body)
            messages = parsed_body.get("messages", [])
            if not isinstance(messages, list) or len(messages) > 40:
                raise ValueError("bad messages")
            images = 0
            for msg in messages:
                content = msg.get("content", "")
                text_len = len(content) if isinstance(content, str) else 0
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            raise ValueError("bad block")
                        if block.get("type") == "text":
                            text_len += len(block.get("text", ""))
                        elif block.get("type") == "image":
                            images += 1
                            data = (block.get("source") or {}).get("data", "")
                            if len(data) > MAX_IMAGE_B64:
                                raise ValueError("Photo too large")
                        else:
                            raise ValueError("bad block type")
                if images > MAX_IMAGES:
                    raise ValueError("Too many photos")
                if text_len > MAX_MSG_LENGTH:
                    refund(info); self._plan = None
                    err = _json.dumps({"error": f"Message too long — maximum {MAX_MSG_LENGTH} characters."}).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_cors()
                    self.end_headers()
                    self.wfile.write(err)
                    return
            # ── Budget protection: only cheap models, capped reply length ──
            if parsed_body.get("model") not in ALLOWED_MODELS:
                parsed_body["model"] = DEFAULT_MODEL
            try:
                mt = int(parsed_body.get("max_tokens", 1024))
            except (TypeError, ValueError):
                mt = 1024
            parsed_body["max_tokens"] = max(1, min(mt, MAX_TOKENS_LIMIT))
            body = _json.dumps(parsed_body).encode()
        except Exception as e:
            refund(info); self._plan = None
            msg = str(e) if str(e).startswith(("Photo", "Too many")) else "Invalid request."
            err = _json.dumps({"error": msg}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_cors()
            self.end_headers()
            self.wfile.write(err)
            return

        upstream_url = UPSTREAM + self.path.split("?")[0]
        curl_cmd = [
            _CURL or "curl", "-s", "-N",
            *(["--http2"] if upstream_url.startswith("https://") else []),
            "--max-time", "120",
            "-X", "POST", upstream_url,
            "-H", "Content-Type: application/json",
            "-H", "@" + _key_header_file(),
            "-H", "anthropic-version: 2023-06-01",
            "--data-binary", "@-",
        ]
        if not _CURL:
            return self._forward_urllib(upstream_url, body, bool(parsed_body.get("stream")))
        print(f"[proxy] → {upstream_url} model={parsed_body.get('model')} stream={bool(parsed_body.get('stream'))} ip={ip}")

        # ── Streaming: pass the answer through piece by piece ──────────
        if parsed_body.get("stream"):
            try:
                proc = subprocess.Popen(curl_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                proc.stdin.write(body); proc.stdin.close()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_cors()
                self.end_headers()
                total = 0
                while True:
                    chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                    if not chunk:
                        break
                    total += len(chunk)
                    try:
                        self.wfile.write(chunk); self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        proc.kill(); break   # the student closed the page
                proc.wait(timeout=5)
                print(f"[proxy] ← streamed {total} bytes")
            except Exception as e:
                print(f"[proxy] stream error: {e}")
            return

        # ── Normal (non-streaming) request ─────────────────────────────
        try:
            result = subprocess.run(curl_cmd, input=body, capture_output=True, timeout=90)
            resp_body = result.stdout
            status = 200
            try:
                parsed = _json.loads(resp_body)
                if parsed.get("type") == "error":
                    etype = (parsed.get("error") or {}).get("type", "")
                    emsg = (parsed.get("error") or {}).get("message", "The AI service returned an error.")
                    status = 401 if "auth" in etype else 429 if etype in ("rate_limit_error", "overloaded_error") else 502
                    print(f"[proxy] upstream error {etype}: {emsg}")
                    friendly = {401: "The AI key is not valid. Check ANTHROPIC_API_KEY on the server.",
                                429: "The AI is busy right now. Please try again in a minute."}.get(status, emsg)
                    resp_body = _json.dumps({"error": friendly}).encode()
            except Exception:
                status = 502
                resp_body = b'{"error":"No answer from the AI service. Check the internet connection of the server."}'

            if status != 200:
                refund(info); self._plan = None
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_cors()
            self.end_headers()
            self.wfile.write(resp_body)
            print(f"[proxy] ← {len(resp_body)} bytes (status {status})")

        except Exception as e:
            refund(info); self._plan = None
            err = _json.dumps({"error": "The AI took too long to answer. Please try again."}).encode()
            print(f"[proxy] error: {e}")
            self.send_response(504)
            self.send_header("Content-Type", "application/json")
            self.send_cors()
            self.end_headers()
            self.wfile.write(err)

    def _forward_urllib(self, url, body, stream):
        """Same as the curl path, for servers without curl. Uses full SSL checks."""
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "x-api-key": API_KEY, "anthropic-version": "2023-06-01"})
        try:
            resp = urllib.request.urlopen(req, timeout=120, context=_SSL_OK)
            status = resp.status
        except urllib.error.HTTPError as e:
            resp, status = e, e.code
        except Exception as e:
            print(f"[proxy] upstream error: {e}")
            refund(self._plan or {}); self._plan = None
            self._json(504, {"error": "No answer from the AI service. Please try again."}); return
        if stream and status == 200:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_cors()
            self.end_headers()
            read = getattr(resp, "read1", None) or resp.read
            while True:
                chunk = read(4096)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            return
        raw = resp.read()
        try:
            parsed = json.loads(raw)
            if parsed.get("type") == "error":
                etype = (parsed.get("error") or {}).get("type", "")
                friendly = ("The AI key is not valid. Check ANTHROPIC_API_KEY on the server." if "auth" in etype else
                            "The AI is busy right now. Please try again in a minute." if etype in ("rate_limit_error", "overloaded_error") else
                            (parsed.get("error") or {}).get("message", "The AI service returned an error."))
                refund(self._plan or {}); self._plan = None
                self._json(401 if "auth" in etype else 429 if "rate" in etype or "overloaded" in etype else 502, {"error": friendly}); return
        except Exception:
            refund(self._plan or {}); self._plan = None
            self._json(502, {"error": "No answer from the AI service."}); return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_cors()
        self.end_headers()
        self.wfile.write(raw)

    def _forbidden(self):
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.send_cors()
        self.end_headers()
        self.wfile.write(b'{"error":"Forbidden"}')

    # ── GET → serve static files ─────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0]

        # ── Admin setup-status (public GET) ─────────────────────────
        if path == "/admin/api/setup-status":
            self._json(200, {"done": _admin.is_setup_done()}); return

        # ── Site config (public, index.html applies dynamically) ─────
        if path == "/site-config.json":
            self._json(200, _admin.site_config_read()); return

        # ── AI config (Node backend reads this internally) ────────────
        if path == "/admin/api/ai-config/internal":
            # Only allow local connections
            if self.client_ip() not in ("127.0.0.1", "::1", "localhost"):
                self._forbidden(); return
            self._json(200, _admin.ai_config_read()); return

        # ── Admin API (GET) ──────────────────────────────────────────
        if path == "/admin/api/logs":
            token = self.headers.get("X-Admin-Token","")
            admin = _admin.verify_session(token)
            if not admin:
                self._json(401, {"message": "Unauthorized"}); return
            self._json(200, _rate_snapshot()); return

        if path.startswith("/admin/api/"):
            token = self.headers.get("X-Admin-Token","")
            # parse query string status filter into body-like dict
            qs    = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            body  = {k: v[0] for k,v in qs.items()}
            # payments with filter — pass status in body
            if path == "/admin/api/payments" and "status" in body:
                status = body.get("status","")
                rows = [dict(r) for r in _admin.db().execute(
                    "SELECT * FROM payments" + (" WHERE status=?" if status else "") + " ORDER BY created_at DESC",
                    (status,) if status else ()
                ).fetchall()]
                self._json(200, rows); return
            status, resp = _admin.handle("GET", path, body, token)
            self._json(status, resp); return

        # ── Admin UI pages ───────────────────────────────────────────
        if path in ("/admin", "/admin/"):
            # check setup
            if not _admin.is_setup_done():
                self._redirect("/admin/setup"); return
            self._serve_admin_file("index.html"); return
        if path == "/admin/setup":
            self._serve_admin_file("setup.html"); return
        if path == "/admin/login":
            self._serve_admin_file("login.html"); return

        if path == "/health":
            self._json(200, {"ok": True, "ai": bool(API_KEY)}); return
        if path == "/v1/plan":
            if not self.origin_ok():
                self._forbidden(); return
            u = verify_user(self.bearer()) if self.bearer() else None
            info = plan_info(self.client_ip(), self.headers.get("X-DAVAI-Device", "")[:64], self.headers.get("X-DAVAI-Plan", "")[:200], u[0] if u else None)
            self._json(200, info); return
        if path == "/":
            path = "/index.html"
        elif path in ("/app", "/app/"):
            path = "/davao_20_5/app.html"
        elif path.startswith("/css/") or path.startswith("/js/") or path.startswith("/assets/"):
            # If requested without prefix (e.g. from /app), check if it exists in davao_20_5
            candidate = os.path.join(STATIC_DIR, "davao_20_5", path.lstrip("/"))
            if os.path.isfile(candidate):
                path = "/davao_20_5/" + path.lstrip("/")
        elif path in ("/podcasts", "/podcasts/"):
            path = "/podcasts.html"
        elif path in ("/privacy", "/privacy/"):
            path = "/privacy.html"

        norm = path.lstrip("/")

        # Block dotfiles (.env, .git, .htaccess …)
        parts = norm.replace("\\", "/").split("/")
        if any(p.startswith(".") for p in parts if p):
            self._forbidden(); return

        # Block blocked path prefixes
        norm_lower = norm.lower()
        if any(norm_lower.startswith(bp) for bp in BLOCKED_PREFIXES):
            self._forbidden(); return
        if any(p.lower() in BLOCKED_SEGMENTS for p in parts):
            self._forbidden(); return

        # Resolve real path and ensure it stays inside STATIC_DIR (blocks ../ tricks)
        file_path = os.path.realpath(os.path.join(STATIC_DIR, norm))
        if not file_path.startswith(os.path.realpath(STATIC_DIR) + os.sep) and \
           file_path != os.path.realpath(STATIC_DIR):
            self._forbidden(); return

        # Allowlist: only serve permitted extensions
        # (a missing page falls through to the friendly 404 below)
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in ALLOWED_EXTENSIONS and os.path.isfile(file_path):
            self._forbidden(); return

        if not os.path.isfile(file_path) or ext not in ALLOWED_EXTENSIONS:
            # Serve branded 404 page
            not_found = os.path.join(STATIC_DIR, "404.html")
            if os.path.isfile(not_found):
                with open(not_found, "rb") as f:
                    data = f.read()
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_cors()
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
            return

        ext = os.path.splitext(file_path)[1]
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css":  "text/css",
            ".js":   "application/javascript",
            ".ico":  "image/x-icon",
            ".png":  "image/png",
            ".svg":  "image/svg+xml",
            ".json": "application/json",
            ".webmanifest": "application/manifest+json",
            ".mp4":  "video/mp4",
            ".webm": "video/webm",
        }.get(ext, "application/octet-stream")

        with open(file_path, "rb") as f:
            data = f.read()

        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        if ONLINE:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_cors()
        # Send CSP headers for HTML pages so YouTube iframes are allowed
        if mime.startswith("text/html"):
            for k, v in PAGE_HEADERS.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer((HOST, PORT), ProxyHandler)
    print(f"DAVAI proxy + static server running → http://{'localhost' if not ONLINE else HOST}:{PORT}")
    if not API_KEY:
        print("⚠️  No ANTHROPIC_API_KEY set: the website works, the AI answers with an error.")
    if ONLINE and not ALLOWED_ORIGINS:
        print("⚠️  ONLINE without ALLOWED_ORIGINS: any website could use your AI budget. Set ALLOWED_ORIGINS!")
    if not _CURL:
        print("ℹ️  curl not found: using Python's built-in HTTPS client.")
    print("AI service: " + UPSTREAM)
    print("Accounts: " + (f"login required ({SUPABASE_URL})" if REQUIRE_LOGIN and SUPABASE_URL else "off (no SUPABASE_URL)")
          + ("" if not SUPABASE_URL or SUPABASE_SERVICE_KEY else "  ⚠️ no SUPABASE_SERVICE_KEY: account deletion won't work"))
    print(f"Plans: Free {PLAN_LIMITS['free']}, Pro {PLAN_LIMITS['pro']}, Pro Max {PLAN_LIMITS['promax']} questions a day."
          + ("" if PLAN_SECRET else "  (No DAVAI_PLAN_SECRET: Pro codes are switched off.)"))
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")