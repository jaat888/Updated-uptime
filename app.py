#!/usr/bin/env python3
"""Render free web service: Chromium keepalive for blitz.cloud apps + dashboard.

Dashboard: "/" (password), apps add/delete, 3-din ka log. "/ping" public (uptime bot ke liye).
Cookies har app ke liye disk par save hoti hain aur purani apne-aap delete.
"""
import os, re, sys, time, json, random, threading, urllib.request, html, hmac, base64, hashlib, secrets
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
VISITS_30 = max(1, env_int("VISITS_PER_30MIN", 2))   # ek app par 30 min me kitni visit (average)
GAP_JIT = max(0, min(90, env_int("GAP_JITTER_PCT", 70)))   # gap kitna idhar-udhar (%)
DWELL_MIN = max(3, env_int("DWELL_MIN_SEC", 8))       # page par minimum rukna
DWELL_MAX = max(DWELL_MIN + 1, env_int("DWELL_MAX_SEC", 35))
INPAGE = env_int("INPAGE_REQ", 1) != 0                # page ke andar se same-site request
INPAGE_MAX = max(1, env_int("INPAGE_MAX", 3))         # ek visit me max itni
INPAGE_PATHS = [x.strip() for x in os.environ.get("INPAGE_PATHS", "/").split(",") if x.strip().startswith("/")] or ["/"]
RETRY_MIN, RETRY_MAX = 60, 180                        # wake fail ho to itne sec baad dobara
LOG_DAYS = env_int("LOG_DAYS", 3)
COOKIE_DAYS = env_int("COOKIE_DAYS", 3)
MEM_LIMIT = env_int("MEM_LIMIT_MB", 500)        # itni memory (MB) par browser restart hoga
MAX_HOURS = env_int("BROWSER_MAX_HOURS", 12)    # itne ghante baad browser restart (safety)
MAX_LOG_LINES = 20000
MAX_STATE_BYTES = 300 * 1024
MAX_APPS = 50
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


def state_path(url):
    return os.path.join(STATE_DIR, hashlib.sha1(url.encode()).hexdigest()[:16] + ".json")


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
    keep = {os.path.basename(state_path(u)) for u in apps}
    cutoff = time.time() - COOKIE_DAYS * 86400
    removed = 0
    for name in os.listdir(STATE_DIR):
        p = os.path.join(STATE_DIR, name)
        try:
            st = os.stat(p)
            if name not in keep or st.st_mtime < cutoff or st.st_size > MAX_STATE_BYTES or name.endswith(".tmp"):
                os.remove(p)
                removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------- proxy (optional)
PROXY_OFF = "off"   # proxy.txt me ye likha ho to env wala PROXY_URL bhi ignore (dashboard se "Band karo")


def get_proxy_url():
    """Runtime proxy URL: pehle file (dashboard se set), phir env. File me PROXY_OFF ho to koi proxy nahi."""
    try:
        with open(PROXY_FILE) as f:
            v = f.read().strip()
            if v == PROXY_OFF:
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
    if u.scheme not in ("http", "https", "socks5") or not u.hostname or not port:
        return None
    if u.scheme == "socks5" and (u.username or u.password):
        return None   # Chromium socks5 par login support nahi karta
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

ANDROID_VIEWPORT = {"width": 412, "height": 915}   # Galaxy S23 Ultra
ANDROID_SCREEN   = {"width": 412, "height": 915}
ANDROID_MODEL = "SM-S918B"
ANDROID_VERSION = "13"


def chrome_major():
    return int(STATUS["chrome_full"].split(".")[0])


def android_ua():
    """UA asli Chromium ke major version se banti hai (Android Chrome 'major.0.0.0' likhta hai)."""
    return (f"Mozilla/5.0 (Linux; Android {ANDROID_VERSION}; {ANDROID_MODEL}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_major()}.0.0.0 Mobile Safari/537.36")


