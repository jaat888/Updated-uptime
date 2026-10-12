// Proxy Hub: fetch lists -> scan every few minutes (max 5000) -> API + dashboard. Persists in Blitz SQL. Zero deps (Node 20+).
const http = require('http'), net = require('net'), tls = require('tls'), crypto = require('crypto');
const E = process.env, PORT = +E.PORT || 3000, DB = (E.DB_URL || '').replace(/\/$/, ''), PASS = E.ADMIN_PASSWORD || 'change-me';
const SIG = k => crypto.createHmac('sha256', PASS).update(k).digest('hex');
let CONC = +E.CONCURRENCY || 2000, INT = +E.FETCH_MINUTES || 5, TMO = +E.TIMEOUT_MS || 6000, MAXSCAN = +E.MAX_SCAN || 10000;  // no upper limit any more
const P = new Map(), DEL = new Set(), KEYS = new Map(), SNAP = new Map(); let SRC = [], WAKE = () => {}, HSCOL = false;
const ST = { checked: 0, t0: Date.now(), last: null, next: 0 };
const clamp = (n, a, z) => Math.min(z, Math.max(a, Math.round(+n) || a));

// ---------- DB ----------
async function post(path, body) {
  if (!DB) return {};
  try { const r = await fetch(DB + path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }); return await r.json(); }
  catch (e) { console.log('db err', e.message); return {}; }
}
const q = (sql, params = []) => post('/sql', { sql, params });
const rows = j => {
  let r = Array.isArray(j) ? j : j.rows || j.data || j.result || (j.results && j.results[0] && (j.results[0].rows || j.results[0])) || [];
  if (!Array.isArray(r)) r = r.rows || [];
  const cols = j.columns || (j.results && j.results[0] && j.results[0].columns);
  return r.map(x => Array.isArray(x) && cols ? Object.fromEntries(cols.map((c, i) => [c.name || c, x[i]])) : x);
};
const batch = async a => {  // try {"batch":[...]}; if DB rejects it, fall back to one-by-one
  const j = await post('/sql', { batch: a.map(([sql, params]) => ({ sql, params })) });
  if (j && j.error) { console.log('batch failed, sequential fallback:', String(j.error).slice(0, 80)); for (const [sql, params] of a) await q(sql, params); }
};
const sv = (k, v) => q('INSERT OR REPLACE INTO settings VALUES(?,?)', [k, String(v)]);
const SCHEMA = `CREATE TABLE IF NOT EXISTS proxies(addr TEXT PRIMARY KEY,t TEXT,s TEXT,ms INTEGER,chk INTEGER,seen INTEGER,hs INTEGER);
CREATE TABLE IF NOT EXISTS sources(url TEXT PRIMARY KEY,name TEXT,t TEXT,enabled INTEGER);
CREATE TABLE IF NOT EXISTS keys(k TEXT PRIMARY KEY,label TEXT,created INTEGER,hits INTEGER);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT);`;
const R = 'https://raw.githubusercontent.com/';
const DEFAULT_SRC = [
  ['TheSpeedX http', R + 'TheSpeedX/PROXY-List/master/http.txt', 'http'],
  ['TheSpeedX socks4', R + 'TheSpeedX/PROXY-List/master/socks4.txt', 'socks4'],
  ['TheSpeedX socks5', R + 'TheSpeedX/PROXY-List/master/socks5.txt', 'socks5'],
  ['VPSLab http', R + 'VPSLabCloud/VPSLab-Free-Proxy-List/main/http_all.txt', 'http'],
  ['VPSLab socks4', R + 'VPSLabCloud/VPSLab-Free-Proxy-List/main/socks4_all.txt', 'socks4'],
  ['VPSLab socks5', R + 'VPSLabCloud/VPSLab-Free-Proxy-List/main/socks5_all.txt', 'socks5'],
  ['iplocate http', R + 'iplocate/free-proxy-list/main/protocols/http.txt', 'http'],
  ['iplocate https', R + 'iplocate/free-proxy-list/main/protocols/https.txt', 'http'],
  ['iplocate socks4', R + 'iplocate/free-proxy-list/main/protocols/socks4.txt', 'socks4'],
  ['iplocate socks5', R + 'iplocate/free-proxy-list/main/protocols/socks5.txt', 'socks5'],
  ['monosans http', R + 'monosans/proxy-list/main/proxies/http.txt', 'http'],
  ['monosans socks4', R + 'monosans/proxy-list/main/proxies/socks4.txt', 'socks4'],
  ['monosans socks5', R + 'monosans/proxy-list/main/proxies/socks5.txt', 'socks5'],
  ['proxifly http', R + 'proxifly/free-proxy-list/main/proxies/protocols/http/data.txt', 'http'],
  ['proxifly socks4', R + 'proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt', 'socks4'],
  ['proxifly socks5', R + 'proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt', 'socks5'],
  ['ProxyScrape http', R + 'ProxyScrape/free-proxy-list/main/proxies/protocols/http/data.txt', 'http'],
  ['ProxyScrape socks4', R + 'ProxyScrape/free-proxy-list/main/proxies/protocols/socks4/data.txt', 'socks4'],
  ['ProxyScrape socks5', R + 'ProxyScrape/free-proxy-list/main/proxies/protocols/socks5/data.txt', 'socks5'],
];
// Very big lists are read in parts: each fetch cycle takes the next CHUNK lines (wraps around), so memory stays small but over time all of it gets covered.
const CHUNK = { [R + 'proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt']: 10000 }, OFF = new Map();

