// FLOW // real-time options flow HUD for NIFTY / SENSEX. Pure canvas, no dependencies.
// Laid out on a fixed 1680x945 virtual screen and scaled to the window, so nothing overlaps at any size.
// Server frames arrive ~10/s; every value glides toward its latest target at 60 fps.

const cv = document.getElementById('c');
const ctx = cv.getContext('2d', { alpha: false });
const FONT = '"JetBrains Mono","Cascadia Mono",Consolas,monospace';
const VW = 1680, VH = 945;
const C = {
  bg: '#03060b', panel: 'rgba(7,14,24,0.84)', edge: 'rgba(70,150,230,0.18)', bracket: 'rgba(34,224,255,0.55)',
  grid: 'rgba(90,150,210,0.07)', cyan: '#22e0ff', green: '#1cf2a0', red: '#ff3d6e', amber: '#ffb627',
  violet: '#a98bff', text: '#cfe6ff', dim: '#5d7893', white: '#f2f8ff', buy: '#29f5c8', sell: '#ff4f86',
};
const TAG = ['', 'LONG', 'WRITE', 'COVER', 'UNWIND'];
const TAGL = ['', 'L', 'W', 'C', 'U'];
const TAGC = ['', C.green, C.amber, C.cyan, C.dim];
const S = {
  ws: null, up: false, fields: [], ix: {}, books: [], mode: '', u: 'NIFTY', e: 0, sel: null, f: null, hist: null,
  feed: [], rows: new Map(), spotD: null, parts: [], mouse: null, days: [], day: null, speed: 30,
  paused: false, hits: [], heatCv: null, heatKey: '', flash: null, fps: 60, last: performance.now(), status: '', geo: null, bw: false,
  tv: false, tcols: [], tapeCv: null, tapeKey: '',
};
let W = VW, H = VH, SC = 1, DPR = 1, L = {}, BG = null;

// ---------- helpers ----------
const fin = (x) => x !== null && x !== undefined && Number.isFinite(x);
const nf = (x, d = 0) => (fin(x) ? x.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—');
const fx = (x, d = 1) => (fin(x) ? x.toFixed(d) : '—');
const sg = (x, d = 1) => (fin(x) ? (x > 0 ? '+' : '') + x.toFixed(d) : '—');
const qf = (x) => {
  if (!fin(x)) return '—';
  const a = Math.abs(x);
  if (a >= 1e7) return (x / 1e7).toFixed(1) + 'Cr';
  if (a >= 1e5) return (x / 1e5).toFixed(1) + 'L';
  if (a >= 1e3) return (x / 1e3).toFixed(1) + 'K';
  return x.toFixed(0);
};
const crf = (x) => (!fin(x) ? '—' : Math.abs(x) >= 1000 ? (x / 1000).toFixed(1) + 'K' : x.toFixed(0));
const ist = (ts) => {
  const d = new Date(ts + 19800000);
  return String(d.getUTCHours()).padStart(2, '0') + ':' + String(d.getUTCMinutes()).padStart(2, '0');
};
const istS = (ts) => ist(ts) + ':' + String(new Date(ts + 19800000).getUTCSeconds()).padStart(2, '0');
const dayStart = (ts) => ts - ((ts + 19800000) % 86400000) + 555 * 60000; // 09:15 IST of that day
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
function font(size, weight = 400) { ctx.font = `${weight} ${size}px ${FONT}`; }
function tw(s, size, weight = 400) { font(size, weight); return ctx.measureText(s).width; }
function T(s, x, y, size = 12, color = C.text, align = 'left', weight = 400) {
  font(size, weight); ctx.fillStyle = color; ctx.textAlign = align; ctx.fillText(s, x, y);
}
function fit(s, maxW, size, weight = 400) {
  if (tw(s, size, weight) <= maxW) return s;
  while (s.length > 3 && tw(s + '…', size, weight) > maxW) s = s.slice(0, -1);
  return s + '…';
}
function rr(x, y, w, h, r) {
  ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}
function line(x1, y1, x2, y2, color, w = 1, dash = null) {
  ctx.strokeStyle = color; ctx.lineWidth = w; ctx.setLineDash(dash || []);
  ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); ctx.setLineDash([]);
}
function alpha(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}
function panel(r, title, right, rightColor = C.text) {
  ctx.fillStyle = C.panel; rr(r.x, r.y, r.w, r.h, 6); ctx.fill();
  ctx.strokeStyle = C.edge; ctx.lineWidth = 1; ctx.stroke();
  const b = 12; ctx.strokeStyle = C.bracket; ctx.lineWidth = 1.5; ctx.beginPath();
  ctx.moveTo(r.x, r.y + b); ctx.lineTo(r.x, r.y); ctx.lineTo(r.x + b, r.y);
  ctx.moveTo(r.x + r.w - b, r.y); ctx.lineTo(r.x + r.w, r.y); ctx.lineTo(r.x + r.w, r.y + b);
  ctx.moveTo(r.x, r.y + r.h - b); ctx.lineTo(r.x, r.y + r.h); ctx.lineTo(r.x + b, r.y + r.h);
  ctx.moveTo(r.x + r.w - b, r.y + r.h); ctx.lineTo(r.x + r.w, r.y + r.h); ctx.lineTo(r.x + r.w, r.y + r.h - b);
  ctx.stroke();
  let used = 0;
  if (title) { ctx.letterSpacing = '1.5px'; T(title, r.x + 12, r.y + 17, 10.5, C.dim, 'left', 600); used = tw(title, 10.5, 600) + title.length * 1.5; ctx.letterSpacing = '0px'; }
  if (right) T(fit(right, r.w - used - 40, 10.5, 500), r.x + r.w - 12, r.y + 17, 10.5, rightColor, 'right', 500);
}
function hit(x, y, w, h, fn) { S.hits.push({ x, y, w, h, fn }); }
function chip(label, x, y, on, fn, color = C.cyan) {
  font(11, 600); const w = ctx.measureText(label).width + 16;
  ctx.fillStyle = on ? alpha(color, 0.18) : 'rgba(255,255,255,0.03)'; rr(x, y, w, 20, 4); ctx.fill();
  ctx.strokeStyle = on ? color : C.edge; ctx.lineWidth = 1; ctx.stroke();
  T(label, x + w / 2, y + 14, 11, on ? C.white : C.dim, 'center', 600);
  if (fn) hit(x, y, w, 20, fn);
  return w;
}
function badge(label, x, y, color, align = 'left') {
  const w = tw(label, 9.5, 700) + 10, x0 = align === 'right' ? x - w : x;
  ctx.fillStyle = alpha(color, 0.18); rr(x0, y - 11, w, 15, 3); ctx.fill();
  ctx.strokeStyle = alpha(color, 0.7); ctx.lineWidth = 1; ctx.stroke();
  T(label, x0 + w / 2, y, 9.5, color, 'center', 700);
  return w;
}

// ---------- sprites & background ----------
function sprite(color) {
  const c = document.createElement('canvas'); c.width = c.height = 32;
  const g = c.getContext('2d'); const gr = g.createRadialGradient(16, 16, 0, 16, 16, 16);
  gr.addColorStop(0, alpha(color, 1)); gr.addColorStop(0.3, alpha(color, 0.55)); gr.addColorStop(1, alpha(color, 0));
  g.fillStyle = gr; g.fillRect(0, 0, 32, 32); return c;
}
const SPR = { buy: sprite(C.buy), sell: sprite(C.sell) };
function buildBg() {
  BG = document.createElement('canvas'); BG.width = cv.width; BG.height = cv.height;
  const g = BG.getContext('2d'); g.setTransform(DPR * SC, 0, 0, DPR * SC, 0, 0);
  const gr = g.createRadialGradient(W * 0.5, H * 0.45, 0, W * 0.5, H * 0.45, Math.max(W, H) * 0.75);
  gr.addColorStop(0, '#071426'); gr.addColorStop(1, C.bg); g.fillStyle = gr; g.fillRect(0, 0, W, H);
  g.strokeStyle = C.grid; g.lineWidth = 1;
  for (let x = 0; x < W; x += 40) { g.beginPath(); g.moveTo(x + 0.5, 0); g.lineTo(x + 0.5, H); g.stroke(); }
  for (let y = 0; y < H; y += 40) { g.beginPath(); g.moveTo(0, y + 0.5); g.lineTo(W, y + 0.5); g.stroke(); }
}

// ---------- layout (virtual coordinates) ----------
function layout() {
  const pad = 8, top = 66, bottomH = 196;
  const midY = pad * 2 + top, midH = H - midY - bottomH - pad * 2;
  const lw = Math.round(W * 0.245), rw = Math.round(W * 0.265), cw = W - lw - rw - pad * 4;
  L.hud = { x: pad, y: pad, w: W - 2 * pad, h: top };
  L.chart = { x: pad, y: midY, w: lw, h: Math.round(midH * 0.57) };
  L.decay = { x: pad, y: midY + L.chart.h + pad, w: lw, h: midH - L.chart.h - pad };
  L.river = { x: pad * 2 + lw, y: midY, w: cw, h: midH };
  const rx = pad * 3 + lw + cw;
  L.read = { x: rx, y: midY, w: rw, h: Math.round(midH * 0.43) };
  L.gex = { x: rx, y: L.read.y + L.read.h + pad, w: rw, h: Math.round(midH * 0.29) };
  L.smile = { x: rx, y: L.gex.y + L.gex.h + pad, w: rw, h: midY + midH - (L.gex.y + L.gex.h + pad) };
  L.depth = { x: rx, y: midY, w: rw, h: midH };
  const by = midY + midH + pad, bw = W - 4 * pad;
  L.heat = { x: pad, y: by, w: Math.round(bw * 0.4), h: bottomH };
  L.meter = { x: pad * 2 + L.heat.w, y: by, w: Math.round(bw * 0.2), h: bottomH };
  L.feed = { x: pad * 3 + L.heat.w + L.meter.w, y: by, w: bw - L.heat.w - L.meter.w, h: bottomH };
}
function resize() {
  DPR = Math.min(window.devicePixelRatio || 1, 2);
  SC = clamp(Math.min(innerWidth / VW, innerHeight / VH), 0.5, 1.6);
  W = innerWidth / SC; H = innerHeight / SC;
  cv.width = Math.round(innerWidth * DPR); cv.height = Math.round(innerHeight * DPR);
  cv.style.width = innerWidth + 'px'; cv.style.height = innerHeight + 'px';
  ctx.setTransform(DPR * SC, 0, 0, DPR * SC, 0, 0); layout(); buildBg(); S.heatKey = '';
}
addEventListener('resize', resize);

