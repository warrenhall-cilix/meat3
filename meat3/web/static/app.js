// meat³ web UI: read-only view of meat3d's sensors. Polls /api/values once per daemon poll.
"use strict";

const NONE = "—";
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

const store = {
  get(k, d) { try { const v = localStorage.getItem("meat3." + k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem("meat3." + k, JSON.stringify(v)); } catch (_) { /* private mode */ } },
};

const S = {
  rev: null, sensors: [], byId: {}, values: {}, t: 0, interval: 1000,
  view: store.get("view", "overview"),
  pins: store.get("pins", []),
  shut: new Set(store.get("shut", [])),
  win: store.get("win", 600),
  ovKey: "", ovEls: {}, rowEls: {}, graphs: {}, hoverT: null,
};

// same rules as meat3/core/fmt.py format_value
function fmt(v, dec, unit) {
  if (v == null || !isFinite(v)) return NONE;
  const s = Number(v).toFixed(Math.max(0, dec | 0));
  if (!unit) return s;
  return unit === "%" ? s + unit : s + " " + unit;
}
const sensorFmt = (id, v) => { const m = S.byId[id] || {}; return fmt(v, m.decimals, m.unit); };
const device = (g) => (g || "?").split("/")[0].trim() || "?";
const subgroup = (g) => (g || "").split("/").slice(1).join(" / ");

// ---------------------------------------------------------------- polling
async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store", credentials: "same-origin" });
  if (r.status === 401) { location.href = "/login"; throw new Error("login required"); }
  if (!r.ok) throw new Error(r.status + " " + r.statusText);
  return r.json();
}

async function poll() {
  let next = S.interval;
  try {
    const d = await getJSON("/api/values");
    setStatus(d);
    if (d.connected) {
      if (d.rev !== S.rev) await loadMeta();
      S.values = d.v || {};
      const fresh = d.t !== S.t;
      S.t = d.t;
      renderOverview(d.overview || []);
      if (S.view === "sensors") updateTable();
      if (fresh) appendGraphs(d.t);
    }
  } catch (e) {
    setStatus({ connected: false, error: "web server unreachable (" + e.message + ")" });
    next = 3000;
  }
  setTimeout(poll, next);
}

function setStatus(d) {
  const dot = $("#conn");
  dot.className = "dot " + (d.connected ? "ok" : "bad");
  let txt = d.connected ? "live" : (d.error || "disconnected");
  if (d.connected && d.poll_ms != null) txt += " · poll " + Math.round(d.poll_ms) + " ms";
  if (d.update && d.update.behind) txt += " · " + d.update.behind + " update" + (d.update.behind > 1 ? "s" : "") + " available";
  $("#status-text").textContent = txt;
  if (d.host) { $("#host").textContent = d.host; document.title = "meat³ · " + d.host; }
  const ver = [d.version ? "protocol " + d.version : "", d.update && d.update.installed ? "meat³ " + d.update.installed : ""];
  $("#version").textContent = ver.filter(Boolean).join(" · ");
}

async function loadMeta() {
  const m = await getJSON("/api/meta");
  S.rev = m.rev;
  S.sensors = m.sensors || [];
  S.byId = Object.fromEntries(S.sensors.map((s) => [s.id, s]));
  $("#logout").hidden = !m.auth;
  buildTable();
  S.ovKey = "";
  syncGraphs(true);
}