// ---------- keys (limit / expiry / refresh are stored in settings as "kc:<key>" so the old table needs no migration) ----------
const kdef = o => { o.lim = clamp(o.lim || 100, 1, 10000); o.exp = +o.exp || 0; o.ref = clamp(o.ref || 5, 1, 1440); o.hs = (o.hs === true || o.hs === 1 || o.hs === '1') ? 1 : 0; return o; };
const kv = k => ({ k: k.k, label: k.label, created: k.created, hits: k.hits, lim: k.lim, exp: k.exp, ref: k.ref, hs: k.hs });
const saveKC = r => sv('kc:' + r.k, JSON.stringify({ lim: r.lim, exp: r.exp, ref: r.ref, hs: r.hs }));
function newKey(label, o = {}) {
  const k = crypto.randomBytes(9).toString('hex'), now = Date.now(), days = Math.max(0, +o.days || 0);
  const r = kdef({ k, label, created: now, hits: 0, lim: o.lim, exp: days ? now + days * 864e5 : 0, ref: o.ref, hs: o.hs });
  KEYS.set(k, r); q('INSERT INTO keys VALUES(?,?,?,0)', [k, label, now]); saveKC(r); return r;
}
function setCfg(b) {
  if (b.conc) { CONC = clamp(b.conc, 100, 10000); sv('conc', CONC); }
  if (b.int) { INT = clamp(b.int, 1, 1440); sv('int', INT); }
  if (b.scan) { MAXSCAN = clamp(b.scan, 1, 1e9); sv('scan', MAXSCAN); }
}

