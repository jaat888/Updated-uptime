#!/usr/bin/env python3
"""Render free web service: Chromium keepalive for blitz.cloud apps + dashboard.

Dashboard: "/" (password), apps add/delete, 3-din ka log. "/ping" public (uptime bot ke liye).
Cookies har app ke liye disk par save hoti hain aur purani apne-aap delete.
"""
from collections import deque
import gzip, os, re, sys, time, json, random, threading, urllib.request, html, hmac, base64, hashlib, secrets, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- config (env)
def env_int(k, d):
    try:
        return int(os.environ.get(k, d))
    except ValueError:
        return d

SEED = [u.strip() for u in os.environ.get("APP_URLS", "").replace("\n", ",").split(",") if u.strip()]
EVERY = env_int("EVERY_MIN", 3) * 60          # rounds ke beech gap
DWELL = env_int("DWELL_SEC", 45)              # jaagne ke baad page par kitni der ruke
WAKE_MAX = env_int("WAKE_MAX", 150)           # ek app ko jagane ka max wait
ENGINE_WANT = os.environ.get("BROWSER_ENGINE", "chromium").strip().lower()   # "chromium" ya "obscura"
OBSCURA_BIN = os.environ.get("OBSCURA_PATH", "/usr/local/bin/obscura")
OBSCURA_PORT = env_int("OBSCURA_PORT", 9222)
OBSCURA_STEALTH = env_int("OBSCURA_STEALTH", 1) != 0
JS_ON = env_int("JS_ENABLED", 1) != 0           # 0 daalo to Chrome me JavaScript band (RAM test)
WAKE_RELOAD = env_int("WAKE_RELOAD_SEC", 45)    # wake page khud na khule to itne sec baad hi reload (5 sec wala reload band)
# ---- insaan-jaisa mode (HUMAN_MODE=0 daalo to purana tareeka: har EVERY_MIN par sab apps)
HUMAN = env_int("HUMAN_MODE", 1) != 0
VISITS_MIN = max(1, env_int("VISITS_MIN_30", 2))      # ek app par 30 min me kam se kam itni visit
VISITS_MAX = max(VISITS_MIN, env_int("VISITS_MAX_30", 5))   # aur zyada se zyada itni (har baar random)
VISITS_30 = max(1, env_int("VISITS_PER_30MIN", (VISITS_MIN + VISITS_MAX) // 2))   # sirf dikhane/shuruaat ke liye
GAP_JIT = max(0, min(90, env_int("GAP_JITTER_PCT", 70)))   # gap kitna idhar-udhar (%)
DWELL_MIN = max(3, env_int("DWELL_MIN_SEC", 10))       # page par minimum rukna
DWELL_MAX = max(DWELL_MIN + 1, env_int("DWELL_MAX_SEC", 120))
INPAGE = env_int("INPAGE_REQ", 1) != 0                # page ke andar se same-site request
INPAGE_MAX = max(1, env_int("INPAGE_MAX", 6))         # ek visit me max itni
INPAGE_PATHS = [x.strip() for x in os.environ.get("INPAGE_PATHS", "/").split(",") if x.strip().startswith("/")] or ["/"]
RETRY_MIN, RETRY_MAX = 60, 180                        # wake fail ho to itne sec baad dobara
LOG_DAYS = env_int("LOG_DAYS", 3)
LOG_VIEW_HOURS = max(1, env_int("LOG_VIEW_HOURS", 8))     # dashboard par itne ghante ka log; poora log /log.txt (download) me
LOG_VIEW_MAX = max(20, env_int("LOG_VIEW_MAX", 150))      # dashboard par max itni entry
WAKE_ERR_MAX = max(1, env_int("WAKE_ERR_MAX", 2))         # proxy se itni baar lagatar network error to ye proxy chhod do (naya proxy lagega)
COOKIE_DAYS = env_int("COOKIE_DAYS", 3)
MEM_LIMIT = env_int("MEM_LIMIT_MB", 500)        # itni memory (MB) par browser restart hoga (visit ke baad)
MEM_VISIT_LIMIT = env_int("MEM_VISIT_LIMIT_MB", MEM_LIMIT + 10)   # visit ke BEECH me itne par hi visit jaldi khatam (pehle 500 tha)
MAX_HOURS = env_int("BROWSER_MAX_HOURS", 12)    # itne ghante baad browser restart (safety)
MAX_LOG_LINES = 20000
MAX_STATE_BYTES = 300 * 1024
COOKIE_MAX_PER_APP = max(1, env_int("COOKIE_MAX_PER_APP", 60))   # ek app ki itni proxy-wali cookie files tak
MAX_APPS = env_int("MAX_APPS", 100)
TZ_MIN = env_int("TZ_OFFSET_MIN", 330)        # IST
SUFFIXES = [s.strip().lower() for s in os.environ.get("ALLOWED_SUFFIXES", "blitz.cloud").split(",") if s.strip()]
DATA = os.environ.get("DATA_DIR", "/tmp/keepalive-data")
STATE_DIR = os.path.join(DATA, "state")
APPS_FILE = os.path.join(DATA, "apps.json")
LOG_FILE = os.path.join(DATA, "log.jsonl")
CHROME = os.environ.get("CHROME_PATH", "/usr/bin/chromium")
PORT = env_int("PORT", 10000)
SELF = os.environ.get("RENDER_EXTERNAL_URL", "")
PROXY_URL = os.environ.get("PROXY_URL", "").strip()   # env se default
PROXY_FILE = os.path.join(DATA, "proxy.txt")           # dashboard se set hoga yahan
# Headed Chrome sirf tab jab DISPLAY (Xvfb) ho; warna headless. HEADED=0 se zabardasti headless.
HEADED = bool(os.environ.get("DISPLAY")) and env_int("HEADED", 1) != 0
FP_SEED = env_int("FP_SEED", 0x5EED1234)               # canvas/audio noise ka seed (stable rahega)
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "")

LOCK = threading.RLock()
TRIGGER = threading.Event()
RESTART = {}   # {"why": "..."} -> dusre thread (dashboard) sirf flag rakhta hai, browser main loop restart karta hai
STATUS = {"started": time.time(), "next": None, "running": False, "b_started": None, "restarts": 0,
          "last_restart": "-", "mem_now": 0.0, "mem_peak": 0.0, "engine": "chromium",
          "chrome_full": "124.0.6367.60"}   # asli Chromium ka version (launch par update hota hai)
OBS = {"proc": None, "failed": False}
PW = None
BROWSER = None

os.makedirs(STATE_DIR, exist_ok=True)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def fmt_t(t):
    return time.strftime("%d %b %H:%M:%S", time.gmtime(t + TZ_MIN * 60))


# ---------------------------------------------------------------- apps list
def norm_url(u):
    """URL saaf karo. Sirf allowed domain (default blitz.cloud). Galat ho to None."""
    u = (u or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "https://" + u
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return None
    h = p.hostname.lower()
    if not any(h == s or h.endswith("." + s) for s in SUFFIXES):
        return None
    try:
        port = p.port
    except ValueError:
        return None
    netloc = h + (f":{port}" if port else "")
    return f"{p.scheme}://{netloc}{p.path or '/'}" + (f"?{p.query}" if p.query else "")


def load_apps():
    with LOCK:
        try:
            with open(APPS_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                return [x for x in data if isinstance(x, str)]
        except (OSError, ValueError):
            pass
        apps = []
        for s in SEED:
            n = norm_url(s)
            if n and n not in apps:
                apps.append(n)
        save_apps(apps)
        return apps


def save_apps(apps):
    with LOCK:
        tmp = APPS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(apps, f)
        os.replace(tmp, APPS_FILE)


def state_prefix(url):
    """Ek app ki saari cookie files isi naam se shuru hoti hain."""
    return hashlib.sha1(url.encode()).hexdigest()[:12] + "_"


def state_path(url, px=None):
    """Cookie file har (app, proxy IP) ke liye alag. px None = seedha (server IP).
    Isse ek IP ki cookies dusre IP/phone ke saath kabhi nahi milti."""
    who = hashlib.sha1((px or "direct").encode()).hexdigest()[:10]
    return os.path.join(STATE_DIR, state_prefix(url) + who + ".json")


# ---------------------------------------------------------------- log (3 din)
def log_add(entry):
    entry["t"] = time.time()
    with LOCK:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")


def log_read(since=None):
    out = []
    with LOCK:
        try:
            with open(LOG_FILE) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if since is None or e.get("t", 0) >= since:
                        out.append(e)
        except OSError:
            pass
    return out


def prune_log():
    cutoff = time.time() - LOG_DAYS * 86400
    with LOCK:
        entries = log_read(cutoff)[-MAX_LOG_LINES:]
        tmp = LOG_FILE + ".tmp"
        with open(tmp, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        os.replace(tmp, LOG_FILE)


def prune_state(apps):
    """Purani/bhari/hataye gaye apps ki cookie files delete, taaki disk na bhare."""
    prefixes = [state_prefix(u) for u in apps]
    cutoff = time.time() - COOKIE_DAYS * 86400
    removed = 0
    per_app = {}
    for name in os.listdir(STATE_DIR):
        p = os.path.join(STATE_DIR, name)
        try:
            st = os.stat(p)
            pre = next((x for x in prefixes if name.startswith(x)), None)
            if pre is None or st.st_mtime < cutoff or st.st_size > MAX_STATE_BYTES or name.endswith(".tmp"):
                os.remove(p)
                removed += 1
            else:
                per_app.setdefault(pre, []).append((st.st_mtime, p))
        except OSError:
            pass
    for pre, items in per_app.items():              # proxy badalte rehte hain: har app ki sirf sabse nayi COOKIE_MAX_PER_APP files
        items.sort(reverse=True)
        for _, p in items[COOKIE_MAX_PER_APP:]:
            try:
                os.remove(p)
                removed += 1
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------- proxy (optional)
PROXY_AUTO = "auto"   # proxy.txt me ye ho to GitHub ki free list wala pool chalega
PROXY_OFF = "off"   # proxy.txt me ye likha ho to env wala PROXY_URL bhi ignore (dashboard se "Band karo")


def get_proxy_url():
    """Runtime proxy URL: pehle file (dashboard se set), phir env. File me PROXY_OFF ho to koi proxy nahi."""
    try:
        with open(PROXY_FILE) as f:
            v = f.read().strip()
            if v == PROXY_OFF or v == PROXY_AUTO:
                return ""
            if v:
                return v
    except OSError:
        pass
    return PROXY_URL   # env wala fallback


def check_proxy(url):
    """Proxy URL theek ho to saaf karke wapas, warna None."""
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    try:
        u = urlparse(url)
        port = u.port
    except ValueError:
        return None
    if u.scheme not in ("http", "https", "socks5", "socks4") or not u.hostname or not port:
        return None
    if u.scheme in ("socks5", "socks4") and (u.username or u.password):
        return None   # Chromium socks par login support nahi karta
    return url


def mask_proxy(url):
    """Password kabhi dashboard/log me na dikhe: scheme://***@host:port"""
    if not url:
        return ""
    try:
        u = urlparse(url if "://" in url else "http://" + url)
        host = u.hostname or "?"
        if u.port:
            host += f":{u.port}"
        return f"{u.scheme}://{'***@' if (u.username or u.password) else ''}{host}"
    except ValueError:
        return "(invalid)"


def save_proxy_url(url):
    """Dashboard se set karo. url khali ho to proxy band (PROXY_OFF likhte hain, taaki env ka PROXY_URL bhi na lage)."""
    os.makedirs(DATA, exist_ok=True)
    tmp = PROXY_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # password wali file sirf hamare liye
    with os.fdopen(fd, "w") as f:
        f.write(url.strip() or PROXY_OFF)
    os.replace(tmp, PROXY_FILE)


def proxy_cfg():
    raw = get_proxy_url()
    if not raw:
        return None
    url = check_proxy(raw)
    if not url:
        log("proxy URL galat hai, proxy ke bina chal raha hoon:", mask_proxy(raw))
        return None
    from urllib.parse import unquote
    u = urlparse(url)
    cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        cfg["username"] = unquote(u.username)
        cfg["password"] = unquote(u.password or "")
    return cfg


# ---------------------------------------------------------------- proxy list (API se)
# Proxy ki list ek API URL se aati hai: har ~5 min me badalti hai, max 5000 line, har line
# socks5://ip:port | socks4://ip:port | http://ip:port. Render koi proxy TEST nahi karta:
# list download (1-5 sec, 1-5 MB se kam RAM) -> seedha pool me. Har visit par naya random proxy.
POOL_AUTO_ENV = env_int("FREE_PROXY_AUTO", 1) != 0   # disk reset ke baad bhi API-list mode se hi shuru (server IP se nahi)
FEED_ENV = os.environ.get("PROXY_FEED_URL", "").strip()               # default URL (dashboard se badal sakte ho)
FEED_FILE = os.path.join(DATA, "feed.json")                           # dashboard se set: {"url": ...}
FEED_MAX = max(100, env_int("FEED_MAX", 5000))                        # list me max itne proxy
FEED_EVERY = max(120, env_int("FEED_EVERY_SEC", 300))                 # ~itne sec me list dobara (+-20% random)
BROWSER_CLOSE_IDLE = env_int("BROWSER_CLOSE_IDLE", 1) != 0            # visit ke baad Chromium band (RAM khali), agli visit par phir khulega
IDLE_REASON = "idle ke baad"
POOL_WAKE_MAX = env_int("POOL_WAKE_MAX", 100)                         # proxy se jagane ke liye max wait (sec), phir naya proxy
POOL_NAV_TIMEOUT = max(15, env_int("POOL_NAV_TIMEOUT_SEC", 45))       # proxy se page kholne ka max wait (sec)
POOL_PRECHECK_SEC = max(1.0, float(os.environ.get("POOL_PRECHECK_SEC", "2.5") or 2.5))   # visit se pehle sirf TCP connect check (test nahi)
POOL_PRECHECK_TRIES = max(6, env_int("POOL_PRECHECK_TRIES", 18))      # ek visit ke liye max itne proxy ka connect check
POOL_AVOID = max(20, env_int("POOL_AVOID_RECENT", 400))               # pichle itne use hue proxy dobara nahi chunte
REUSE_HOURS = env_int("PROXY_REUSE_HOURS", 8)                         # (sirf REUSE_PCT>0 ho tab) itne ghante tak purana proxy
REUSE_PCT = max(0, min(100, env_int("PROXY_REUSE_PCT", 0)))           # 0 = har visit naya proxy (default)
REUSE_WAKE_MAX = env_int("REUSE_WAKE_MAX", 150)
RECENT_FILE = os.path.join(DATA, "recent_px.json")
RECENT = {"d": None}                                                   # {app_url: [{"hp","t","cc"}]}
POOL_FALLBACK_DIRECT = env_int("POOL_FALLBACK_DIRECT", 0) != 0         # 1 = proxy na mile to server ke IP se visit (default band)
POOL_RETRIES = max(0, env_int("POOL_RETRIES", 3))                      # proxy se visit fail ho to itni baar naye proxy se turant dobara
POOL = {"list": [], "cc": {}, "bad": {}, "updated": None, "found": 0, "busy": False, "fails": 0, "ev_t": 0.0,
        "note": "list abhi nahi aayi"}
POOL_LOCK = threading.Lock()
POOL_EVT = threading.Event()
LAST_PX = {"hp": None}
USED = deque(maxlen=POOL_AVOID)
PICK_INFO = {"tried": 0}
_IPPORT = re.compile(r"(?:(socks5|socks4|https?)://)?(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})(?!\d)", re.I)


def proxy_auto():
    try:
        with open(PROXY_FILE) as f:
            v = f.read().strip()
        if v == PROXY_AUTO:
            return True
        if v:
            return False
    except OSError:
        pass
    return POOL_AUTO_ENV and not PROXY_URL


def feed_url():
    """Dashboard ka URL pehle, warna env (PROXY_FEED_URL)."""
    try:
        with open(FEED_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("url"):
            return str(d["url"]).strip()
    except (OSError, ValueError):
        pass
    return FEED_ENV


def feed_every():
    """Dashboard se set interval (sec) pehle, warna env (FEED_EVERY_SEC)."""
    try:
        with open(FEED_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("every"):
            return max(120, int(d["every"]))
    except (OSError, ValueError, TypeError):
        pass
    return FEED_EVERY


def save_feed(url, every=None):
    os.makedirs(DATA, exist_ok=True)
    data = {"url": url}
    if every:
        data["every"] = max(120, int(every))
    else:
        try:
            with open(FEED_FILE) as f:
                old = json.load(f)
            if isinstance(old, dict) and old.get("every"):
                data["every"] = old["every"]
        except (OSError, ValueError):
            pass
    tmp = FEED_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # key wali file sirf hamare liye
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, FEED_FILE)


def norm_feed_url(raw):
    raw = (raw or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    try:
        u = urlparse(raw)
        if u.scheme not in ("http", "https") or not u.hostname:
            return None
    except ValueError:
        return None
    return raw


def mask_feed(url):
    """Key dashboard par poori na dikhe: https://host/path?key=a86…"""
    try:
        u = urlparse(url)
        q = re.sub(r"=([^&]{0,3})[^&]*", lambda m: "=" + m.group(1) + "…", u.query)
        return f"{u.scheme}://{u.netloc}{u.path}" + (f"?{q}" if q else "")
    except ValueError:
        return "(invalid)"


def feed_fetch(url, timeout=20):
    """API se list -> ['socks5://ip:port', ...] (max FEED_MAX, duplicate hata ke). Fail par exception."""
    req = urllib.request.Request(url, headers={"User-Agent": "keepalive"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(2 * 1024 * 1024)                      # 5000 line ~150 KB, 2 MB se upar kabhi nahi
    out, seen = [], set()
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("https://"):   # TLS wali proxy: Chromium certificate reject karta hai
            continue
        m = _IPPORT.search(line)
        if not m or not 0 < int(m.group(3)) < 65536:
            continue
        hp = f"{(m.group(1) or 'http').lower()}://{m.group(2)}:{m.group(3)}"
        if hp not in seen:
            seen.add(hp)
            out.append(hp)
            if len(out) >= FEED_MAX:
                break
    return out


def log_event(msg):
    """Dashboard log me event, par wahi baat 60 sec me dobara nahi (baar-baar click par spam nahi)."""
    if msg == _LAST_EV["m"] and time.time() - _LAST_EV["t"] < 60:
        return
    _LAST_EV["m"], _LAST_EV["t"] = msg, time.time()
    log_add({"event": "proxy", "msg": msg})


_LAST_EV = {"m": "", "t": 0.0}


class NoProxy(RuntimeError):
    pass


def pool_good_n():
    now = time.time()
    with POOL_LOCK:
        return max(0, len(POOL["list"]) - sum(1 for v in POOL["bad"].values() if v > now))


def pool_ready():
    """API-list mode me visit tabhi jab list me kam se kam 1 proxy ho (server IP se visit nahi)."""
    if not proxy_auto() or STATUS["engine"] == "obscura":
        return True
    return pool_good_n() >= 1


def pool_set(lst, quiet=False):
    """Nayi list ek jhatke me badlo (lock sirf swap tak)."""
    now = time.time()
    with POOL_LOCK:
        POOL["list"] = lst
        POOL["bad"] = {k: v for k, v in POOL["bad"].items() if v > now}
        POOL["updated"], POOL["found"], POOL["fails"] = now, len(lst), 0
        POOL["note"] = f"API se {len(lst)} proxy"
        hourly = now - POOL["ev_t"] > 3600
        if hourly:
            POOL["ev_t"] = now
    log("pool:", f"API se {len(lst)} proxy")
    if hourly and not quiet:
        log_add({"event": "proxy", "msg": f"proxy list update: {len(lst)} proxy (har ~5 min me aati hai, yahan ghante me ek baar likhta hoon)"})


def pool_feed_sync(url=None):
    """List download karo aur pool badlo. Fail ho to purani list chalti rehti hai. -> True/False."""
    url = url or feed_url()
    if not url:
        with POOL_LOCK:
            POOL["note"] = "proxy list ka URL set nahi hai"
        return False
    with POOL_LOCK:
        POOL["busy"] = True
    try:
        lst = feed_fetch(url)
        if not lst:
            raise ValueError("list khali")
        pool_set(lst)
        return True
    except Exception as e:
        with POOL_LOCK:
            POOL["fails"] += 1
            why = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__
            POOL["note"] = f"list nahi mili ({why}), purani chal rahi hai"
        log("pool: list fetch fail:", type(e).__name__, str(e)[:100])
        return False
    finally:
        with POOL_LOCK:
            POOL["busy"] = False


def pool_loop():
    """Alag thread: ~5 min (random +-20%) me list lo. Visit ke beech me ho to uske gap tak (max 45 sec) ruko, kisi visit ko rokta nahi."""
    while True:
        wait = 20
        try:
            if proxy_auto() and STATUS["engine"] != "obscura" and feed_url():
                POOL_EVT.clear()
                w = 0
                while STATUS.get("visiting") and w < 45:
                    time.sleep(random.uniform(3, 6))
                    w += 4
                ok = pool_feed_sync()
                wait = feed_every() * random.uniform(0.8, 1.2) if ok else min(300, 30 * (1 + POOL["fails"])) * random.uniform(0.8, 1.2)
        except Exception as e:
            log("pool loop error:", type(e).__name__)
            wait = 30
        POOL_EVT.wait(timeout=wait)


def chrome_busy():
    """Visit chal rahi hai ya (idle-close mode me) Chrome khula hai -> backup mat chalao."""
    return bool(STATUS.get("visiting") or (BROWSER_CLOSE_IDLE and BROWSER is not None and pool_ready()))


def _recent_load():
    if RECENT["d"] is None:
        try:
            with open(RECENT_FILE) as f:
                d = json.load(f)
            RECENT["d"] = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            RECENT["d"] = {}
    return RECENT["d"]


def _recent_save():
    try:
        tmp = RECENT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(RECENT["d"], f)
        os.replace(tmp, RECENT_FILE)
    except OSError:
        pass


def recent_add(url, hp):
    """Visit chal gayi: ye proxy is app ke liye 'chala hua' yaad rakho (sirf REUSE_PCT>0 ho tab kaam aata hai)."""
    if not hp or REUSE_PCT <= 0:
        return
    with POOL_LOCK:
        d = _recent_load()
        now = time.time()
        lst = [x for x in d.get(url, []) if x["hp"] != hp and now - x["t"] < REUSE_HOURS * 3600]
        lst.insert(0, {"hp": hp, "t": now, "cc": POOL["cc"].get(hp)})
        d[url] = lst[:5]
        _recent_save()


def recent_drop(hp):
    if not RECENT["d"]:
        return
    with POOL_LOCK:
        d = _recent_load()
        ch = False
        for u in d:
            n = [x for x in d[u] if x["hp"] != hp]
            if len(n) != len(d[u]):
                d[u], ch = n, True
        if ch:
            _recent_save()


def pool_country(hp):
    return POOL["cc"].get(hp)        # API se desh nahi aata, to aam taur par None (phone India wala default)


def tcp_alive(hp, timeout):
    """Sirf TCP connect (test nahi): ~1 KB RAM, dead proxy 2-3 sec me pakda jata hai, 90 sec browser me atakta nahi."""
    import socket
    host, _, port = hp.partition("://")[2].rpartition(":")
    try:
        socket.create_connection((host, int(port)), timeout=timeout).close()
        return True
    except (OSError, ValueError):
        return False


GEO = {}   # {hp: (cc, timezone)} ya None (lookup fail -> India default)


def geo_lookup(hp):
    """Sirf chune hue ek proxy ka desh+timezone (ip-api.com, ~0.2 sec, bahut kam RAM). Fail ho to None = India wala default."""
    if hp in GEO:
        return GEO[hp]
    res = None
    try:
        ip = hp.partition("://")[2].rpartition(":")[0]
        req = urllib.request.Request(f"http://ip-api.com/json/{ip}?fields=status,countryCode,timezone",
                                     headers={"User-Agent": "keepalive"})
        with urllib.request.urlopen(req, timeout=3) as r:
            d = json.loads(r.read(2048).decode("utf-8", "replace"))
        if d.get("status") == "success" and d.get("countryCode"):
            res = (str(d["countryCode"]).upper(), str(d.get("timezone") or ""))
            with POOL_LOCK:
                POOL["cc"][hp] = res[0]
    except Exception:
        res = None
    if len(GEO) > 2000:
        GEO.clear()
    GEO[hp] = res
    return res


def pool_pick(url=None):
    """-> (proxy, reused). Har visit naya random proxy: pichle POOL_AVOID use hue aur bad-marked nahi.
    Chunne ke baad sirf TCP connect check (6 ek saath); dead ho to agla."""
    from concurrent.futures import ThreadPoolExecutor
    PICK_INFO["tried"] = 0
    now = time.time()
    with POOL_LOCK:
        pool, bad = POOL["list"], POOL["bad"]
        if not pool:
            return None, False
        if url and REUSE_PCT > 0:
            rec = [x for x in _recent_load().get(url, [])
                   if now - x["t"] < REUSE_HOURS * 3600 and bad.get(x["hp"], 0) < now]
            if rec and random.random() * 100 < REUSE_PCT:
                return rec[0]["hp"], True
        used = set(USED)
        sample = random.sample(pool, min(len(pool), POOL_PRECHECK_TRIES * 4))
        cands = [h for h in sample if bad.get(h, 0) < now and h not in used]
        if len(cands) < POOL_PRECHECK_TRIES:                 # list chhoti hai: used wale bhi chalenge (par bad nahi)
            cands += [h for h in sample if bad.get(h, 0) < now and h in used]
    for i in range(0, min(len(cands), POOL_PRECHECK_TRIES), 6):
        batch = cands[i:i + 6]
        PICK_INFO["tried"] += len(batch)
        with ThreadPoolExecutor(max_workers=len(batch)) as ex:
            res = list(ex.map(lambda h: tcp_alive(h, POOL_PRECHECK_SEC), batch))
        with POOL_LOCK:
            for h, ok in zip(batch, res):
                if not ok:
                    POOL["bad"][h] = time.time() + 3600
        alive = [h for h, ok in zip(batch, res) if ok]
        if alive:
            USED.append(alive[0])
            return alive[0], False
    return None, False


def pool_mark_bad(hp, why=""):
    if not hp:
        return
    with POOL_LOCK:
        POOL["bad"][hp] = time.time() + 3600      # 1 ghanta ke liye hata do
    recent_drop(hp)
    log("pool: proxy bad mark", hp, why)


def scrub_proxy_log():
    """Purani log entries me agar proxy ka password likha ho to hata do (ek baar, start par)."""
    with LOCK:
        entries = log_read()
        if not any(e.get("event") == "proxy" and "@" in str(e.get("msg", "")) for e in entries):
            return
        tmp = LOG_FILE + ".tmp"
        with open(tmp, "w") as f:
            for e in entries:
                if e.get("event") == "proxy":
                    e["msg"] = "proxy badla (purani entry, password hata diya)"
                f.write(json.dumps(e) + "\n")
        os.replace(tmp, LOG_FILE)


# ---------------------------------------------------------------- visiting
# ---- insaan-jaisa behaviour -------------------------------------------------
VIEWPORTS = [(1366, 768), (1536, 864), (1440, 900), (1920, 1080), (1280, 720), (1600, 900), (1280, 800)]

# ---- Android Phone Fingerprint (sab apps k liye same) ----------------------
# Ek hi fixed Android phone identity — Samsung Galaxy S23 Ultra jaisi.
# Headless OFF — real visible Chrome window.

ANDROID_VIEWPORT = {"width": 412, "height": 915}   # Pixel 8
ANDROID_SCREEN   = {"width": 412, "height": 915}
ANDROID_MODEL = "Pixel 8"
ANDROID_VERSION = "16"


def chrome_major():
    return int(STATUS["chrome_full"].split(".")[0])


def android_ua(model=None, ver=None):
    """UA asli Chromium ke major version se banti hai (Android Chrome 'major.0.0.0' likhta hai)."""
    return (f"Mozilla/5.0 (Linux; Android {ver or ANDROID_VERSION}; {model or ANDROID_MODEL}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_major()}.0.0.0 Mobile Safari/537.36")


# Alag-alag asli Android phones. Har proxy ko hash se ek fixed phone milta hai (wahi proxy = wahi phone).
PHONES = [
    # model, android, (w, h), dpr, cores, ram_gb, gpu_vendor, gpu_renderer, year
    # w/h, dpr = yesviz.com (CSS viewport) se mile; android = us phone ka aakhri OS (Chrome 124 ke liye 8.0+)
    ("SM-G950F", "9", (360, 740), 4.0, 8, 4, "ARM", "Mali-G71", 2017),
    ("SM-G955F", "9", (360, 740), 4.0, 8, 4, "ARM", "Mali-G71", 2017),
    ("SM-N950F", "9", (414, 846), 3.5, 8, 4, "ARM", "Mali-G71", 2017),
    ("SM-G611F", "8.1", (412, 732), 2.625, 8, 2, "ARM", "Mali-T830", 2018),
    ("SM-G960F", "10", (360, 740), 4.0, 8, 4, "ARM", "Mali-G72", 2018),
    ("SM-G965F", "10", (360, 740), 4.0, 8, 4, "ARM", "Mali-G72", 2018),
    ("SM-N960F", "10", (414, 846), 3.5, 8, 4, "ARM", "Mali-G72", 2018),
    ("SM-J400F", "9", (360, 640), 2.0, 8, 2, "ARM", "Mali-T720", 2018),
    ("SM-J600F", "10", (360, 740), 2.0, 8, 2, "ARM", "Mali-T830", 2018),
    ("SM-J810F", "9", (360, 740), 2.0, 8, 4, "Qualcomm", "Adreno (TM) 506", 2018),
    ("SM-J730F", "9", (360, 640), 3.0, 8, 4, "ARM", "Mali-T830", 2017),
    ("SM-G973F", "12", (360, 760), 4.0, 8, 8, "ARM", "Mali-G76", 2019),
    ("SM-G975F", "12", (412, 869), 3.5, 8, 8, "ARM", "Mali-G76", 2019),
    ("SM-N970F", "12", (412, 869), 2.625, 8, 8, "ARM", "Mali-G76", 2019),
    ("SM-N975F", "12", (412, 869), 3.5, 8, 8, "ARM", "Mali-G76", 2019),
    ("SM-A305F", "11", (412, 892), 2.625, 8, 4, "ARM", "Mali-G71", 2019),
    ("SM-A505F", "11", (412, 892), 2.625, 8, 4, "ARM", "Mali-G72", 2019),
    ("SM-A507FN", "11", (412, 892), 2.625, 8, 4, "ARM", "Mali-G72", 2019),
    ("SM-A707F", "11", (412, 914), 2.625, 8, 8, "Qualcomm", "Adreno (TM) 612", 2019),
    ("SM-A515F", "13", (412, 915), 2.625, 8, 4, "ARM", "Mali-G72", 2020),
    ("SM-A715F", "13", (412, 914), 2.625, 8, 8, "Qualcomm", "Adreno (TM) 618", 2020),
    ("SM-A315F", "12", (412, 915), 2.625, 8, 4, "ARM", "Mali-G52", 2020),
    ("SM-G770F", "12", (412, 914), 2.625, 8, 8, "Qualcomm", "Adreno (TM) 640", 2020),
    ("SM-G980F", "13", (360, 800), 4.0, 8, 8, "ARM", "Mali-G77", 2020),
    ("SM-G985F", "13", (384, 854), 3.75, 8, 8, "ARM", "Mali-G77", 2020),
    ("SM-G988B", "13", (412, 915), 3.5, 8, 8, "ARM", "Mali-G77", 2020),
    ("SM-G780F", "13", (412, 914), 2.625, 8, 8, "ARM", "Mali-G77", 2020),
    ("SM-N980F", "13", (412, 915), 2.625, 8, 8, "ARM", "Mali-G77", 2020),
    ("SM-N986B", "13", (412, 883), 3.5, 8, 8, "ARM", "Mali-G77", 2020),
    ("SM-F700F", "12", (412, 1004), 2.625, 8, 8, "Qualcomm", "Adreno (TM) 640", 2020),
    ("SM-A325F", "13", (412, 915), 2.625, 8, 4, "ARM", "Mali-G52", 2021),
    ("SM-G998B", "14", (384, 854), 3.75, 8, 8, "ARM", "Mali-G78", 2021),
    ("SM-S911B", "16", (360, 780), 3.0, 8, 8, "Qualcomm", "Adreno (TM) 740", 2023),
    ("SM-A546E", "16", (412, 892), 2.625, 8, 8, "ARM", "Mali-G68", 2023),
    ("SM-S921B", "16", (360, 780), 3.0, 8, 8, "Samsung", "Xclipse 940", 2024),
    ("SM-A556E", "16", (412, 892), 2.625, 8, 8, "Samsung", "Xclipse 530", 2024),
    ("SM-A356E", "16", (412, 892), 2.625, 8, 8, "ARM", "Mali-G68", 2024),
    ("SM-S931B", "16", (360, 780), 3.0, 8, 8, "Qualcomm", "Adreno (TM) 830", 2025),
    ("SM-S936B", "16", (412, 891), 3.5, 8, 8, "Qualcomm", "Adreno (TM) 830", 2025),
    ("SM-S938B", "16", (412, 891), 3.5, 8, 8, "Qualcomm", "Adreno (TM) 830", 2025),
    ("SM-S937B", "16", (412, 891), 3.5, 8, 8, "Qualcomm", "Adreno (TM) 830", 2025),
    ("SM-A366B", "16", (412, 918), 2.625, 8, 8, "Qualcomm", "Adreno (TM) 710", 2025),
    ("Pixel 2", "11", (412, 732), 2.625, 8, 4, "Qualcomm", "Adreno (TM) 540", 2017),
    ("Pixel 2 XL", "11", (412, 823), 3.5, 8, 4, "Qualcomm", "Adreno (TM) 540", 2017),
    ("Pixel 3", "12", (393, 786), 2.75, 8, 4, "Qualcomm", "Adreno (TM) 630", 2018),
    ("Pixel 3 XL", "12", (412, 846), 3.5, 8, 4, "Qualcomm", "Adreno (TM) 630", 2018),
    ("Pixel 3a", "12", (393, 808), 2.75, 8, 4, "Qualcomm", "Adreno (TM) 615", 2019),
    ("Pixel 3a XL", "12", (412, 823), 2.625, 8, 4, "Qualcomm", "Adreno (TM) 615", 2019),
    ("Pixel 4", "13", (393, 830), 2.75, 8, 4, "Qualcomm", "Adreno (TM) 640", 2019),
    ("Pixel 4 XL", "13", (412, 869), 3.5, 8, 4, "Qualcomm", "Adreno (TM) 640", 2019),
    ("Pixel 4a", "13", (393, 851), 2.75, 8, 4, "Qualcomm", "Adreno (TM) 618", 2020),
    ("Pixel 5", "14", (393, 851), 2.75, 8, 8, "Qualcomm", "Adreno (TM) 620", 2020),
    ("Pixel 6", "16", (412, 915), 2.625, 8, 8, "ARM", "Mali-G78", 2021),
    ("Pixel 6 Pro", "16", (412, 892), 3.5, 8, 8, "ARM", "Mali-G78", 2021),
    ("Pixel 6a", "16", (412, 915), 2.625, 8, 4, "ARM", "Mali-G78", 2022),
    ("Pixel 7", "16", (412, 915), 2.625, 8, 8, "ARM", "Mali-G710", 2022),
    ("Pixel 7 Pro", "16", (412, 892), 3.5, 8, 8, "ARM", "Mali-G710", 2022),
    ("Pixel 7a", "16", (412, 915), 2.625, 8, 8, "ARM", "Mali-G710", 2023),
    ("Pixel 8", "16", (412, 915), 2.625, 8, 8, "ARM", "Mali-G715", 2023),
    ("Pixel 8 Pro", "16", (448, 997), 3.0, 8, 8, "ARM", "Mali-G715", 2023),
    ("Pixel 8a", "16", (412, 915), 2.625, 8, 8, "ARM", "Mali-G715", 2024),
    ("Pixel 9", "16", (360, 808), 3.0, 8, 8, "ARM", "Mali-G715", 2024),
    ("Pixel 9 Pro", "16", (427, 952), 3.0, 8, 8, "ARM", "Mali-G715", 2024),
    ("Pixel 9 Pro XL", "16", (448, 997), 3.0, 8, 8, "ARM", "Mali-G715", 2024),
    ("A5010", "10", (360, 720), 3.0, 8, 8, "Qualcomm", "Adreno (TM) 540", 2017),
    ("A6003", "11", (412, 869), 2.625, 8, 8, "Qualcomm", "Adreno (TM) 630", 2018),
    ("A6013", "11", (412, 892), 2.625, 8, 8, "Qualcomm", "Adreno (TM) 630", 2018),
    ("GM1901", "11", (412, 892), 2.625, 8, 8, "Qualcomm", "Adreno (TM) 640", 2019),
    ("GM1917", "11", (412, 892), 3.5, 8, 8, "Qualcomm", "Adreno (TM) 640", 2019),
    ("HD1903", "12", (412, 914), 2.625, 8, 8, "Qualcomm", "Adreno (TM) 640", 2019),
    ("HD1913", "12", (412, 892), 3.5, 8, 8, "Qualcomm", "Adreno (TM) 640", 2019),
    ("IN2013", "13", (412, 915), 2.625, 8, 8, "Qualcomm", "Adreno (TM) 650", 2020),
    ("IN2023", "13", (412, 906), 3.5, 8, 8, "Qualcomm", "Adreno (TM) 650", 2020),
    ("KB2003", "13", (412, 914), 2.625, 8, 8, "Qualcomm", "Adreno (TM) 650", 2020),
    ("AC2003", "12", (412, 915), 2.625, 8, 8, "Qualcomm", "Adreno (TM) 620", 2020),
    ("LE2113", "14", (412, 915), 2.625, 8, 8, "Qualcomm", "Adreno (TM) 660", 2021),
    ("LE2123", "14", (412, 919), 3.5, 8, 8, "Qualcomm", "Adreno (TM) 660", 2021),
    ("DN2103", "13", (412, 915), 2.625, 8, 8, "ARM", "Mali-G77", 2021),
    ("NE2213", "15", (412, 919), 3.5, 8, 8, "Qualcomm", "Adreno (TM) 730", 2022),
    ("CPH2573", "16", (412, 905), 3.5, 8, 8, "Qualcomm", "Adreno (TM) 750", 2024),
    ("CPH2663", "15", (360, 924), 3.0, 8, 8, "Qualcomm", "Adreno (TM) 732", 2024),
    ("CPH2655", "16", (412, 905), 3.5, 8, 8, "Qualcomm", "Adreno (TM) 830", 2025),
    ("M1803E1A", "10", (393, 817), 2.75, 8, 4, "Qualcomm", "Adreno (TM) 630", 2018),
    ("M1805E2A", "9", (393, 816), 2.75, 8, 4, "Qualcomm", "Adreno (TM) 616", 2018),
    ("M1902F1G", "11", (393, 851), 2.75, 8, 4, "Qualcomm", "Adreno (TM) 640", 2019),
    ("M1903F2G", "10", (393, 851), 2.75, 8, 4, "Qualcomm", "Adreno (TM) 616", 2019),
    ("M1904F3BG", "10", (393, 851), 2.75, 8, 4, "Qualcomm", "Adreno (TM) 616", 2019),
    ("M1903F11G", "11", (393, 851), 2.75, 8, 4, "Qualcomm", "Adreno (TM) 640", 2019),
    ("M1910F4G", "10", (393, 851), 2.75, 8, 4, "Qualcomm", "Adreno (TM) 618", 2019),
    ("M1901F7G", "10", (393, 851), 2.75, 8, 4, "Qualcomm", "Adreno (TM) 512", 2019),
    ("M1908C3JG", "11", (393, 851), 2.75, 8, 4, "Qualcomm", "Adreno (TM) 610", 2019),
    ("M1906G7G", "11", (393, 851), 2.75, 8, 4, "ARM", "Mali-G76", 2019),
    ("M2001J2G", "13", (393, 851), 2.75, 8, 8, "Qualcomm", "Adreno (TM) 650", 2020),
    ("M2001J1G", "13", (393, 851), 2.75, 8, 8, "Qualcomm", "Adreno (TM) 650", 2020),
    ("M2007J3SY", "12", (393, 873), 2.75, 8, 8, "Qualcomm", "Adreno (TM) 650", 2020),
    ("M2003J15SS", "11", (393, 851), 2.75, 8, 4, "ARM", "Mali-G52", 2020),
    ("M2003J6B2G", "12", (393, 873), 2.75, 8, 4, "Qualcomm", "Adreno (TM) 618", 2020),
    ("LG-H870", "8.0", (360, 720), 4.0, 4, 4, "Qualcomm", "Adreno (TM) 530", 2017),
    ("LM-G710", "10", (360, 780), 4.0, 8, 4, "Qualcomm", "Adreno (TM) 630", 2018),
    ("LM-G820", "11", (360, 780), 4.0, 8, 4, "Qualcomm", "Adreno (TM) 640", 2019),
    ("LM-G850", "11", (360, 780), 3.0, 8, 4, "Qualcomm", "Adreno (TM) 640", 2019),
    ("LM-X410", "8.1", (360, 640), 2.0, 4, 2, "Qualcomm", "Adreno (TM) 308", 2018),
    ("LM-X420", "9", (360, 720), 2.0, 4, 2, "Qualcomm", "Adreno (TM) 308", 2019),
    ("LM-X520", "9", (360, 760), 2.0, 8, 2, "Imagination Technologies", "PowerVR Rogue GE8320", 2019),
    ("LM-X440", "9", (360, 760), 2.0, 8, 2, "Imagination Technologies", "PowerVR Rogue GE8320", 2019),
    ("LM-X120", "9", (320, 640), 1.5, 4, 2, "Qualcomm", "Adreno (TM) 308", 2019),
    ("LM-Q710", "8.1", (360, 720), 3.0, 8, 2, "Qualcomm", "Adreno (TM) 506", 2018),
    ("LM-Q720", "9", (360, 720), 3.0, 8, 4, "Qualcomm", "Adreno (TM) 506", 2019),
    ("LM-Q630", "10", (360, 780), 3.0, 8, 4, "Imagination Technologies", "PowerVR Rogue GE8320", 2020),
    ("LM-K510", "10", (360, 800), 2.0, 8, 2, "Imagination Technologies", "PowerVR Rogue GE8320", 2020),
    ("RNE-L21", "8.0", (360, 720), 3.0, 8, 4, "ARM", "Mali-T830", 2017),
    ("PRA-LX1", "8.0", (360, 640), 3.0, 8, 2, "ARM", "Mali-T830", 2017),
    ("VKY-L29", "9", (360, 640), 4.0, 8, 4, "ARM", "Mali-G71", 2017),
    ("BND-L21", "9", (360, 720), 3.0, 8, 4, "ARM", "Mali-T830", 2017),
    ("LYA-L29", "10", (360, 780), 4.0, 8, 4, "ARM", "Mali-G76", 2018),
    ("SNE-LX1", "9", (360, 780), 3.0, 8, 4, "ARM", "Mali-G51", 2018),
    ("CLT-L29", "10", (360, 747), 3.0, 8, 4, "ARM", "Mali-G72", 2018),
    ("ANE-LX1", "9", (360, 760), 3.0, 8, 4, "ARM", "Mali-T830", 2018),
    ("JSN-L21", "10", (360, 780), 3.0, 8, 4, "ARM", "Mali-G51", 2018),
    ("ELE-L29", "10", (360, 780), 3.0, 8, 4, "ARM", "Mali-G76", 2019),
    ("VOG-L29", "10", (360, 780), 3.0, 8, 8, "ARM", "Mali-G76", 2019),
    ("MAR-LX1", "10", (360, 771), 3.0, 8, 4, "ARM", "Mali-G51", 2019),
    ("STK-LX3", "9", (360, 780), 3.0, 8, 4, "ARM", "Mali-G51", 2019),
    ("STK-L21", "9", (360, 780), 3.0, 8, 4, "ARM", "Mali-G51", 2019),
    ("STK-LX1", "9", (360, 780), 3.0, 8, 4, "ARM", "Mali-G51", 2019),
    ("TAS-L29", "10", (360, 780), 3.0, 8, 8, "ARM", "Mali-G76", 2019),
    ("LIO-L29", "10", (392, 800), 3.0, 8, 8, "ARM", "Mali-G76", 2019),
    ("ANA-NX9", "10", (360, 780), 3.0, 8, 8, "ARM", "Mali-G76", 2020),
    ("ELS-NX9", "10", (400, 880), 3.0, 8, 8, "ARM", "Mali-G76", 2020),
    ("JNY-LX1", "10", (360, 770), 3.0, 8, 4, "ARM", "Mali-G52", 2020),
    ("J9110", "11", (411, 960), 4.0, 8, 4, "Qualcomm", "Adreno (TM) 640", 2019),
    ("J9210", "11", (360, 840), 3.0, 8, 4, "Qualcomm", "Adreno (TM) 640", 2019),
    ("I4113", "10", (360, 840), 3.0, 8, 2, "Qualcomm", "Adreno (TM) 508", 2019),
    ("I4213", "10", (360, 840), 3.0, 8, 4, "Qualcomm", "Adreno (TM) 509", 2019),
    ("I4312", "10", (360, 840), 2.0, 8, 2, "Imagination Technologies", "PowerVR Rogue GE8320", 2020),
    ("HTC U11", "8.0", (360, 640), 4.0, 8, 4, "Qualcomm", "Adreno (TM) 540", 2017),
    ("HTC U11+", "9", (360, 720), 4.0, 8, 4, "Qualcomm", "Adreno (TM) 540", 2017),
    ("HTC U12+", "10", (360, 720), 4.0, 8, 4, "Qualcomm", "Adreno (TM) 630", 2018),
    ("XT1924-1", "8.1", (360, 720), 2.0, 4, 2, "Qualcomm", "Adreno (TM) 308", 2018),
    ("XT2045-2", "10", (360, 780), 2.0, 8, 4, "Qualcomm", "Adreno (TM) 610", 2019),
    ("V2029", "11", (385, 854), 1.875, 8, 4, "Qualcomm", "Adreno (TM) 610", 2020),
]
# country -> (language, timezone). Proxy ka desh pata ho to timezone/language usi se milte hain.
COUNTRY_LOC = {
    "IN": ("en-IN", "Asia/Kolkata"), "US": ("en-US", "America/New_York"), "GB": ("en-GB", "Europe/London"),
    "DE": ("de-DE", "Europe/Berlin"), "FR": ("fr-FR", "Europe/Paris"), "NL": ("nl-NL", "Europe/Amsterdam"),
    "SG": ("en-SG", "Asia/Singapore"), "BR": ("pt-BR", "America/Sao_Paulo"), "ID": ("id-ID", "Asia/Jakarta"),
    "RU": ("ru-RU", "Europe/Moscow"), "UA": ("uk-UA", "Europe/Kyiv"), "TR": ("tr-TR", "Europe/Istanbul"),
    "PL": ("pl-PL", "Europe/Warsaw"), "IT": ("it-IT", "Europe/Rome"), "ES": ("es-ES", "Europe/Madrid"),
    "TH": ("th-TH", "Asia/Bangkok"), "VN": ("vi-VN", "Asia/Ho_Chi_Minh"), "PH": ("en-PH", "Asia/Manila"),
    "BD": ("bn-BD", "Asia/Dhaka"), "PK": ("ur-PK", "Asia/Karachi"), "MX": ("es-MX", "America/Mexico_City"),
    "AR": ("es-AR", "America/Argentina/Buenos_Aires"), "CO": ("es-CO", "America/Bogota"), "EC": ("es-EC", "America/Guayaquil"),
    "ZA": ("en-ZA", "Africa/Johannesburg"), "NG": ("en-NG", "Africa/Lagos"), "EG": ("ar-EG", "Africa/Cairo"),
    "JP": ("ja-JP", "Asia/Tokyo"), "KR": ("ko-KR", "Asia/Seoul"), "HK": ("zh-HK", "Asia/Hong_Kong"),
    "CA": ("en-CA", "America/Toronto"), "AU": ("en-AU", "Australia/Sydney"), "MY": ("ms-MY", "Asia/Kuala_Lumpur"),
}
_TZ_STD = {"Asia/Kolkata": 330, "America/New_York": -300, "Europe/London": 0, "Europe/Berlin": 60, "Europe/Paris": 60,
           "Europe/Amsterdam": 60, "Asia/Singapore": 480, "America/Sao_Paulo": -180, "Asia/Jakarta": 420}


def tz_offset_min(tz):
    try:
        from zoneinfo import ZoneInfo
        import datetime
        return int(datetime.datetime.now(ZoneInfo(tz)).utcoffset().total_seconds() // 60)   # DST ke saath
    except Exception:
        return _TZ_STD.get(tz, 330)


def pick_phone(h):
    """Hash se ek fixed phone chuno; naye saal ke phone ka weight zyada (2017 -> 1, 2025 -> 9)."""
    total = sum(max(1, p[8] - 2016) for p in PHONES)
    r = h % total
    for p in PHONES:
        r -= max(1, p[8] - 2016)
        if r < 0:
            return p
    return PHONES[-1]


def get_fingerprint(url, px=None):
    """px (proxy) ho to us proxy ka apna phone + desh ke hisaab se timezone. px None ho to purana Galaxy S23 Ultra."""
    fp = {
        "ua": android_ua(), "platform": "Linux armv8l", "vendor": "Google Inc.",
        "viewport": ANDROID_VIEWPORT, "screen": ANDROID_SCREEN, "color_depth": 24, "dpr": 2.625,
        "hardware_concurrency": 8, "device_memory": 8, "language": "en-IN", "timezone_id": "Asia/Kolkata",
        "timezone_offset": 330, "battery_level": 0.82, "battery_charging": False, "touch": True, "seed": FP_SEED,
        "model": ANDROID_MODEL, "android": ANDROID_VERSION, "gpu_vendor": "ARM", "gpu_renderer": "Mali-G715",
    }
    if not px:
        return fp
    h = int(hashlib.sha1(("fp|" + px).encode()).hexdigest(), 16)
    model, ver, (w, hh), dpr, cores, ram, gv, gr, _yr = pick_phone(h)
    geo = GEO.get(px)
    cc = geo[0] if geo else "IN"
    lang, tz = COUNTRY_LOC.get(cc, ("en-US", None))
    if geo and geo[1]:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(geo[1])
            tz = geo[1]                                  # API ka asli timezone
        except Exception:
            pass
    if not geo or not tz:
        lang, tz = COUNTRY_LOC["IN"]                     # lookup fail / timezone nahi mila: India
    fp.update({
        "ua": android_ua(model, ver), "viewport": {"width": w, "height": hh}, "screen": {"width": w, "height": hh},
        "dpr": dpr, "hardware_concurrency": cores, "device_memory": ram, "language": lang, "timezone_id": tz,
        "timezone_offset": tz_offset_min(tz), "battery_level": round(0.35 + ((h >> 8) % 60) / 100.0, 2),
        "battery_charging": bool((h >> 16) % 4 == 0), "seed": (h >> 20) & 0x7FFFFFFF,
        "model": model, "android": ver, "gpu_vendor": gv, "gpu_renderer": gr,
    })
    return fp


def ua_override(fp, native_brands):
    """CDP Emulation.setUserAgentOverride ka payload: UA + Client Hints (headers aur navigator.userAgentData dono).

    native_brands = engine ki apni brands list (usme us version ka asli GREASE brand hota hai).
    Hum usme "Google Chrome" jodte hain aur full version asli Chromium ka rakhte hain, taaki UA se match kare.
    """
    major, full = str(chrome_major()), STATUS["chrome_full"]
    brands, fulls = [], []
    for b in native_brands or []:
        name, ver = str(b.get("brand", "")), str(b.get("version", ""))
        if name and ver:
            brands.append({"brand": name, "version": ver})
            fulls.append({"brand": name, "version": full if name in ("Chromium", "Google Chrome") else ver + ".0.0.0"})
    if not any(b["brand"] == "Chromium" for b in brands):          # native list na mile to fallback
        brands = [{"brand": "Chromium", "version": major}, {"brand": "Not?A_Brand", "version": "8"}]
        fulls = [{"brand": "Chromium", "version": full}, {"brand": "Not?A_Brand", "version": "8.0.0.0"}]
    if not any(b["brand"] == "Google Chrome" for b in brands):
        brands.append({"brand": "Google Chrome", "version": major})
        fulls.append({"brand": "Google Chrome", "version": full})
    return {
        "userAgent": fp["ua"],
        "acceptLanguage": f"{fp['language']},en;q=0.9",            # navigator.languages se match
        "platform": fp["platform"],
        "userAgentMetadata": {
            "brands": brands, "fullVersionList": fulls, "fullVersion": full,
            "platform": "Android", "platformVersion": f"{fp.get('android', ANDROID_VERSION)}.0.0",
            "architecture": "", "bitness": "", "model": fp.get("model", ANDROID_MODEL),   # Android Chrome in dono ko khali rakhta hai
            "mobile": True, "wow64": False,
        },
    }


# __FP__ ki jagah fingerprint JSON .replace() se bharta hai (% formatting nahi, taaki JS me % chal sake).
# Jo cheezein Chromium has_touch/is_mobile se khud sahi deta hai (touch events, pointer:coarse, hover:none,
# userAgentData) unhe yahan fake NAHI karte; patch utna kam jitna zaroori.
_FP_INIT_SCRIPT = """
(function(fp){

  // seeded PRNG: same seed => hamesha same sequence (noise stable rehta hai, har call par badalta nahi)
  const _rng = function(seed) {
    let a = seed >>> 0;
    return function() {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  };

  // ── 1. Navigator basic ──────────────────────────────────────────────────
  try { Object.defineProperty(navigator, 'platform',   {get:()=>fp.platform,   configurable:true}); } catch(e){}
  try { Object.defineProperty(navigator, 'vendor',     {get:()=>fp.vendor,     configurable:true}); } catch(e){}
  try { Object.defineProperty(navigator, 'hardwareConcurrency', {get:()=>fp.hardware_concurrency, configurable:true}); } catch(e){}
  try { Object.defineProperty(navigator, 'deviceMemory', {get:()=>fp.device_memory, configurable:true}); } catch(e){}
  try { Object.defineProperty(navigator, 'language',  {get:()=>fp.language,   configurable:true}); } catch(e){}
  try { Object.defineProperty(navigator, 'languages', {get:()=>[fp.language,'en'], configurable:true}); } catch(e){}
  try { Object.defineProperty(navigator, 'webdriver', {get:()=>undefined,       configurable:true}); } catch(e){}
  try { Object.defineProperty(navigator, 'maxTouchPoints', {get:()=>5,          configurable:true}); } catch(e){}
  // appVersion yahan nahi: Chromium use UA se khud banata hai (UA override ke baad sahi aata hai)

  // ── 2. Screen ───────────────────────────────────────────────────────────
  try { Object.defineProperty(screen, 'width',      {get:()=>fp.screen.width,  configurable:true}); } catch(e){}
  try { Object.defineProperty(screen, 'height',     {get:()=>fp.screen.height, configurable:true}); } catch(e){}
  try { Object.defineProperty(screen, 'availWidth', {get:()=>fp.screen.width,  configurable:true}); } catch(e){}
  try { Object.defineProperty(screen, 'availHeight',{get:()=>fp.screen.height - 48, configurable:true}); } catch(e){}
  try { Object.defineProperty(screen, 'colorDepth', {get:()=>fp.color_depth,   configurable:true}); } catch(e){}
  try { Object.defineProperty(screen, 'pixelDepth', {get:()=>fp.color_depth,   configurable:true}); } catch(e){}
  try { Object.defineProperty(window, 'devicePixelRatio', {get:()=>fp.dpr,     configurable:true}); } catch(e){}

  // ── 3. Touch: patch nahi. Context has_touch=True se ontouchstart/TouchEvent/pointer:coarse native aate hain ──

  // ── 4. Battery API ──────────────────────────────────────────────────────
  try {
    navigator.getBattery = function() {
      return Promise.resolve({
        charging: fp.battery_charging,
        chargingTime: Infinity,
        dischargingTime: 14400,
        level: fp.battery_level,
        addEventListener: function(){},
        removeEventListener: function(){}
      });
    };
  } catch(e){}

  // ── 5. Timezone ─────────────────────────────────────────────────────────
  try { Date.prototype.getTimezoneOffset = function() { return -fp.timezone_offset; }; } catch(e){}

  // ── 6. WebGL — Adreno 740 (Galaxy S23 Ultra) spoof ─────────────────────
  const _getCtx = HTMLCanvasElement.prototype.getContext;
  try {
    HTMLCanvasElement.prototype.getContext = function(type, attrs) {
      const ctx = _getCtx.call(this, type, attrs);
      if (!ctx) return ctx;
      if (type === 'webgl' || type === 'webgl2' || type === 'experimental-webgl') {
        const _getPar = ctx.getParameter.bind(ctx);
        ctx.getParameter = function(p) {
          // UNMASKED_VENDOR_WEBGL
          if (p === 37445) return fp.gpu_vendor || 'Qualcomm';
          // UNMASKED_RENDERER_WEBGL
          if (p === 37446) return fp.gpu_renderer || 'Adreno (TM) 740';
          // MAX_TEXTURE_SIZE
          if (p === 3379)  return 16384;
          // MAX_RENDERBUFFER_SIZE
          if (p === 34024) return 16384;
          // SHADING_LANGUAGE_VERSION
          if (p === 35724) return 'OpenGL ES GLSL ES 3.20';
          // VERSION
          if (p === 7938)  return 'OpenGL ES 3.2 V@0615.73';
          return _getPar(p);
        };
        const _getExt = ctx.getExtension.bind(ctx);
        ctx.getExtension = function(name) {
          if (name === 'WEBGL_debug_renderer_info') {
            return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
          }
          return _getExt(name);
        };
      }
      return ctx;
    };
  } catch(e){}

  // ── 7. Canvas fingerprint — stable noise, asli canvas ko chhue bina ─────
  // Noise ek COPY par lagta hai, original canvas same rehta hai, aur seed fixed hai:
  // same drawing => hamesha same output (pehle har call par noise badhta tha, fingerprint har baar badalta tha).
  try {
    const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
    const _toBlob = HTMLCanvasElement.prototype.toBlob;
    const _gid = CanvasRenderingContext2D.prototype.getImageData;
    const noisyCopy = function(src) {
      const w = src.width, h = src.height;
      if (!w || !h || w * h > 4000000) return null;
      const c = document.createElement('canvas');
      c.width = w; c.height = h;
      const x = c.getContext('2d');
      x.drawImage(src, 0, 0);
      const img = _gid.call(x, 0, 0, w, h);          // tainted canvas par yahin SecurityError aayega
      const d = img.data;
      const r = _rng((fp.seed ^ Math.imul(w, 73856093) ^ Math.imul(h, 19349663)) >>> 0);
      for (let i = 0; i < d.length; i += 4 * (40 + Math.floor(r() * 60))) {
        d[i + Math.floor(r() * 3)] ^= 1;               // sirf 1 LSB, kuch hi pixels
      }
      x.putImageData(img, 0, 0);
      return c;
    };
    HTMLCanvasElement.prototype.toDataURL = function(type, q) {
      let c = null;
      try { c = noisyCopy(this); } catch (e) {}
      return _toDataURL.call(c || this, type, q);
    };
    if (_toBlob) {
      HTMLCanvasElement.prototype.toBlob = function(cb, type, q) {
        let c = null;
        try { c = noisyCopy(this); } catch (e) {}
        return _toBlob.call(c || this, cb, type, q);
      };
    }
  } catch(e){}

  // ── 8. Audio fingerprint — AudioBuffer / Analyser me stable, bahut chhota noise ──
  try {
    const done = new WeakMap();                        // buffer -> {channel: true}: noise sirf ek baar lagta hai
    const _gcd = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function(ch) {
      const d = _gcd.call(this, ch);
      let m = done.get(this);
      if (!m) { m = {}; done.set(this, m); }
      if (!m[ch] && d.length > 0) {
        m[ch] = true;
        const r = _rng((fp.seed ^ Math.imul(ch + 1, 2654435761)) >>> 0);
        for (let i = 0; i < d.length; i += 100 + Math.floor(r() * 50)) {
          d[i] += (r() - 0.5) * 2e-7;                  // sunai nahi deta, hash badal jata hai
        }
      }
      return d;
    };
    const _cfc = AudioBuffer.prototype.copyFromChannel;
    AudioBuffer.prototype.copyFromChannel = function(dest, ch, off) {
      this.getChannelData(ch);                         // pehle noise lagao, taaki dono tareeke ek jaisa data dein
      return _cfc.call(this, dest, ch, off === undefined ? 0 : off);
    };
    const _gffd = AnalyserNode.prototype.getFloatFrequencyData;
    AnalyserNode.prototype.getFloatFrequencyData = function(arr) {
      _gffd.call(this, arr);
      const r = _rng((fp.seed ^ 0xA5A5) >>> 0);        // har call par reseed => same input, same output
      for (let i = 0; i < arr.length; i++) arr[i] += (r() - 0.5) * 1e-3;
    };
  } catch(e){}

  // ── 9. Sensors — Android motion/orientation events ──────────────────────
  try {
    // DeviceMotionEvent — Android phone hamesha thoda hilta rehta hai
    if (window.DeviceMotionEvent) {
      let _motTick = 0;
      setInterval(function() {
        _motTick++;
        const noise = function(base, amp) { return base + (Math.random() - 0.5) * amp; };
        const ev = new DeviceMotionEvent('devicemotion', {
          acceleration: {x: noise(0, 0.04), y: noise(0, 0.04), z: noise(0, 0.04)},
          accelerationIncludingGravity: {x: noise(0.1, 0.05), y: noise(0.15, 0.05), z: noise(-9.78, 0.08)},
          rotationRate: {alpha: noise(0, 0.2), beta: noise(0, 0.2), gamma: noise(0, 0.2)},
          interval: 16
        });
        window.dispatchEvent(ev);
      }, 500 + Math.random() * 200);
    }
    // DeviceOrientationEvent — table par rakha phone
    if (window.DeviceOrientationEvent) {
      setInterval(function() {
        const ev = new DeviceOrientationEvent('deviceorientation', {
          alpha: 0 + (Math.random() - 0.5) * 2,
          beta:  2 + (Math.random() - 0.5) * 1,
          gamma: 0 + (Math.random() - 0.5) * 1,
          absolute: false
        });
        window.dispatchEvent(ev);
      }, 800 + Math.random() * 400);
    }
  } catch(e){}

  // ── 10. Fonts: JS me fake nahi. Asli fonts Dockerfile me install hain (fonts-roboto, fonts-noto-core) ──

  // ── 11. WebRTC — local IP leak band karo ────────────────────────────────
  try {
    const _RTCPeer = window.RTCPeerConnection || window.webkitRTCPeerConnection;
    if (_RTCPeer) {
      const _Patched = function(cfg, opts) {
        // ICE servers hata do — local IP gather nahi hogi
        const safeCfg = cfg ? Object.assign({}, cfg, {iceServers: []}) : {iceServers: []};
        const pc = new _RTCPeer(safeCfg, opts);
        const _createDataChannel = pc.createDataChannel.bind(pc);
        // onicecandidate patch — koi local IP candidate pass nahi hone dete
        const _origAddEL = pc.addEventListener.bind(pc);
        pc.addEventListener = function(type, handler, opts2) {
          if (type === 'icecandidate') {
            return _origAddEL(type, function(e) {
              if (e.candidate && e.candidate.candidate &&
                  (e.candidate.candidate.indexOf('192.168') !== -1 ||
                   e.candidate.candidate.indexOf('10.') !== -1 ||
                   e.candidate.candidate.indexOf('172.') !== -1)) {
                return; // local IP wale candidates drop karo
              }
              handler(e);
            }, opts2);
          }
          return _origAddEL(type, handler, opts2);
        };
        return pc;
      };
      try {
        _Patched.prototype = _RTCPeer.prototype;
        if (window.RTCPeerConnection) window.RTCPeerConnection = _Patched;
        if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = _Patched;
      } catch(e){}
    }
  } catch(e){}

  // ── 12. Network Connection — WiFi Android jaisa ──────────────────────────
  try {
    const conn = {
      effectiveType: '4g',
      type: 'wifi',
      downlink: 18.5,
      downlinkMax: Infinity,
      rtt: 65,
      saveData: false,
      onchange: null,
      addEventListener: function(){},
      removeEventListener: function(){}
    };
    Object.defineProperty(navigator, 'connection',       {get:()=>conn, configurable:true});
    Object.defineProperty(navigator, 'mozConnection',    {get:()=>conn, configurable:true});
    Object.defineProperty(navigator, 'webkitConnection', {get:()=>conn, configurable:true});
  } catch(e){}

  // ── 13. Permissions API — Android phone jaisi ────────────────────────────
  try {
    if (navigator.permissions && navigator.permissions.query) {
      const _origQuery = navigator.permissions.query.bind(navigator.permissions);
      navigator.permissions.query = function(desc) {
        const name = (desc && desc.name) || '';
        // Android par yeh permissions prompt karti hain (granted nahi, denied bhi nahi)
        if (['camera','microphone','geolocation','notifications'].indexOf(name) !== -1) {
          return Promise.resolve({state: 'prompt', onchange: null,
            addEventListener: function(){}, removeEventListener: function(){}});
        }
        // clipboard Android par granted hoti hai
        if (name === 'clipboard-read' || name === 'clipboard-write') {
          return Promise.resolve({state: 'granted', onchange: null,
            addEventListener: function(){}, removeEventListener: function(){}});
        }
        return _origQuery(desc);
      };
    }
  } catch(e){}

  // ── 14. Media Devices — front + back camera + mic ────────────────────────
  try {
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
      navigator.mediaDevices.enumerateDevices = function() {
        return Promise.resolve([
          {kind:'videoinput',  label:'',  deviceId:'front001', groupId:'grp1'},
          {kind:'videoinput',  label:'',  deviceId:'back001',  groupId:'grp2'},
          {kind:'audioinput',  label:'',  deviceId:'mic001',   groupId:'grp3'},
          {kind:'audiooutput', label:'',  deviceId:'spk001',   groupId:'grp3'}
        ]);
      };
    }
  } catch(e){}

  // ── 15. Speech Synthesis — Android TTS voices ────────────────────────────
  try {
    if (window.speechSynthesis) {
      const _fakeVoices = [
        {voiceURI:'com.google.android.tts', name:'Android Speech Recognition and Synthesis from Google',
         lang:'en-US', localService:true, default:true},
        {voiceURI:'com.google.android.tts-hi', name:'Android Hindi TTS',
         lang:'hi-IN', localService:true, default:false}
      ];
      const _origGetVoices = window.speechSynthesis.getVoices.bind(window.speechSynthesis);
      window.speechSynthesis.getVoices = function() {
        const real = _origGetVoices();
        return real.length ? real : _fakeVoices;
      };
      // onvoiceschanged fire karo
      setTimeout(function() {
        try {
          const ev = new Event('voiceschanged');
          window.speechSynthesis.dispatchEvent(ev);
        } catch(e2){}
      }, 200);
    }
  } catch(e){}

  // ── 16. matchMedia: patch hata diya. Chromium khud pointer:coarse / hover:none deta hai (test se verify) ──

  // ── 17. Clipboard — Android par available ────────────────────────────────
  try {
    if (!navigator.clipboard) {
      Object.defineProperty(navigator, 'clipboard', {
        get: () => ({
          readText:  () => Promise.resolve(''),
          writeText: (t) => Promise.resolve(),
          read:      () => Promise.resolve([]),
          write:     () => Promise.resolve()
        }),
        configurable: true
      });
    }
  } catch(e){}

})(__FP__);
"""


LEARNED = {}           # app url -> us page ne khud jo same-site GET (xhr/fetch) kiye, wahi hum dohrayenge
BAD_WORDS = ("logout", "signout", "delete", "remove", "clear", "reset", "stream", "chat/completions", "generate", "send")
JS_FETCH = """async (u)=>{const c=new AbortController();const t=setTimeout(()=>c.abort(),15000);
try{const r=await fetch(u,{credentials:'include',cache:'no-store',signal:c.signal});await r.arrayBuffer();return r.status}
catch(e){return -1}finally{clearTimeout(t)}}"""


def rwait(page, lo, hi):
    """Random ruko (sec). Beech ke chhote aur kabhi lambe wait zyada, taaki pattern na bane."""
    page.wait_for_timeout(int(random.triangular(lo, hi, lo + (hi - lo) * random.random() ** 2) * 1000))


# Tap sirf aisi jagah jahan koi link/button/input na ho (tap = click; app ka koi action na chal jaye)
JS_SAFE_TAP = """([x, y]) => {
  let n = document.elementFromPoint(x, y);
  if (!n) return false;
  const bad = 'a,button,input,select,textarea,label,summary,option,video,audio,canvas,iframe,[role],[onclick],[tabindex],[contenteditable]';
  for (; n && n !== document.documentElement; n = n.parentElement) {
    if (n.matches && n.matches(bad)) return false;
    if (getComputedStyle(n).cursor === 'pointer') return false;
  }
  return true;
}"""


class Touch:
    """Asli phone jaisa touch: tap aur swipe (CDP Input.dispatchTouchEvent). Mouse/wheel events nahi bante.
    cdp None ho (obscura) to purana wheel scroll fallback."""

    def __init__(self, page, cdp, vw, vh):
        self.page, self.cdp, self.vw, self.vh = page, cdp, vw, vh

    def _ev(self, typ, x=None, y=None, rad=7.0, force=0.5):
        pts = []
        if x is not None:
            pts = [{"x": round(x, 2), "y": round(y, 2), "id": 0,
                    "radiusX": round(rad + random.uniform(-0.6, 0.6), 2), "radiusY": round(rad + random.uniform(-0.6, 0.6), 2),
                    "force": round(max(0.05, min(1.0, force + random.uniform(-0.06, 0.06))), 3)}]
        self.cdp.send("Input.dispatchTouchEvent", {"type": typ, "touchPoints": pts})

    def swipe(self, scroll_down=True):
        """Ungli upar kheench ke neeche scroll (ya ulta). Halka tedha, beech me tez, ant me dheema."""
        vw, vh = self.vw, self.vh
        dist = random.uniform(vh * 0.12, vh * 0.55)
        if self.cdp is None:
            self.page.mouse.wheel(0, dist if scroll_down else -dist)
            return
        x0 = random.uniform(vw * 0.25, vw * 0.8)
        y0 = random.uniform(vh * 0.55, vh * 0.85) if scroll_down else random.uniform(vh * 0.2, vh * 0.45)
        y1 = min(vh * 0.95, max(vh * 0.05, y0 + (-dist if scroll_down else dist)))
        x1 = x0 + random.uniform(-vw * 0.07, vw * 0.07)
        n = random.randint(9, 18)
        step_ms = random.uniform(130, 420) / n
        rad, force = random.uniform(5.5, 10.0), random.uniform(0.3, 0.65)
        self._ev("touchStart", x0, y0, rad, force)
        for i in range(1, n + 1):
            t = i / n
            e = t * t * (3 - 2 * t)
            self.page.wait_for_timeout(max(4, int(step_ms * random.uniform(0.7, 1.3))))
            self._ev("touchMove", x0 + (x1 - x0) * e + random.uniform(-0.8, 0.8),
                     y0 + (y1 - y0) * e + random.uniform(-0.8, 0.8), rad, force)
        self._ev("touchEnd")

    def tap(self):
        """Safe jagah par ek tap. Jagah na mile to False (kuch nahi hota)."""
        if self.cdp is None:
            return False
        for _ in range(6):
            x = random.uniform(self.vw * 0.1, self.vw * 0.9)
            y = random.uniform(self.vh * 0.12, self.vh * 0.88)
            if not self.page.evaluate(JS_SAFE_TAP, [x, y]):
                continue
            rad, force = random.uniform(5.5, 10.0), random.uniform(0.3, 0.65)
            self._ev("touchStart", x, y, rad, force)
            self.page.wait_for_timeout(random.randint(45, 130))
            self._ev("touchEnd")
            return True
        return False


def h_scroll(touch):
    down = random.choice([True, True, True, False])
    for _ in range(random.randint(1, 3)):
        touch.swipe(down)
        touch.page.wait_for_timeout(random.randint(400, 1800))
        if random.random() < 0.25:
            down = not down


def h_inpage_request(page, url):
    """Page ke andar se same-site GET (jaise app khud karta hai). Chat/POST nahi bhejta, to AI kharcha nahi."""
    cand = LEARNED.get(url) or []
    path = random.choice(cand) if cand and random.random() < 0.75 else random.choice(INPAGE_PATHS)
    try:
        code = page.evaluate(JS_FETCH, path)
    except Exception as e:
        return f"{path}:err"
    return f"{path}:{code}"


def human_dwell(page, url, obs, touch):
    """Chhoti visit: random der rukna, touch swipe/tap, aur beech me page ke andar se request."""
    mid1 = DWELL_MIN + (DWELL_MAX - DWELL_MIN) * 0.25
    mid2 = DWELL_MIN + (DWELL_MAX - DWELL_MIN) * 0.6
    b = random.random()                                # kabhi chhoti, kabhi bahut lambi visit
    target = (random.uniform(DWELL_MIN, mid1) if b < 0.5 else
              random.uniform(mid1, mid2) if b < 0.85 else random.uniform(mid2, DWELL_MAX))
    if random.random() < 0.08:
        target *= random.uniform(1.6, 2.4)             # kabhi kabhi zyada der
    n_req = random.randint(1, INPAGE_MAX) if INPAGE else 0
    req_at = sorted(random.uniform(0.15, 0.9) * target for _ in range(n_req))
    reqs = []
    d0 = time.time()
    mem_chk = 0.0
    try:
        rwait(page, 1.5, 7.0)                          # page khulte hi koi kuch nahi karta, pehle dekhta hai
        while time.time() - d0 < target:
            if time.time() - mem_chk > 4:               # RAM sakht: limit paar hote hi visit samet lo, browser agli baar naya
                mem_chk = time.time()
                if mem_mb() > MEM_VISIT_LIMIT:
                    RESTART["why"] = f"memory limit {MEM_VISIT_LIMIT} MB paar (visit ke beech), Python {STATUS.get('py_now', 0):.0f} + Chrome {STATUS.get('chrome_now', 0):.0f} MB"
                    log(f"RAM {STATUS['mem_now']:.0f} MB > {MEM_VISIT_LIMIT} MB, visit jaldi khatam")
                    break
            if req_at and time.time() - d0 >= req_at[0]:
                req_at.pop(0)
                reqs.append(h_inpage_request(page, url))
                mem_mb()
                continue
            r = random.random()
            if r < 0.55:
                h_scroll(touch)
            elif r < 0.68:
                touch.tap()                            # safe khali jagah par tap (link/button par kabhi nahi)
            elif r < 0.85:
                rwait(page, 0.8, 4.5)                  # bas kuch der dekhna
            else:
                h_scroll(touch)
                rwait(page, 0.5, 2.0)
                touch.tap()
            rwait(page, 0.2, 2.5)
        while req_at:                                  # request bachi ho to bhej do (har visit me kam se kam 1)
            req_at.pop(0)
            reqs.append(h_inpage_request(page, url))
            rwait(page, 0.3, 1.5)
    except Exception as e:
        if not obs:
            log("human_dwell ruka:", type(e).__name__)
    return int(time.time() - d0), reqs


def visit(browser, url, direct=False):
    STATUS["visit_kinds"] = {}
    STATUS["visit_peak"] = STATUS["visit_py"] = STATUS["visit_chrome"] = 0.0     # peak wake + reload + dwell, poori visit ka
    _p0 = time.time()
    px, reused = pool_pick(url) if (proxy_auto() and not direct and STATUS["engine"] != "obscura") else (None, False)
    pick_ms = int((time.time() - _p0) * 1000)
    geo_ms = 0
    if px:
        _g0 = time.time()
        geo_lookup(px)                                  # proxy ka desh/timezone (fail ho to India default)
        geo_ms = int((time.time() - _g0) * 1000)
    base = mem_mb()                                     # visit shuru hone se pehle ki RAM (fresh browser ka asli base)
    base_py = STATUS.get("py_now", 0)
    base_kinds = dict(STATUS.get("visit_kinds") or {})
    sp = state_path(url, px)                            # is proxy IP ki apni cookie file
    wake_max = (REUSE_WAKE_MAX if reused else POOL_WAKE_MAX) if px else WAKE_MAX   # purana chala hua proxy: zyada sabr
    fp = get_fingerprint(url, px)                       # proxy ke hisaab se alag phone (px None to default phone)
    vp = fp["viewport"]                                 # hamesha phone ka size (is_mobile ke saath desktop size mismatch hota)
    kw = dict(user_agent=fp["ua"], viewport=vp,
              locale=fp["language"], timezone_id=fp["timezone_id"],
              extra_http_headers={"Accept-Language": f"{fp['language']},en;q=0.9"},   # navigator.languages se match
              java_script_enabled=JS_ON,
              has_touch=True, is_mobile=True,
              device_scale_factor=fp["dpr"])
    had_cookies = False
    if os.path.exists(sp):
        try:                                  # file sach me kharab ho tabhi hatao (browser crash par nahi)
            with open(sp) as f:
                json.load(f)
            had_cookies = True
        except (OSError, ValueError):
            try:
                os.remove(sp)
            except OSError:
                pass
    obs = STATUS["engine"] == "obscura"
    LAST_PX["hp"] = None
    if proxy_auto() and not obs:
        if px:
            kw["proxy"] = {"server": px}
            LAST_PX["hp"] = px
            log(f"pool proxy{' (purana, cookies ke saath)' if reused else ''}: {px} [{pool_country(px) or '?'}] phone={fp['model']} Android {fp['android']} tz={fp['timezone_id']}")
        elif not POOL_FALLBACK_DIRECT:
            raise NoProxy("pool me koi chalta proxy nahi")
    if obs and had_cookies:
        try:
            ctx = browser.new_context(storage_state=sp, **kw)
        except Exception as e:
            log("obscura: cookies load nahi hui, bina cookies:", type(e).__name__)
            had_cookies = False
            ctx = browser.new_context(**kw)
    else:
        ctx = browser.new_context(storage_state=sp, **kw) if had_cookies else browser.new_context(**kw)
    try:
        fp_json = json.dumps({
            "platform": fp["platform"], "vendor": fp["vendor"],
            "hardware_concurrency": fp["hardware_concurrency"],
            "device_memory": fp["device_memory"],
            "language": fp["language"],
            "screen": fp["screen"],
            "color_depth": fp["color_depth"],
            "dpr": fp["dpr"],
            "battery_level": fp["battery_level"],
            "battery_charging": fp["battery_charging"],
            "timezone_offset": fp["timezone_offset"],
            "seed": fp["seed"],
            "gpu_vendor": fp.get("gpu_vendor", "Qualcomm"), "gpu_renderer": fp.get("gpu_renderer", "Adreno (TM) 740"),
        })
        ctx.add_init_script(_FP_INIT_SCRIPT.replace("__FP__", fp_json))
    except Exception:
        if not obs:
            raise
    page = ctx.new_page()
    cdp = None
    if not obs:                               # touch + UA/Client Hints CDP se (obscura me CDP ye sab nahi karta)
        try:
            cdp = ctx.new_cdp_session(page)
        except Exception as e:
            log("cdp session nahi bana (touch band, wheel fallback):", type(e).__name__)
        if cdp is not None:
            try:
                native = page.evaluate("() => navigator.userAgentData ? navigator.userAgentData.brands : []")
            except Exception:
                native = []
            try:
                cdp.send("Emulation.setUserAgentOverride", ua_override(fp, native))
            except Exception as e:
                log("UA metadata set nahi hua:", type(e).__name__, str(e)[:100])
    touch = Touch(page, cdp, fp["viewport"]["width"], fp["viewport"]["height"])
    if not obs:                             # obscura render nahi karta, route ki zaroorat nahi
        page.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font") else r.continue_())
    t0, status, gate, first = time.time(), None, None, None
    ok, dwell_done, reloads, inpage = False, 0, 0, []
    host = urlparse(url).hostname
    nav = {"status": None, "gate": None, "n": 0}

    def on_resp(r):                           # page khud navigate kare (wake page -> app) to yahan pata chalta hai
        try:
            if r.request.is_navigation_request() and r.frame == page.main_frame:
                nav["status"], nav["gate"] = r.status, r.headers.get("x-blitz-gate")
                nav["n"] += 1
        except Exception:
            pass

    def on_req(rq):                           # app khud jo same-site GET (xhr/fetch) karta hai wo yaad rakho
        try:
            if rq.resource_type in ("xhr", "fetch") and rq.method == "GET":
                pu = urlparse(rq.url)
                low = rq.url.lower()
                if pu.hostname == host and not any(b in low for b in BAD_WORDS):
                    pth = pu.path + (("?" + pu.query) if pu.query else "")
                    lst = LEARNED.setdefault(url, [])
                    if pth and pth not in lst and len(pth) < 300:
                        lst.append(pth)
                        del lst[:-8]
        except Exception:
            pass

    def awake(s_, g_):
        return bool(s_ and s_ < 400 and not g_)

    def load():
        try:
            r = page.goto(url, wait_until="domcontentloaded", timeout=(POOL_NAV_TIMEOUT * 1000 if px else 90000))
            return (r.status if r else None), (r.headers.get("x-blitz-gate") if r else None)
        except Exception as e:
            return None, f"err:{type(e).__name__}"

    page.on("response", on_resp)
    page.on("request", on_req)
    try:
        status, gate = load()                 # sirf ek baar kholo
        first = (status, gate)
        ok = awake(status, gate)
        mem_mb()
        err_streak = 1 if str(gate).startswith("err:") else 0
        seen, last_load = nav["n"], time.time()
        while not ok and time.time() - t0 < wake_max:
            if px and err_streak >= WAKE_ERR_MAX:      # proxy se lagatar network error: ye proxy kharab, reload mat karo (naya proxy lagega)
                log(f"proxy se {err_streak} baar network error, chhod diya: {px}")
                break
            page.wait_for_timeout(2000)       # wake page par tab khula rehne do, bar-bar reload nahi
            if nav["n"] != seen:              # wake page ne khud app khola ya naya page laya
                seen = nav["n"]
                err_streak = 0
                status, gate = nav["status"], nav["gate"]
                ok = awake(status, gate)
                continue
            wait_s = 5 if str(gate).startswith("err:") else WAKE_RELOAD   # network error ho to jaldi retry, warna dheema reload
            if time.time() - last_load >= wait_s:
                reloads += 1
                status, gate = load()
                seen, last_load = nav["n"], time.time()
                ok = awake(status, gate)
                err_streak = err_streak + 1 if str(gate).startswith("err:") else 0
                mem_mb()
        if ok and HUMAN:
            dwell_done, inpage = human_dwell(page, url, obs, touch)
        elif ok:
            d0 = time.time()
            while time.time() - d0 < DWELL:         # page par asli insaan jaisa thoda ruko
                page.wait_for_timeout(random.randint(3000, 7000))
                mem_mb()
                try:
                    touch.swipe(random.random() < 0.75)
                except Exception:
                    if not obs:
                        break
            dwell_done = int(time.time() - d0)
        try:
            tmp = sp + ".tmp"
            ctx.storage_state(path=tmp)
            os.replace(tmp, sp)
        except Exception as e:
            log("cookie save failed:", type(e).__name__)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    was_asleep = not (first and first[0] and first[0] < 400 and not first[1])
    res = {"url": url, "status": status, "gate": gate, "took": int(time.time() - t0), "ok": ok,
           "was_asleep": was_asleep, "dwell": dwell_done, "cookies": had_cookies, "reloads": reloads, "inpage": inpage,
           "via": (LAST_PX["hp"] or "seedha"), "phone": fp.get("model"),
           "cc": (pool_country(LAST_PX["hp"]) if LAST_PX["hp"] else None), "android": fp.get("android"),
           "mem": int(STATUS.get("visit_peak") or 0),
           "py": int(STATUS.get("visit_py") or 0), "chrome": int(STATUS.get("visit_chrome") or 0),
           "procs": STATUS.get("visit_kinds") or {}, "base_procs": base_kinds, "cv": STATUS.get("chrome_full"), "headed": HEADED,
           "base": int(base), "base_py": int(base_py), "tz": fp.get("timezone_id"),
           "geo_ms": geo_ms, "pick_ms": pick_ms, "tried": PICK_INFO["tried"], "pool": pool_good_n(),
           "bage": int((time.time() - (STATUS["b_started"] or time.time())) / 60), "restarts": STATUS["restarts"]}
    log("visit", f"{url} status={status} gate={gate} took={res['took']}s {'AWAKE' if ok else 'STILL-ASLEEP'} "
                 f"was_asleep_before={'YES' if was_asleep else 'no'} cookies={'yes' if had_cookies else 'new'} reloads={reloads} js={'on' if JS_ON else 'OFF'} dwell={dwell_done}s inpage={inpage} "
                 f"fp=[{fp['platform']}/{fp['viewport']['width']}x{fp['viewport']['height']}/bat={fp['battery_level']}/cores={fp['hardware_concurrency']}] "
                 f"ram={res['mem']}MB(py={res['py']}+chrome={res['chrome']}) base={res['base']}MB pick={pick_ms}ms/{PICK_INFO['tried']} geo={geo_ms}ms tz={fp['timezone_id']}")
    log_add(res)
    return res


def mem_mb():
    """Python + Chromium ke saare processes ki memory (PSS, MB). Bina psutil ke, /proc se."""
    me = os.getpid()
    ppid = {}
    try:
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    with open(f"/proc/{d}/stat") as f:
                        raw = f.read()
                    ppid[int(d)] = int(raw[raw.rindex(")") + 2:].split()[1])
                except (OSError, ValueError, IndexError):
                    pass
    except OSError:
        return 0.0
    mine, changed = {me}, True
    while changed:
        changed = False
        for pid, pp in ppid.items():
            if pp in mine and pid not in mine:
                mine.add(pid)
                changed = True
    tot, py_kb = 0, 0
    kinds = {}
    for pid in mine:
        got = False
        before = tot
        try:
            with open(f"/proc/{pid}/smaps_rollup") as f:
                for line in f:
                    if line.startswith("Pss:"):
                        tot += int(line.split()[1])
                        got = True
                        break
        except OSError:
            pass
        if not got:
            try:
                with open(f"/proc/{pid}/statm") as f:
                    tot += int(f.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
            except (OSError, ValueError, IndexError):
                pass
        if pid == me:
            py_kb = tot - before
        else:                                   # Chromium process ka type (browser/renderer/gpu/network...) cmdline se
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().decode("utf-8", "replace")
                m = re.search(r"--type=([a-z\-]+)", cmd)
                k = m.group(1) if m else "browser"
                if k == "utility" and "network" in cmd:
                    k = "network"
                elif k == "utility" and "storage" in cmd:
                    k = "storage"
                kinds[k] = kinds.get(k, 0) + (tot - before)
            except OSError:
                pass
    mb = tot / 1024
    py = py_kb / 1024
    STATUS["mem_now"], STATUS["py_now"], STATUS["chrome_now"] = mb, py, max(0.0, mb - py)
    if mb > STATUS.get("visit_peak", 0):
        STATUS["visit_peak"] = mb
        STATUS["visit_kinds"] = {k: int(v / 1024) for k, v in kinds.items()}
        STATUS["visit_py"], STATUS["visit_chrome"] = py, max(0.0, mb - py)
    if mb > STATUS["mem_peak"]:
        STATUS["mem_peak"] = mb
    return mb


def stop_obscura():
    p = OBS["proc"]
    OBS["proc"] = None
    if p is not None and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def start_obscura():
    """obscura serve chalao aur Playwright ko CDP se jodo. Kuch bhi fail ho to exception."""
    import subprocess, socket
    stop_obscura()
    cmd = [OBSCURA_BIN, "serve", "--port", str(OBSCURA_PORT)]
    if OBSCURA_STEALTH:
        cmd.append("--stealth")
    _pu = get_proxy_url()
    if _pu:
        cmd += ["--proxy", _pu]
    OBS["proc"] = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while True:
        if OBS["proc"].poll() is not None:
            raise RuntimeError("obscura chalte hi band ho gaya")
        try:
            socket.create_connection(("127.0.0.1", OBSCURA_PORT), timeout=1).close()
            break
        except OSError:
            if time.time() - t0 > 20:
                raise RuntimeError("obscura ka port 20 sec me nahi khula")
            time.sleep(0.5)
    last = None
    for ep in (f"ws://127.0.0.1:{OBSCURA_PORT}", f"http://127.0.0.1:{OBSCURA_PORT}"):   # dono tareeke try
        try:
            return PW.chromium.connect_over_cdp(ep, timeout=30000)
        except Exception as e:
            last = e
    raise last


def obscura_failed(why):
    OBS["failed"] = True
    log("OBSCURA FAIL -> Chromium par wapas:", why)
    log_add({"event": "browser", "msg": f"Obscura fail, Chromium par wapas: {why}"[:200]})


def launch_browser(reason):
    """Browser ek baar kholo aur chalta rakho. Sirf zaroorat par (memory, umar, crash) restart."""
    global PW, BROWSER
    if BROWSER is not None:
        try:
            BROWSER.close()
        except Exception:
            pass
        BROWSER = None
    stop_obscura()
    if PW is None:
        PW = sync_playwright().start()
    engine = "chromium"
    if ENGINE_WANT == "obscura" and not OBS["failed"]:
        if not os.path.exists(OBSCURA_BIN):
            obscura_failed(f"binary nahi mili ({OBSCURA_BIN})")
        else:
            try:
                BROWSER = start_obscura()
                engine = "obscura"
            except Exception as e:
                BROWSER = None
                stop_obscura()
                obscura_failed(f"{type(e).__name__}: {str(e)[:100]}")
    if BROWSER is None:
        pc = proxy_cfg()
        kw = {"proxy": pc} if pc else {}
        if pc:
            log("proxy ON:", pc["server"])
        BROWSER = PW.chromium.launch(executable_path=CHROME, headless=not HEADED, timeout=120000, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--enable-low-end-device-mode",
            "--renderer-process-limit=1",                   # RAM: bahut saare renderer process nahi
            "--disable-features=site-per-process,IsolateOrigins,Translate,OptimizationHints,MediaRouter,AutofillServerCommunication,AudioServiceOutOfProcess,InterestFeedContentSuggestions",
            "--no-zygote", "--disable-component-update", "--disable-sync", "--disable-default-apps", "--disable-breakpad",
            "--disable-domain-reliability", "--disable-client-side-phishing-detection", "--metrics-recording-only",
            "--disable-gpu-shader-disk-cache", "--disable-hang-monitor", "--disable-renderer-backgrounding",
            "--disable-site-isolation-trials",
            "--disk-cache-size=1", "--media-cache-size=1", "--disable-software-rasterizer",
            "--js-flags=--max-old-space-size=128",          # JS heap par limit
            "--disable-extensions", "--disable-background-networking", "--mute-audio", "--no-first-run",
            "--window-size=412,915",                        # Android screen size
            "--force-device-scale-factor=3.0",             # Android DPR
        ], **kw)
    STATUS["engine"] = engine
    try:                                   # UA/Client Hints isi asli version se banenge, taaki engine se match kare
        v = str(BROWSER.version or "")
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", v):
            STATUS["chrome_full"] = v
    except Exception:
        pass
    if reason == IDLE_REASON:
        STATUS["b_started"] = STATUS["b_started"] or time.time()
        log("browser start:", f"[{engine}]", reason)
        return BROWSER
    STATUS["b_started"] = time.time()
    STATUS["restarts"] += 1
    STATUS["last_restart"] = f"{fmt_t(time.time())} - {reason}"
    log("browser start:", f"[{engine}]", reason)
    log_add({"event": "browser", "msg": f"browser start [{engine}]: {reason}"})
    return BROWSER


IDLE = {"closed": False}


def close_if_idle():
    """Visit khatam: Chromium band karo taaki RAM khali ho (proxy test ke liye bhi jagah). Sirf main loop thread se."""
    global BROWSER
    if not BROWSER_CLOSE_IDLE or BROWSER is None or STATUS["engine"] == "obscura":
        return
    try:
        BROWSER.close()
    except Exception:
        pass
    BROWSER = None
    IDLE["closed"] = True
    log(f"browser band (idle), RAM abhi {mem_mb():.0f} MB")


def get_browser():
    if BROWSER is None or not BROWSER.is_connected():
        if IDLE["closed"]:
            IDLE["closed"] = False
            return launch_browser(IDLE_REASON)
        return launch_browser("pehli baar" if STATUS["b_started"] is None else "browser band tha/crash")
    return BROWSER


def check_health():
    """Visit se pehle: proxy badla / memory ya umar zyada ho to browser restart (hamesha isi main thread se)."""
    why = RESTART.pop("why", None)
    if why:
        launch_browser(why)
        return
    if BROWSER is None:
        return
    mb = mem_mb()
    age_h = (time.time() - (STATUS["b_started"] or time.time())) / 3600
    if mb > MEM_LIMIT:
        launch_browser(f"memory {mb:.0f} MB > limit {MEM_LIMIT} MB, Python {STATUS.get('py_now', 0):.0f} + Chrome {STATUS.get('chrome_now', 0):.0f} MB")
    elif age_h > MAX_HOURS:
        launch_browser(f"{age_h:.0f} ghante purana")


NOTE_T = {"gate": 0.0}


def run_one(u):
    """Visit ke dauran STATUS["visiting"]=True (scan/backup isi se ruk-te hain)."""
    if not pool_ready():
        return _run_one(u)
    STATUS["visiting"] = True
    try:
        return _run_one(u)
    finally:
        STATUS["visiting"] = False


def _run_one(u):
    """Ek app ko visit karo (browser crash par ek baar retry). Result dict ya None."""
    if not pool_ready():                                  # proxy tayyar nahi: server IP se visit nahi, baad me
        if time.time() - NOTE_T["gate"] > 600:
            NOTE_T["gate"] = time.time()
            log_add({"event": "proxy", "msg": "visit ruki hain: proxy list abhi tak nahi aayi (dashboard me URL check karo)"})
        return None
    for attempt in (1, 2):
        try:
            check_health()
            res = visit(get_browser(), u)
            if LAST_PX["hp"] and res and res.get("ok"):
                recent_add(u, LAST_PX["hp"])
            tries = 0
            while LAST_PX["hp"] and not (res and res.get("ok")):
                pool_mark_bad(LAST_PX["hp"], "visit ok nahi")
                if tries < POOL_RETRIES and pool_ready():     # app jagni zaroori hai: ek aur naya proxy
                    tries += 1
                    log(f"proxy se app nahi jaagi, naya proxy ({tries}/{POOL_RETRIES}):", u)
                    res = visit(get_browser(), u)
                    if LAST_PX["hp"] and res and res.get("ok"):
                        recent_add(u, LAST_PX["hp"])
                    continue
                if POOL_FALLBACK_DIRECT:                      # sirf agar tumne khud on kiya ho
                    log("proxy se app nahi jaagi, seedha retry:", u)
                    res = visit(get_browser(), u, direct=True)
                break
            return res
        except NoProxy:
            log("visit ruki: koi chalta proxy nahi:", u)
            return None
        except Exception as e:
            log("visit error:", u, type(e).__name__, str(e)[:120])
            pool_mark_bad(LAST_PX["hp"], type(e).__name__)
            if STATUS["engine"] == "obscura":
                obscura_failed(f"visit error {type(e).__name__}: {str(e)[:80]}")
            try:
                launch_browser(f"error: {type(e).__name__}")
            except Exception as e2:
                log("browser relaunch failed:", type(e2).__name__, str(e2)[:120])
                break
    log_add({"url": u, "ok": False, "status": None, "gate": "browser-error", "took": 0,
             "was_asleep": True, "dwell": 0, "cookies": False})
    return None


def run_round(apps):
    for u in apps:
        run_one(u)
    mem_mb()


def next_gap():
    """Agli visit tak kitna ruko: 30 min me ~VISITS_30 visit, par har baar alag, kabhi jaldi kabhi der se."""
    base = 1800.0 / random.randint(VISITS_MIN, VISITS_MAX)    # har baar 30 min me 2-5 me se koi ek ginti
    j = GAP_JIT / 100.0
    g = random.uniform(base * (1 - j), base * (1 + j))
    r = random.random()
    if r < 0.12:
        g *= random.uniform(0.4, 0.7)
    elif r < 0.20:
        g *= random.uniform(1.15, 1.5)
    return max(120.0, g)


def loop_human():
    """Har app ka apna random time. Ek waqt par ek hi visit (RAM ke liye)."""
    DUE = {}
    last_prune = 0.0
    while True:
        apps = load_apps()
        now = time.time()
        for u in list(DUE):
            if u not in apps:
                DUE.pop(u)
        for u in apps:
            if u not in DUE:
                DUE[u] = now + random.uniform(10, max(40, 1800.0 / VISITS_MAX * 0.6))
        if TRIGGER.is_set():                       # "Abhi visit karo" ya naya app add hua
            TRIGGER.clear()
            for u in apps:
                DUE[u] = now + random.uniform(0, 15)
        due_now = [u for u in apps if DUE.get(u, now + 1) <= now]
        random.shuffle(due_now)
        if due_now:
            STATUS["running"] = True
            for i, u in enumerate(due_now):
                res = None
                try:
                    res = run_one(u)
                except Exception as e:
                    log("visit failed:", type(e).__name__, e)
                    log_add({"event": "error", "msg": f"{type(e).__name__}: {e}"[:200]})
                gap = random.uniform(RETRY_MIN, RETRY_MAX) if not (res and res.get("ok")) else next_gap()
                DUE[u] = time.time() + gap
                log(f"agli visit {u}: {int(gap)//60}m{int(gap)%60}s baad")
                if i < len(due_now) - 1:
                    TRIGGER.wait(timeout=random.uniform(5, 40))   # apps ke beech bhi random gap
            STATUS["running"] = False
            close_if_idle()
            mem_mb()
        if time.time() - last_prune > 600:
            last_prune = time.time()
            try:
                prune_log()
                n = prune_state(load_apps())
                if n:
                    log(f"purani cookie files hataayi: {n}")
            except Exception as e:
                log("prune failed:", e)
        nxt = min(DUE.values()) if DUE else time.time() + 60
        STATUS["next"] = nxt
        TRIGGER.wait(timeout=max(2.0, min(30.0, nxt - time.time())) + random.random())


def loop():
    prev = None
    while True:
        now = time.time()
        apps = load_apps()
        if apps:
            STATUS["running"] = True
            log_add({"event": "round", "gap": int(now - prev) if prev else None, "apps": len(apps)})
            log(f"=== round start ({len(apps)} apps)" + (f", pichhle se {int(now-prev)//60}m{int(now-prev)%60}s" if prev else ""))
            prev = now
            try:
                run_round(apps)
            except Exception as e:
                log("round failed:", type(e).__name__, e)
                log_add({"event": "error", "msg": f"{type(e).__name__}: {e}"[:200]})
            STATUS["running"] = False
        try:
            prune_log()
            n = prune_state(load_apps())
            if n:
                log(f"purani cookie files hataayi: {n}")
        except Exception as e:
            log("prune failed:", e)
        wait = EVERY + random.randint(0, 30)
        STATUS["next"] = time.time() + wait
        TRIGGER.wait(timeout=wait)
        TRIGGER.clear()


def self_ping():
    while True:
        time.sleep(240)
        if SELF:
            try:
                urllib.request.urlopen(SELF.rstrip("/") + "/ping", timeout=30).read()
            except Exception as e:
                log("self-ping failed:", e)


# ---------------------------------------------------------------- dashboard
CSS = """
:root{color-scheme:light dark;--bg:#f2f4f8;--panel:#fff;--ink:#141821;--mute:#667085;--line:#e3e7ef;--soft:#f5f7fb;--acc:#3a56d4;--on-acc:#fff;--ok:#0f7a56;--ok-bg:#e2f4ec;--warn:#9a5b00;--warn-bg:#fcefd6;--bad:#c42b3e;--bad-bg:#fde7ea;--py:#8b5cf6;--ch:#2f80ed}
@media(prefers-color-scheme:dark){:root{--bg:#0b0e13;--panel:#131821;--ink:#e8ecf3;--mute:#8b95a7;--line:#222a36;--soft:#171d27;--acc:#8aa0ff;--on-acc:#0b0e13;--ok:#3fd0a0;--ok-bg:#10302a;--warn:#f2b552;--warn-bg:#352a11;--bad:#ff7d8c;--bad-bg:#3b1a21;--py:#b49cff;--ch:#6eacff}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
.wrap{max-width:720px;margin:0 auto;padding:max(16px,env(safe-area-inset-top)) 14px max(40px,env(safe-area-inset-bottom))}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:4px 2px 14px}
h1{font-size:22px;font-weight:700;letter-spacing:-.02em;margin:0}
h2{font-size:15px;font-weight:650;margin:0}
.pill{display:inline-flex;align-items:center;gap:7px;padding:5px 12px;border-radius:99px;font-size:12.5px;font-weight:600;background:var(--ok-bg);color:var(--ok)}
.pill:before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.pill.y{background:var(--warn-bg);color:var(--warn)}.pill.r{background:var(--bad-bg);color:var(--bad)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;margin:12px 0;overflow:hidden}
.ph{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding:15px 16px 11px}
.pb{padding:4px 16px 16px}
.sub{color:var(--mute);font-size:12.5px}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}.warn{color:var(--warn);font-weight:600}
.meta{display:flex;flex-wrap:wrap;gap:2px 12px;color:var(--mute);font-size:12.5px}
.dot{width:9px;height:9px;border-radius:50%;background:#b3bac8;flex:none}.dot.g{background:var(--ok)}.dot.r{background:var(--bad)}
.app{display:grid;grid-template-columns:9px 1fr auto;gap:12px;align-items:center;padding:12px 16px;border-top:1px solid var(--line)}
.app b,.lg b{word-break:break-all;font-weight:600}
.ram{padding:4px 16px 14px}
.ramtop{display:flex;align-items:baseline;gap:6px;margin-bottom:8px}
.ramnum{font-size:30px;font-weight:700;letter-spacing:-.02em;line-height:1}
.bar{position:relative;height:10px;border-radius:6px;background:var(--soft);border:1px solid var(--line);overflow:hidden}
.bar i{display:block;float:left;height:100%}.bar .py{background:var(--py)}.bar .ch{background:var(--ch)}
.bar .lim{position:absolute;top:0;bottom:0;width:2px;background:var(--ink);transform:translateX(-2px)}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:9px;color:var(--mute);font-size:12.5px}
.sw{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:6px}.sw.py{background:var(--py)}.sw.ch{background:var(--ch)}
.cells{display:grid;grid-template-columns:repeat(3,1fr);border-top:1px solid var(--line)}
.cell{padding:12px 16px}.cell+.cell{border-left:1px solid var(--line)}
.cell .k{color:var(--mute);font-size:12.5px}.cell .v{font-size:19px;font-weight:700;letter-spacing:-.01em;margin-top:1px}
.log details.lg,.log .evr{border-top:1px solid var(--line)}
.lg summary{display:grid;grid-template-columns:9px 1fr auto 8px;gap:12px;align-items:center;padding:11px 16px;cursor:pointer;list-style:none}
.lg summary::-webkit-details-marker{display:none}
.lg summary::after{content:"";width:7px;height:7px;border-right:2px solid var(--mute);border-bottom:2px solid var(--mute);transform:rotate(45deg);margin-top:-3px}
.lg[open] summary::after{transform:rotate(-135deg);margin-top:3px}
.lg .end{display:flex;flex-direction:column;align-items:flex-end;gap:1px;text-align:right}
.lg .end .sub{display:flex;gap:10px}
.lg[open] summary{background:var(--soft)}
.kv{margin:0;padding:6px 16px 12px;background:var(--soft)}
.kv div{display:flex;justify-content:space-between;gap:16px;padding:5px 0;border-top:1px dashed var(--line)}.kv div:first-child{border-top:0}
.kv dt{color:var(--mute);flex:none}.kv dd{margin:0;text-align:right;word-break:break-all}
.evr{display:grid;grid-template-columns:44px 1fr;gap:10px;padding:9px 16px;color:var(--mute);font-size:13px}
.evr.bad{color:var(--bad);font-weight:400}
.chip{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:600;background:var(--soft);color:var(--mute);border:1px solid var(--line)}
.chip.g{background:var(--ok-bg);color:var(--ok);border-color:transparent}.chip.r{background:var(--bad-bg);color:var(--bad);border-color:transparent}.chip.y{background:var(--warn-bg);color:var(--warn);border-color:transparent}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--soft);border:1px solid var(--line);padding:2px 6px;border-radius:6px;word-break:break-all}
input[type=text],input[type=password]{display:block;width:100%;min-height:44px;padding:0 13px;margin:10px 0 0;border:1px solid var(--line);border-radius:11px;font-size:16px;font-family:inherit;background:var(--soft);color:var(--ink);outline:none}
input:focus-visible{border-color:var(--acc);box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 25%,transparent)}
button{min-height:42px;padding:0 16px;border:0;border-radius:11px;background:var(--acc);color:var(--on-acc);font-size:14px;font-weight:600;font-family:inherit;cursor:pointer}
button.soft{background:var(--soft);color:var(--ink);border:1px solid var(--line)}
button.red{background:var(--bad-bg);color:var(--bad)}
button.sm{min-height:34px;padding:0 12px;font-size:13px}
button:disabled{opacity:.5}
button:focus-visible,summary:focus-visible,a:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}.btns>*{flex:1 1 130px}
form{margin:0}
details.more{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
details.more summary{cursor:pointer;color:var(--mute);font-weight:600}
a{color:var(--acc);text-decoration:none;font-weight:600;font-size:13px}
.note{margin:0 0 10px}
"""

JS = """
document.querySelectorAll('form').forEach(f=>f.addEventListener('submit',()=>{setTimeout(()=>Array.from(f.elements).forEach(el=>{if(el.tagName==='BUTTON')el.disabled=true}),0)}));
setInterval(()=>{const a=document.activeElement;if((a&&a.tagName==='INPUT')||document.querySelector('details[open]'))return;location.reload()},12000);
"""


def _chip(txt, cls=""):
    return f'<span class="chip {cls}">{txt}</span>'


def esc(s):
    return html.escape(str(s), quote=True)


def ago(t):
    s = max(0, int(time.time() - t))
    if s < 60:
        return "abhi"
    if s < 3600:
        return f"{s // 60} min pehle"
    if s < 86400:
        return f"{s // 3600} ghante pehle"
    return f"{s // 86400} din pehle"


def dur(s):
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _host(u):
    return u.replace("https://", "").replace("http://", "").strip("/")


def _kv(k, v):
    return f"<div><dt>{k}</dt><dd>{v}</dd></div>"


def _log_row(e):
    hhmm = fmt_t(e["t"])[-8:]
    if "event" in e:
        cls = " bad" if e["event"] == "error" else ""
        return (f'<div class="evr{cls}"><span>{hhmm[:5]}</span>'
                f'<span>{esc(e.get("msg", e["event"]))}</span></div>')
    ok = bool(e.get("ok"))
    via = e.get("via")
    direct = via in (None, "seedha")
    shown = "" if direct else mask_proxy(via)
    scheme, hp = (shown.split("://", 1) if "://" in shown else ("", shown))
    st = esc(e["status"]) if e.get("status") is not None else ""
    if e.get("gate"):
        st = (st + " " + esc(e["gate"])).strip()
    st = st or "-"
    asleep = "so raha tha" if e.get("was_asleep") else "jaaga tha"
    end = f'<span>{e.get("took", 0)}s</span>'
    if e.get("mem"):
        end += f'<span>{e["mem"]} MB</span>'
    rows = [_kv("Proxy", "server IP" if direct else esc((scheme + " " + hp).strip()))]
    geo = " ".join(str(x) for x in (e.get("cc"), e.get("tz")) if x)
    if geo:
        rows.append(_kv("Desh / timezone", esc(geo)))
    if e.get("phone"):
        rows.append(_kv("Phone", esc(e["phone"]) + (" (Android " + esc(e["android"]) + ")" if e.get("android") else "")))
    rows.append(_kv("Cookies", "purani" if e.get("cookies") else "nayi"))
    rows.append(_kv("Reloads", esc(e.get("reloads", 0))))
    if e.get("dwell"):
        rows.append(_kv("Page par", f'{e["dwell"]}s'))
    if e.get("inpage"):
        rows.append(_kv("Page ke andar", esc(" ".join(str(x) for x in e["inpage"][:8]))))
    if e.get("mem"):
        split = f' (Python {e["py"]} + Chrome {e["chrome"]})' if "py" in e else ""
        rows.append(_kv("RAM peak", f'{e["mem"]} MB{split}'))
    if e.get("procs"):
        rows.append(_kv("Process RAM", esc(" ".join(f"{k}={v}" for k, v in sorted(e["procs"].items(), key=lambda x: -x[1])))))
    if e.get("base_procs"):
        rows.append(_kv("Process RAM (shuru me)", esc(" ".join(f"{k}={v}" for k, v in sorted(e["base_procs"].items(), key=lambda x: -x[1])))))
    if e.get("cv"):
        rows.append(_kv("Chromium", esc(e["cv"]) + (" headed" if e.get("headed") else " headless")))
    if e.get("base"):
        rows.append(_kv("RAM shuru me", f'{e["base"]} MB (Python {e.get("base_py", 0)})'))
    if "pick_ms" in e:
        rows.append(_kv("Proxy chunna", f'{e["pick_ms"]} ms, {e.get("tried", 0)} check, geo {e.get("geo_ms", 0)} ms, pool {e.get("pool", 0)}'))
    if "bage" in e:
        rows.append(_kv("Browser", f'{e["bage"]} min purana, restarts {e.get("restarts", 0)}'))
    dot = "g" if ok else "r"
    cls = "ok" if ok else "bad"
    return (f'<details class="lg"><summary><span class="dot {dot}"></span>'
            f'<span><b>{esc(_host(e["url"]))}</b><span class="meta"><span>{hhmm}</span><span>{asleep}</span></span></span>'
            f'<span class="end"><b class="{cls}">{st}</b><span class="sub">{end}</span></span></summary>'
            f'<dl class="kv">{"".join(rows)}</dl></details>')


def dashboard():
    apps = load_apps()
    now = time.time()
    entries = log_read(now - LOG_DAYS * 86400)
    day = [e for e in entries if e.get("t", 0) >= now - 86400 and "url" in e]
    # ---- apps
    app_rows, awake_n = [], 0
    for u in apps:
        mine = [e for e in entries if e.get("url") == u]
        last = mine[-1] if mine else None
        d24 = [e for e in day if e["url"] == u]
        slept = sum(1 for e in d24 if e.get("was_asleep"))
        if last and last.get("ok"):
            awake_n += 1
            dot, st = "g", '<span class="ok">Jaag raha hai</span>'
        elif last:
            dot, st = "r", '<span class="bad">So raha hai</span>'
        else:
            dot, st = "", "<span>Abhi visit nahi hui</span>"
        extra = (f'<span>aakhri visit {ago(last["t"])}</span><span>24h me so raha tha {slept}/{len(d24)}</span>'
                 if last else "")
        app_rows.append(
            f'<div class="app"><span class="dot {dot}"></span><div><b>{esc(_host(u))}</b>'
            f'<div class="meta">{st}{extra}</div></div>'
            f'<form method="post" action="/del" onsubmit="return confirm(\'Hata dun?\')">'
            f'<input type="hidden" name="url" value="{esc(u)}"><button class="soft sm">Hatao</button></form></div>')
    # ---- log (sirf pichle LOG_VIEW_HOURS ghante; poora log /log.txt download me)
    cut = now - LOG_VIEW_HOURS * 3600
    recent = [e for e in reversed(entries) if e.get("t", 0) >= cut and e.get("event") != "round"]
    shown = recent[:LOG_VIEW_MAX]
    log_html = "".join(_log_row(e) for e in shown)
    cut_note = (f'<div class="pb sub">Latest {LOG_VIEW_MAX} dikh rahe hain, baaki download me.</div>'
                if len(recent) > LOG_VIEW_MAX else "")
    # ---- status
    nxt = STATUS.get("next")
    nxt_s = "chal raha" if STATUS["running"] else (dur(nxt - now) if nxt else "-")
    up = int(now - STATUS["started"])
    mem, lim = STATUS.get("mem_now", 0) or 0, MEM_LIMIT
    py, chm = STATUS.get("py_now", 0) or 0, STATUS.get("chrome_now", 0) or 0
    if not (py or chm):
        chm = mem
    den = max(lim, mem, 1)
    w_py, w_ch, lim_x = py / den * 100, chm / den * 100, lim / den * 100
    mem_cls = "warn" if mem > lim else ""
    # ---- proxy
    cur, auto, f_url = get_proxy_url(), proxy_auto(), feed_url()
    with POOL_LOCK:
        pnote, found = POOL["note"], POOL["found"]
    pg = pool_good_n()
    ready = pool_ready()
    if auto:
        src = f'<code>{esc(mask_feed(f_url))}</code>' if f_url else _chip("URL set nahi hai", "r")
        top = '<span class="pill">Chal raha hai</span>' if ready else '<span class="pill y">Proxy list ka intezaar</span>'
        pchip = _chip("API list", "g" if ready else "y")
    elif cur:
        src, top, pchip = f'<code>{esc(mask_proxy(cur))}</code>', '<span class="pill">Manual proxy</span>', _chip("Manual", "g")
    else:
        src, top, pchip = "", '<span class="pill r">Proxy band (server IP)</span>', _chip("Band", "r")
    if BACKUP_URL:
        ok_t = ago(BACKUP["last_ok"]) if BACKUP["last_ok"] else "abhi tak nahi"
        state = _chip("ON", "g") if not BACKUP["err"] else _chip("Error: " + esc(BACKUP["err"]), "r")
        restored = f', restore: {esc(BACKUP["restored"])}' if BACKUP["restored"] else ""
        backup_html = (f'<div>{state} <span class="sub">aakhri save {ok_t}, {BACKUP["size"] // 1024} KB{restored}</span></div>'
                       '<form method="post" action="/backup"><div class="btns"><button name="act" value="save" class="soft">Abhi backup lo</button>'
                       '<button name="act" value="restore" class="soft" onclick="return confirm(\'Backup se wapas laun? Abhi ka data badal jayega.\')">Restore karo</button></div></form>')
    else:
        backup_html = f'<div>{_chip("OFF", "r")} <span class="sub">Render env me <code>BACKUP_URL</code> daalo.</span></div>'
    return f"""<!doctype html><html lang="hi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Keepalive</title><style>{CSS}</style></head><body><div class="wrap">
<div class="top"><h1>Keepalive</h1>{top}</div>

<section class="panel"><div class="ph"><h2>Apps</h2><span class="sub">{awake_n} / {len(apps)} jaag rahe</span></div>
{''.join(app_rows) or '<div class="pb sub">Koi app nahi. Neeche link daalo.</div>'}
<form class="pb" method="post" action="/add" style="border-top:1px solid var(--line);margin-top:0;padding-top:6px">
<input type="text" name="url" placeholder="https://myapp.blitz.cloud" required autocapitalize="off" autocorrect="off" spellcheck="false">
<div class="btns"><button>Add karo</button><button class="soft" formaction="/run">Abhi visit karo</button></div></form></section>

<section class="panel"><div class="ph"><h2>Server</h2><span class="sub">{dur(up)} se chalu</span></div>
<div class="ram"><div class="ramtop"><span class="ramnum {mem_cls}">{mem:.0f}</span><span class="sub">/ {lim} MB RAM</span></div>
<div class="bar"><i class="py" style="width:{w_py:.1f}%"></i><i class="ch" style="width:{w_ch:.1f}%"></i><b class="lim" style="left:{lim_x:.1f}%"></b></div>
<div class="legend"><span><i class="sw py"></i>Python {py:.0f}</span><span><i class="sw ch"></i>Chrome {chm:.0f}</span><span>peak {STATUS['mem_peak']:.0f}</span></div></div>
<div class="cells">
<div class="cell"><div class="k">Proxy ready</div><div class="v">{pg}</div><div class="sub">list me {found}</div></div>
<div class="cell"><div class="k">Agli visit</div><div class="v">{nxt_s}</div></div>
<div class="cell"><div class="k">Restarts</div><div class="v">{STATUS['restarts']}</div><div class="sub">{esc(STATUS['engine'])}</div></div>
</div></section>

<section class="panel log"><div class="ph"><h2>Log</h2><a href="/log.txt" download>Poora log download</a></div>
<div class="pb sub" style="padding-top:0;padding-bottom:10px">Pichhle {LOG_VIEW_HOURS} ghante, {len(shown)} entry.</div>
{log_html or '<div class="pb sub" style="border-top:1px solid var(--line);padding-top:12px">Is dauran koi log nahi.</div>'}{cut_note}</section>

<section class="panel"><div class="ph"><h2>Proxy</h2>{pchip}</div>
<div class="pb"><p class="note sub">{esc(pnote)}</p>{src}
<p class="sub">Har {feed_every()}s me list dobara aati hai.</p>
<div class="btns"><button name="act" value="refresh" form="feedform">Abhi list lo</button></div>
<details class="more"><summary>URL ya manual proxy badlo</summary>
<form id="feedform" method="post" action="/feed" autocomplete="off">
<input type="text" name="feed_url" placeholder="https://.../api/proxies?key=..." autocomplete="off" autocapitalize="off" spellcheck="false">
<input type="text" name="feed_every" placeholder="Refresh interval sec (min 120, default 300)" autocomplete="off" inputmode="numeric">
<div class="btns"><button name="act" value="save">Save + list lo</button><button name="act" value="interval" class="soft">Sirf interval save</button></div></form>
<form method="post" action="/proxy" autocomplete="off">
<input type="text" name="proxy_url" placeholder="http://user:pass@host:port" autocomplete="off" autocapitalize="off" spellcheck="false">
<div class="btns"><button name="act" value="save" class="soft">Manual save</button><button name="act" value="auto" class="soft">API list wapas</button><button name="act" value="off" class="red">Band</button></div></form>
</details></div></section>

<section class="panel"><div class="ph"><h2>Backup</h2></div><div class="pb">{backup_html}</div></section>
</div>
<script>{JS}</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authed(self):
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                good = hmac.compare_digest(u.encode(), ADMIN_USER.encode()) & hmac.compare_digest(p.encode(), ADMIN_PASS.encode())
                if good:
                    return True
            except Exception:
                pass
            time.sleep(1)           # galat password par thoda ruko
        self._send(401, "Login chahiye", "text/plain; charset=utf-8",
                   {"WWW-Authenticate": 'Basic realm="Keepalive"'})
        return False

    def _same_origin(self):
        host = self.headers.get("Host", "")
        for hdr in ("Origin", "Referer"):
            v = self.headers.get(hdr)
            if v:
                return urlparse(v).netloc == host
        return True

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/ping", "/robots.txt"):
            return self._send(200, "ok\n", "text/plain; charset=utf-8")
        if not self._authed():
            return
        if path == "/":
            return self._send(200, dashboard())
        if path == "/log.txt":
            lines = [json.dumps({**e, "time": fmt_t(e["t"])}) for e in log_read(time.time() - LOG_DAYS * 86400)]
            return self._send(200, "\n".join(lines) + "\n", "text/plain; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="keepalive-log.txt"'})
        self._send(404, "nahi mila", "text/plain; charset=utf-8")

    def do_POST(self):
        if not self._authed():
            return
        if not self._same_origin():
            return self._send(403, "blocked", "text/plain; charset=utf-8")
        n = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        val = (form.get("url") or [""])[0]
        path = urlparse(self.path).path
        msg = ""
        if path == "/backup":
            act = (form.get("act") or [""])[0]
            if not BACKUP_URL:
                return self._send(400, "BACKUP_URL Render env me set nahi hai.", "text/plain; charset=utf-8")
            if act == "restore":
                try:
                    backup_restore()
                except Exception as e:
                    return self._send(400, f"Restore nahi hua: {type(e).__name__} {str(e)[:100]}", "text/plain; charset=utf-8")
                POOL_EVT.set()
            elif act == "save":
                if not backup_save():
                    return self._send(400, f"Backup nahi hua: {BACKUP['err']}", "text/plain; charset=utf-8")
            return self._send(303, "", "text/plain; charset=utf-8", {"Location": "/"})
        if path == "/feed":
            act = (form.get("act") or [""])[0]
            raw = (form.get("feed_url") or [""])[0].strip()
            every_raw = (form.get("feed_every") or [""])[0].strip()
            if act == "refresh" and not raw:
                if not feed_url():
                    return self._send(400, "Pehle proxy list ka URL save karo.", "text/plain; charset=utf-8")
                POOL_EVT.set()
                return self._send(303, "", "text/plain; charset=utf-8", {"Location": "/"})
            if act == "interval":
                if not feed_url():
                    return self._send(400, "Pehle proxy list ka URL save karo.", "text/plain; charset=utf-8")
                if not every_raw.isdigit() or int(every_raw) < 120:
                    return self._send(400, "Interval kam se kam 120 sec hona chahiye.", "text/plain; charset=utf-8")
                save_feed(feed_url(), every=int(every_raw))
                log_event(f"proxy refresh interval save: {every_raw}s")
                return self._send(303, "", "text/plain; charset=utf-8", {"Location": "/"})
            url = norm_feed_url(raw) if raw else None
            if not url:
                return self._send(400, "URL likho, jaise https://proxy.xyz.blitz.cloud/api/proxies?key=...", "text/plain; charset=utf-8")
            try:
                lst = feed_fetch(url, timeout=20)                  # save se pehle check
                if not lst:
                    raise ValueError("list khali")
            except urllib.error.HTTPError as e:
                why = "key galat" if e.code in (401, 403) else f"HTTP {e.code}"
                return self._send(400, f"API ne mana kiya: {why}. Save nahi hua.", "text/plain; charset=utf-8")
            except Exception as e:
                return self._send(400, f"List nahi mili ({type(e).__name__}). URL check karo. Save nahi hua.", "text/plain; charset=utf-8")
            try:
                save_feed(url, every=int(every_raw) if every_raw.isdigit() and int(every_raw) >= 120 else None)
                if not proxy_auto():
                    save_proxy_url(PROXY_AUTO)
                    RESTART["why"] = "API list mode on"
            except Exception as e:
                return self._send(500, f"Save nahi hua: {type(e).__name__}", "text/plain; charset=utf-8")
            pool_set(lst, quiet=True)                              # list abhi se pool me, 5 min ka intezaar nahi
            log_event(f"proxy list URL save: {mask_feed(url)} ({len(lst)} proxy)")
            POOL_EVT.set()
            return self._send(303, "", "text/plain; charset=utf-8", {"Location": "/"})
        with LOCK:
            apps = load_apps()
            if path == "/add":
                u = norm_url(val)
                if not u:
                    return self._send(400, "Galat URL. Sirf " + ", ".join(SUFFIXES) + " wale link chalenge.", "text/plain; charset=utf-8")
                if u not in apps and len(apps) < MAX_APPS:
                    apps.append(u)
                    save_apps(apps)
                    TRIGGER.set()
            elif path == "/del":
                if val in apps:
                    apps.remove(val)
                    save_apps(apps)
                    pre = state_prefix(val)
                    for fn in os.listdir(STATE_DIR):
                        if fn.startswith(pre):
                            try:
                                os.remove(os.path.join(STATE_DIR, fn))
                            except OSError:
                                pass
            elif path == "/run":
                TRIGGER.set()
            elif path == "/proxy":
                act = (form.get("act") or [""])[0]
                raw = (form.get("proxy_url") or [""])[0].strip()
                if act == "off":
                    new_proxy = ""
                elif act == "auto":
                    new_proxy = PROXY_AUTO
                elif raw:
                    new_proxy = check_proxy(raw)
                    if not new_proxy:
                        return self._send(400, "Proxy URL galat. Format: http://user:pass@host:port ya socks5://host:port "
                                               "(socks5 par user/pass nahi chalta).", "text/plain; charset=utf-8")
                else:
                    return self._send(303, "", "text/plain; charset=utf-8", {"Location": "/"})   # khali Save = kuch nahi
                try:
                    save_proxy_url(new_proxy)
                except Exception as e:
                    return self._send(500, f"Save nahi hua: {type(e).__name__}", "text/plain; charset=utf-8")
                shown = "free auto pool" if new_proxy == PROXY_AUTO else (mask_proxy(new_proxy) or "band")
                if new_proxy == PROXY_AUTO:
                    POOL_EVT.set()
                log("proxy update:", shown)
                log_event(f"proxy: {shown}")
                # Playwright dusre thread se nahi chalta: yahan sirf flag, restart main loop karega
                RESTART["why"] = f"proxy badla ({shown})"
            else:
                return self._send(404, "nahi mila", "text/plain; charset=utf-8")
        self._send(303, "", "text/plain; charset=utf-8", {"Location": "/"})

    def log_message(self, *a):
        pass



# ================= Backup (BACKUP_URL env): disk reset hone par sab wapas =================
BACKUP_URL = os.environ.get("BACKUP_URL", "").strip()          # SECRET: https://.../p/<key>  (sirf Render env me, GitHub me kabhi nahi); /sql apne aap jud jata hai
BACKUP_EVERY = max(120, env_int("BACKUP_EVERY_SEC", 600))
BACKUP = {"last_ok": 0.0, "err": "", "size": 0, "method": "", "restored": ""}
_SMALL = ("apps.json", "proxy.txt", "feed.json")


def _backup_collect():
    files = {}
    for root, _d, names in os.walk(DATA):
        for n in names:
            if n.endswith(".tmp"):
                continue
            p = os.path.join(root, n)
            try:
                if os.path.getsize(p) <= 8 * 1024 * 1024:
                    with open(p, "rb") as f:
                        files[os.path.relpath(p, DATA)] = f.read()
            except OSError:
                pass
    with POOL_LOCK:
        good = list(POOL["list"][:300])                      # chhota fallback; asli list restore ke baad API se turant aati hai
    files["pool.json"] = json.dumps({"t": time.time(), "list": good}).encode()
    return files


_SCHEMA = "CREATE TABLE IF NOT EXISTS kv(name TEXT NOT NULL, part INTEGER NOT NULL, data TEXT NOT NULL, t REAL, PRIMARY KEY(name, part))"
_CHUNK = 200_000


def _sql_url():
    u = BACKUP_URL.rstrip("/")
    return u if u.endswith("/sql") else u + "/sql"


def _sql_post(payload, url=None, timeout=60):
    req = urllib.request.Request(url or _sql_url(), data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "keepalive"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(60 * 1024 * 1024).decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def _sql_rows(resp):
    """Alag-alag response shape se rows nikalo: list | {rows|results|data: [...]}; row dict ya list ho sakti hai."""
    if isinstance(resp, dict):
        for k in ("rows", "results", "data", "result"):
            if isinstance(resp.get(k), list):
                resp = resp[k]
                break
            if isinstance(resp.get(k), dict) and isinstance(resp[k].get("rows"), list):
                resp = resp[k]["rows"]
                break
    if isinstance(resp, list) and resp and isinstance(resp[0], dict) and isinstance(resp[0].get("rows"), list):
        resp = resp[0]["rows"]
    out = []
    for r in resp if isinstance(resp, list) else []:
        if isinstance(r, dict):
            out.append((r.get("name"), r.get("part"), r.get("data")))
        elif isinstance(r, (list, tuple)) and len(r) >= 3:
            out.append((r[0], r[1], r[2]))
    return out


def _err_text(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            return f"HTTP {e.code}: {e.read(200).decode('utf-8', 'replace')}"
        except Exception:
            return f"HTTP {e.code}"
    return type(e).__name__


def backup_save():
    if not BACKUP_URL:
        return False
    try:
        _sql_post({"script": _SCHEMA})
        now, batch = time.time(), [{"sql": "DELETE FROM kv"}]
        for name, raw in _backup_collect().items():
            enc = base64.b64encode(gzip.compress(raw)).decode()
            for i in range(0, max(1, len(enc)), _CHUNK):
                batch.append({"sql": "INSERT INTO kv(name, part, data, t) VALUES(?,?,?,?)",
                              "params": [name, i // _CHUNK, enc[i:i + _CHUNK], now]})
        _sql_post({"batch": batch})                         # ek transaction: aadha save nahi hoga
        BACKUP.update(last_ok=time.time(), err="", size=sum(len(x["params"][2]) for x in batch[1:]), method="SQL")
        return True
    except Exception as e:
        BACKUP["err"] = _err_text(e)[:160]
        return False


def backup_restore():
    """Backup (SQL) se files wapas. -> kitni files. Khali/galat ho to ValueError."""
    try:
        rows = _sql_rows(_sql_post({"sql": "SELECT name, part, data FROM kv ORDER BY name, part"}))
    except urllib.error.HTTPError as e:
        if "no such table" in _err_text(e).lower():
            raise ValueError("backup abhi khali hai")
        raise
    parts = {}
    for name, part, data in rows:
        if isinstance(name, str) and isinstance(data, str):
            parts.setdefault(name, []).append((int(part or 0), data))
    if not parts:
        raise ValueError("backup abhi khali hai")
    base, n = os.path.realpath(DATA), 0
    for rel, ps in parts.items():
        raw = gzip.decompress(base64.b64decode("".join(d for _p, d in sorted(ps))))
        if rel == "pool.json":
            d = json.loads(raw.decode())
            if time.time() - float(d.get("t", 0)) < 3 * 3600 and not POOL["list"]:
                lst = [x for x in d.get("list", []) if isinstance(x, str) and check_proxy(x)]
                if lst:
                    with POOL_LOCK:
                        POOL["list"] = lst
                        POOL["note"] = f"backup se {len(lst)} proxy (API se jaldi badal jayengi)"
                        POOL["updated"] = time.time()
            continue
        dest = os.path.realpath(os.path.join(DATA, rel))
        if not dest.startswith(base + os.sep):              # path se bahar likhna mana
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest + ".tmp", "wb") as f:
            f.write(raw)
        os.replace(dest + ".tmp", dest)
        n += 1
    BACKUP["restored"] = f"{n} files, {fmt_t(time.time())}"
    return n


def backup_boot():
    os.makedirs(DATA, exist_ok=True)
    if BACKUP_URL and not os.path.exists(APPS_FILE):                # naya/khali disk: pehle backup se wapas
        try:
            log("backup: disk khali hai, backup se wapas la raha hoon:", backup_restore(), "files")
        except Exception as e:
            BACKUP["err"] = f"restore: {type(e).__name__}"
            log("backup restore nahi hua:", type(e).__name__, str(e)[:100])


def backup_loop():
    def small():
        h = hashlib.sha1()
        for n in _SMALL:
            try:
                with open(os.path.join(DATA, n), "rb") as f:
                    h.update(f.read())
            except OSError:
                pass
        return h.hexdigest()
    last_small, last_full = None, 0.0
    while BACKUP_URL:
        if chrome_busy() or POOL["busy"]:   # Chrome/scan chal raha hai: baad me
            time.sleep(20)
            continue
        try:
            sm, now = small(), time.time()
            if sm != last_small or now - last_full >= BACKUP_EVERY:
                if backup_save():
                    last_small, last_full = sm, now
                else:
                    last_full = now - BACKUP_EVERY + 90               # 90 sec me dobara
        except Exception as e:
            BACKUP["err"] = type(e).__name__
        time.sleep(20)


if __name__ == "__main__":
    if not ADMIN_PASS:
        ADMIN_PASS = secrets.token_urlsafe(9)
        log(f"ADMIN_PASSWORD set nahi hai. Is baar ka password: {ADMIN_PASS}  (user: {ADMIN_USER}). "
            "Render Environment me ADMIN_PASSWORD daalo.")
    backup_boot()
    log_add({"event": "start"})
    try:
        scrub_proxy_log()
    except Exception as e:
        log("proxy log scrub failed:", type(e).__name__)
    threading.Thread(target=loop_human if HUMAN else loop, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    threading.Thread(target=pool_loop, daemon=True).start()
    threading.Thread(target=backup_loop, daemon=True).start()
    log("listening on", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
