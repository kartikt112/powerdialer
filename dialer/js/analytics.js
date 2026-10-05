/* Analytics: dials and the funnel by day, week or month for the campaign
   being called (or all). One bar series (dials) with a hover readout, the
   daily target as a reference line on the day view, totals on top, and the
   full table underneath. */
import { $, esc, fmtNum, fmtPct, fmtTalk } from "./util.js";
import { S } from "./state.js";
import { api, withCampaign } from "./api.js";
import { openModal } from "./ui.js";
import { closeX } from "./ui.js";

let period = "day";
const COUNT = { day: 30, week: 12, month: 12 };
const NAME = { day: "Day", week: "Week", month: "Month" };
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function label(iso, p, long) {
  const d = new Date(iso + "T12:00:00Z");
  if (p === "month") return MON[d.getUTCMonth()] + (long ? " " + d.getUTCFullYear() : "");
  if (p === "week") return (long ? "Week of " : "") + d.getUTCDate() + " " + MON[d.getUTCMonth()];
  return (long ? DOW[d.getUTCDay()] + " " : "") + d.getUTCDate() + " " + MON[d.getUTCMonth()];
}

function chart(series, target) {
  const W = 760, H = 220, L = 34, R = 8, T = 12, B = 26;
  const max = Math.max(1, target || 0, ...series.map((s) => s.dials));
  const step = Math.pow(10, Math.floor(Math.log10(max))) * (max / Math.pow(10, Math.floor(Math.log10(max))) > 5 ? 2 : 1);
  const top = Math.ceil(max / step) * step;
  const y = (v) => T + (H - T - B) * (1 - v / top);
  const slot = (W - L - R) / series.length, bw = Math.max(3, Math.min(28, slot - 2));
  let g = "";
  for (let v = 0; v <= top; v += step) {
    g += '<line class="ax-grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + y(v) + '" y2="' + y(v) + '"/>' +
      '<text class="ax-t" x="' + (L - 6) + '" y="' + (y(v) + 4) + '" text-anchor="end">' + fmtNum(v) + "</text>";
  }
  const every = Math.ceil(series.length / 8);
  const bars = series.map((s, i) => {
    const x = L + i * slot + (slot - bw) / 2, h = Math.max(0, y(0) - y(s.dials));
    const r = Math.min(4, bw / 2, h);
    const path = h ? "M" + x + "," + y(0) + "V" + (y(s.dials) + r) + "Q" + x + "," + y(s.dials) + " " + (x + r) + "," + y(s.dials) +
      "H" + (x + bw - r) + "Q" + (x + bw) + "," + y(s.dials) + " " + (x + bw) + "," + (y(s.dials) + r) + "V" + y(0) + "Z" : "";
    return '<g class="bar" data-i="' + i + '"><rect class="hit" x="' + (L + i * slot) + '" y="' + T + '" width="' + slot + '" height="' + (H - T - B) + '"/>' +
      (path ? '<path class="mark" d="' + path + '"/>' : "") + "</g>" +
      (i % every === 0 || (i === series.length - 1 && i % every >= every / 2) ? '<text class="ax-t" x="' + (x + bw / 2) + '" y="' + (H - 8) + '" text-anchor="middle">' + esc(label(s.start, period)) + "</text>" : "");
  }).join("");
  const ref = target && period === "day"
    ? '<line class="ref" x1="' + L + '" x2="' + (W - R) + '" y1="' + y(target) + '" y2="' + y(target) + '"/>' +
      '<text class="ax-t ref-t" x="' + (W - R) + '" y="' + (y(target) - 5) + '" text-anchor="end">target ' + fmtNum(target) + "</text>"
    : "";
  return '<div class="an-chart"><svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Dials per ' + period + '">' + g + ref + bars +
    '<line class="ax-base" x1="' + L + '" x2="' + (W - R) + '" y1="' + y(0) + '" y2="' + y(0) + '"/></svg><div class="an-tip" id="an-tip" hidden></div></div>';
}

function tipHTML(s) {
  const r = s.rates || {};
  return "<b>" + esc(label(s.start, period, true)) + "</b>" +
    "<span>" + fmtNum(s.dials) + " dials</span><span>" + fmtNum(s.pickups) + " picked up" + (s.dials ? " (" + fmtPct(r.pickup, 0) + ")" : "") + "</span>" +
    "<span>" + fmtNum(s.pitched) + " pitched</span><span>" + fmtNum(s.booked) + " booked" + (s.dials ? " (" + fmtPct(r.abr, 1) + ")" : "") + "</span>";
}

function tile(label, value, sub) {
  return '<div class="an-tile"><span class="lbl">' + esc(label) + "</span><b>" + value + "</b>" + (sub ? '<span class="muted">' + sub + "</span>" : "") + "</div>";
}