async function load() {
  await post('/sql', { script: SCHEMA });
  // older DBs: add the hs column once (so HTTPS-pass status survives a restart). If this cannot be detected, hs is simply not stored (safe fallback).
  const hasHs = async () => rows(await q('PRAGMA table_info(proxies)')).some(c => c && c.name === 'hs');
  HSCOL = await hasHs(); if (!HSCOL) { await q('ALTER TABLE proxies ADD COLUMN hs INTEGER'); HSCOL = await hasHs(); }
  await q('DELETE FROM sources WHERE url=?', [R + 'VPSLabCloud/VPSLab-Free-Proxy-List/main/http.txt']);
  // one-time source refresh (v2): drop stale lists (ShiftyTR, clarketm, proxifly 'all'), add the daily-updated ones
  const SRCV = 3, cur = rows(await q("SELECT v FROM settings WHERE k='srcv'"))[0];
  if (!cur || +cur.v < SRCV) {
    await q('DELETE FROM sources WHERE url LIKE ? OR url LIKE ? OR url=?', ['%ShiftyTR/Proxy-List/%', '%clarketm/proxy-list/%', R + 'proxifly/free-proxy-list/main/proxies/all/data.txt']);
    for (const [name, url, t] of DEFAULT_SRC) await q('INSERT OR IGNORE INTO sources VALUES(?,?,?,1)', [url, name, t]);
    await sv('srcv', SRCV);
  }
  if (!rows(await q('SELECT url FROM sources')).length) for (const [name, url, t] of DEFAULT_SRC) await q('INSERT OR IGNORE INTO sources VALUES(?,?,?,1)', [url, name, t]);
  SRC = rows(await q('SELECT * FROM sources')).map(r => ({ ...r, n: 0 }));
  if (!SRC.length) SRC = DEFAULT_SRC.map(([name, url, t]) => ({ name, url, t, enabled: 1, n: 0 }));
  rows(await q('SELECT * FROM keys')).forEach(r => KEYS.set(r.k, r));
  if (!KEYS.size) newKey('default');
  for (const r of rows(await q('SELECT k,v FROM settings'))) {
    if (r.k === 'conc') CONC = +r.v; if (r.k === 'int') INT = +r.v; if (r.k === 'scan') MAXSCAN = +r.v || MAXSCAN;
    if (String(r.k).startsWith('kc:') && KEYS.has(r.k.slice(3))) { try { Object.assign(KEYS.get(r.k.slice(3)), JSON.parse(r.v)); } catch { } }
  }
  KEYS.forEach(k => kdef(k));
  for (const r of rows(await q('SELECT * FROM proxies'))) {  // DB addr is now 'type|ip:port'; old rows are plain 'ip:port' -> re-saved under the new key, old row deleted
    const legacy = !String(r.addr).includes('|'), a = legacy ? r.addr : String(r.addr).split('|')[1], key = r.t + '|' + a;
    P.set(key, { a, key, t: r.t, s: r.s, alive: 1, ms: r.ms, chk: 0, fail: 0, seen: r.seen, hs: r.hs ? 1 : 0, dirty: legacy ? 1 : 0 });
    if (legacy) DEL.add(r.addr);
  }
  console.log('loaded', P.size, 'proxies,', SRC.length, 'sources,', KEYS.size, 'keys');
}
async function flush() {
  const ops = [];
  for (const p of P.values()) if (p.dirty && p.alive) { ops.push(HSCOL ? ['INSERT OR REPLACE INTO proxies(addr,t,s,ms,chk,seen,hs) VALUES(?,?,?,?,?,?,?)', [p.key, p.t, p.s, p.ms, p.chk, p.seen, p.hs ? 1 : 0]] : ['INSERT OR REPLACE INTO proxies(addr,t,s,ms,chk,seen) VALUES(?,?,?,?,?,?)', [p.key, p.t, p.s, p.ms, p.chk, p.seen]]); p.dirty = 0; }
  for (const a of DEL) ops.push(['DELETE FROM proxies WHERE addr=?', [a]]); DEL.clear();
  for (const k of KEYS.values()) if (k.hd) { ops.push(['UPDATE keys SET hits=? WHERE k=?', [k.hits, k.k]]); k.hd = 0; }
  for (let i = 0; i < ops.length; i += 100) await batch(ops.slice(i, i + 100));
}

// ---------- fetch sources ----------
const RE = /(?:([a-z0-9]+):\/\/)?(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})/gi;
async function fetchAll() {
  let added = 0;
  await Promise.all(SRC.filter(s => s.enabled).map(async s => {
    try {
      const txt = await (await fetch(s.url, { signal: AbortSignal.timeout(30000) })).text(); let n = 0, m; RE.lastIndex = 0;
      const all = []; while ((m = RE.exec(txt))) all.push(m);
      let use = all; const cs = CHUNK[s.url];
      if (cs && all.length > cs) { const off = OFF.get(s.url) || 0; use = all.slice(off, off + cs); if (use.length < cs) use = use.concat(all.slice(0, cs - use.length)); OFF.set(s.url, (off + cs) % all.length); }
      for (m of use) {
        let t = (m[1] || s.t).toLowerCase(); if (t === 'https') t = 'http'; if (!['http', 'socks4', 'socks5'].includes(t)) continue;
        const a = m[2] + ':' + m[3], key = t + '|' + a; n++;
        if (!P.has(key)) { P.set(key, { a, key, t, s: s.name, alive: 0, ms: 0, chk: 0, fail: 0, seen: Date.now(), dirty: 0 }); added++; }
      }
      s.n = n; s.err = '';
    } catch (e) { s.err = e.message; }
  }));
  console.log('fetch done, new:', added, 'total:', P.size);
}
// loaded-from-DB rows have no .key; normalise
function fixKeys() { for (const [k, p] of [...P]) if (!k.includes('|')) { P.delete(k); p.key = p.t + '|' + p.a; P.set(p.key, p); } }