// ---------- socket ----------
function send(m) { if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(m)); }
function view() { send({ t: 'view', u: S.u, e: S.e, sel: S.sel }); }
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  S.ws = ws;
  ws.onopen = () => { S.up = true; };
  ws.onclose = () => { S.up = false; setTimeout(connect, 1500); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.t === 'hello') onHello(m); else if (m.t === 'history') onHistory(m); else if (m.t === 'frame') onFrame(m);
  };
}
function resetView() { S.rows.clear(); S.parts = []; S.feed = []; S.hist = null; S.f = null; S.spotD = null; S.heatKey = ''; S.tcols = []; S.tapeKey = ''; }
function onHello(m) {
  S.fields = m.fields; S.ix = Object.fromEntries(m.fields.map((f, i) => [f, i]));
  S.books = m.books; S.mode = m.mode; S.days = m.replay_days || []; S.day = m.day; S.speed = m.speed || S.speed;
  S.ofUrl = m.order_flow || '';
  if (!S.books.includes(S.u)) S.u = S.books[0];
  resetView(); view();
}
function onHistory(m) {
  if (m.u !== S.u) return;
  S.hist = m; S.heatKey = '';
  const seen = new Set(S.feed.map((e) => e.id));
  for (const e of m.events) if (!seen.has(e.id)) S.feed.push({ ...e, born: 0 });
  S.feed.sort((a, b) => b.id - a.id); S.feed.length = Math.min(S.feed.length, 80);
}
const RV = (row, side, f) => row[S.ix[side + '_' + f]];
function onFrame(f) {
  if (f.u !== S.u || !f.ok) return;
  S.f = f; S.status = f.status; S.of = f.of || null;
  const h = f.hud, keep = new Set(), lot = h.lot || 1, flows = [];
  for (const row of f.rows) {
    const k = row[0]; keep.add(k);
    let r = S.rows.get(k);
    if (!r) { r = { k, d: {}, t: {} }; S.rows.set(k, r); }
    r.raw = row;
    for (const side of ['ce', 'pe']) {
      r.t[side + 'oi'] = RV(row, side, 'oi') || 0; r.t[side + 'doi'] = RV(row, side, 'doi') || 0;
      r.t[side + 'act'] = RV(row, side, 'act') || 0; r.t[side + 'b1'] = RV(row, side, 'b1') || 0; r.t[side + 's1'] = RV(row, side, 's1') || 0;
      flows.push([k, side, (RV(row, side, 'fbuy') || 0) / lot, (RV(row, side, 'fsell') || 0) / lot]);
    }
  }
  for (const k of [...S.rows.keys()]) if (!keep.has(k)) S.rows.delete(k);
  spawn(flows);
  if (S.spotD === null && fin(h.spot)) S.spotD = h.spot;
  for (const e of f.events || []) if (!S.feed.some((x) => x.id === e.id)) S.feed.unshift({ ...e, born: performance.now() });
  S.feed.length = Math.min(S.feed.length, 80);
  if (f.tcols && f.tcols.length) {
    const lastSeq = S.tcols.length ? S.tcols[S.tcols.length - 1][0] : 0;
    if (f.tcols[0][0] <= lastSeq) S.tcols = [];
    S.tcols.push(...f.tcols);
    if (S.tcols.length > TAPE_COLS) S.tcols.splice(0, S.tcols.length - TAPE_COLS);
  }
}
function spawn(flows) {
  // only the five busiest strikes emit trade particles; the per-row bars carry the detail
  const busy = flows.filter((x) => x[2] + x[3] > 0).sort((a, b) => b[2] + b[3] - (a[2] + a[3])).slice(0, 5);
  if (!busy.length) return;
  const mx = busy[0][2] + busy[0][3];
  for (const [k, side, b, s] of busy) {
    for (const [dir, lots] of [[1, b], [-1, s]]) {
      const n = Math.min(4, Math.round((lots / mx) * 4 + Math.random() * 0.6));
      for (let i = 0; i < n; i++) {
        S.parts.push({ k, side, dir, p: Math.random() * 0.05, sp: 0.55 + Math.random() * 0.4, jy: (Math.random() - 0.5) * 0.7,
          s: 4 + 5 * Math.sqrt(lots / mx) });
      }
    }
  }
  if (S.parts.length > 600) S.parts.splice(0, S.parts.length - 600);
}

// ---------- input ----------
addEventListener('keydown', (e) => {
  const k = e.key.toLowerCase();
  if (k === 'n' && S.books.includes('NIFTY')) setU('NIFTY');
  else if (k === 's' && S.books.includes('SENSEX')) setU('SENSEX');
  else if (k === 'tab') { e.preventDefault(); setU(S.books[(S.books.indexOf(S.u) + 1) % S.books.length]); }
  else if (k === 'e') { S.e = S.e ? 0 : 1; S.rows.clear(); view(); }
  else if (k === 'escape') { S.sel = null; view(); }
  else if (k === 'b') S.bw = !S.bw;
  else if (k === 't') S.tv = !S.tv;
  else if (k === ' ' && S.mode === 'replay') { e.preventDefault(); S.paused = !S.paused; send({ t: 'replay', pause: S.paused }); }
  else if ('12345'.includes(k) && S.mode === 'replay') setSpeed([1, 5, 30, 120, 600]['12345'.indexOf(k)]);
  else if ((k === '[' || k === ']') && S.mode === 'replay') stepDay(k === ']' ? 1 : -1);
});
function setU(u) { if (u && u !== S.u) { S.u = u; S.sel = null; resetView(); view(); } }
function setSpeed(x) { S.speed = x; send({ t: 'replay', speed: x }); }
function stepDay(d) {
  const i = S.days.indexOf(S.day), j = clamp(i + d, 0, S.days.length - 1);
  if (j !== i) { S.day = S.days[j]; send({ t: 'replay', day: S.day }); }
}
const vpt = (e) => ({ x: e.clientX / SC, y: e.clientY / SC });
cv.addEventListener('mousemove', (e) => { S.mouse = vpt(e); });
cv.addEventListener('mouseleave', () => { S.mouse = null; });
cv.addEventListener('click', (e) => {
  const { x, y } = vpt(e);
  for (let i = S.hits.length - 1; i >= 0; i--) {
    const h = S.hits[i];
    if (x >= h.x && x <= h.x + h.w && y >= h.y && y <= h.y + h.h) { h.fn(); return; }
  }
  const k = strikeAt(x, y);
  if (k !== null) { S.sel = S.sel === k ? null : k; view(); }
});
function strikeAt(x, y) {
  const g = S.geo; if (!g || x < L.river.x || x > L.river.x + L.river.w || y < g.top || y > g.bot) return null;
  return g.ks[Math.floor((y - g.top) / g.rowH)] ?? null;
}

// ---------- animation loop ----------
function tick(now) {
  const dt = Math.min(0.1, (now - S.last) / 1000); S.last = now; S.fps = S.fps * 0.95 + (1 / Math.max(dt, 1e-3)) * 0.05;
  const k = 1 - Math.exp(-dt / 0.14);
  for (const r of S.rows.values()) for (const key in r.t) { const a = r.d[key]; r.d[key] = a === undefined ? r.t[key] : a + (r.t[key] - a) * k; }
  if (S.f && fin(S.f.hud.spot)) S.spotD = S.spotD === null ? S.f.hud.spot : S.spotD + (S.f.hud.spot - S.spotD) * k;
  for (const p of S.parts) p.p += p.sp * dt;
  S.parts = S.parts.filter((p) => p.p < 1);
  draw(now);
  requestAnimationFrame(tick);
}
function draw(now) {
  S.hits = [];
  ctx.drawImage(BG, 0, 0, W, H);
  drawHud();
  if (!S.f) { T(S.up ? 'waiting for data…' : 'connecting to the flow server…', W / 2, H / 2, 16, C.dim, 'center'); return; }
  if (S.tv) drawTape(L.chart); else drawChart(L.chart);
  drawDecay(L.decay); drawRiver(L.river, now);
  if (S.sel !== null && S.f.depth) drawDepth(L.depth); else { drawRead(L.read); drawGex(L.gex); drawSmile(L.smile); }
  drawHeat(L.heat); drawMeters(L.meter); if (S.bw) drawBounce(L.feed); else drawFeed(L.feed, now); drawTip(); drawDataTip();
  T(`${S.fps.toFixed(0)} fps`, W - 12, H - 3, 9, C.dim, 'right');
}
function drawOfTip() {
  const b = S.ofBtn, m = S.mouse;
  if (!b || !m || m.x < b.x || m.x > b.x + b.w || m.y < b.y || m.y > b.y + b.h) return;
  const br = S.of && S.of.breadth, lines = [];
  if (!S.of || !S.of.up) lines.push([`Order Flow is not answering on ${S.ofUrl}: it starts with the live dashboard`, C.amber]);
  else if (!br) lines.push(['Order Flow is up; breadth appears once it has read this session (live only)', C.dim]);
  else {
    const [u, f, d] = br.fno, [u5, f5, d5] = br.n50;
    lines.push([`F&O ${u} up · ${f} flat · ${d} down of ${br.n} stocks (±0.5% on the day)`, u >= d ? C.green : C.red]);
    lines.push([`NIFTY 50: ${u5} up · ${f5} flat · ${d5} down`, u5 >= d5 ? C.green : C.red]);
    if (br.lead) lines.push([`leading ${br.lead[0]} ${sg(br.lead[1], 2)}% · lagging ${br.lag[0]} ${sg(br.lag[1], 2)}%`, C.text]);
  }
  lines.push(['click: open the whole-market board (every F&O name, build-ups, OI levels)', C.dim]);
  const w = 520, h = lines.length * 16 + 12, x = b.x, y = b.y + b.h + 6;
  ctx.fillStyle = 'rgba(4,10,18,0.96)'; rr(x, y, w, h, 6); ctx.fill(); ctx.strokeStyle = C.bracket; ctx.lineWidth = 1; ctx.stroke();
  lines.forEach(([t, c], i) => T(fit(t, w - 20, 10.5), x + 10, y + 19 + i * 16, 10.5, c));
}
function drawDataTip() {
  drawOfTip();
  const b = S.dataBtn, m = S.mouse, dv = S.f && S.f.data;
  if (!b || !m || !dv || m.x < b.x || m.x > b.x + b.w || m.y < b.y || m.y > b.y + b.h) return;
  const f = dv.fresh || {}, lines = [
    [`stored: index & VIX to ${f.index || '—'} · chains NIFTY ${f.NIFTY || '—'} · SENSEX ${f.SENSEX || '—'}`, C.text],
    [`last closed session ${dv.due}: ${dv.stale ? 'data is behind → click to update' : 'up to date'}`, dv.stale ? C.amber : C.green],
  ];
  if (dv.state === 'running') lines.push([`running ${dv.step}: ${dv.line || '…'}`, C.amber]);
  else if (dv.result) lines.push([`last run: ${dv.result}`, dv.state === 'failed' ? C.red : C.dim]);
  lines.push([`Order Flow archive (previous-day OI for its board) to ${dv.archive || '—'}`, C.dim]);
  lines.push(['updates index, VIX, option chains and the Order Flow archive', C.dim]);
  if (dv.market) lines.push(['market open: chains wait until after 15:35 IST (shared Dhan limit)', C.dim]);
  const w = 560, h = lines.length * 16 + 12, x = b.x, y = b.y + b.h + 6;
  ctx.fillStyle = 'rgba(4,10,18,0.96)'; rr(x, y, w, h, 6); ctx.fill(); ctx.strokeStyle = C.bracket; ctx.lineWidth = 1; ctx.stroke();
  lines.forEach(([t, c], i) => T(fit(t, w - 20, 10.5), x + 10, y + 19 + i * 16, 10.5, c));
}