def get_fingerprint(url):
    """Sab apps k liye same Android phone fingerprint."""
    return {
        "ua": android_ua(),
        "platform": "Linux armv8l",
        "vendor": "Google Inc.",
        "viewport": ANDROID_VIEWPORT,
        "screen": ANDROID_SCREEN,
        "color_depth": 24,
        "dpr": 3.0,
        "hardware_concurrency": 8,
        "device_memory": 8,
        "language": "en-IN",
        "timezone_id": "Asia/Kolkata",
        "timezone_offset": 330,
        "battery_level": 0.82,
        "battery_charging": False,
        "touch": True,
        "seed": FP_SEED,
    }


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
            "platform": "Android", "platformVersion": f"{ANDROID_VERSION}.0.0",
            "architecture": "", "bitness": "", "model": ANDROID_MODEL,   # Android Chrome in dono ko khali rakhta hai
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
  try { Date.prototype.getTimezoneOffset = function() { return -330; }; } catch(e){}

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
          if (p === 37445) return 'Qualcomm';
          // UNMASKED_RENDERER_WEBGL
          if (p === 37446) return 'Adreno (TM) 740';
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
    target = random.uniform(DWELL_MIN, DWELL_MAX)
    if random.random() < 0.08:
        target *= random.uniform(1.6, 2.4)             # kabhi kabhi zyada der
    n_req = random.randint(1, INPAGE_MAX) if INPAGE else 0
    req_at = sorted(random.uniform(0.15, 0.9) * target for _ in range(n_req))
    reqs = []
    d0 = time.time()
    try:
        rwait(page, 1.5, 7.0)                          # page khulte hi koi kuch nahi karta, pehle dekhta hai
        while time.time() - d0 < target:
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


def visit(browser, url):
    sp = state_path(url)
    fp = get_fingerprint(url)                           # per-app stable fingerprint
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
            r = page.goto(url, wait_until="domcontentloaded", timeout=90000)
            return (r.status if r else None), (r.headers.get("x-blitz-gate") if r else None)
        except Exception as e:
            return None, f"err:{type(e).__name__}"

    page.on("response", on_resp)
    page.on("request", on_req)
    try:
        status, gate = load()                 # sirf ek baar kholo
        first = (status, gate)
        ok = awake(status, gate)
        seen, last_load = nav["n"], time.time()
        while not ok and time.time() - t0 < WAKE_MAX:
            page.wait_for_timeout(2000)       # wake page par tab khula rehne do, bar-bar reload nahi
            if nav["n"] != seen:              # wake page ne khud app khola ya naya page laya
                seen = nav["n"]
                status, gate = nav["status"], nav["gate"]
                ok = awake(status, gate)
                continue
            wait_s = 5 if str(gate).startswith("err:") else WAKE_RELOAD   # network error ho to jaldi retry, warna dheema reload
            if time.time() - last_load >= wait_s:
                reloads += 1
                status, gate = load()
                seen, last_load = nav["n"], time.time()
                ok = awake(status, gate)
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
           "was_asleep": was_asleep, "dwell": dwell_done, "cookies": had_cookies, "reloads": reloads, "inpage": inpage}
    log("visit", f"{url} status={status} gate={gate} took={res['took']}s {'AWAKE' if ok else 'STILL-ASLEEP'} "
                 f"was_asleep_before={'YES' if was_asleep else 'no'} cookies={'yes' if had_cookies else 'new'} reloads={reloads} js={'on' if JS_ON else 'OFF'} dwell={dwell_done}s inpage={inpage} "
                 f"fp=[{fp['platform']}/{fp['viewport']['width']}x{fp['viewport']['height']}/bat={fp['battery_level']}/cores={fp['hardware_concurrency']}]")
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
    tot = 0
    for pid in mine:
        got = False
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
    mb = tot / 1024
    STATUS["mem_now"] = mb
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
    STATUS["b_started"] = time.time()
    STATUS["restarts"] += 1
    STATUS["last_restart"] = f"{fmt_t(time.time())} - {reason}"
    log("browser start:", f"[{engine}]", reason)
    log_add({"event": "browser", "msg": f"browser start [{engine}]: {reason}"})
    return BROWSER


def get_browser():
    if BROWSER is None or not BROWSER.is_connected():
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
        launch_browser(f"memory {mb:.0f} MB > limit {MEM_LIMIT} MB")
    elif age_h > MAX_HOURS:
        launch_browser(f"{age_h:.0f} ghante purana")