// ---------------------------------------------------------------- overview
function renderOverview(panels) {
  const key = JSON.stringify(panels.map((p) => [p.title, p.rows.map((r) => r.kind || r.label + "|" + r.id + "|" + (r.max != null)), p.rows.map((r) => (r.ids || []).length)]));
  const root = $("#cards");
  if (key !== S.ovKey) {
    S.ovKey = key;
    S.ovEls = [];
    root.replaceChildren();
    for (const p of panels) {
      const card = el("div", "card");
      card.append(el("h2", null, p.title));
      for (const r of p.rows) {
        const row = el("div", "row");
        const lab = el("span", "lab", r.kind === "pins" ? "12V-2x6 pins" : r.kind === "cores" ? "Core clocks" : r.label);
        row.append(lab);
        const ent = { r, row, lab };
        if (r.kind === "pins") {
          const box = el("div", "chips num"); box.style.gridColumn = "2 / 4";
          ent.chips = r.ids.map((id, i) => { const c = el("span"); c.dataset.n = i + 1; box.append(c); return c; });
          row.append(box);
          row.title = "Click to graph all pins";
          row.onclick = () => r.ids.filter(Boolean).forEach((id) => pin(id, true));
        } else if (r.kind === "cores") {
          const box = el("div", "spark"); box.style.gridColumn = "2 / 4";
          ent.bars = r.ids.map(() => { const b = el("i"); box.append(b); return b; });
          row.append(box);
          row.onclick = () => r.ids.filter(Boolean).forEach((id) => pin(id, true));
        } else {
          const bar = el("div", "bar"); const fill = el("i"); bar.append(fill);
          if (r.max == null) bar.style.visibility = "hidden";
          const val = el("span", "val num");
          row.append(bar, val);
          Object.assign(ent, { fill, val });
          row.onclick = () => pin(r.id);
          row.title = (S.byId[r.id] || {}).hint || r.id;
        }
        card.append(row);
        S.ovEls.push(ent);
      }
      root.append(card);
    }
  }
  // values only
  let i = 0;
  for (const p of panels) for (const r of p.rows) {
    const ent = S.ovEls[i++];
    if (!ent) continue;
    if (r.kind === "pins") {
      r.ids.forEach((id, k) => {
        const v = id && S.values[id];
        ent.chips[k].textContent = (k + 1) + ": " + (v && v[0] != null ? v[0].toFixed(1) + " A" : NONE);
        ent.chips[k].className = v ? "s" + v[4] : "";
      });
    } else if (r.kind === "cores") {
      const vals = r.ids.map((id) => (id && S.values[id] ? S.values[id][0] : null));
      const top = Math.max(1, ...vals.filter((v) => v != null));
      vals.forEach((v, k) => {
        ent.bars[k].style.height = (v ? Math.max(4, (100 * v) / top) : 0) + "%";
        ent.bars[k].title = "Core " + k + ": " + sensorFmt(r.ids[k], v);
      });
    } else {
      const v = S.values[r.id] || [];
      ent.val.textContent = sensorFmt(r.id, v[0]);
      ent.val.className = "val num s" + (v[4] || 0);
      ent.lab.textContent = (S.pins.includes(r.id) ? "★ " : "") + r.label;
      if (r.max) {
        ent.fill.style.width = Math.max(0, Math.min(100, (100 * (v[0] || 0)) / r.max)) + "%";
        ent.fill.className = "s" + (v[4] || 0);
      }
    }
  }
}

// ---------------------------------------------------------------- sensors table
function buildTable() {
  const table = $("#sensors");
  for (const tb of table.querySelectorAll("tbody")) tb.remove();
  S.rowEls = {};
  let tb = null, dev = null, grp = null;
  for (const s of S.sensors) {
    const d = device(s.group), g = subgroup(s.group);
    if (d !== dev) {
      dev = d; grp = null;
      tb = el("tbody");
      tb.dataset.dev = d;
      const tr = el("tr", "dev"); const td = el("td", null, d); td.colSpan = 6; tr.append(td);
      tr.dataset.key = d;
      tr.onclick = () => toggleShut(d);
      tb.append(tr);
      table.append(tb);
    }
    if (g && g !== grp) {
      grp = g;
      const tr = el("tr", "grp"); const td = el("td", null, g); td.colSpan = 6; tr.append(td);
      tr.dataset.key = d + "/" + g;
      tr.dataset.dev = d;
      tr.onclick = () => toggleShut(d + "/" + g);
      tb.append(tr);
    }
    const tr = el("tr", "s");
    tr.dataset.dev = d; tr.dataset.grp = d + "/" + g;
    tr.dataset.search = (s.label + " " + s.group + " " + s.id).toLowerCase();
    const star = el("button", "star", "☆"); star.title = "Graph this sensor";
    star.onclick = () => pin(s.id);
    const tdp = el("td", "pin"); tdp.append(star);
    const lab = el("td", "lab", s.label); lab.title = s.hint ? s.hint + "\n" + s.id : s.id;
    const cells = [el("td", "v num"), el("td", "v num"), el("td", "v num"), el("td", "v num")];
    tr.append(tdp, lab, ...cells);
    tb.append(tr);
    S.rowEls[s.id] = { tr, star, cells };
  }
  applyVisibility();
  updateTable();
}