// ---------- HUD ----------
function drawHud() {
  const r = L.hud; panel(r);
  ctx.save(); ctx.shadowColor = C.cyan; ctx.shadowBlur = 14; T('FLOW//', r.x + 14, r.y + 27, 20, C.cyan, 'left', 800); ctx.restore();
  const dv = S.f && S.f.data, run = dv && dv.state === 'running';
  const dl = !dv ? '⟳ DATA' : run ? '⟳ UPDATING' : dv.state === 'failed' ? '✕ DATA' : dv.stale ? '⟳ DATA' : '✓ DATA';
  const dc = !dv ? C.dim : run || dv.stale ? C.amber : dv.state === 'failed' ? C.red : C.green;
  const dw = chip(dl, r.x + 14, r.y + 36, !!dv, () => { if (dv && !run) send({ t: 'update' }); }, dc);
  S.dataBtn = { x: r.x + 14, y: r.y + 36, w: dw, h: 20 };
  let x = r.x + 112;
  for (const u of S.books) x += chip(u, x, r.y + 8, u === S.u, () => setU(u)) + 6;
  if (S.ofUrl) {
    const ofUp = !!(S.of && S.of.up), w = chip('ORDER FLOW ↗', x + 4, r.y + 8, ofUp, () => window.open(S.ofUrl, '_blank', 'noopener'), ofUp ? C.green : C.violet);
    S.ofBtn = { x: x + 4, y: r.y + 8, w, h: 20 };
  }
  const h = S.f ? S.f.hud : null, exps = h ? h.exps || [] : [];
  let x2 = r.x + 112;
  exps.forEach((d, i) => { x2 += chip((i ? 'NEXT ' : 'WEEKLY ') + d.slice(5), x2, r.y + 36, i === S.e, () => { S.e = i; S.rows.clear(); view(); }, C.violet) + 6; });
  const right = r.x + r.w - 14;
  let x1 = right - 210;
  if (S.mode === 'replay') {
    x1 = right - 330;
    T(`REPLAY ${S.day || ''}`, x1, r.y + 20, 12, C.amber, 'left', 700);
    chip('◀', x1 + 168, r.y + 6, false, () => stepDay(-1), C.amber); chip('▶', x1 + 196, r.y + 6, false, () => stepDay(1), C.amber);
    let cx = x1;
    for (const v of [1, 5, 30, 120, 600]) cx += chip(v + 'x', cx, r.y + 36, S.speed === v, () => setSpeed(v), C.amber) + 4;
    chip(S.paused ? '▶' : '❚❚', cx + 4, r.y + 36, S.paused, () => { S.paused = !S.paused; send({ t: 'replay', pause: S.paused }); }, C.amber);
    if (S.f) T(istS(S.f.ts), right, r.y + 20, 13, C.white, 'right', 700);
  } else {
    const live = S.up && S.f;
    ctx.save(); ctx.shadowColor = live ? C.green : C.red; ctx.shadowBlur = 10;
    T(live ? '● LIVE' : '● OFFLINE', x1 + 6, r.y + 24, 13, live ? C.green : C.red, 'left', 800); ctx.restore();
    if (S.f) T(istS(S.f.ts) + ' IST', right, r.y + 24, 13, C.white, 'right', 700);
    T(fit(S.status || '', 196, 9.5), right, r.y + 46, 9.5, C.dim, 'right');
  }
  if (!h) return;
  const chg = fin(h.spot) && h.prev ? (h.spot / h.prev - 1) * 100 : null, gexOn = fin(h.net_gex);
  const tte = fin(h.tte) ? `${Math.floor(h.tte / 86400)}d ${Math.floor((h.tte % 86400) / 3600)}h ${Math.floor((h.tte % 3600) / 60)}m` : '—';
  const tiles = [
    ['SPOT', nf(S.spotD, 2), `${sg(chg, 2)}%`, chg >= 0 ? C.green : C.red, 18, 1.5],
    ['FWD · BASIS', nf(h.fwd, 1), sg(h.fwd - h.spot, 1), C.text],
    ['INDIA VIX', fx(h.vix, 2), sg(h.vix - h.vix_prev, 2), h.vix >= h.vix_prev ? C.red : C.green],
    ['PCR OI · VOL', fx(h.pcr_oi, 2), `vol ${fx(h.pcr_vol, 2)}`, h.pcr_oi >= 1 ? C.green : C.red],
    ['MAX PAIN', nf(h.max_pain), fin(h.max_pain) && fin(h.spot) ? `${sg(h.spot - h.max_pain, 0)} away` : '', C.amber],
    ['Γ FLIP', nf(h.flip), fin(h.flip) ? (h.spot > h.flip ? 'spot above' : 'spot BELOW') : 'none', C.violet],
    ['NET GEX ₹cr', crf(h.net_gex), gexOn ? (h.net_gex > 0 ? 'PINNING' : 'TRENDING') : '', gexOn && h.net_gex > 0 ? C.green : C.red],
    ['IV · RV', `${fx(h.iv_ref, 1)}%`, `rv ${fx(h.rv, 1)}%${h.dte0 ? ' · next exp' : ''}`, C.cyan],
    ['STRADDLE', fx(h.straddle, 1), fin(h.straddle) ? `±${(h.straddle / h.spot * 100).toFixed(2)}%` : '', C.text],
    ['EXPIRY', h.exp ? h.exp.slice(5) : '—', h.dte0 ? `0DTE · ${tte.replace('0d ', '')}` : tte, h.dte0 ? C.amber : C.text],
  ];
  const x0 = r.x + 350, xe = x1 - 12, wts = tiles.map((t) => t[5] || 1), tot = wts.reduce((a, b) => a + b, 0);
  let tx = x0;
  tiles.forEach(([label, val, sub, col, size], i) => {
    const w = ((xe - x0) * wts[i]) / tot;
    line(tx, r.y + 10, tx, r.y + r.h - 10, C.edge);
    ctx.letterSpacing = '1px'; T(fit(label, w - 12, 9, 600), tx + 8, r.y + 18, 9, C.dim, 'left', 600); ctx.letterSpacing = '0px';
    T(fit(val, w - 12, size || 14, 700), tx + 8, r.y + 39, size || 14, C.white, 'left', 700);
    T(fit(sub, w - 12, 10), tx + 8, r.y + 55, 10, col, 'left', 500);
    tx += w;
  });
}

