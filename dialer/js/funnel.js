/* The funnel bar under the top bar, and the full stats sheet.
   Counts and rates come from the server (dialer/funnel.py); this file only
   lays them out and colours each rate against its target. */
import { $, icon, esc, fmtPct, fmtNum, fmtTalk } from "./util.js";
import { S, on, actions } from "./state.js";
import { api, withAgent } from "./api.js";
import { openModal, closeX } from "./ui.js";

let range = "today";
let last = null;

function grade(value, target, sample) {
  const min = (S.cfg.targets && S.cfg.targets.min_sample) || 10;
  if (value == null || target == null || sample < min) return "none";
  if (value >= target) return "good";
  return value >= target * 0.75 ? "warn" : "bad";
}
const T = () => S.cfg.targets || {};

/* One definition drives both the bar and the sheet. */
function rateDefs(f) {
  const t = T(), r = f.rates;
  return {
    pickup:   { name: "Pickup rate",      short: "",     v: r.pickup,   target: t.pickup,   sample: f.dials,        how: "pickups / dials" },
    pr:       { name: "PR pitch rate",    short: "PR",   v: r.pr,       target: null,       sample: f.dials,        how: "DMs pitched / dials" },
    dm_reach: { name: "DM reach rate",    short: "reach", v: r.dm_reach, target: t.dm_reach, sample: f.pickups,     how: "DMs pitched / pickups" },
    rr:       { name: "RR resonation rate", short: "RR", v: r.rr,       target: null,       sample: f.pitched,      how: "resonations / DMs pitched" },
    offer:    { name: "Offer rate",       short: "",     v: r.offer,    target: t.offer,    sample: f.resonated,    how: "offered / resonations" },
    abr:      { name: "ABR booking rate", short: "ABR",  v: r.abr,      target: t.abr,      sample: f.dials,        how: "booked / dials" },
    sur:      { name: "SUR show-up rate", short: "SUR",  v: r.sur,      target: t.sur,      sample: f.due_bookings, how: "showed / booked calls that have come due" },
    scr:      { name: "SCR sales conversion", short: "SCR", v: r.scr,   target: t.scr,      sample: f.showed,       how: "sales / showed" }
  };
}

function chip(d) {
  if (d.v == null) return "";
  const g = grade(d.v, d.target, d.sample);
  const tip = d.how + (d.target != null ? ". Target " + fmtPct(d.target, 0) + "+" : "");
  return '<span class="rate ' + g + '" title="' + esc(tip) + '">' + (d.short ? d.short + " " : "") + fmtPct(d.v) + "</span>";
}

function cell(label, count, chips, opts) {
  opts = opts || {};
  return '<div class="sb-cell' + (opts.star ? " star" : "") + '"' + (opts.title ? ' title="' + esc(opts.title) + '"' : "") + ">" +
    '<span class="eyebrow">' + (opts.star ? icon("star") : "") + esc(label) + "</span>" +
    '<span class="v"><b>' + count + "</b>" + (chips || "") + "</span></div>";
}

function renderBar(s) {
  if (!s || !s.funnel) return;
  last = s;
  const f = s.funnel, d = rateDefs(f), t = T(), today = s.today || f;
  const convTarget = t.conversations_per_day || 10;
  const convGrade = today.conversations >= convTarget ? "good" : "";
  let html =
    cell("Dials", fmtNum(f.dials)) +
    cell("Pickups", fmtNum(f.pickups), chip(d.pickup)) +
    cell("DMs pitched", fmtNum(f.pitched), chip(d.pr) + chip(d.dm_reach)) +
    cell("Resonated", fmtNum(f.resonated), chip(d.rr)) +
    cell("Offered", fmtNum(f.offered), chip(d.offer)) +
    cell("Booked", fmtNum(f.booked), chip(d.abr), { star: true, title: "ABR is the star metric. Target " + fmtPct(t.abr, 0) + "+, team benchmark " + fmtPct(t.abr_benchmark, 1) }) +
    cell("Calls done", fmtNum(f.showed), chip(d.sur)) +
    cell("Sales", fmtNum(f.sales), chip(d.scr) + (f.sales_amount ? "<small>$" + fmtNum(Math.round(f.sales_amount)) + "</small>" : "")) +
    cell("Convos today", '<span class="' + (convGrade ? "rate good" : "") + '" style="font:inherit;padding:0 3px">' + today.conversations + "</span><small>/ " + convTarget + "</small>", "",
         { star: true, title: "Unique leads today where someone picked up and you offered the meeting or they resonated. Beginner star metric." });
  if (s.session && S.session.on) {
    const hrs = Math.max(S.session.activeSec, 60) / 3600, ss = s.session;
    html += cell("This session /hr", Math.round(ss.dials / hrs), "<small>" + (ss.pitched / hrs).toFixed(1) + " pitch · " + (ss.booked / hrs).toFixed(1) + " book</small>");
  }
  if ((s.objections || []).length) {
    html += '<div class="sb-cell"><span class="eyebrow">Top objections today</span><span class="sb-obj">' +
      s.objections.map((o) => "<b>" + esc(o.label) + "</b> " + o.count).join(" · ") + "</span></div>";
  }
  $("sb-cells").innerHTML = html;
}