function updateTable() {
  for (const [id, e] of Object.entries(S.rowEls)) {
    if (e.tr.classList.contains("hidden")) continue;
    const v = S.values[id] || [];
    for (let k = 0; k < 4; k++) e.cells[k].textContent = sensorFmt(id, v[k]);
    e.cells[0].className = "v num s" + (v[4] || 0);
    const on = S.pins.includes(id);
    e.star.textContent = on ? "★" : "☆";
    e.star.classList.toggle("on", on);
  }
}

function toggleShut(key) {
  if (S.shut.has(key)) S.shut.delete(key); else S.shut.add(key);
  store.set("shut", [...S.shut]);
  applyVisibility();
  updateTable();
}

function applyVisibility() {
  const q = $("#filter").value.trim().toLowerCase();
  for (const tb of document.querySelectorAll("#sensors tbody")) {
    const d = tb.dataset.dev;
    let any = false;
    const grpHas = {};
    for (const tr of tb.querySelectorAll("tr.s")) {
      const match = !q || tr.dataset.search.includes(q);
      const hide = !match || (!q && (S.shut.has(d) || S.shut.has(tr.dataset.grp)));
      tr.classList.toggle("hidden", hide);
      if (match) { any = true; grpHas[tr.dataset.grp] = true; }
    }
    tb.querySelector("tr.dev").classList.toggle("shut", !q && S.shut.has(d));
    tb.querySelector("tr.dev").classList.toggle("hidden", !any);
    for (const tr of tb.querySelectorAll("tr.grp")) {
      tr.classList.toggle("hidden", !grpHas[tr.dataset.key] || (!q && S.shut.has(d)));
      tr.classList.toggle("shut", !q && S.shut.has(tr.dataset.key));
    }
  }
}

// ---------------------------------------------------------------- graphs
function pin(id, onlyAdd) {
  const i = S.pins.indexOf(id);
  if (i >= 0) { if (onlyAdd) return; S.pins.splice(i, 1); } else S.pins.push(id);
  store.set("pins", S.pins);
  syncGraphs(false);
  updateTable();
}

async function syncGraphs(all) {
  const box = $("#graphs");
  for (const id of Object.keys(S.graphs)) {
    if (!S.pins.includes(id)) { S.graphs[id].card.remove(); delete S.graphs[id]; }
  }
  const need = [];
  for (const id of S.pins) {
    if (!S.graphs[id]) { S.graphs[id] = makeGraph(id); box.append(S.graphs[id].card); need.push(id); }
    else if (all) need.push(id);
  }
  // keep pin order
  for (const id of S.pins) box.append(S.graphs[id].card);
  $("#graphs-empty").hidden = S.pins.length > 0;
  $("#pin-count").textContent = S.pins.length ? "(" + S.pins.length + ")" : "";
  if (!need.length) return;
  try {
    const h = await getJSON("/api/history?s=" + S.win + "&ids=" + need.map(encodeURIComponent).join(","));
    for (const id of need) {
      const g = S.graphs[id];
      if (!g) continue;
      g.t = h.t.slice();
      g.y = (h.series[id] || h.t.map(() => null)).slice();
      draw(g);
    }
  } catch (_) { /* next poll fills in */ }
}

