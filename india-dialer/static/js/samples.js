/* The Samples rail: every sample and demo card, grouped by stage, with the
   one button that moves it on. Everything on a card is credited to the date
   of the dial that created it, so the rail says which call that was. */
import { $, esc, parseUTC, istWhen, istDay, rel, fmtINR, fmtNum, fromCall } from "./util.js";
import { S, on, emit, actions } from "./state.js";
import { api } from "./api.js";
import { toast, openModal, closeModal, closeX } from "./ui.js";
import { registerTab } from "./rails.js";

const OPEN = ["demo", "asked", "received", "delivered", "feedback", "quote"];
const stageLabel = (k) => ((S.cfg.pipeline || {}).stages || []).reduce((a, s) => (s.key === k ? s.label : a), k);

/* What each stage offers next: [label, stage, needs a form]. */
function nextSteps(c) {
  switch (c.stage) {
    case "demo": return [["Demo done, quote sent", "quote"], ["They will send drawings", "asked"]];
    case "asked": return [["Drawings in", "received"]];
    case "received": return [["Delivered", "delivered", true]];
    case "delivered": return [["Feedback", "feedback", true]];
    case "feedback": return [["Quote sent", "quote"]];
    case "quote": return [["Won", "won", true]];
    default: return [];
  }
}

function since(c) {
  const at = { demo: c.demo_at, asked: c.asked_at, received: c.received_at, delivered: c.delivered_at,
               feedback: c.feedback_at, quote: c.quote_sent_at, won: c.won_at, lost: c.lost_at }[c.stage];
  const d = parseUTC(at);
  if (!d) return "";
  return c.stage === "demo" ? "demo " + istWhen(d) : rel(d);
}

function card(c) {
  const bits = [];
  if (c.dm_name) bits.push(esc(c.dm_name));
  if (c.city) bits.push(esc(c.city));
  if (c.turnaround_min != null && c.stage !== "asked" && c.stage !== "received") bits.push("turnaround " + fmtNum(c.turnaround_min) + " min");
  if (c.feedback) bits.push("feedback: " + esc(c.feedback));
  if (c.stage === "won") bits.push(fmtINR(c.deal_value_inr) + (c.drawings_committed ? ", " + fmtNum(c.drawings_committed) + " drawings a month" : ""));
  if (c.stage === "lost" && c.lost_reason) bits.push(esc(c.lost_reason));
  const origin = parseUTC(c.origin_dialed_at);
  const acts = OPEN.indexOf(c.stage) >= 0
    ? '<span class="acts">' + nextSteps(c).map((s) => '<button class="btn sm" data-stage="' + s[1] + '" data-card="' + c.id + '">' + esc(s[0]) + "</button>").join("") +
      '<button class="btn quiet sm" data-stage="lost" data-card="' + c.id + '">Lost</button>' +
      (c.stage === "asked" || c.stage === "delivered" ? '<button class="btn quiet sm" data-fu-card="' + c.id + '" data-lead-id="' + c.lead_id + '" data-t="' +
        (c.stage === "asked" ? "sample_request" : "sample_delivered") + '">WhatsApp</button>' : "") + "</span>"
    : "";
  return '<div class="row" data-lead="' + c.lead_id + '"><span class="t1">' + esc(c.company) + '</span><span class="r">' + esc(since(c)) + "</span>" +
    '<span class="t2 full">' + (c.kind === "demo" ? "Demo · " : "") + bits.join(" · ") + (origin ? (bits.length ? " · " : "") + esc(fromCall(origin)) : "") + "</span>" + acts + "</div>";
}

let board = [];
function render() {
  const stages = ((S.cfg.pipeline || {}).stages || []).map((s) => s.key);
  const open = board.filter((c) => OPEN.indexOf(c.stage) >= 0);
  $("n-samples").textContent = open.length || "";
  if (!board.length) {
    $("l-samples").innerHTML = '<p class="note"><b>No samples yet.</b>When someone agrees to send 5 drawings, pick <b>Agreed to send 5 drawings</b> in the wrap-up and the card starts here.</p>';
    return;
  }
  let h = "";
  stages.forEach((st) => {
    const list = board.filter((c) => c.stage === st);
    if (!list.length) return;
    const shown = st === "won" || st === "lost" ? list.slice(0, 8) : list;
    h += '<p class="stage-h">' + esc(stageLabel(st)) + "<span>" + list.length + "</span></p>" + shown.map(card).join("");
  });
  $("l-samples").innerHTML = h;
}