// ---------- checker ----------
// check(p) = normal liveness test (handshake only, fast).
// check(p, true) = REAL tunnel test: open the tunnel to 1.1.1.1:443, then do a full TLS handshake (certificate verified) and a real HTTPS request through it.
// Only a proxy that really forwards data passes. (Old version stopped at the proxy's "OK" reply, so fake/broken proxies passed.)
const TEST_SNI = E.TEST_SNI || 'one.one.one.one';
function check(p, hs) {
  return new Promise(res => {
    const [h, pt] = p.a.split(':'), t0 = Date.now(); let done = 0, st = 0;
    const s = net.connect({ host: h, port: +pt, timeout: TMO });
    let hard = 0;
    const end = ok => { if (done) return; done = 1; clearTimeout(hard); s.destroy(); res(ok ? Date.now() - t0 : 0); };
    if (hs) hard = setTimeout(() => end(0), TMO * 2);  // whole deep test must finish in 2x timeout
    const deep = () => {  // tunnel is open: TLS handshake + real request through it
      s.off('data', onData);
      const t = tls.connect({ socket: s, servername: TEST_SNI });
      t.on('secureConnect', () => t.write('GET / HTTP/1.1\r\nHost: ' + TEST_SNI + '\r\nConnection: close\r\n\r\n'));
      t.on('data', d => end(/^HTTP\/1\.[01] \d\d\d/.test(d.toString('latin1', 0, 12))));
      t.on('error', () => end(0)).on('close', () => end(0));
    };
    const onData = d => {
      if (p.t === 'http') { const ok = (hs ? /^HTTP\/1\.[01] 200/ : /^HTTP\/1\.[01] [23]\d\d/).test(d.toString('latin1', 0, 14)); return hs && ok ? deep() : end(ok); }
      if (p.t === 'socks4') { const ok = d[1] === 0x5a; return hs && ok ? deep() : end(ok); }
      if (st === 0) { if (d[0] !== 5 || d[1] !== 0) return end(0); st = 1; s.write(Buffer.from(hs ? [5, 1, 0, 1, 1, 1, 1, 1, 1, 187] : [5, 1, 0, 1, 1, 1, 1, 1, 0, 80])); }
      else { const ok = d[0] === 5 && d[1] === 0; return hs && ok ? deep() : end(ok); }
    };
    s.on('timeout', () => end(0)).on('error', () => end(0)).on('close', () => end(0));
    s.on('connect', () => {
      if (p.t === 'http') s.write(hs ? 'CONNECT 1.1.1.1:443 HTTP/1.1\r\nHost: 1.1.1.1:443\r\n\r\n' : 'GET http://neverssl.com/ HTTP/1.1\r\nHost: neverssl.com\r\nConnection: close\r\n\r\n');
      else if (p.t === 'socks4') s.write(Buffer.from(hs ? [4, 1, 1, 187, 1, 1, 1, 1, 0] : [4, 1, 0, 80, 1, 1, 1, 1, 0]));
      else s.write(Buffer.from([5, 1, 0]));
    });
    s.on('data', onData);
  });
}
async function probe(p) {
  let ms = await check(p); if (!ms && p.alive) ms = await check(p);  // one retry for proxies that were alive
  p.chk = Date.now(); ST.checked++; p.dirty = 1;
  if (ms) { p.alive = 1; p.ms = ms; p.fail = 0; let h = await check(p, true); if (!h && p.hs) h = await check(p, true); p.hs = h ? 1 : 0; if (h) p.ms = h; }  // ms of a deep-tested proxy = real TLS+request time, not just the handshake
  else { p.hs = 0; p.fail++; if (p.alive) { p.alive = 0; DEL.add(p.key); } if (p.fail >= 3) P.delete(p.key); }
}
// One scan = at most MAXSCAN proxies (no hard cap). Alive ones are re-checked first (up to 80% of the budget), the rest goes to oldest-checked / new ones.
async function scan() {
  const t0 = Date.now(), al = [], ot = [];
  for (const p of P.values()) (p.alive ? al : ot).push(p);
  const by = (a, b) => a.chk - b.chk; al.sort(by); ot.sort(by);
  const na = Math.min(al.length, Math.floor(MAXSCAN * 0.8));
  const todo = [...al.slice(0, na), ...ot.slice(0, MAXSCAN - na)];
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(CONC, todo.length) }, async () => { while (i < todo.length) await probe(todo[i++]); }));
  SNAP.clear();  // keys get the freshly scanned list at once, not the cached one
  ST.last = { n: todo.length, alive: alive().length, secs: Math.round((Date.now() - t0) / 1000) };
  console.log('scan done:', ST.last.n, 'checked,', ST.last.alive, 'alive,', ST.last.secs + 's');
}
const sleep = ms => new Promise(r => { const t = setTimeout(r, ms); WAKE = () => { clearTimeout(t); r(); }; });
async function loop() {
  for (;;) {
    const t0 = Date.now(); ST.next = 0;
    try { await fetchAll(); await scan(); } catch (e) { console.log('cycle err', e.message); }
    ST.next = t0 + INT * 60000;
    const w = ST.next - Date.now(); if (w > 0) await sleep(w);
  }
}