function makeGraph(id) {
  const m = S.byId[id] || { label: id, group: "" };
  const card = el("div", "graph");
  const head = el("header");
  const title = el("div", "t", m.label + " ");
  title.append(el("small", null, device(m.group)));
  title.title = (m.group || "") + "\n" + id;
  const now = el("span", "now num");
  const x = el("button", "x", "✕"); x.title = "Remove graph"; x.onclick = () => pin(id);
  head.append(title, now, x);
  const plot = el("div", "plot");
  const canvas = el("canvas");
  const tip = el("div", "tip num"); tip.hidden = true;
  plot.append(canvas, tip);
  const stats = el("div", "stats num");
  card.append(head, plot, stats);
  const g = { id, card, canvas, tip, now, stats, t: [], y: [], geo: null };
  // hover (mouse) or drag (finger): crosshair, dot and value at that moment, on every graph at once
  const at = (ev) => {
    if (!g.geo) return;
    const r = canvas.getBoundingClientRect(), { padL, pw, tStart } = g.geo;
    const x = Math.min(Math.max(ev.clientX - r.left, padL), padL + pw);
    setHover(tStart + ((x - padL) / pw) * S.win);
  };
  canvas.addEventListener("pointermove", at);
  canvas.addEventListener("pointerdown", at);
  canvas.addEventListener("pointerleave", (ev) => { if (ev.pointerType === "mouse") setHover(null); });
  return g;
}

function setHover(t) {
  S.hoverT = t;
  Object.values(S.graphs).forEach(draw);
}

// index of the non-empty sample nearest to time t (g.t is sorted)
function nearest(g, t) {
  let lo = 0, hi = g.t.length - 1;
  if (hi < 0) return -1;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (g.t[mid] < t) lo = mid + 1; else hi = mid; }
  if (lo > 0 && Math.abs(g.t[lo - 1] - t) < Math.abs(g.t[lo] - t)) lo--;
  for (let d = 0; d < 5; d++) {          // step over a gap of a few missing readings
    if (g.y[lo + d] != null) return lo + d;
    if (g.y[lo - d] != null) return lo - d;
  }
  return -1;
}

function ago(sec) {
  sec = Math.round(sec);
  if (sec < 1) return "now";
  return sec < 60 ? sec + " s ago" : Math.floor(sec / 60) + " min " + (sec % 60 ? (sec % 60) + " s " : "") + "ago";
}

function appendGraphs(t) {
  for (const g of Object.values(S.graphs)) {
    const v = S.values[g.id];
    g.t.push(t); g.y.push(v ? v[0] : null);
    while (g.t.length && g.t[0] < t - S.win) { g.t.shift(); g.y.shift(); }
    if (S.view === "graphs") draw(g);
  }
}

