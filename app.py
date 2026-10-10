#!/usr/bin/env python3
"""Render free web service: tiny HTTP server + Chromium jo blitz apps ko kholta rehta hai."""
import os, time, random, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from playwright.sync_api import sync_playwright

URLS = [u.strip() for u in os.environ.get("APP_URLS", "").replace("\n", ",").split(",") if u.strip()]
EVERY = int(os.environ.get("EVERY_MIN", "8")) * 60       # visits ke beech (sec), 30 min se kam rakho
WAKE_MAX = int(os.environ.get("WAKE_MAX", "300"))
CHROME = os.environ.get("CHROME_PATH", "/usr/bin/chromium")
PORT = int(os.environ.get("PORT", "10000"))
SELF = os.environ.get("RENDER_EXTERNAL_URL", "")          # Render khud deta hai
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
last = []

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

def visit(ctx, url):
    page = ctx.new_page()
    page.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font") else r.continue_())
    t0, status, gate = time.time(), None, None
    try:
        while time.time() - t0 < WAKE_MAX:
            try:
                r = page.goto(url, wait_until="domcontentloaded", timeout=90000)
                status = r.status if r else None
                gate = r.headers.get("x-blitz-gate") if r else None
            except Exception as e:
                status, gate = None, f"err:{type(e).__name__}"
            if status and status < 400 and not gate:
                break
            time.sleep(5)
        ok = bool(status and status < 400 and not gate)
        if ok:
            page.wait_for_timeout(random.randint(2500, 4000))
            page.mouse.wheel(0, random.randint(100, 400))
            page.wait_for_timeout(1000)
        line = f"{url} status={status} gate={gate} took={time.time()-t0:.0f}s {'AWAKE' if ok else 'STILL-ASLEEP'}"
        log("visit", line)
        return line
    finally:
        page.close()

def run_once():
    out = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, headless=True, timeout=120000, args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(user_agent=UA, viewport={"width": 1366, "height": 768}, locale="en-US",
                            timezone_id="Asia/Kolkata")
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        for u in URLS:
            out.append(visit(ctx, u))
        b.close()   # har round ke baad browser band: RAM free
    return out

def loop():
    while True:
        try:
            res = run_once()
            last[:] = [time.strftime("%Y-%m-%d %H:%M:%S")] + res
        except Exception as e:
            log("round failed:", type(e).__name__, e)
            last[:] = [time.strftime("%Y-%m-%d %H:%M:%S"), f"round failed: {e}"]
        time.sleep(EVERY + random.randint(0, 60))

def self_ping():
    # Render ko 15 min me inbound traffic chahiye warna wo so jata hai
    while True:
        time.sleep(240)
        if SELF:
            try:
                urllib.request.urlopen(SELF, timeout=30).read()
            except Exception as e:
                log("self-ping failed:", e)

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ("ok\n" + "\n".join(last) + "\n").encode()
        self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass

if __name__ == "__main__":
    if not URLS:
        log("APP_URLS khali hai (Render > Environment me daalo)")
    else:
        threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    log("listening on", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