// ---------- strike river ----------
function gutterLabels(items, x, align) {
  items.sort((a, b) => a.y - b.y);
  let last = -1e9;
  for (const it of items) { const y = Math.max(it.y, last + 15); last = y; badge(it.t, x, y, it.c, align); }
}
function drawRiver(r, now) {
  const f = S.f, h = f.hud;
  const rows = [...S.rows.values()].sort((a, b) => b.k - a.k);
  panel(r, `STRIKE FLOW · ${S.u} · ${h.exp || ''}${h.dte0 && S.e === 0 ? ' · 0DTE' : ''}`, 'PUTS ◄ ► CALLS', C.dim);
  const n = rows.length; if (!n) return;
  const G = 78, lab = 96, top = r.y + 28, bot = r.y + r.h - 22, rowH = (bot - top) / n, cx = r.x + r.w / 2;
  const inL = cx - lab / 2, inR = cx + lab / 2, outL = r.x + G, outR = r.x + r.w - G, sideW = inL - outL;
  const TXT = 82, AB = 34, oiW = sideW - TXT - AB - 14;
  const kmax = rows[0].k, step = n > 1 ? rows[0].k - rows[1].k : (h.step || 50);
  const yOf = (p) => top + ((kmax - p) / step + 0.5) * rowH;
  S.geo = { top, bot, rowH, ks: rows.map((x) => x.k) };
  let maxOI = 1, maxTag = 1, maxFlow = 1;
  for (const x of rows) {
    maxOI = Math.max(maxOI, x.d.ceoi || 0, x.d.peoi || 0);
    maxTag = Math.max(maxTag, Math.abs(x.raw[S.ix.ce_tag_doi] || 0), Math.abs(x.raw[S.ix.pe_tag_doi] || 0));
    maxFlow = Math.max(maxFlow, (x.d.ceb1 || 0) + (x.d.ces1 || 0), (x.d.peb1 || 0) + (x.d.pes1 || 0));
  }
  if (fin(h.straddle) && fin(S.spotD)) {
    const y1 = clamp(yOf(S.spotD + h.straddle), top, bot), y2 = clamp(yOf(S.spotD - h.straddle), top, bot);
    ctx.fillStyle = alpha(C.cyan, 0.04); ctx.fillRect(outL, y1, outR - outL, y2 - y1);
    line(outL, y1, outR, y1, alpha(C.cyan, 0.3), 1, [3, 5]); line(outL, y2, outR, y2, alpha(C.cyan, 0.3), 1, [3, 5]);
  }
  const fs = clamp(rowH * 0.62, 8.5, 11.5);
  rows.forEach((x, i) => {
    const y = top + i * rowH, mid = y + rowH / 2, bh = clamp(rowH * 0.42, 3, 11), d = x.d, raw = x.raw;
    for (const [side, dirn, inner, col] of [['ce', 1, inR, C.red], ['pe', -1, inL, C.green]]) {
      const act = clamp((d[side + 'act'] || 0) / 5, 0, 1);
      if (act > 0.05) {
        const g = ctx.createLinearGradient(inner, 0, inner + dirn * sideW, 0);
        g.addColorStop(0, alpha(C.cyan, 0.22 * act)); g.addColorStop(1, alpha(C.cyan, 0));
        ctx.fillStyle = g; ctx.fillRect(Math.min(inner, inner + dirn * sideW), y + 1, sideW, rowH - 2);
      }
      const ltp = raw[S.ix[side + '_ltp']], iv = raw[S.ix[side + '_iv']];
      const tx = inner + dirn * 6, al = dirn > 0 ? 'left' : 'right';
      T(fx(ltp, ltp >= 100 ? 1 : 2), tx, mid + fs * 0.36, fs, C.white, al, 700);
      if (rowH >= 12) T(fin(iv) ? iv.toFixed(1) : '', tx + dirn * 50, mid + fs * 0.36, fs * 0.82, C.dim, al);
      // last-minute aggressors: teal = bought at the offer, pink = sold at the bid
      const b1 = d[side + 'b1'] || 0, s1 = d[side + 's1'] || 0, tot = b1 + s1, ax = inner + dirn * TXT;
      const ax0 = dirn > 0 ? ax : ax - AB;
      ctx.fillStyle = 'rgba(255,255,255,0.05)'; ctx.fillRect(ax0, mid - 2.5, AB, 5);
      if (tot > 0) {
        const a = 0.35 + 0.65 * Math.min(1, tot / maxFlow), bw = AB * (b1 / tot);
        ctx.fillStyle = alpha(C.buy, a); ctx.fillRect(ax0, mid - 2.5, bw, 5);
        ctx.fillStyle = alpha(C.sell, a); ctx.fillRect(ax0 + bw, mid - 2.5, AB - bw, 5);
      }
      // OI bar with the change since the open at its outer end
      const ob = inner + dirn * (TXT + AB + 12), len = ((d[side + 'oi'] || 0) / maxOI) * oiW, doi = d[side + 'doi'] || 0;
      const dl = Math.min((Math.abs(doi) / maxOI) * oiW, len + 30);
      ctx.fillStyle = alpha(col, 0.34); ctx.fillRect(dirn > 0 ? ob : ob - len, mid - bh / 2, len, bh);
      if (doi > 0) { ctx.fillStyle = alpha(col, 0.95); ctx.fillRect(dirn > 0 ? ob + len - dl : ob - len, mid - bh / 2, dl, bh); }
      else if (doi < 0) {
        ctx.strokeStyle = alpha(col, 0.75); ctx.lineWidth = 1; ctx.setLineDash([2, 2]);
        ctx.strokeRect(dirn > 0 ? ob + len : ob - len - dl, mid - bh / 2 + 0.5, dl, bh - 1); ctx.setLineDash([]);
      }
      const tag = raw[S.ix[side + '_tag']], st = Math.abs(raw[S.ix[side + '_tag_doi']] || 0) / maxTag;
      if (tag && st >= 0.15) {
        const cxp = dirn > 0 ? inR - 9 : inL + 9;
        ctx.globalAlpha = clamp(0.4 + st, 0.4, 1); ctx.fillStyle = alpha(TAGC[tag], 0.22);
        rr(cxp - 7, mid - 7, 14, 14, 3); ctx.fill(); T(TAGL[tag], cxp, mid + 3.5, 9.5, TAGC[tag], 'center', 800); ctx.globalAlpha = 1;
      }
    }
    const atm = x.k === h.atm, sel = x.k === S.sel, fl = S.flash && S.flash.k === x.k && now < S.flash.until;
    if (atm || sel || fl) { ctx.fillStyle = sel ? alpha(C.violet, 0.25) : fl ? alpha(C.amber, 0.3) : alpha(C.cyan, 0.12); ctx.fillRect(inL + 18, y + 1, lab - 36, rowH - 2); }
    T(nf(x.k), cx, mid + fs * 0.36, fs, atm ? C.cyan : C.text, 'center', atm ? 800 : 500);
  });
  ctx.save(); ctx.globalCompositeOperation = 'lighter';
  const idx = new Map(rows.map((x, i) => [x.k, i]));
  for (const p of S.parts) {
    const i = idx.get(p.k); if (i === undefined) continue;
    const mid = top + (i + 0.5) * rowH + p.jy * rowH * 0.5, e = p.p * p.p * (3 - 2 * p.p);
    const from = p.side === 'ce' ? outR : outL, to = p.side === 'ce' ? inR + TXT + AB : inL - TXT - AB;
    ctx.globalAlpha = Math.min(1, p.p * 6) * Math.min(1, (1 - p.p) * 2) * 0.6;
    ctx.drawImage(p.dir > 0 ? SPR.buy : SPR.sell, from + (to - from) * e - p.s / 2, mid - p.s / 2, p.s, p.s);
  }
  ctx.restore();
  const left = [], rightL = [];
  const inside = (k) => fin(k) && yOf(k) > top && yOf(k) < bot;
  if (inside(h.flip)) { const y = yOf(h.flip); line(r.x + 4, y, r.x + r.w - 4, y, alpha(C.violet, 0.75), 1.2, [6, 4]); left.push({ y: y + 4, t: `Γ ${nf(h.flip)}`, c: C.violet }); }
  if (inside(h.put_wall)) left.push({ y: yOf(h.put_wall) + 4, t: 'PUT WALL', c: C.green });
  if (inside(h.call_wall)) rightL.push({ y: yOf(h.call_wall) + 4, t: 'CALL WALL', c: C.red });
  if (inside(h.max_pain)) {
    const y = yOf(h.max_pain); ctx.fillStyle = C.amber; ctx.beginPath();
    ctx.moveTo(inL + 2, y); ctx.lineTo(inL + 7, y - 5); ctx.lineTo(inL + 12, y); ctx.lineTo(inL + 7, y + 5); ctx.fill();
    rightL.push({ y: y + 4, t: 'MAX PAIN', c: C.amber });
  }
  if (fin(h.straddle) && fin(S.spotD)) {
    const y1 = yOf(S.spotD + h.straddle), y2 = yOf(S.spotD - h.straddle);
    if (y1 > top) rightL.push({ y: y1 + 4, t: `EM +${fx(h.straddle, 0)}`, c: C.cyan });
    if (y2 < bot) rightL.push({ y: y2 + 4, t: `EM −${fx(h.straddle, 0)}`, c: C.cyan });
  }
  gutterLabels(left, r.x + 8, 'left'); gutterLabels(rightL, r.x + r.w - 8, 'right');
  if (fin(S.spotD)) {
    const y = clamp(yOf(S.spotD), top, bot);
    ctx.save(); ctx.shadowColor = C.cyan; ctx.shadowBlur = 12; line(r.x + 4, y, r.x + r.w - 4, y, C.cyan, 1.6); ctx.restore();
    ctx.fillStyle = C.cyan; rr(cx - 44, y - 9, 88, 18, 4); ctx.fill();
    T(nf(S.spotD, 2), cx, y + 4, 11, '#00151c', 'center', 800);
  }
  const ly = r.y + r.h - 7;
  T('OI bar · bright = added since open · dashed = cut', r.x + 12, ly, 9, C.dim);
  T('▮ buyers vs sellers, last minute', cx, ly, 9, C.buy, 'center');
  T('L long · W write · C cover · U unwind', r.x + r.w - 12, ly, 9, C.dim, 'right');
}