// ---------- http ----------
const body = (req, max = 5e7) => new Promise(r => { let b = '', n = 0; req.on('data', d => { n += d.length; if (n > max) { req.destroy(); return r({}); } b += d; }); req.on('end', () => { try { r(JSON.parse(b || '{}')); } catch { r({}); } }); req.on('error', () => r({})); });
const J = (res, o, c = 200) => { res.writeHead(c, { 'content-type': 'application/json' }); res.end(JSON.stringify(o)); };
const sha = x => crypto.createHash('sha256').update(String(x)).digest(), same = (a, b) => crypto.timingSafeEqual(sha(a), sha(b));
const authed = req => { const m = /sid=([a-f0-9]+)/.exec(req.headers.cookie || ''); return !!m && same(m[1], SIG('admin')); };
const FAILS = new Map(), MAXFAIL = 5, LOCKMS = 15 * 60000;  // login: 5 wrong passwords per IP -> locked for 15 min
const clientIp = req => String(req.headers['x-forwarded-for'] || req.socket.remoteAddress || '').split(',').pop().trim();
setInterval(() => { const n = Date.now(); for (const [k, v] of FAILS) if (n - v.t > LOCKMS) FAILS.delete(k); }, 60000).unref();
if (PASS === 'change-me') console.log('WARNING: ADMIN_PASSWORD is still the default "change-me" - set the ADMIN_PASSWORD env variable!');
const alive = () => [...P.values()].filter(p => p.alive);
const fmt = p => p.t + '://' + p.a;
// each key sees a snapshot of the best (lowest ping) proxies, rebuilt every `ref` minutes
function snap(k, t) {
  const id = k.k + '|' + t, c = SNAP.get(id);
  if (c && Date.now() - c.t < k.ref * 60000) return c.l;
  const l = alive().filter(p => (!t || p.t === t) && (!k.hs || p.hs)).sort((a, b) => a.ms - b.ms).slice(0, k.lim);
  SNAP.set(id, { t: Date.now(), l }); return l;
}

