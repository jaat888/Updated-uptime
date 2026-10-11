#!/usr/bin/env python3
"""Render free web service: Chromium keepalive for blitz.cloud apps + dashboard.

Dashboard: "/" (password), apps add/delete, 3-din ka log. "/ping" public (uptime bot ke liye).
Cookies har app ke liye disk par save hoti hain aur purani apne-aap delete.
"""
import os, sys, time, json, random, threading, urllib.request, html, hmac, base64, hashlib, secrets
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
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

LOCK = threading.RLock()
TRIGGER = threading.Event()
STATUS = {"started": time.time(), "next": None, "running": False, "b_started": None, "restarts": 0,
          "last_restart": "-", "mem_now": 0.0, "mem_peak": 0.0, "engine": "chromium"}
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
def proxy_cfg():
    if not PROXY_URL:
        return None
    from urllib.parse import unquote
    u = urlparse(PROXY_URL if "://" in PROXY_URL else "http://" + PROXY_URL)
    cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        cfg["username"] = unquote(u.username)
        cfg["password"] = unquote(u.password or "")
    return cfg


# ---------------------------------------------------------------- visiting
def human_wiggle(page):
    page.mouse.move(random.randint(80, 1200), random.randint(80, 650), steps=random.randint(8, 20))
    if random.random() < 0.5:
        page.mouse.wheel(0, random.randint(-200, 400))


def visit(browser, url):
    sp = state_path(url)
    kw = dict(user_agent=UA, viewport={"width": 1366, "height": 768}, locale="en-US", timezone_id="Asia/Kolkata",
              java_script_enabled=JS_ON)
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
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    except Exception:
        if not obs:
            raise
    page = ctx.new_page()
    if not obs:                               # obscura render nahi karta, route ki zaroorat nahi
        page.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font") else r.continue_())
    t0, status, gate, first = time.time(), None, None, None
    ok, dwell_done, reloads = False, 0, 0
    nav = {"status": None, "gate": None, "n": 0}

    def on_resp(r):                           # page khud navigate kare (wake page -> app) to yahan pata chalta hai
        try:
            if r.request.is_navigation_request() and r.frame == page.main_frame:
                nav["status"], nav["gate"] = r.status, r.headers.get("x-blitz-gate")
                nav["n"] += 1
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
        if ok:
            d0 = time.time()
            while time.time() - d0 < DWELL:         # page par asli insaan jaisa thoda ruko
                page.wait_for_timeout(random.randint(3000, 7000))
                mem_mb()
                try:
                    human_wiggle(page)
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
           "was_asleep": was_asleep, "dwell": dwell_done, "cookies": had_cookies, "reloads": reloads}
    log("visit", f"{url} status={status} gate={gate} took={res['took']}s {'AWAKE' if ok else 'STILL-ASLEEP'} "
                 f"was_asleep_before={'YES' if was_asleep else 'no'} cookies={'yes' if had_cookies else 'new'} reloads={reloads} js={'on' if JS_ON else 'OFF'}")
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
    if PROXY_URL:
        cmd += ["--proxy", PROXY_URL]
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
        BROWSER = PW.chromium.launch(executable_path=CHROME, headless=True, timeout=120000, args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--enable-low-end-device-mode",                 # naap me: bhaari page par peak ~20% kam
            "--disable-extensions", "--disable-background-networking", "--mute-audio", "--no-first-run"], **kw)
    STATUS["engine"] = engine
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
    """Visit se pehle: memory ya umar zyada ho to browser restart."""
    if BROWSER is None:
        return
    mb = mem_mb()
    age_h = (time.time() - (STATUS["b_started"] or time.time())) / 3600
    if mb > MEM_LIMIT:
        launch_browser(f"memory {mb:.0f} MB > limit {MEM_LIMIT} MB")
    elif age_h > MAX_HOURS:
        launch_browser(f"{age_h:.0f} ghante purana")


def run_round(apps):
    for u in apps:
        done = False
        for attempt in (1, 2):
            try:
                check_health()
                visit(get_browser(), u)
                done = True
                break
            except Exception as e:
                log("visit error:", u, type(e).__name__, str(e)[:120])
                if STATUS["engine"] == "obscura":
                    obscura_failed(f"visit error {type(e).__name__}: {str(e)[:80]}")
                try:
                    launch_browser(f"error: {type(e).__name__}")
                except Exception as e2:
                    log("browser relaunch failed:", type(e2).__name__, str(e2)[:120])
                    break
        if not done:
            log_add({"url": u, "ok": False, "status": None, "gate": "browser-error", "took": 0,
                     "was_asleep": True, "dwell": 0, "cookies": False})
    mem_mb()


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
    bs = STATUS["b_started"]
    if bs:
        ag = int(now - bs)
        browser_line = (f"engine: <b>{STATUS['engine']}</b> &middot; khula hai {ag//3600}h {ag%3600//60}m se &middot; memory abhi {STATUS['mem_now']:.0f} MB, "
                        f"peak {STATUS['mem_peak']:.0f} MB (limit {MEM_LIMIT} MB) &middot; starts: {STATUS['restarts']} "
                        f"&middot; aakhri: {esc(STATUS['last_restart'])}")
    else:
        browser_line = "abhi shuru nahi hua"
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Keepalive</title><style>{CSS}</style></head><body>
<h1>Keepalive dashboard</h1>
<div class="mut">Server chalu hua: {fmt_t(STATUS["started"])} ({up//3600}h {up%3600//60}m pehle) &middot; agla round: {nxt_s}
&middot; gap {EVERY//60} min &middot; page par ruko {DWELL}s &middot; cookies: {len(files)} files, {cb//1024} KB</div>
<div class="mut">Browser: {browser_line}</div>
<div class="card"><h2 style="margin-top:0">Apps ({len(apps)})</h2>
<table><tr><th>App</th><th>Abhi</th><th>Aakhri visit</th><th>24h me so raha tha</th><th></th></tr>{''.join(rows) or '<tr><td colspan=5 class="mut">Koi app nahi. Neeche se jodo.</td></tr>'}</table>
<form method="post" action="/add" style="display:block;margin-top:10px"><input type="text" name="url" placeholder="https://myapp.blitz.cloud" required>
<div style="margin-top:8px"><button>Add karo</button></div></form>
<form method="post" action="/run" style="margin-top:8px"><button class="grey">Abhi visit karo</button></form></div>
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
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    log("listening on", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