// ---------- index chart ----------
function liveCandles() {
  const cs = S.hist ? S.hist.candles.slice() : [];
  const f = S.f; if (!f || !fin(f.hud.spot)) return cs;
  const m = Math.floor(f.ts / 60000) * 60000, p = f.hud.spot, last = cs[cs.length - 1];
  if (last && last[0] === m) cs[cs.length - 1] = [m, last[1], Math.max(last[2], p), Math.min(last[3], p), p];
  else if (!last || m > last[0]) cs.push([m, last ? last[4] : p, p, p, p]);
  return cs;
}
function drawChart(r) {
  const h = S.f.hud, cs = liveCandles();
  panel(r, `${S.u} · 1 MIN · T = BOOK & TAPE`, fin(h.spot) && h.open ? `open ${nf(h.open, 1)} · ${sg(h.spot - h.open, 1)}` : '', C.dim);
  hit(r.x, r.y, 240, 24, () => { S.tv = true; });
  if (cs.length < 2) return;
  const px = r.x + 12, pw = r.w - 74, py = r.y + 28, ph = r.h - 46, day0 = dayStart(cs[0][0]);
  const xOf = (t) => px + ((t - day0) / 60000 / 375) * pw;
  let lo = Infinity, hi = -Infinity;
  for (const c of cs) { lo = Math.min(lo, c[3]); hi = Math.max(hi, c[2]); }
  const levels = [[h.call_wall, 'CW', C.red], [h.put_wall, 'PW', C.green], [h.flip, 'Γ', C.violet], [h.max_pain, 'MP', C.amber]];
  for (const [v] of levels) if (fin(v) && Math.abs(v - h.spot) / h.spot < 0.012) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  const padv = (hi - lo) * 0.08 || 1; lo -= padv; hi += padv;
  const yOf = (v) => py + (1 - (v - lo) / (hi - lo)) * ph;
  for (let i = 0; i <= 4; i++) { const v = lo + ((hi - lo) * i) / 4, y = yOf(v); line(px, y, px + pw, y, C.grid); T(nf(v, 0), px + pw + 6, y + 3, 9, C.dim); }
  for (let hh = 10; hh <= 15; hh++) { const x = xOf(day0 + (hh * 60 - 555) * 60000); line(x, py, x, py + ph, C.grid); T(`${hh}:00`, x, py + ph + 13, 9, C.dim, 'center'); }
  if (fin(h.straddle)) {
    const y1 = yOf(h.spot + h.straddle), y2 = yOf(h.spot - h.straddle);
    ctx.fillStyle = alpha(C.cyan, 0.04); ctx.fillRect(px, Math.max(py, y1), pw, Math.min(py + ph, y2) - Math.max(py, y1));
  }
  if (h.open) line(px, yOf(h.open), px + pw, yOf(h.open), alpha(C.text, 0.25), 1, [2, 4]);
  const tags = [];
  for (const [v, lbl, col] of levels) {
    if (!fin(v)) continue; const y = yOf(v); if (y < py || y > py + ph) continue;
    line(px, y, px + pw, y, alpha(col, 0.5), 1, [5, 4]); tags.push({ y: y + 4, t: lbl, c: col });
  }
  const bw = Math.max(1, pw / 375 - 0.6);
  for (const c of cs) {
    const x = xOf(c[0]), up = c[4] >= c[1], col = up ? C.green : C.red;
    line(x, yOf(c[2]), x, yOf(c[3]), alpha(col, 0.6), 1);
    ctx.fillStyle = alpha(col, 0.85); const y1 = yOf(Math.max(c[1], c[4])), y2 = yOf(Math.min(c[1], c[4]));
    ctx.fillRect(x - bw / 2, y1, bw, Math.max(1, y2 - y1));
  }
  gutterLabels(tags, px + 4, 'left');
  const last = cs[cs.length - 1], ly = yOf(last[4]);
  ctx.save(); ctx.shadowColor = C.cyan; ctx.shadowBlur = 10; ctx.fillStyle = C.cyan; rr(px + pw + 2, ly - 8, 64, 16, 3); ctx.fill(); ctx.restore();
  T(nf(last[4], 1), px + pw + 34, ly + 4, 10, '#00151c', 'center', 800);
}

// ---------- futures book & tape ----------
const TAPE_COLS = 450, TAPE_SPAN = 15 * 60000;
function tapeHeat(cols, t0, lo, hi, h) {
  // one pixel column per 2-second book snapshot, rows = price; scaled onto the panel without smoothing
  const c = S.tapeCv || document.createElement('canvas'); S.tapeCv = c; c.width = TAPE_COLS; c.height = h;
  const g = c.getContext('2d'); g.clearRect(0, 0, c.width, c.height);
  const qs = []; for (const col of cols) { for (const q of col[9]) qs.push(q); for (const q of col[11]) qs.push(q); }
  qs.sort((a, b) => a - b); const q95 = qs[Math.floor(qs.length * 0.95)] || 1, cell = Math.max(2, h / 70);
  for (const col of cols) {
    const xi = Math.floor(((col[1] - t0) / TAPE_SPAN) * TAPE_COLS); if (xi < 0 || xi >= TAPE_COLS) continue;
    for (const [pxs, qty, colr] of [[col[8], col[9], C.buy], [col[10], col[11], C.sell]]) {
      pxs.forEach((p, i) => {
        if (p < lo || p > hi) return;
        const a = Math.min(1, Math.sqrt((qty[i] || 0) / q95));
        if (a < 0.05) return;
        g.fillStyle = alpha(colr, 0.1 + 0.8 * a); g.fillRect(xi, (1 - (p - lo) / (hi - lo)) * h - cell / 2, 2, cell);
      });
    }
  }
  return c;
}
function drawTape(r) {
  const tp = S.f.tape, cols = S.tcols;
  panel(r, `${S.u} FUT · BOOK & TAPE`, tp ? `basis ${sg(tp.basis, 1)} · spread ${fx(tp.spread, 1)} · ${tp.levels} lvls · T = chart` : 'T = chart', C.dim);
  hit(r.x, r.y, 240, 24, () => { S.tv = false; });
  if (!tp || cols.length < 3) {
    const msg = tp ? 'collecting the futures book…' : S.mode === 'replay' ? 'live only: no stored order-book history to replay yet'
      : 'no futures feed for this index';
    T(msg, r.x + r.w / 2, r.y + r.h / 2, 11, C.dim, 'center'); return;
  }
  const px = r.x + 10, pw = r.w - 70, py = r.y + 28, ph = r.h - 28 - 64, t1 = cols[cols.length - 1][1], t0 = t1 - TAPE_SPAN;
  const vis = cols.filter((c) => c[1] >= t0);
  let lo = Infinity, hi = -Infinity;
  for (const c of vis) { lo = Math.min(lo, c[4] || c[2]); hi = Math.max(hi, c[3] || c[2]); }
  const pad = Math.max((hi - lo) * 0.3, tp.ltp * 0.0004); lo -= pad; hi += pad;
  const xOf = (t) => px + ((t - t0) / TAPE_SPAN) * pw, yOf = (v) => py + (1 - (v - lo) / (hi - lo)) * ph;
  const key = `${t1}|${lo.toFixed(1)}|${hi.toFixed(1)}|${Math.round(ph)}`;
  if (key !== S.tapeKey) { S.tapeKey = key; tapeHeat(vis, t0, lo, hi, Math.round(ph)); }
  ctx.imageSmoothingEnabled = false; ctx.drawImage(S.tapeCv, px, py, pw, ph); ctx.imageSmoothingEnabled = true;
  for (let i = 0; i <= 4; i++) { const v = lo + ((hi - lo) * i) / 4; T(nf(v, 0), px + pw + 6, yOf(v) + 3, 9, C.dim); }
  for (let t = Math.ceil(t0 / 300000) * 300000; t <= t1; t += 300000) { line(xOf(t), py, xOf(t), py + ph, C.grid); T(ist(t), xOf(t), py + ph - 3, 8.5, C.dim, 'center'); }
  for (const [side, p, lots, age] of tp.walls) {
    if (p < lo || p > hi) continue;
    const y = yOf(p), col = side === 'bid' ? C.green : C.red;
    line(px, y, px + pw, y, alpha(col, age >= 60 ? 0.9 : 0.5), 1, age >= 60 ? null : [4, 3]);
    T(`${qf(lots)}L wall`, px + pw - 4, y - 3, 9, col, 'right', 700);
  }
  const vols = vis.map((c) => c[5] + c[6]).sort((a, b) => a - b), v90 = vols[Math.floor(vols.length * 0.9)] || 0, vmax = vols[vols.length - 1] || 1;
  for (const c of vis) {
    const v = c[5] + c[6]; if (v <= 0 || v < v90) continue;
    const sh = c[5] / v, col = sh >= 0.6 ? C.buy : sh <= 0.4 ? C.sell : C.amber;
    ctx.fillStyle = alpha(col, 0.55); ctx.beginPath(); ctx.arc(xOf(c[1]), yOf(c[2]), 2 + 6 * Math.sqrt(v / vmax), 0, Math.PI * 2); ctx.fill();
  }
  ctx.save(); ctx.shadowColor = C.white; ctx.shadowBlur = 6; ctx.strokeStyle = C.white; ctx.lineWidth = 1.3; ctx.beginPath();
  vis.forEach((c, i) => { const x = xOf(c[1]), y = yOf(c[2]); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); }); ctx.stroke(); ctx.restore();
  for (const [ts, side, n, lots, p] of tp.sweeps) {
    if (ts < t0 || p < lo || p > hi) continue;
    const x = xOf(ts), y = yOf(p), d = side > 0 ? -1 : 1; ctx.fillStyle = side > 0 ? C.buy : C.sell;
    ctx.beginPath(); ctx.moveTo(x, y + d * 9); ctx.lineTo(x - 5, y + d * 1); ctx.lineTo(x + 5, y + d * 1); ctx.closePath(); ctx.fill();
  }
  for (const [side, p, hitL, shown] of tp.absorb) {
    if (p < lo || p > hi) continue;
    T(`◆ ${side === 'bid' ? 'bid' : 'offer'} absorbing ${qf(hitL)}L vs ~${qf(shown)} shown`, px + 4, yOf(p) + (side === 'bid' ? 12 : -5), 9, C.amber, 'left', 700);
  }
  const ly = yOf(tp.ltp);
  ctx.save(); ctx.shadowColor = C.cyan; ctx.shadowBlur = 10; ctx.fillStyle = C.cyan; rr(px + pw + 2, ly - 8, 64, 16, 3); ctx.fill(); ctx.restore();
  T(nf(tp.ltp, 1), px + pw + 34, ly + 4, 10, '#00151c', 'center', 800);
  const cy = py + ph + 6, chh = 30; let a = Infinity, b = -Infinity;
  for (const c of vis) { a = Math.min(a, c[7]); b = Math.max(b, c[7]); }
  const cyOf = (v) => cy + chh - ((v - a) / (b - a || 1)) * chh, dc = vis[vis.length - 1][7] - vis[0][7], ccol = dc >= 0 ? C.buy : C.sell;
  ctx.strokeStyle = ccol; ctx.lineWidth = 1.3; ctx.beginPath();
  vis.forEach((c, i) => { const x = xOf(c[1]), y = cyOf(c[7]); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); }); ctx.stroke();
  T('AGGRESSORS', px + 2, cy + 9, 8.5, C.dim, 'left', 700);
  T(`${sg(dc, 0)} lots net bought / 15m`, px + pw + 66, cy + 9, 9, ccol, 'right', 700);
  const z = tp.ofi_z, sy = r.y + r.h - 10;
  const stats = `1m buy ${qf(tp.buy1)} · sell ${qf(tp.sell1)} | queues ${fin(z) ? sg(z, 1) + 'σ' : '—'} | book ${fin(tp.imb20 ?? tp.imb5) ? sg((tp.imb20 ?? tp.imb5) * 100, 0) + '%' : '—'}`
    + ` | walls pulled ${tp.pulled.bid}/${tp.pulled.offer} filled ${tp.filled.bid}/${tp.filled.offer}`;
  T(fit(stats, r.w - 20, 9.5, 600), px, sy, 9.5, C.text, 'left', 600);
}