http.createServer(async (req, res) => {
  const u = new URL(req.url, 'http://x'), path = u.pathname;
  if (path === '/health') return J(res, { ok: 1, alive: alive().length });
  if (path === '/api/proxies') {
    const k = KEYS.get(u.searchParams.get('key')); if (!k) return J(res, { error: 'bad key' }, 401);
    if (k.exp && Date.now() > k.exp) return J(res, { error: 'key expired' }, 403);
    k.hits++; k.hd = 1;
    let t = u.searchParams.get('type') || ''; if (!['http', 'socks4', 'socks5'].includes(t)) t = '';
    const want = Math.min(k.lim, Math.max(1, +u.searchParams.get('limit') || k.lim));
    const l = snap(k, t).slice(0, want);
    if (u.searchParams.get('format') === 'json') return J(res, l.map(p => ({ proxy: p.a, type: p.t, ms: p.ms, https: p.hs ? 1 : 0, source: p.s })));
    res.writeHead(200, { 'content-type': 'text/plain' }); return res.end(l.map(p => t ? p.a : fmt(p)).join('\n'));
  }
  if (path === '/login' && req.method === 'POST') {
    const b = await body(req, 4096), who = clientIp(req), f = FAILS.get(who), now = Date.now(), live = f && now - f.t < LOCKMS;
    if (live && f.n >= MAXFAIL) return J(res, { error: 'too many wrong attempts, try again in 15 minutes' }, 429);
    if (typeof b.password === 'string' && same(b.password, PASS)) {
      FAILS.delete(who);
      const sec = req.headers['x-forwarded-proto'] === 'https' || req.socket.encrypted ? '; Secure' : '';
      res.setHeader('set-cookie', `sid=${SIG('admin')}; HttpOnly; Path=/; Max-Age=2592000; SameSite=Lax${sec}`); return J(res, { ok: 1 });
    }
    FAILS.set(who, { n: (live ? f.n : 0) + 1, t: now });
    return J(res, { error: 'wrong' }, 401);
  }
  if (path === '/') { res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }); return res.end(authed(req) ? DASH : LOGIN); }
  if (!path.startsWith('/admin/') || !authed(req)) return J(res, { error: 'auth' }, 401);
  const b = req.method === 'POST' ? await body(req) : {};
  if (path === '/admin/state') {
    const a = alive(), by = t => ({ alive: a.filter(p => p.t === t).length, total: [...P.values()].filter(p => p.t === t).length });
    return J(res, { http: by('http'), socks4: by('socks4'), socks5: by('socks5'), total: P.size, alive: a.length, hs: a.filter(p => p.hs).length, checked: ST.checked, last: ST.last, next: ST.next, conc: CONC, int: INT, scanMax: MAXSCAN, src: SRC, keys: [...KEYS.values()].map(kv), top: a.sort((x, y) => x.ms - y.ms).slice(0, 40).map(p => ({ p: fmt(p), ms: p.ms, s: p.s })) });
  }
  if (path === '/admin/key') {
    if (b.edit) {
      const k = KEYS.get(b.edit);
      if (k) {
        if (b.label) { k.label = String(b.label).slice(0, 40); q('UPDATE keys SET label=? WHERE k=?', [k.label, k.k]); }
        if (b.lim) k.lim = b.lim; if (b.ref) k.ref = b.ref; if (b.hs !== undefined) k.hs = b.hs;
        if (b.days !== undefined && b.days !== '') k.exp = +b.days > 0 ? Date.now() + +b.days * 864e5 : 0;
        kdef(k); saveKC(k);
        for (const x of [...SNAP.keys()]) if (x.startsWith(k.k + '|')) SNAP.delete(x);
      }
    } else if (b.del) {
      KEYS.delete(b.del); q('DELETE FROM keys WHERE k=?', [b.del]); q('DELETE FROM settings WHERE k=?', ['kc:' + b.del]);
      for (const x of [...SNAP.keys()]) if (x.startsWith(b.del + '|')) SNAP.delete(x);
    } else newKey(String(b.label || 'key').slice(0, 40), b);
    return J(res, { ok: 1 });
  }
  if (path === '/admin/source') {
    if (b.del) { SRC = SRC.filter(s => s.url !== b.del); q('DELETE FROM sources WHERE url=?', [b.del]); }
    else if (/^https?:\/\//.test(b.url || '')) { SRC.push({ name: b.name || b.url.slice(8, 40), url: b.url, t: b.t || 'http', enabled: 1, n: 0 }); q('INSERT OR REPLACE INTO sources VALUES(?,?,?,1)', [b.url, b.name || b.url.slice(8, 40), b.t || 'http']); }
    return J(res, { ok: 1 });
  }
  if (path === '/admin/fetch') { WAKE(); return J(res, { ok: 1 }); }  // scan now
  if (path === '/admin/settings') { setCfg(b); return J(res, { ok: 1 }); }
  if (path === '/admin/backup') {
    res.writeHead(200, { 'content-type': 'application/json', 'content-disposition': 'attachment; filename=proxyhub-backup.json' });
    return res.end(JSON.stringify({ settings: { conc: CONC, int: INT, scan: MAXSCAN }, sources: SRC.map(s => ({ name: s.name, url: s.url, t: s.t, enabled: s.enabled })), keys: [...KEYS.values()].map(kv), proxies: alive().map(p => ({ a: p.a, t: p.t, s: p.s, ms: p.ms })) }));
  }
  if (path === '/admin/restore') {
    if (b.settings) setCfg(b.settings);
    (b.sources || []).forEach(s => { if (s.url && !SRC.find(x => x.url === s.url)) { SRC.push({ ...s, enabled: 1, n: 0 }); q('INSERT OR IGNORE INTO sources VALUES(?,?,?,1)', [s.url, s.name, s.t]); } });
    (b.keys || []).forEach(k => {
      if (!k.k || KEYS.has(k.k)) return;
      const o = kdef({ k: k.k, label: k.label || 'key', created: k.created || Date.now(), hits: k.hits || 0, lim: k.lim, exp: k.exp, ref: k.ref, hs: k.hs });
      KEYS.set(o.k, o); q('INSERT OR IGNORE INTO keys VALUES(?,?,?,?)', [o.k, o.label, o.created, o.hits]); saveKC(o);
    });
    (b.proxies || []).forEach(p => { const key = p.t + '|' + p.a; if (!P.has(key)) P.set(key, { ...p, key, alive: 0, chk: 0, fail: 0, seen: Date.now(), dirty: 0 }); });
    return J(res, { ok: 1 });
  }
  J(res, { error: 'nf' }, 404);
}).listen(PORT, () => console.log('port', PORT));

(async () => {
  await load(); fixKeys();
  setInterval(flush, 60000); loop();
  process.on('SIGTERM', async () => { await flush(); process.exit(0); });
})();