export function refreshStats() {
  let url = "/api/stats?range=" + range;
  if (S.session.on && S.session.startedAt) url += "&since=" + encodeURIComponent(S.session.startedAt);
  return api(withAgent(url)).then((s) => { if (!s.error) actions.renderStats(s); return s; }).catch(() => {});
}

/* ---- the full sheet --------------------------------------------------------- */

function sheetHTML(s) {
  const f = s.funnel, d = rateDefs(f);
  const countRows = [["Dials", f.dials], ["Pickups", f.pickups], ["DMs pitched", f.pitched], ["Resonations", f.resonated],
    ["Offered", f.offered], ["Booked", f.booked], ["Sales calls done", f.showed], ["No-shows", f.no_shows],
    ["Sales", f.sales], ["Sales $", "$" + fmtNum(Math.round(f.sales_amount))], ["Talk time", fmtTalk(f.talk_seconds)],
    ["Effective conversations", f.conversations]];
  const rateRows = ["abr", "pickup", "dm_reach", "pr", "rr", "offer", "sur", "scr"].map((k) => {
    const x = d[k], g = grade(x.v, x.target, x.sample);
    return "<tr><td>" + esc(x.name) + '<span class="t">' + esc(x.how) + "</span></td><td>" +
      '<span class="rate ' + g + '">' + fmtPct(x.v) + "</span></td><td>" + (x.target != null ? fmtPct(x.target, 0) + "+" : "-") + "</td></tr>";
  }).join("");
  const scripts = (s.by_script || []).map((g) => {
    const r = g.rates;
    return "<tr><td>" + esc(g.script_version) + "</td><td>" + g.dials + "</td><td>" + g.pitched + "</td><td>" + g.resonated + "</td><td>" + g.booked +
      "</td><td>" + fmtPct(r.pr) + "</td><td>" + fmtPct(r.rr) + '</td><td><span class="rate ' + grade(r.abr, T().abr, g.dials) + '">' + fmtPct(r.abr) + "</span></td></tr>";
  }).join("");
  const t = T();
  return '<div class="dh"><div><h2>Funnel</h2><p class="sub">Sales calls done and sales are credited to the date of the dial that booked them, not the date of the meeting. ' +
    "Days run on " + esc(S.cfg.stats_timezone || "US Eastern") + " time.</p></div>" + closeX() + "</div>" +
    '<div class="seg" id="sh-range" style="width:fit-content">' + ["today", "week", "all"].map((r) =>
      '<button data-r="' + r + '" aria-selected="' + (r === s.range) + '">' + { today: "Today", week: "This week", all: "All time" }[r] + "</button>").join("") + "</div>" +
    '<div class="stat-grid"><div class="stat-scroll"><table class="stat-table"><thead><tr><th>Rate</th><th>Now</th><th>Target</th></tr></thead><tbody>' + rateRows +
    '</tbody></table><p class="muted" style="margin-top:10px;font-size:12px">ABR team benchmark ' + fmtPct(t.abr_benchmark, 1) + ". Rates stay grey until " + (t.min_sample || 10) +
    " in the denominator. Pickup rate under " + fmtPct(t.pickup, 0) + " across 50+ dials on one caller ID usually means that number is spam-labelled.</p></div>" +
    '<div class="stat-scroll"><table class="stat-table"><thead><tr><th>Count</th><th></th></tr></thead><tbody>' +
    countRows.map((r) => "<tr><td>" + r[0] + "</td><td>" + r[1] + "</td></tr>").join("") + "</tbody></table></div></div>" +
    '<div><span class="eyebrow">By script version</span><div class="stat-scroll"><table class="stat-table" style="margin-top:8px"><thead><tr><th>Version</th><th>Dials</th><th>Pitched</th><th>Reso</th><th>Booked</th><th>PR</th><th>RR</th><th>ABR</th></tr></thead><tbody>' +
    (scripts || '<tr><td colspan="8">No calls in this range yet.</td></tr>') + "</tbody></table></div></div>" +
    '<div class="acts" style="justify-content:flex-start"><span class="eyebrow" style="align-self:center">Imperium tracker CSV</span>' +
    ["today", "week", "all"].map((r) => '<a class="btn sm" href="/api/funnel.csv?range=' + r + '" download>' + icon("download", "sm") + { today: "Today", week: "This week", all: "All time" }[r] + "</a>").join("") + "</div>";
}

export function statsSheet(which) {
  const r = which || range;
  api(withAgent("/api/stats?range=" + r)).then((s) => {
    if (s.error) return;
    openModal(sheetHTML(s), { xwide: true });
    $("sh-range").addEventListener("click", (e) => { const b = e.target.closest("[data-r]"); if (b) statsSheet(b.getAttribute("data-r")); });
  });
}

export function wireFunnel() {
  $("sb-range").addEventListener("click", (e) => {
    const b = e.target.closest("[data-r]"); if (!b) return;
    range = b.getAttribute("data-r");
    $("sb-range").querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", x === b ? "true" : "false"));
    refreshStats();
  });
  $("sb-more").addEventListener("click", () => statsSheet());
  on("stats", (s) => {
    if (!s || !s.funnel) return;
    const wantSession = S.session.on && S.session.startedAt && !s.session;
    if (s.range !== range || wantSession) { refreshStats(); return; }   // a save answers with plain "today"
    renderBar(s);
  });
  on("tick", () => { if (last && last.session && S.session.on && S.session.activeSec % 15 === 0) renderBar(last); });
}