function render(d) {
  const series = d.series || [], t = d.totals || {}, r = t.rates || {};
  const c = (S.campaigns || []).find((x) => x.id === S.campaign);
  const target = (c && c.daily_target) || S.cfg.daily_target || 0;
  const days = series.filter((s) => s.dials).length;
  const rows = series.slice().reverse().map((s) => {
    const q = s.rates || {};
    return "<tr><td>" + esc(label(s.start, period, true)) + "</td><td>" + fmtNum(s.dials) + "</td><td>" + fmtNum(s.pickups) + "</td><td>" + fmtNum(s.pitched) +
      "</td><td>" + fmtNum(s.resonated) + "</td><td>" + fmtNum(s.booked) + "</td><td>" + fmtNum(s.showed) + "</td><td>" + fmtNum(s.sales) +
      "</td><td>" + (s.sales_amount ? "$" + fmtNum(Math.round(s.sales_amount)) : "") + "</td><td>" + (s.dials ? fmtPct(q.pickup, 0) : "") +
      "</td><td>" + (s.dials ? fmtPct(q.abr, 1) : "") + "</td>" +
      (period === "day" && target ? "<td>" + (s.dials ? (s.dials >= target ? '<span class="good">hit</span>' : fmtNum(target - s.dials) + " short") : "") + "</td>" : "") + "</tr>";
  }).join("");
  openModal('<div class="dh"><div><h2>Analytics</h2><p class="sub">' + esc(c ? c.name : "All campaigns") +
    ". Days are the prospects' day (" + esc(d.tz || "") + "). Bookings, shows and sales count on the day of the dial that booked them.</p></div>" + closeX() + "</div>" +
    '<div class="seg" id="an-period">' + ["day", "week", "month"].map((p) => '<button data-p="' + p + '" aria-selected="' + (p === period) + '">' + NAME[p] + "</button>").join("") + "</div>" +
    '<div class="an-tiles">' +
    tile("Dials", fmtNum(t.dials || 0), period === "day" && days ? fmtNum(Math.round((t.dials || 0) / days)) + " a calling day" : "") +
    tile("Picked up", fmtNum(t.pickups || 0), fmtPct(r.pickup, 0)) +
    tile("Pitched", fmtNum(t.pitched || 0), fmtPct(r.pr, 0) + " of dials") +
    tile("Booked", fmtNum(t.booked || 0), fmtPct(r.abr, 1) + " of dials") +
    tile("Showed", fmtNum(t.showed || 0), r.sur == null ? "" : fmtPct(r.sur, 0) + " show-up") +
    tile("Sales", fmtNum(t.sales || 0), t.sales_amount ? "$" + fmtNum(Math.round(t.sales_amount)) : "") +
    tile("Talk time", fmtTalk(t.talk_seconds || 0), "") +
    "</div>" +
    '<h3 class="an-h">Dials per ' + period + "</h3>" + chart(series, target) +
    '<div class="stat-scroll"><table class="stat-table an-table"><thead><tr><th>' + NAME[period] + "</th><th>Dials</th><th>Picked up</th><th>Pitched</th><th>Resonated</th><th>Booked</th><th>Showed</th><th>Sales</th><th>Sales $</th><th>Pickup</th><th>Booking</th>" +
    (period === "day" && target ? "<th>Target</th>" : "") + "</tr></thead><tbody>" + rows + "</tbody></table></div>" +
    '<div class="acts" style="justify-content:flex-start"><a class="btn sm" style="text-decoration:none" href="' + withCampaign("/api/funnel.csv?range=all") + '" download>Daily tracker CSV</a></div>', { xwide: true });

  $("an-period").addEventListener("click", (e) => { const b = e.target.closest("[data-p]"); if (b) { period = b.dataset.p; openAnalytics(); } });
  const tip = $("an-tip"), box = tip.parentNode;
  box.querySelector("svg").addEventListener("mousemove", (e) => {
    const g = e.target.closest(".bar"); if (!g) { tip.hidden = true; return; }
    box.querySelectorAll(".bar.on").forEach((x) => x.classList.remove("on"));
    g.classList.add("on");
    tip.innerHTML = tipHTML(series[+g.dataset.i]); tip.hidden = false;
    const rect = box.getBoundingClientRect();
    const x = Math.min(rect.width - 170, Math.max(0, e.clientX - rect.left + 12));
    tip.style.left = x + "px"; tip.style.top = Math.max(0, e.clientY - rect.top - 70) + "px";
  });
  box.querySelector("svg").addEventListener("mouseleave", () => { tip.hidden = true; box.querySelectorAll(".bar.on").forEach((x) => x.classList.remove("on")); });
}

export function openAnalytics() {
  api("/api/analytics?period=" + period + "&count=" + COUNT[period]).then((d) => { if (!d.error) render(d); });
}