function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function draw(g) {
  const id = g.id, m = S.byId[id] || {};
  const nums = g.y.filter((v) => v != null);
  const last = g.y.length ? g.y[g.y.length - 1] : null;
  g.now.textContent = sensorFmt(id, last);
  const cur = S.values[id] || [];
  g.now.className = "now num s" + (cur[4] || 0);
  g.stats.textContent = nums.length
    ? "window min " + sensorFmt(id, Math.min(...nums)) + " · max " + sensorFmt(id, Math.max(...nums)) +
      " · avg " + sensorFmt(id, nums.reduce((a, b) => a + b, 0) / nums.length)
    : "no data yet";

  const c = g.canvas, dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (!w || !h) return;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
  }
  const ctx = c.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const padL = 58, padR = 6, padT = 6, padB = 16;
  const pw = w - padL - padR, ph = h - padT - padB;
  let lo = nums.length ? Math.min(...nums) : 0, hi = nums.length ? Math.max(...nums) : 1;
  if (m.unit === "%" ) { lo = Math.min(lo, 0); hi = Math.max(hi, 100); }
  if (hi - lo < 1e-9) { const d = Math.abs(hi) * 0.05 || 1; lo -= d; hi += d; }
  const span = hi - lo; lo -= span * 0.06; hi += span * 0.06;
  const tEnd = g.t.length ? g.t[g.t.length - 1] : Date.now() / 1000;
  const tStart = tEnd - S.win;
  const X = (t) => padL + ((t - tStart) / S.win) * pw;
  const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * ph;

  ctx.font = "11px system-ui, sans-serif";
  ctx.fillStyle = css("--dim");
  ctx.strokeStyle = css("--border");
  ctx.lineWidth = 1;
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let k = 0; k <= 3; k++) {
    const v = lo + ((hi - lo) * k) / 3, y = Y(v);
    ctx.beginPath(); ctx.moveTo(padL, Math.round(y) + 0.5); ctx.lineTo(w - padR, Math.round(y) + 0.5); ctx.stroke();
    ctx.fillText(fmt(v, Math.max(m.decimals | 0, span < 3 ? 2 : 0), m.unit), padL - 6, y);
  }
  ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
  const step = S.win <= 60 ? 15 : S.win <= 300 ? 60 : S.win <= 600 ? 120 : 300;
  for (let s = 0; s <= S.win; s += step) {
    const x = padL + pw - (s / S.win) * pw;
    ctx.fillText(s === 0 ? "now" : "-" + (s >= 60 ? s / 60 + "m" : s + "s"), Math.min(Math.max(x, padL + 12), w - 14), h - 3);
  }

  ctx.strokeStyle = css("--accent");
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  let pen = false;
  for (let k = 0; k < g.t.length; k++) {
    const v = g.y[k];
    if (v == null || g.t[k] < tStart) { pen = false; continue; }
    const x = X(g.t[k]), y = Y(v);
    if (pen) ctx.lineTo(x, y); else ctx.moveTo(x, y);
    pen = true;
  }
  ctx.stroke();

  g.geo = { padL, pw, tStart };
  const k = S.hoverT == null || S.hoverT < tStart ? -1 : nearest(g, S.hoverT);
  if (k < 0 || g.t[k] < tStart) { g.tip.hidden = true; return; }
  const px = X(g.t[k]), py = Y(g.y[k]);
  ctx.strokeStyle = css("--dim");
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(Math.round(px) + 0.5, padT); ctx.lineTo(Math.round(px) + 0.5, padT + ph); ctx.stroke();
  ctx.setLineDash([]);
  ctx.beginPath(); ctx.arc(px, py, 4.5, 0, 2 * Math.PI);
  ctx.fillStyle = css("--accent"); ctx.fill();
  ctx.lineWidth = 2; ctx.strokeStyle = css("--card"); ctx.stroke();
  const when = new Date(g.t[k] * 1000);
  g.tip.textContent = "";
  g.tip.append(el("b", null, sensorFmt(id, g.y[k])),
               el("span", null, when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) +
                                " · " + ago(tEnd - g.t[k])));
  g.tip.hidden = false;
  const tw = g.tip.offsetWidth, th = g.tip.offsetHeight;
  let left = px + 10, top = py - th - 8;
  if (left + tw > w) left = px - tw - 10;              // flip to the left near the right edge
  if (top < 0) top = py + 10;
  g.tip.style.left = Math.max(0, left) + "px";
  g.tip.style.top = Math.min(top, h - th) + "px";
}

// ---------------------------------------------------------------- views
function show(view) {
  S.view = view;
  store.set("view", view);
  for (const b of document.querySelectorAll("#tabs button")) b.classList.toggle("on", b.dataset.view === view);
  for (const v of document.querySelectorAll(".view")) v.classList.toggle("on", v.id === "view-" + view);
  if (view === "sensors") updateTable();
  if (view === "graphs") Object.values(S.graphs).forEach(draw);
}

function init() {
  for (const b of document.querySelectorAll("#tabs button")) b.onclick = () => show(b.dataset.view);
  $("#filter").addEventListener("input", () => { applyVisibility(); updateTable(); });
  $("#expand").onclick = () => { S.shut.clear(); store.set("shut", []); applyVisibility(); updateTable(); };
  $("#collapse").onclick = () => {
    for (const tr of document.querySelectorAll("#sensors tr.dev")) S.shut.add(tr.dataset.key);
    store.set("shut", [...S.shut]); applyVisibility();
  };
  const win = $("#window");
  win.value = String(S.win);
  if (!win.value) { win.value = "600"; S.win = 600; }
  win.onchange = () => { S.win = Number(win.value); store.set("win", S.win); syncGraphs(true); };
  $("#clear-pins").onclick = () => { S.pins = []; store.set("pins", []); syncGraphs(false); updateTable(); };
  // phones: a tap outside the graphs clears the crosshair
  document.addEventListener("pointerdown", (ev) => { if (S.hoverT != null && !ev.target.closest(".plot")) setHover(null); });
  window.addEventListener("resize", () => { if (S.view === "graphs") Object.values(S.graphs).forEach(draw); });
  show(["overview", "sensors", "graphs"].includes(S.view) ? S.view : "overview");
  poll();
}

document.addEventListener("DOMContentLoaded", init);