export function refreshSamples() {
  if (!$("l-samples").children.length) $("l-samples").innerHTML = '<div class="skel"></div>'.repeat(4);
  return api("/api/samples").then((d) => { board = d.samples || []; S.data.samples = board; render(); emit("samples", board); });
}
registerTab("samples", refreshSamples);

/* ---- moving a card ---------------------------------------------------------- */

function move(id, stage, fields) {
  return api("/api/sample", Object.assign({ id, stage }, fields || {})).then((d) => {
    if (d.error) return d.error;
    board = d.samples || board; render();
    const c = board.find((x) => x.id === id);
    toast("ok", "<b>" + esc(stageLabel(stage)) + "</b> · " + esc(c ? c.company : ""));
    emit("saved", {});
    return null;
  });
}

function form(c, stage) {
  let body = "";
  if (stage === "delivered") {
    const from = parseUTC(c.received_at), mins = from ? Math.max(0, Math.round((Date.now() - from) / 60000)) : "";
    body = '<p class="sub">Speed is the pitch, so log how long it took from drawings in to ballooned output back.</p>' +
      '<div class="grid2"><div><label class="lbl" for="f-min">Turnaround, minutes</label><input class="field" id="f-min" type="number" min="0" value="' + mins + '"></div></div>';
  } else if (stage === "feedback") {
    body = '<div class="chips" id="f-fb"><button class="chip" data-fb="good" aria-pressed="false">Good</button><button class="chip" data-fb="issues" aria-pressed="false">Issues</button></div>' +
      '<div><label class="lbl" for="f-note">What they said</label><input class="field" id="f-note" maxlength="500"></div>';
  } else if (stage === "won") {
    body = '<div class="grid2"><div><label class="lbl" for="f-inr">Deal value, rupees</label><input class="field" id="f-inr" inputmode="numeric" placeholder="50000"></div>' +
      '<div><label class="lbl" for="f-dr">Drawings a month committed</label><input class="field" id="f-dr" inputmode="numeric"></div></div>' +
      '<p class="sub">The win counts on the day of the dial that started it: ' + esc(istDay(parseUTC(c.origin_dialed_at) || new Date()).toLowerCase()) + ".</p>";
  } else if (stage === "lost") {
    body = '<div><label class="lbl" for="f-why">Why</label><input class="field" id="f-why" maxlength="300" placeholder="Went quiet, chose another vendor, price"></div>';
  }
  openModal('<div class="dh"><h2>' + esc(stageLabel(stage)) + ": " + esc(c.company) + "</h2>" + closeX() + "</div>" + body +
    '<p class="err" id="f-err"></p><div class="acts"><button class="btn primary" id="f-go">Save</button><button class="btn quiet" data-close>Cancel</button></div>');
  let fb = "";
  if ($("f-fb")) $("f-fb").addEventListener("click", (e) => {
    const b = e.target.closest("[data-fb]"); if (!b) return;
    fb = b.getAttribute("data-fb");
    $("f-fb").querySelectorAll(".chip").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  });
  const go = () => {
    const f = {};
    if (stage === "delivered") f.turnaround_min = $("f-min").value;
    if (stage === "feedback") { f.feedback = fb; f.feedback_note = $("f-note").value.trim(); }
    if (stage === "won") { f.deal_value_inr = $("f-inr").value.replace(/[^\d]/g, ""); f.drawings_committed = $("f-dr").value.replace(/[^\d]/g, ""); }
    if (stage === "lost") f.lost_reason = $("f-why").value.trim();
    move(c.id, stage, f).then((err) => { if (err) $("f-err").textContent = err; else closeModal(); });
  };
  $("f-go").addEventListener("click", go);
  $("modal-box").onkeydown = (e) => { if (e.key === "Enter" && !e.target.matches("button")) { e.preventDefault(); go(); } };
}

export function wireSamples() {
  $("l-samples").addEventListener("click", (e) => {
    const b = e.target.closest("[data-stage]"); if (!b) return;
    const c = board.find((x) => x.id === +b.getAttribute("data-card")); if (!c) return;
    const stage = b.getAttribute("data-stage");
    if (["delivered", "feedback", "won", "lost"].indexOf(stage) >= 0) form(c, stage); else move(c.id, stage);
  });
  on("saved", () => refreshSamples());
  refreshSamples();
}
