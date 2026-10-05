/* Numbers: the stats bar at the bottom, the Stats rail, and the details
   dialog with rates against targets, breakdowns and the best hour to call.
   Samples, wins and rupees count on the day of the dial that created them. */
import { $, esc, fmtNum, fmtINR, fmtPct, fmtClock, store } from "./util.js";
import { S, on } from "./state.js";
import { api } from "./api.js";
import { openModal, closeX, modal } from "./ui.js";
import { registerTab, currentTab } from "./rails.js";

let range = store.get("in_range", "today");
let data = null;

const pct = (r) => '<span class="rate ' + r.tone + '" title="' + esc(r.label) + ": " + r.num + " of " + r.den + ", target " + fmtPct(r.target) + '">' + fmtPct(r.value) + "</span>";
const rate = (key) => ((data && data.rates) || []).find((r) => r.key === key) || { tone: "none", value: null, num: 0, den: 0, target: 0, label: key };

function renderBar() {
  if (!data) return;
  const c = data.counts;
  const cell = (n, label, extra, cls) => '<span' + (cls ? ' class="' + cls + '"' : "") + "><b>" + n + "</b>" + label + (extra || "") + "</span>";
  const pos = range === "today" ? c.positive + "/" + data.positive_target : c.positive;
  $("sb-cells").innerHTML = [
    cell(fmtNum(c.dials), "dials"),
    cell(fmtNum(c.connects), "connects", pct(rate("connect_rate"))),
    cell(fmtNum(c.pitched), "pitched", pct(rate("pitch_rate"))),
    cell(fmtNum(c.interested), "interested", pct(rate("interest_rate"))),
    cell(fmtNum(c.samples_asked), "samples", pct(rate("sample_rate")), "star"),
    cell(fmtNum(c.demos_booked), "demos"),
    cell(fmtNum(c.samples_delivered), "delivered"),
    cell(fmtNum(c.won), "won", c.inr_won ? '<span class="rate">' + fmtINR(c.inr_won) + "</span>" : ""),
    cell(pos, "positive")
  ].join("");
}

function rateRows(list) {
  return list.map((r) => '<div><span>' + (r.star ? "<b>" + esc(r.label) + "</b>" : esc(r.label)) +
    '<span class="muted"> ' + r.num + " of " + r.den + "</span></span>" +
    '<span class="rv"><span class="rate ' + r.tone + '">' + fmtPct(r.value) + '</span><span class="muted"> / ' + fmtPct(r.target) + "</span></span></div>").join("");
}

function countRows(c) {
  const rows = [["Dials", fmtNum(c.dials)], ["Connects", fmtNum(c.connects)], ["DMs reached", fmtNum(c.dms_reached)],
    ["DMs pitched", fmtNum(c.pitched)], ["Interested", fmtNum(c.interested)], ["Samples asked", fmtNum(c.samples_asked)],
    ["Samples received", fmtNum(c.samples_received)], ["Samples delivered", fmtNum(c.samples_delivered)],
    ["Demos booked", fmtNum(c.demos_booked)], ["Won", fmtNum(c.won)], ["₹ won", fmtINR(c.inr_won)],
    ["Drawings committed", fmtNum(c.drawings_committed)], ["Average talk", fmtClock(c.avg_talk || 0)],
    ["Average turnaround", c.avg_turnaround_min != null ? fmtNum(c.avg_turnaround_min) + " min" : "-"],
    ["Positive conversations", c.positive + (data.range === "today" ? " of " + data.positive_target : "")],
    ["Dials an hour", data.pace && data.pace.dials_per_hour != null ? data.pace.dials_per_hour : "-"]];
  return rows.map((r) => "<div><span>" + esc(r[0]) + '</span><b class="num">' + r[1] + "</b></div>").join("");
}

function objRows() {
  return (data.objections || []).length
    ? data.objections.map((o, i) => "<div><span>" + (i + 1) + ". " + esc(o.label) + '</span><b class="num">' + o.count + "</b></div>").join("")
    : '<p class="muted small">None logged yet.</p>';
}

const RANGE_NAME = { today: "Today", week: "This week", all: "All time" };

/* ---- Stats rail ----------------------------------------------------------- */

function renderRail() {
  if (!data) { $("l-stats").innerHTML = '<div class="skel"></div>'.repeat(5); return; }
  $("l-stats").innerHTML =
    '<p class="stage-h">' + esc(RANGE_NAME[range]) + " rates<span>target</span></p>" + '<div class="kv">' + rateRows(data.rates) + "</div>" +
    '<p class="stage-h">Top objections</p><div class="kv">' + objRows() + "</div>" +
    '<p class="stage-h">Best hour to call</p><div class="kv">' + bestHours(4) + "</div>" +
    '<div class="acts" style="padding:12px 8px"><button class="btn sm" data-details>All numbers</button>' +
    '<a class="btn quiet sm" href="/api/export/tracker.csv?range=' + range + '" download>Tracker CSV</a></div>';
}