// ---------- straddle decay ----------
function drawDecay(r) {
  const d = S.f.hud.decay;
  panel(r, 'STRADDLE · WHO IS WINNING', d ? `ATM ${nf(d.k)} since ${ist(d.since)}` : '', C.dim);
  if (!d) { T('anchors on the ATM straddle at 09:20 IST', r.x + r.w / 2, r.y + r.h / 2, 11, C.dim, 'center'); return; }
  const sellers = d.actual < 0;
  ctx.save(); ctx.shadowColor = sellers ? C.red : C.green; ctx.shadowBlur = 12;
  T(sellers ? 'SELLERS AHEAD' : 'BUYERS AHEAD', r.x + 14, r.y + 44, 16, sellers ? C.red : C.green, 'left', 800); ctx.restore();
  T(`₹${fx(d.p0, 1)} → ₹${fx(d.p0 + d.actual, 1)} (${sg(d.actual, 1)})`, r.x + r.w - 14, r.y + 44, 11.5, C.white, 'right', 700);
  const parts = [['decay θ', d.theta, C.red], ['move Γ', d.move, C.cyan], ['IV ν', d.vol, C.violet]];
  parts.forEach(([l, v, c], i) => { const x = r.x + 14 + i * ((r.w - 28) / 3); T(l, x, r.y + 62, 9.5, C.dim); T(sg(v, 1), x + 50, r.y + 62, 11, c, 'left', 700); });
  const ser = (S.hist ? S.hist.decay : []).slice();
  ser.push([S.f.ts, d.actual, d.theta, d.move, d.vol]);
  if (ser.length < 2) return;
  const px = r.x + 12, pw = r.w - 24, py = r.y + 74, ph = r.h - 92;
  let lo = 0, hi = 0;
  for (const s of ser) for (let j = 1; j <= 4; j++) if (fin(s[j])) { lo = Math.min(lo, s[j]); hi = Math.max(hi, s[j]); }
  const span = hi - lo || 1, t0 = ser[0][0], t1 = Math.max(ser[ser.length - 1][0], t0 + 60000);
  const xOf = (t) => px + ((t - t0) / (t1 - t0)) * pw, yOf = (v) => py + (1 - (v - lo) / span) * ph;
  line(px, yOf(0), px + pw, yOf(0), alpha(C.text, 0.25), 1, [2, 3]);
  for (const [j, col, w] of [[2, C.red, 1.2], [3, C.cyan, 1.2], [4, C.violet, 1.2], [1, C.white, 2]]) {
    ctx.strokeStyle = col; ctx.lineWidth = w; ctx.beginPath(); let st = false;
    for (const s of ser) { if (!fin(s[j])) continue; const x = xOf(s[0]), y = yOf(s[j]); if (st) ctx.lineTo(x, y); else { ctx.moveTo(x, y); st = true; } }
    ctx.stroke();
  }
  [['— actual', C.white], ['— decay', C.red], ['— move', C.cyan], ['— IV', C.violet]].forEach(([l, c], i) => T(l, px + i * 70, r.y + r.h - 7, 9, c));
}

// ---------- market read ----------
const BIASC = { 'BULLISH TILT': C.green, 'BEARISH TILT': C.red, 'LEANING BULLISH': '#7ff2c4', 'LEANING BEARISH': '#ff8fac', 'RANGE / PIN': C.amber, MIXED: C.dim };
function drawRead(r) {
  const rd = S.f.hud.read;
  panel(r, 'MARKET READ', 'rule-based context · not a signal', C.dim);
  if (!rd) { T('building…', r.x + r.w / 2, r.y + r.h / 2, 11, C.dim, 'center'); return; }
  const col = BIASC[rd.bias] || C.text;
  ctx.save(); ctx.shadowColor = col; ctx.shadowBlur = 14; T(rd.bias, r.x + 14, r.y + 46, 20, col, 'left', 800); ctx.restore();
  for (let i = -rd.max; i <= rd.max; i++) {
    if (!i) continue;
    const on = (i > 0 && rd.score >= i) || (i < 0 && rd.score <= i), x = r.x + r.w - 20 - (rd.max - i) * 12 - (i < 0 ? 6 : 0);
    ctx.fillStyle = on ? (i > 0 ? C.green : C.red) : 'rgba(255,255,255,0.1)'; ctx.beginPath(); ctx.arc(x, r.y + 40, 4, 0, Math.PI * 2); ctx.fill();
  }
  let y = r.y + 70;
  const lh = Math.min(19, (r.h - 78) / Math.max(rd.lines.length, 1));
  for (const ln of rd.lines) {
    if (y > r.y + r.h - 6) break;
    ctx.letterSpacing = '1px'; T(ln.l, r.x + 14, y, 9, C.dim, 'left', 700); ctx.letterSpacing = '0px';
    ctx.fillStyle = ln.tone > 0 ? C.green : ln.tone < 0 ? C.red : 'rgba(255,255,255,0.25)';
    ctx.beginPath(); ctx.arc(r.x + 86, y - 3.5, 3, 0, Math.PI * 2); ctx.fill();
    T(fit(ln.t, r.w - 110, 10.5), r.x + 96, y, 10.5, C.text);
    y += lh;
  }
}

// ---------- GEX ----------
function drawGex(r) {
  const h = S.f.hud, g = S.f.gex, on = fin(h.net_gex);
  panel(r, 'GAMMA EXPOSURE ₹cr/1%', on ? `net ${crf(h.net_gex)} · ${h.net_gex > 0 ? 'PINNING' : 'TRENDING'}` : '', on && h.net_gex > 0 ? C.green : C.red);
  if (h.dte0 && S.e === 0) badge('0DTE', r.x + r.w / 2 - 6, r.y + 18, C.amber, 'left');
  if (!g.grid.length) return;
  const px = r.x + 14, pw = r.w - 28, py = r.y + 28, ph = r.h - 50;
  const x0 = g.grid[0], x1 = g.grid[g.grid.length - 1];
  let m = 1e-9; for (const v of g.net) if (fin(v)) m = Math.max(m, Math.abs(v));
  const rows = [...S.rows.values()];
  for (const x of rows) { const v = (x.raw[S.ix.ce_gex] || 0) + (x.raw[S.ix.pe_gex] || 0); m = Math.max(m, Math.abs(v)); }
  const xOf = (v) => px + ((v - x0) / (x1 - x0)) * pw, yOf = (v) => py + ph / 2 - (v / m) * (ph / 2);
  line(px, yOf(0), px + pw, yOf(0), alpha(C.text, 0.3));
  const bw = Math.max(2, (pw / Math.max(rows.length, 1)) * 0.5);
  for (const x of rows) {
    const v = (x.raw[S.ix.ce_gex] || 0) + (x.raw[S.ix.pe_gex] || 0), xx = xOf(x.k);
    ctx.fillStyle = alpha(v >= 0 ? C.green : C.red, 0.55); ctx.fillRect(xx - bw / 2, Math.min(yOf(0), yOf(v)), bw, Math.abs(yOf(v) - yOf(0)));
  }
  ctx.save(); ctx.shadowColor = C.cyan; ctx.shadowBlur = 8; ctx.strokeStyle = C.cyan; ctx.lineWidth = 1.6; ctx.beginPath();
  g.grid.forEach((v, i) => { const x = xOf(v), y = yOf(g.net[i] || 0); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); }); ctx.stroke(); ctx.restore();
  if (fin(h.spot)) { const x = xOf(h.spot); line(x, py, x, py + ph, alpha(C.white, 0.8), 1); T('spot', x + 3, py + 9, 9, C.white); }
  if (fin(h.flip)) { const x = xOf(h.flip); line(x, py, x, py + ph, C.violet, 1, [4, 3]); }
  T(`call wall ${nf(h.call_wall)}`, px, r.y + r.h - 8, 10, C.red, 'left', 600);
  T(`put wall ${nf(h.put_wall)}`, px + pw / 2, r.y + r.h - 8, 10, C.green, 'center', 600);
  T(`Γ flip ${nf(h.flip)}`, px + pw, r.y + r.h - 8, 10, C.violet, 'right', 600);
}