// ---------- UI (white theme) ----------
const CSS = `<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><style>
:root{color-scheme:light}*{box-sizing:border-box}
body{margin:0 auto;padding:16px;max-width:760px;font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;background:#fff;color:#111827}
h1{font-size:22px;text-align:center;margin:14px 0 20px}
h2{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#6b7280;margin:28px 0 10px}
.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px}
.c{border:1px solid #e5e7eb;border-radius:12px;padding:12px;text-align:center}
.c b{display:block;font-size:18px;margin-top:2px;word-break:break-word}
.card{border:1px solid #e5e7eb;border-radius:12px;padding:12px;margin-bottom:10px}
.f{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:#6b7280}
input,select,button{font:inherit;border:1px solid #d1d5db;border-radius:8px;padding:8px 10px;background:#fff;color:#111827}
input,select{width:100%}
button{background:#2563eb;color:#fff;border-color:#2563eb;cursor:pointer}
button.o{background:#fff;color:#2563eb}
button.x{background:#fff;color:#dc2626;border-color:#fca5a5;padding:5px 10px}
.r{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.u{background:#f3f4f6;border-radius:8px;padding:6px 8px;margin-top:8px;word-break:break-all;font:12px ui-monospace,Menlo,monospace}
.l{padding:8px 0;border-bottom:1px solid #f0f1f3}
small{color:#6b7280}
</style>`;
const LOGIN = `<!doctype html><title>Proxy Hub</title>${CSS}<div style="max-width:320px;margin:22vh auto 0;text-align:center"><h1>Proxy Hub</h1><input id=p type=password placeholder=Password style="margin-bottom:10px" onkeydown="if(event.key=='Enter')go()"><button style="width:100%" onclick="go()">Login</button></div><script>function go(){fetch('/login',{method:'POST',body:JSON.stringify({password:document.getElementById('p').value})}).then(r=>r.ok?location.reload():alert('Wrong password'))}</script>`;
const DASH = `<!doctype html><title>Proxy Hub</title>${CSS}<h1>Proxy Hub</h1>
<div class=g id=st></div>

<h2>API keys</h2>
<div class=card>
<div class=f>
<label>Name<input id=kl placeholder="e.g. my-bot"></label>
<label>Proxies (1-10000)<input id=kn type=number min=1 max=10000 value=500></label>
<label>URL valid for<select id=kd><option value=1>1 day<option value=7>7 days<option value=30 selected>30 days<option value=90>90 days<option value=365>1 year<option value=0>Never expires</select></label>
<label>Only HTTPS-pass proxies<select id=kh><option value=0 selected>No (all alive)<option value=1>Yes (HTTPS only)</select></label>
<label>List updates every<select id=kr><option value=1 selected>1 min<option value=5>5 min<option value=10>10 min<option value=30>30 min<option value=60>1 hour</select></label>
</div>
<button style="margin-top:12px;width:100%" onclick="mk()">Create key</button>
</div>
<div id=keys></div>
<small>Optional URL params: &amp;type=http / socks4 / socks5, &amp;format=json, &amp;limit=N (cannot be more than the key's limit). Fastest (lowest ping) proxies come first.</small>

<h2>Sources</h2>
<div class=card><div class=f>
<label>Name<input id=sn placeholder=name></label>
<label>Raw URL<input id=su placeholder="https://..."></label>
<label>Type<select id=stt><option>http<option>socks4<option>socks5</select></label>
</div><button style="margin-top:12px;width:100%" onclick="as()">Add source</button></div>
<div id=src></div>

<h2>Settings</h2>
<div class=card><div class=f>
<label>Parallel checks<input id=cc type=number></label>
<label>Scan every (min)<input id=ii type=number></label>
<label>Max proxies per scan (no limit)<input id=sm type=number min=1></label>
</div>
<div class=r style="margin-top:12px"><button onclick="ss()">Save</button><button class=o onclick="sn2()">Scan now</button></div></div>

<h2>Backup</h2>
<div class=card>
<div class=r><a href=/admin/backup><button class=o>Download backup</button></a></div>
<div class=r style="margin-top:12px"><input type=file id=rf accept=".json,application/json" style="flex:1"><button onclick="rs()">Restore</button></div>
<small>Restore only starts when you press the button and confirm.</small>
</div>

<h2>Fastest alive</h2><div id=fast></div>

<script>
const $=i=>document.getElementById(i),O=location.origin;let S=null;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const A=(p,b)=>fetch('/admin/'+p,{method:'POST',body:JSON.stringify(b||{})}).then(r=>r.json()).then(j=>{if(j.error)alert(j.error);return j});
const ago=ms=>{const m=Math.round(ms/60000);return m<60?m+' min':m<1440?Math.round(m/60)+' h':Math.round(m/1440)+' days'};
function mk(){A('key',{label:$('kl').value||'key',lim:$('kn').value,days:$('kd').value,ref:$('kr').value,hs:$('kh').value}).then(()=>{$('kl').value='';L()})}
function dk(i){const k=S.keys[i];if(confirm('Delete key "'+k.label+'"?\\nAnything using this URL will stop working.'))A('key',{del:k.k}).then(L)}
function ek(i){const k=S.keys[i];
const n=prompt('Name',k.label);if(n===null)return;
const l=prompt('Proxies (1-10000)',k.lim);if(l===null)return;
const d=prompt('Valid for how many days from NOW? (0 = never expires, empty = keep as is)','');if(d===null)return;
const r=prompt('List updates every (min)',k.ref);if(r===null)return;
const h=confirm('HTTPS-pass proxies only? OK = yes, Cancel = no (all alive)');
A('key',{edit:k.k,label:n,lim:l,days:d,ref:r,hs:h?1:0}).then(L)}
function cp(i){const u=O+'/api/proxies?key='+S.keys[i].k;(navigator.clipboard?navigator.clipboard.writeText(u):Promise.reject()).then(()=>alert('Copied')).catch(()=>prompt('Copy this URL:',u))}
function as(){A('source',{name:$('sn').value,url:$('su').value,t:$('stt').value}).then(()=>{$('sn').value='';$('su').value='';L()})}
function ds(i){const x=S.src[i];if(confirm('Delete source "'+x.name+'"?'))A('source',{del:x.url}).then(L)}
function ss(){A('settings',{conc:$('cc').value,int:$('ii').value,scan:$('sm').value}).then(()=>{alert('Saved');L()})}
function sn2(){A('fetch').then(()=>setTimeout(L,1500))}
function rs(){const f=$('rf').files[0];if(!f)return alert('Choose a backup file first');
if(!confirm('Restore from "'+f.name+'"?\\nSources, keys and proxies will be merged in.'))return;
f.text().then(t=>{let j;try{j=JSON.parse(t)}catch(e){return alert('Invalid backup file')}A('restore',j).then(()=>{$('rf').value='';alert('Restored');L()})})}
function draw(){const s=S,now=Date.now();
$('st').innerHTML=[['Alive',s.alive+' / '+s.total],['HTTPS ok',s.hs],['HTTP',s.http.alive],['SOCKS4',s.socks4.alive],['SOCKS5',s.socks5.alive],['Last scan',s.last?s.last.n+' in '+s.last.secs+'s':'-'],['Next scan',s.next?Math.max(0,Math.round((s.next-now)/1000))+'s':'scanning...']].map(x=>'<div class=c><small>'+x[0]+'</small><b>'+x[1]+'</b></div>').join('');
$('keys').innerHTML=s.keys.map((k,i)=>{const dead=k.exp&&k.exp<now;
const ex=!k.exp?'never expires':dead?'<b style="color:#dc2626">EXPIRED</b>':'expires '+new Date(k.exp).toLocaleDateString()+' ('+ago(k.exp-now)+' left)';
return '<div class=card><div class=r><b style="flex:1">'+esc(k.label)+'</b><button class=o onclick="cp('+i+')">Copy URL</button><button class=o onclick="ek('+i+')">Edit</button><button class=x onclick="dk('+i+')">Delete</button></div><small>'+k.lim+' proxies &middot; '+(k.hs?'HTTPS only &middot; ':'')+'updates every '+k.ref+' min &middot; '+ex+' &middot; '+k.hits+' hits</small><div class=u>'+esc(O+'/api/proxies?key='+k.k)+'</div></div>'}).join('');
$('src').innerHTML=s.src.map((x,i)=>'<div class="r l"><span style="flex:1;word-break:break-all">'+esc(x.name)+' <small>['+x.t+'] '+x.n+' found '+esc(x.err||'')+'</small></span><button class=x onclick="ds('+i+')">Delete</button></div>').join('');
$('fast').innerHTML=s.top.map(x=>'<div class="r l"><span style="flex:1;word-break:break-all">'+esc(x.p)+'</span><small>'+x.ms+' ms</small></div>').join('');
['cc','ii','sm'].forEach((id,i)=>{if(document.activeElement.id!==id)$(id).value=[s.conc,s.int,s.scanMax][i]})}
function L(){fetch('/admin/state').then(r=>r.json()).then(s=>{if(s.error){location.reload();return}S=s;draw()})}
L();setInterval(L,10000)</script>`;