function bestHours(n) {
  const list = (data.best_hours || []).slice(0, n || 24);
  if (!list.length) return '<p class="muted small">Needs a few calls first.</p>';
  const hr = (h) => { h = +h; return ((h % 12) || 12) + (h < 12 ? " am" : " pm"); };
  return list.map((h) => "<div><span>" + hr(h.k) + " to " + hr(+h.k + 1) + (h.enough ? "" : '<span class="muted"> (few calls)</span>') + "</span>" +
    '<span class="rv"><span class="rate">' + fmtPct(h.connect_rate) + '</span><span class="muted"> of ' + h.dials + "</span></span></div>").join("");
}

/* ---- details dialog --------------------------------------------------------- */

function table(b) {
  if (!b.rows.length) return '<p class="muted small">No calls.</p>';
  return '<table class="bt"><thead><tr><th>' + esc(b.label) + "</th><th>Dials</th><th>Connect</th><th>Pitched</th><th>Samples</th><th>Sample rate</th></tr></thead><tbody>" +
    b.rows.map((r) => "<tr><td>" + esc(b.label.indexOf("Hour") === 0 && r.k !== "(none)" ? String(r.k).padStart(2, "0") + ":00" : r.k) + "</td><td>" + r.dials +
      "</td><td>" + fmtPct(r.connect_rate) + "</td><td>" + (r.pitched || 0) + "</td><td>" + (r.samples || 0) + "</td><td>" + fmtPct(r.sample_rate) + "</td></tr>").join("") +
    "</tbody></table>";
}

function details(script) {
  const q = "/api/stats?range=" + range + (script ? "&script=" + encodeURIComponent(script) : "");
  const draw = (d) => {
    const scripts = d.scripts || [];
    const bd = d.breakdowns || {};
    openModal('<div class="dh"><h2>' + esc(RANGE_NAME[range]) + (script ? ", script " + esc(script) : "") + "</h2>" + closeX() + "</div>" +
      '<p class="sub">Samples, demos, wins and rupees count on the day of the dial that created them.</p>' +
      '<div class="acts"><span class="tabs-text" id="d-range">' + ["today", "week", "all"].map((r) => '<button data-r="' + r + '" aria-selected="' + (r === range) + '">' + RANGE_NAME[r] + "</button>").join("") + "</span>" +
      (scripts.length > 1 ? '<select class="field" id="d-script" style="max-width:160px"><option value="">Every script</option>' +
        scripts.map((s) => "<option" + (s === script ? " selected" : "") + ">" + esc(s) + "</option>").join("") + "</select>" : "") + "</div>" +
      '<div class="cols2"><div><h3 class="h3">Rates</h3><div class="kv">' + rateRows(d.rates) + '</div><h3 class="h3">Top objections</h3><div class="kv">' + objRows() + "</div></div>" +
      '<div><h3 class="h3">Counts</h3><div class="kv">' + countRows(d.counts) + "</div></div></div>" +
      '<h3 class="h3">Best hour to call</h3><div class="kv">' + bestHours() + "</div>" +
      Object.keys(bd).map((k) => '<h3 class="h3">By ' + esc(bd[k].label.toLowerCase()) + "</h3>" + table(bd[k])).join("") +
      '<div class="acts" style="margin-top:16px"><a class="btn" href="/api/export/tracker.csv?range=' + range + (script ? "&script=" + encodeURIComponent(script) : "") + '" download>Tracker CSV (Imperium columns)</a>' +
      '<a class="btn quiet" href="/api/export/calls.csv?range=' + range + '" download>Every call, CSV</a></div>', { xwide: true });
    $("d-range").addEventListener("click", (e) => { const b = e.target.closest("[data-r]"); if (b) { setRange(b.getAttribute("data-r")); details(script); } });
    if ($("d-script")) $("d-script").addEventListener("change", (e) => details(e.target.value));
  };
  api(q).then((d) => { const keep = data; data = d; draw(d); if (script) data = keep; });
}

/* ---- wiring ------------------------------------------------------------------- */

function setRange(r) {
  range = r; store.set("in_range", r);
  $("sb-range").querySelectorAll("[data-r]").forEach((b) => b.setAttribute("aria-selected", String(b.getAttribute("data-r") === r)));
}

let pending = null;
export function refreshStats() {
  clearTimeout(pending);
  pending = setTimeout(() => api("/api/stats?range=" + range).then((d) => {
    if (d.error) return;
    data = d; S.stats = d; renderBar();
    if (currentTab() === "stats") renderRail();
  }), 120);
}

registerTab("stats", () => { renderRail(); refreshStats(); });

export function wireFunnel() {
  setRange(range);
  $("sb-range").addEventListener("click", (e) => { const b = e.target.closest("[data-r]"); if (b) { setRange(b.getAttribute("data-r")); refreshStats(); } });
  $("sb-more").addEventListener("click", () => details());
  $("l-stats").addEventListener("click", (e) => { if (e.target.closest("[data-details]")) details(); });
  on("saved", refreshStats);
  refreshStats();
}