// ---------- IV smile ----------
function drawSmile(r) {
  const h = S.f.hud, sm = (S.f.smile || []).filter((p) => fin(p[1])), so = (S.f.smile_open || []).filter((p) => fin(p[1]));
  panel(r, 'VOLATILITY SMILE', `ATM ${fx(h.atm_iv, 1)}% · next ${fx(h.atm_iv_next, 1)}% · skew ${sg(h.skew, 1)}`, C.cyan);
  if (sm.length < 3) return;
  const px = r.x + 14, pw = r.w - 28, py = r.y + 28, ph = r.h - 44;
  const all = sm.concat(so), k0 = Math.min(...sm.map((p) => p[0])), k1 = Math.max(...sm.map((p) => p[0]));
  let lo = Math.min(...all.map((p) => p[1])), hi = Math.max(...all.map((p) => p[1])); const pd = (hi - lo) * 0.1 || 1; lo -= pd; hi += pd;
  const xOf = (k) => px + ((k - k0) / (k1 - k0 || 1)) * pw, yOf = (v) => py + (1 - (v - lo) / (hi - lo)) * ph;
  for (let i = 0; i <= 3; i++) { const v = lo + ((hi - lo) * i) / 3; line(px, yOf(v), px + pw, yOf(v), C.grid); T(fx(v, 1), px + pw, yOf(v) - 2, 8.5, C.dim, 'right'); }
  const path = (pts, col, w, dash, glow) => {
    ctx.save(); if (glow) { ctx.shadowColor = col; ctx.shadowBlur = 8; }
    ctx.strokeStyle = col; ctx.lineWidth = w; ctx.setLineDash(dash || []); ctx.beginPath();
    pts.filter((p) => p[0] >= k0 && p[0] <= k1).forEach((p, i) => { if (i) ctx.lineTo(xOf(p[0]), yOf(p[1])); else ctx.moveTo(xOf(p[0]), yOf(p[1])); });
    ctx.stroke(); ctx.restore();
  };
  if (so.length > 2) path(so, alpha(C.text, 0.35), 1, [3, 3], false);
  path(sm, C.cyan, 2, null, true);
  if (fin(h.atm)) { const x = xOf(h.atm); line(x, py, x, py + ph, alpha(C.white, 0.35), 1, [2, 3]); }
  T('— now   ┄ 09:20', px, r.y + r.h - 5, 9, C.dim);
  const iv = S.hist ? S.hist.iv.filter((p) => fin(p[1])) : [];
  if (iv.length > 2) {
    const sx = px + pw - 120, sy = r.y + r.h - 22, sw = 116, sh = 14; let a = Infinity, b = -Infinity;
    for (const p of iv) { a = Math.min(a, p[1]); b = Math.max(b, p[1]); }
    ctx.strokeStyle = C.amber; ctx.lineWidth = 1; ctx.beginPath();
    iv.forEach((p, i) => { const x = sx + (i / (iv.length - 1)) * sw, y = sy + sh - ((p[1] - a) / (b - a || 1)) * sh; if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    ctx.stroke(); T('ATM IV today', sx - 4, sy + 10, 8.5, C.amber, 'right');
  }
}

// ---------- meters ----------
function meter(x, y, w, label, left, right, lcol, rcol, ltxt, rtxt) {
  T(label, x, y, 9.5, C.dim, 'left', 600);
  const tot = (left || 0) + (right || 0), share = tot > 0 ? left / tot : 0.5, by = y + 6;
  ctx.fillStyle = alpha(lcol, 0.75); ctx.fillRect(x, by, w * share, 7);
  ctx.fillStyle = alpha(rcol, 0.75); ctx.fillRect(x + w * share, by, w * (1 - share), 7);
  line(x + w / 2, by - 2, x + w / 2, by + 9, alpha(C.white, 0.4));
  T(ltxt.replace('{p}', (share * 100).toFixed(0)), x, by + 19, 10, lcol, 'left', 600);
  T(rtxt.replace('{p}', ((1 - share) * 100).toFixed(0)), x + w, by + 19, 10, rcol, 'right', 600);
}
function drawMeters(r) {
  const h = S.f.hud, fl = h.flow || {};
  panel(r, 'FLOW · last ~5 min');
  const x = r.x + 12, w = r.w - 24;
  meter(x, r.y + 36, w, 'CALLS bought vs sold', fl.cb, fl.cs, C.buy, C.sell, 'buy {p}%', 'sell {p}%');
  meter(x, r.y + 76, w, 'PUTS bought vs sold', fl.pb, fl.ps, C.buy, C.sell, 'buy {p}%', 'sell {p}%');
  const imb = fin(h.imb) ? h.imb : 0;
  meter(x, r.y + 116, w, 'BOOK bids vs offers', 1 + imb, 1 - imb, C.green, C.red, 'bids {p}%', 'offers {p}%');
  const rich = fin(h.rv) && fin(h.iv_ref) ? h.iv_ref - h.rv : null;
  T(rich === null ? 'IV vs RV —' : rich > 0 ? `options RICH +${fx(rich, 1)} vol` : `options CHEAP ${fx(rich, 1)} vol`,
    x, r.y + 162, 11, rich === null ? C.dim : rich > 0 ? C.amber : C.cyan, 'left', 700);
  T(h.dte0 ? 'vs next expiry IV (0DTE today)' : 'weekly ATM IV vs realised', x, r.y + 178, 9, C.dim);
}

// ---------- depth popup ----------
function drawDepth(r) {
  const D = S.f.depth;
  panel(r, `STRIKE ${nf(D.k)} · BOOK & GREEKS`, 'Esc ✕', C.dim);
  hit(r.x + r.w - 50, r.y + 4, 46, 18, () => { S.sel = null; view(); });
  const colW = (r.w - 36) / 2;
  ['ce', 'pe'].forEach((side, ci) => {
    const o = D[side], x = r.x + 12 + ci * (colW + 12); let y = r.y + 38;
    if (!o) { T('no data', x, y, 11, C.dim); return; }
    T(`${side.toUpperCase()} ${o.levels}-LEVEL`, x, y, 11, side === 'ce' ? C.red : C.green, 'left', 800);
    T(fx(o.ltp, 2), x + colW, y, 13, C.white, 'right', 800); y += 16;
    const g = [['IV', fx(o.iv, 1) + '%'], ['Δ', fx(o.delta, 2)], ['Γ', fin(o.gamma) ? o.gamma.toFixed(4) : '—'], ['Θ/day', fx(o.theta, 1)], ['ν/pt', fx(o.vega, 1)],
      ['OI', qf(o.oi)], ['ΔOI', qf(o.doi)], ['VOL', qf(o.vol)]];
    g.forEach(([l, v], i) => { const gx = x + (i % 4) * (colW / 4), gy = y + Math.floor(i / 4) * 26; T(l, gx, gy + 8, 8.5, C.dim); T(v, gx, gy + 20, 10.5, C.text, 'left', 600); });
    y += 58;
    const tot = (o.buy || 0) + (o.sell || 0), sh = tot ? o.buy / tot : 0.5;
    ctx.fillStyle = alpha(C.buy, 0.7); ctx.fillRect(x, y, colW * sh, 5); ctx.fillStyle = alpha(C.sell, 0.7); ctx.fillRect(x + colW * sh, y, colW * (1 - sh), 5);
    T(`buyers ${(sh * 100).toFixed(0)}% · sellers ${((1 - sh) * 100).toFixed(0)}%`, x, y + 17, 9, C.dim); y += 28;
    const lv = Math.max(o.bids.length, o.asks.length), rowH = Math.min(15, (r.y + r.h - y - 10) / Math.max(lv * 2, 1));
    let mq = 1; for (const a of o.bids.concat(o.asks)) mq = Math.max(mq, a[1] || 0);
    o.asks.slice().reverse().forEach((a, i) => { const yy = y + i * rowH; ctx.fillStyle = alpha(C.red, 0.35); ctx.fillRect(x + colW * (1 - (a[1] || 0) / mq), yy, colW * (a[1] || 0) / mq, rowH - 2); T(fx(a[0], 2), x + 2, yy + rowH - 4, 9.5, C.red); T(qf(a[1]), x + colW - 2, yy + rowH - 4, 9.5, C.text, 'right'); });
    const y2 = y + o.asks.length * rowH + 3;
    o.bids.forEach((a, i) => { const yy = y2 + i * rowH; ctx.fillStyle = alpha(C.green, 0.35); ctx.fillRect(x + colW * (1 - (a[1] || 0) / mq), yy, colW * (a[1] || 0) / mq, rowH - 2); T(fx(a[0], 2), x + 2, yy + rowH - 4, 9.5, C.green); T(qf(a[1]), x + colW - 2, yy + rowH - 4, 9.5, C.text, 'right'); });
  });
}

// ---------- OI heatmap ----------
function drawHeat(r) {
  panel(r, 'OI FLOW', 'green = put writing (support) · red = call writing (resistance)', C.dim);
  const hist = S.hist, rows = [...S.rows.values()].sort((a, b) => b.k - a.k);
  if (!hist || !hist.heat.length || rows.length < 2) return;
  const px = r.x + 56, pw = r.w - 70, py = r.y + 26, ph = r.h - 42, ks = rows.map((x) => x.k), n = ks.length, step = ks[0] - ks[1];
  const m0 = hist.heat[0][0], mLast = hist.heat[hist.heat.length - 1][0];
  const t0 = Math.max(m0, dayStart(m0)), t1 = Math.max(mLast + 60000, t0 + 60 * 60000), cols = Math.round((t1 - t0) / 60000);
  const key = `${hist.minute_seq}|${S.u}|${ks[0]}|${n}|${cols}`;
  if (key !== S.heatKey) {
    S.heatKey = key;
    const c = S.heatCv || document.createElement('canvas'); S.heatCv = c; c.width = cols; c.height = n;
    const g = c.getContext('2d'); g.clearRect(0, 0, cols, n);
    const vals = []; for (const [, col] of hist.heat) for (const [, dc, dp] of col) vals.push(Math.abs((dp || 0) - (dc || 0)));
    vals.sort((a, b) => a - b); const sc = vals[Math.floor(vals.length * 0.97)] || 1;
    for (const [m, col] of hist.heat) {
      const xi = Math.round((m - t0) / 60000); if (xi < 0 || xi >= cols) continue;
      for (const [k, dc, dp] of col) {
        const yi = Math.round((ks[0] - k) / step); if (yi < 0 || yi >= n) continue;
        const v = (dp || 0) - (dc || 0), a = Math.min(1, Math.abs(v) / sc);
        if (a < 0.04) continue;
        g.fillStyle = alpha(v > 0 ? C.green : C.red, 0.15 + 0.85 * a); g.fillRect(xi, yi, 1, 1);
      }
    }
  }
  ctx.imageSmoothingEnabled = false; ctx.drawImage(S.heatCv, px, py, pw, ph); ctx.imageSmoothingEnabled = true;
  const rh = ph / n, xOf = (t) => px + ((t - t0) / (t1 - t0)) * pw;
  ks.forEach((k, i) => { if (i % Math.ceil(12 / rh) === 0) T(nf(k), px - 6, py + i * rh + rh / 2 + 3, 8.5, C.dim, 'right'); });
  const cs = liveCandles().filter((c) => c[0] >= t0);
  if (cs.length > 1) {
    ctx.save(); ctx.shadowColor = C.cyan; ctx.shadowBlur = 6; ctx.strokeStyle = C.cyan; ctx.lineWidth = 1.4; ctx.beginPath();
    cs.forEach((c, i) => { const x = xOf(c[0]), y = py + ((ks[0] - c[4]) / step + 0.5) * rh; if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    ctx.stroke(); ctx.restore();
  }
  const span = (t1 - t0) / 60000, every = span > 180 ? 60 : 30;
  for (let t = Math.ceil(t0 / (every * 60000)) * every * 60000; t <= t1; t += every * 60000) T(ist(t), xOf(t), r.y + r.h - 4, 8.5, C.dim, 'center');
}

// ---------- JARVIS feed ----------
function wrap(text, w, size) {
  font(size); const words = text.split(' '), out = []; let cur = '';
  for (const wd of words) { const t = cur ? cur + ' ' + wd : wd; if (ctx.measureText(t).width > w && cur) { out.push(cur); cur = wd; } else cur = t; }
  if (cur) out.push(cur); return out;
}
function drawFeed(r, now) {
  const bw = S.f.bounce;
  panel(r, 'J.A.R.V.I.S · WHAT THE FLOW IS SAYING', `${S.feed.length} events · B = bounce watch${bw && bw.open ? ` (${bw.open} open)` : ''}`,
    bw && bw.open ? C.amber : C.dim);
  hit(r.x + r.w - 220, r.y, 220, 24, () => { S.bw = true; });
  let y = r.y + 34;
  const sevC = { hi: C.amber, med: C.cyan, low: C.dim };
  for (const e of S.feed) {
    if (y > r.y + r.h - 10) break;
    const low = e.sev === 'low', size = low ? 10.5 : 11.5;
    const a = e.born ? clamp((now - e.born) / 500, 0, 1) : 1, off = (1 - a) * 24;
    const lines = wrap(e.text, r.w - 100, size).slice(0, low ? 1 : 2);
    ctx.globalAlpha = a * (low ? 0.7 : 1);
    if (e.born && now - e.born < 3000 && !low) { ctx.fillStyle = alpha(sevC[e.sev], 0.08 * (1 - (now - e.born) / 3000)); ctx.fillRect(r.x + 4, y - 13, r.w - 8, lines.length * 15 + 6); }
    ctx.save(); ctx.shadowColor = sevC[e.sev]; ctx.shadowBlur = e.sev === 'hi' ? 10 : 0;
    ctx.fillStyle = sevC[e.sev]; ctx.beginPath(); ctx.arc(r.x + 16 + off, y - 4, low ? 2.5 : 3.5, 0, Math.PI * 2); ctx.fill(); ctx.restore();
    T(ist(e.ts), r.x + 26 + off, y, 10, C.dim, 'left', 600);
    lines.forEach((l, i) => T(fit(l, r.w - 100, size), r.x + 74 + off, y + i * 15, size, e.sev === 'hi' ? C.white : low ? C.dim : C.text, 'left', e.sev === 'hi' ? 600 : 400));
    if (fin(e.k)) hit(r.x + 4, y - 13, r.w - 8, lines.length * 15 + 4, () => { S.flash = { k: e.k, until: performance.now() + 4000 }; });
    ctx.globalAlpha = 1;
    y += lines.length * 15 + (low ? 5 : 8);
  }
}

// ---------- bounce watch (paper) ----------
const pct = (x, d = 1) => (fin(x) ? (x > 0 ? '+' : '') + (x * 100).toFixed(d) + '%' : '—');
function drawBounce(r) {
  const bw = S.f.bounce;
  panel(r, 'BOUNCE WATCH · PAPER ONLY, NEVER ORDERS', 'B = back to feed', C.dim);
  hit(r.x + r.w - 160, r.y, 160, 24, () => { S.bw = false; });
  if (!bw) return;
  const lx = r.x + 12, lw = 270, c1 = lx + 178;
  let y = r.y + 38;
  T('EXIT', lx, y, 9, C.dim, 'left', 700); T('TODAY n·win·avg', c1, y, 9, C.dim, 'right', 700); T('BACKTEST', lx + lw, y, 9, C.dim, 'right', 700);
  y += 16;
  bw.today.forEach(([plan, n, win, avg], i) => {
    const bt = bw.bt[i], main = plan === bw.main;
    T(plan, lx, y, 10.5, main ? C.white : C.text, 'left', main ? 700 : 400);
    T(n ? `${n} · ${(win * 100).toFixed(0)}% · ${pct(avg)}` : '—', c1, y, 10.5, !n ? C.dim : avg > 0 ? C.green : C.red, 'right', 600);
    T(fin(bt[2]) ? `${(bt[2] * 100).toFixed(0)}% · ${pct(bt[3])}` : '—', lx + lw, y, 10.5, C.dim, 'right');
    y += 16;
  });
  y += 6;
  T(`avg per trade after costs, ${bw.lots} lots, exits at the bid`, lx, y, 9, C.dim); y += 13;
  T(`backtest: expiry days since 2024-07, any trigger`, lx, y, 9, C.dim); y += 13;
  T(`(buying at any time instead: ${pct(bw.base)})`, lx, y, 9, C.dim);
  const tx = lx + lw + 22, tr = r.x + r.w - 12;
  line(tx - 11, r.y + 28, tx - 11, r.y + r.h - 10, C.edge);
  y = r.y + 38;
  T('SIGNAL', tx, y, 9, C.dim, 'left', 700); T('PAPER BUY', tx + 210, y, 9, C.dim, 'right', 700); T(bw.main, tr, y, 9, C.dim, 'right', 700);
  y += 16;
  if (!bw.tickets.length) {
    wrap('No triggers yet today. A contract qualifies at ≤ half its 09:20 price and ≥ ₹5, 09:45–15:00; '
      + 'triggers: flush (−25% in 10m), turn (green bar over the last high after a fresh low), buying burst, '
      + 'writers covering. Each one is paper-bought at the ask and graded live.', tr - tx, 10)
      .forEach((l, i) => T(l, tx, y + i * 14, 10, C.dim));
    return;
  }
  for (const t of bw.tickets) {
    if (y > r.y + r.h - 8) break;
    const res = t.res[bw.main], open = !res;
    T(ist(t.ts), tx, y, 10, C.dim, 'left', 600);
    T(`${nf(t.k)} ${t.side}`, tx + 38, y, 10.5, t.side === 'PE' ? C.green : C.red, 'left', 700);
    T(fit(t.lbl, 76, 10), tx + 104, y, 10, C.text);
    T(fx(t.entry, 2), tx + 210, y, 10.5, C.white, 'right', 600);
    const txt = open ? `${pct(t.mark)} now · best ${pct(t.best, 0)}` : `${pct(res[0])} ${res[1]} ${res[2]}m`;
    const v = open ? t.mark : res[0];
    T(fit(txt, tr - tx - 218, 10.5, 600), tr, y, 10.5, !fin(v) ? C.dim : v > 0 ? C.green : C.red, 'right', 600);
    if (open) { ctx.fillStyle = C.amber; ctx.beginPath(); ctx.arc(tx - 5, y - 4, 2.5, 0, Math.PI * 2); ctx.fill(); }
    hit(tx - 8, y - 12, tr - tx + 8, 15, () => { S.flash = { k: t.k, until: performance.now() + 4000 }; });
    y += 15;
  }
}

// ---------- hover tooltip ----------
function drawTip() {
  if (!S.mouse) return;
  const k = strikeAt(S.mouse.x, S.mouse.y); if (k === null) return;
  const r = S.rows.get(k); if (!r) return;
  const v = (side, f) => r.raw[S.ix[side + '_' + f]];
  const lines = [`${nf(k)}            CALL        PUT`,
    ['LTP', 'ltp', (x) => fx(x, 2)], ['IV %', 'iv', (x) => fx(x, 1)], ['Delta', 'delta', (x) => fx(x, 2)], ['Gamma', 'gamma', (x) => (fin(x) ? x.toFixed(4) : '—')],
    ['Theta/d', 'theta', (x) => fx(x, 1)], ['Vega', 'vega', (x) => fx(x, 1)], ['OI', 'oi', qf], ['ΔOI open', 'doi', qf], ['Volume', 'vol', qf],
    ['Bought 1m', 'b1', qf], ['Sold 1m', 's1', qf], ['Buyers 5m', 'bshare', (x) => (fin(x) ? (x * 100).toFixed(0) + '%' : '—')],
    ['Book imb.', 'imb', (x) => fx(x, 2)], ['Activity', 'act', (x) => fx(x, 1) + 'x'], ['Build-up', 'tag', (x) => TAG[x] || '—']];
  const w = 250, hgt = lines.length * 15 + 12; let x = S.mouse.x + 16, y = S.mouse.y + 12;
  if (x + w > W) x = S.mouse.x - w - 16; if (y + hgt > H) y = H - hgt - 6;
  ctx.fillStyle = 'rgba(4,10,18,0.95)'; rr(x, y, w, hgt, 6); ctx.fill(); ctx.strokeStyle = C.bracket; ctx.lineWidth = 1; ctx.stroke();
  lines.forEach((l, i) => {
    const yy = y + 18 + i * 15;
    if (typeof l === 'string') { T(l, x + 10, yy, 11, C.cyan, 'left', 700); return; }
    T(l[0], x + 10, yy, 10.5, C.dim); T(l[2](v('ce', l[1])), x + 150, yy, 10.5, C.red, 'right', 600); T(l[2](v('pe', l[1])), x + 235, yy, 10.5, C.green, 'right', 600);
  });
}

resize();
connect();
requestAnimationFrame(tick);
