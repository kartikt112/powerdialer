/* The funnel bar under the top bar, and the full stats sheet.
   Counts and rates come from the server (dialer/funnel.py); this file only
   lays them out and colours each rate against its target. */
import { $, icon, esc, fmtPct, fmtNum, fmtTalk } from "./util.js";
import { S, on, actions } from "./state.js";
import { api, withAgent, withCampaign } from "./api.js";
import { openModal, closeX } from "./ui.js";

let range = "today";
const RANGE_LABEL = { today: "Today", week: "This week", month: "This month", all: "All time" };
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

/* A rate is plain grey text. It only takes a colour when it is off target,
   or green once it clears one: the bar should be quiet on a good day. */
function rate(d, withName) {
  if (d.v == null) return "";
  const g = grade(d.v, d.target, d.sample);
  const tip = d.name + ": " + d.how + (d.target != null ? ". Target " + fmtPct(d.target, 0) + " or more" : "");
  return '<span class="r ' + (g === "none" ? "" : g) + '" title="' + esc(tip) + '">' + (withName && d.short ? d.short + " " : "") + fmtPct(d.v) + "</span>";
}
const cell = (n, label, extra, title) => '<span class="fc"' + (title ? ' title="' + esc(title) + '"' : "") + "><b>" + n + "</b>" + label + (extra || "") + "</span>";

function renderBar(s) {
  if (!s || !s.funnel) return;
  last = s;
  const f = s.funnel, d = rateDefs(f), t = T(), today = s.today || f;
  let html =
    cell(fmtNum(f.dials), "dials") +
    cell(fmtNum(f.pickups), "pickups", rate(d.pickup)) +
    cell(fmtNum(f.pitched), "pitched", rate(d.pr)) +
    cell(fmtNum(f.resonated), "resonated", rate(d.rr)) +
    cell(fmtNum(f.offered), "offered", rate(d.offer)) +
    cell(fmtNum(f.booked), "booked", rate(d.abr, true), "ABR is the star metric. Target " + fmtPct(t.abr, 0) + " or more, team benchmark " + fmtPct(t.abr_benchmark, 1));
  if (f.showed || f.sales) html += cell(fmtNum(f.showed), "calls done", rate(d.sur)) + cell(fmtNum(f.sales), "sales", rate(d.scr) + (f.sales_amount ? '<span class="r">$' + fmtNum(Math.round(f.sales_amount)) + "</span>" : ""));
  html += '<span class="fc sep"></span>' +
    cell(today.conversations + "<span class=\"r\" style=\"font-weight:400\">/" + (t.conversations_per_day || 10) + "</span>", "conversations today", "",
         "Unique leads today where someone picked up and you offered the meeting or they resonated.");
  if (s.session && S.session.on) {
    const hrs = Math.max(S.session.activeSec, 60) / 3600;
    html += cell(Math.round(s.session.dials / hrs), "dials an hour", '<span class="r">' + (s.session.pitched / hrs).toFixed(1) + " pitches · " + (s.session.booked / hrs).toFixed(1) + " bookings</span>");
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
    '<div class="seg" id="sh-range">' + ["today", "week", "month", "all"].map((r) =>
      '<button data-r="' + r + '" aria-selected="' + (r === s.range) + '">' + RANGE_LABEL[r] + "</button>").join("") + "</div>" +
    '<div class="stat-grid"><div class="stat-scroll"><table class="stat-table"><thead><tr><th>Rate</th><th>Now</th><th>Target</th></tr></thead><tbody>' + rateRows +
    '</tbody></table><p class="muted" style="margin-top:10px;font-size:12px">ABR team benchmark ' + fmtPct(t.abr_benchmark, 1) + ". Rates stay grey until " + (t.min_sample || 10) +
    " in the denominator. Pickup rate under " + fmtPct(t.pickup, 0) + " across 50+ dials on one caller ID usually means that number is spam-labelled.</p></div>" +
    '<div class="stat-scroll"><table class="stat-table"><thead><tr><th>Count</th><th></th></tr></thead><tbody>' +
    countRows.map((r) => "<tr><td>" + r[0] + "</td><td>" + r[1] + "</td></tr>").join("") + "</tbody></table></div></div>" +
    '<div><h3 style="font-size:13px">By script version</h3><div class="stat-scroll"><table class="stat-table" style="margin-top:6px"><thead><tr><th>Version</th><th>Dials</th><th>Pitched</th><th>Reso</th><th>Booked</th><th>PR</th><th>RR</th><th>ABR</th></tr></thead><tbody>' +
    (scripts || '<tr><td colspan="8">No calls in this range yet.</td></tr>') + "</tbody></table></div></div>" +
    ((s.objections || []).length ? '<p class="hint">Top objections today: ' + s.objections.map((o) => esc(o.label) + " (" + o.count + ")").join(", ") + ".</p>" : "") +
    '<div class="acts" style="justify-content:flex-start;align-items:baseline"><span class="hint">Imperium tracker CSV</span>' +
    ["today", "week", "month", "all"].map((r) => '<a class="btn sm" style="text-decoration:none" href="' + withCampaign("/api/funnel.csv?range=" + r) + '" download>' + RANGE_LABEL[r] + "</a>").join("") + "</div>";
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