def run_one(u):
    """Ek app ko visit karo (browser crash par ek baar retry). Result dict ya None."""
    for attempt in (1, 2):
        try:
            check_health()
            return visit(get_browser(), u)
        except Exception as e:
            log("visit error:", u, type(e).__name__, str(e)[:120])
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
    base = 1800.0 / VISITS_30
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
                DUE[u] = now + random.uniform(10, max(40, 1800.0 / VISITS_30 * 0.6))
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
body{font-family:system-ui,sans-serif;margin:0;padding:12px;background:#f5f5f7;color:#1c1c1e;font-size:15px}
h1{font-size:20px;margin:4px 0 2px}h2{font-size:16px;margin:18px 0 6px}
.card{background:#fff;border-radius:12px;padding:12px;margin:10px 0;box-shadow:0 1px 3px #0001;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:5px 6px;border-bottom:1px solid #eee;white-space:nowrap}
.ok{color:#0a7d33;font-weight:600}.bad{color:#c0262d;font-weight:600}.mut{color:#777;font-size:12px}
input[type=text],input[type=url]{padding:9px;width:100%;box-sizing:border-box;border:1px solid #ccc;border-radius:8px;font-size:15px}
button{padding:8px 14px;border:0;border-radius:8px;background:#1c64f2;color:#fff;font-size:14px}
button.red{background:#c0262d}button.grey{background:#555}
form{display:inline}
"""


def esc(s):
    return html.escape(str(s), quote=True)


def dashboard():
    apps = load_apps()
    now = time.time()
    entries = log_read(now - LOG_DAYS * 86400)
    day = [e for e in entries if e.get("t", 0) >= now - 86400 and "url" in e]
    rows = []
    for u in apps:
        mine = [e for e in entries if e.get("url") == u]
        last = mine[-1] if mine else None
        d24 = [e for e in day if e["url"] == u]
        slept = sum(1 for e in d24 if e.get("was_asleep"))
        if last:
            st = f'<span class="{"ok" if last.get("ok") else "bad"}">{"AWAKE" if last.get("ok") else "ASLEEP"}</span>'
            lt = fmt_t(last["t"])
        else:
            st, lt = '<span class="mut">abhi tak visit nahi</span>', "-"
        rows.append(
            f"<tr><td>{esc(u)}</td><td>{st}</td><td>{lt}</td><td>{slept}/{len(d24)}</td>"
            f'<td><form method="post" action="/del" onsubmit="return confirm(\'Hata dun?\')">'
            f'<input type="hidden" name="url" value="{esc(u)}"><button class="red">Delete</button></form></td></tr>')
    log_rows = []
    for e in reversed(entries[-150:]):
        if "event" in e:
            if e["event"] == "round":
                g = e.get("gap")
                log_rows.append(f'<tr><td>{fmt_t(e["t"])}</td><td colspan="5" class="mut">--- round, {e.get("apps")} apps'
                                + (f", pichhle se {g//60}m{g%60}s" if g else "") + "</td></tr>")
            else:
                log_rows.append(f'<tr><td>{fmt_t(e["t"])}</td><td colspan="5" class="bad">{esc(e.get("msg", e["event"]))}</td></tr>')
            continue
        log_rows.append(
            f'<tr><td>{fmt_t(e["t"])}</td><td>{esc(e["url"].replace("https://", "").replace("http://", ""))}</td>'
            f'<td class="{"ok" if e.get("ok") else "bad"}">{esc(e.get("status"))}{" " + esc(e["gate"]) if e.get("gate") else ""}</td>'
            f'<td>{"SO RAHA THA" if e.get("was_asleep") else "jaaga tha"}</td><td>{e.get("took")}s</td>'
            f'<td>{"cookies" if e.get("cookies") else "nayi"}</td></tr>')
    nxt = STATUS.get("next")
    nxt_s = "abhi chal raha hai" if STATUS["running"] else (f"{max(0, int(nxt - now))} sec me" if nxt else "-")
    up = int(now - STATUS["started"])
    try:
        files = os.listdir(STATE_DIR)
        cb = sum(os.path.getsize(os.path.join(STATE_DIR, f)) for f in files)
    except OSError:
        files, cb = [], 0
    if HUMAN:
        mode_line = f"insaan mode: ~{VISITS_30} visit/30 min (random), page par {DWELL_MIN}-{DWELL_MAX}s, andar se request {'ON' if INPAGE else 'OFF'}"
    else:
        mode_line = f"gap {EVERY//60} min, page par ruko {DWELL}s"
    bs = STATUS["b_started"]
    if bs:
        ag = int(now - bs)
        browser_line = (f"engine: <b>{STATUS['engine']}</b> &middot; khula hai {ag//3600}h {ag%3600//60}m se &middot; memory abhi {STATUS['mem_now']:.0f} MB, "
                        f"peak {STATUS['mem_peak']:.0f} MB (limit {MEM_LIMIT} MB) &middot; starts: {STATUS['restarts']} "
                        f"&middot; aakhri: {esc(STATUS['last_restart'])}")
    else:
        browser_line = "abhi shuru nahi hua"
    cur_proxy = get_proxy_url()
    proxy_status = (f'<span class="ok">ON</span> &mdash; <code>{esc(mask_proxy(cur_proxy))}</code>'
                    if cur_proxy else '<span class="mut">Band hai (server ka apna IP use ho raha hai)</span>')
    proxy_card = f"""
<div class="card"><h2 style="margin-top:0">Proxy</h2>
<div style="margin-bottom:8px">Abhi: {proxy_status}</div>
<form method="post" action="/proxy" style="display:block" autocomplete="off">
  <input type="text" name="proxy_url" placeholder="naya proxy: http://user:pass@host:port" autocomplete="off">
  <div style="margin-top:8px;display:flex;gap:8px">
    <button type="submit" name="act" value="save">Save karo</button>
    <button type="submit" name="act" value="off" class="red">Band karo</button>
  </div>
</form>
<div class="mut" style="margin-top:8px">Format: <code>http://user:pass@host:port</code> &nbsp;|&nbsp; <code>socks5://host:port</code> (socks5 par login nahi chalta)<br>
Password yahan kabhi nahi dikhta. Box khali chhod ke Save karoge to kuch nahi badlega.
Naya proxy agli visit se pehle browser restart karke lagega.</div>
</div>"""
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Keepalive</title><style>{CSS}</style></head><body>
<h1>Keepalive dashboard</h1>
<div class="mut">Server chalu hua: {fmt_t(STATUS["started"])} ({up//3600}h {up%3600//60}m pehle) &middot; agla round: {nxt_s}
&middot; {mode_line} &middot; cookies: {len(files)} files, {cb//1024} KB</div>
<div class="mut">Browser: {browser_line}</div>
<div class="card"><h2 style="margin-top:0">Apps ({len(apps)})</h2>
<table><tr><th>App</th><th>Abhi</th><th>Aakhri visit</th><th>24h me so raha tha</th><th></th></tr>{''.join(rows) or '<tr><td colspan=5 class="mut">Koi app nahi. Neeche se jodo.</td></tr>'}</table>
<form method="post" action="/add" style="display:block;margin-top:10px"><input type="text" name="url" placeholder="https://myapp.blitz.cloud" required>
<div style="margin-top:8px"><button>Add karo</button></div></form>
<form method="post" action="/run" style="margin-top:8px"><button class="grey">Abhi visit karo</button></form></div>
{proxy_card}
<div class="card"><h2 style="margin-top:0">Log (pichhle {LOG_DAYS} din, naya upar)</h2>
<table><tr><th>Time</th><th>App</th><th>Status</th><th>Visit se pehle</th><th>Laga</th><th>Browser</th></tr>{''.join(log_rows) or '<tr><td colspan=6 class="mut">Abhi log khali hai.</td></tr>'}</table>
<div class="mut" style="margin-top:8px"><a href="/log.txt">log download</a></div></div>
<div class="mut">Dhyan: Render free ka disk restart/redeploy par saaf ho jata hai. Tab apps list <code>APP_URLS</code> se wapas banegi aur log khali hoga.</div>
</body></html>"""


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
            return self._send(200, "\n".join(lines) + "\n", "text/plain; charset=utf-8")
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
                    try:
                        os.remove(state_path(val))
                    except OSError:
                        pass
            elif path == "/run":
                TRIGGER.set()
            elif path == "/proxy":
                act = (form.get("act") or [""])[0]
                raw = (form.get("proxy_url") or [""])[0].strip()
                if act == "off":
                    new_proxy = ""
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
                shown = mask_proxy(new_proxy) or "band"
                log("proxy update:", shown)
                log_add({"event": "proxy", "msg": f"proxy: {shown}"})
                # Playwright dusre thread se nahi chalta: yahan sirf flag, restart main loop karega
                RESTART["why"] = f"proxy badla ({shown})"
            else:
                return self._send(404, "nahi mila", "text/plain; charset=utf-8")
        self._send(303, "", "text/plain; charset=utf-8", {"Location": "/"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not ADMIN_PASS:
        ADMIN_PASS = secrets.token_urlsafe(9)
        log(f"ADMIN_PASSWORD set nahi hai. Is baar ka password: {ADMIN_PASS}  (user: {ADMIN_USER}). "
            "Render Environment me ADMIN_PASSWORD daalo.")
    log_add({"event": "start"})
    try:
        scrub_proxy_log()
    except Exception as e:
        log("proxy log scrub failed:", type(e).__name__)
    threading.Thread(target=loop_human if HUMAN else loop, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    log("listening on", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
