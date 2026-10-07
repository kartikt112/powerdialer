/* The pipeline: stage and follow-up on the lead card, the Pipeline tab, the
   lead's timeline (stage changes, Claude's summaries, drafts and invite
   links), and Copy for Claude. Claude itself works through /api/agent via
   the dialer connector; this is the human side of the same records. */
import { $, esc, parseUTC, toServer, myClock, rel, fmtPhone, fmtClock } from "./util.js";
import { S, on, actions, outcome } from "./state.js";
import { api } from "./api.js";
import { toast, copyText } from "./ui.js";
import { registerTab } from "./rails.js";

const STAGES = [["interested", "Interested"], ["invite_sent", "Invite sent"], ["signed_up", "Signed up"], ["using", "Using it"],
  ["call_booked", "Call booked"], ["showed", "Showed"], ["no_show", "No-show"], ["sold", "Sold"], ["lost", "Lost"]];
const LABEL = Object.fromEntries(STAGES);
let filter = "";

/* datetime-local (your clock) <-> UTC server stamps */
const pad = (n) => String(n).padStart(2, "0");
function toLocalInput(utc) {
  const d = parseUTC(utc); if (!d) return "";
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
}
const fromLocalInput = (v) => (v ? toServer(new Date(v)) : null);

/* ---- lead card ------------------------------------------------------------- */

function renderLeadPipe() {
  const l = S.cur;
  $("pipe-row").hidden = !l;
  if (!l) return;
  $("pipe-stage").innerHTML = '<option value="">Not in the pipeline</option>' +
    STAGES.map(([k, v]) => '<option value="' + k + '"' + (l.stage === k ? " selected" : "") + ">" + v + "</option>").join("");
  $("pipe-fu").value = toLocalInput(l.follow_up_at);
  $("pipe-note").value = l.follow_up_note || "";
  $("pipe-row").classList.toggle("due", !!(l.follow_up_at && parseUTC(l.follow_up_at) <= new Date()));
}

function savePipe() {
  const l = S.cur; if (!l) return;
  const stage = $("pipe-stage").value, at = fromLocalInput($("pipe-fu").value), note = $("pipe-note").value.trim();
  const jobs = [];
  if (stage !== (l.stage || "")) jobs.push(api("/api/pipeline/stage", { phone: l.phone, stage, agent: S.agent }));
  if (at !== (l.follow_up_at || null) || note !== (l.follow_up_note || "")) jobs.push(api("/api/pipeline/follow-up", { phone: l.phone, at, note, agent: S.agent }));
  if (!jobs.length) { toast("info", "Nothing changed."); return; }
  Promise.all(jobs).then((rs) => {
    const bad = rs.find((r) => r.error);
    if (bad) { toast("error", esc(bad.error)); return; }
    l.stage = stage; l.follow_up_at = at || ""; l.follow_up_note = note;
    toast("success", "Saved: " + esc(LABEL[stage] || "not in the pipeline") + (at ? ", follow up " + esc(myClock(parseUTC(at))) + " " + esc(rel(parseUTC(at))) : "") + ".");
    renderLeadPipe(); loadEvents(); refreshPipeline();
  });
}

/* ---- timeline + Copy for Claude --------------------------------------------------- */

let detail = null;
function loadEvents() {
  const l = S.cur;
  detail = null;
  $("lead-events").innerHTML = "";
  if (!l) return;
  api("/api/lead/detail?phone=" + encodeURIComponent(l.phone)).then((d) => {
    if (!S.cur || S.cur.phone !== l.phone || d.error) return;
    detail = d.lead;
    const ev = detail.timeline || [];
    $("lead-events").innerHTML = ev.length ? '<h3 class="ev-h">Pipeline timeline</h3>' + ev.map((e) =>
      '<div class="pev"><span class="when">' + esc(myClock(parseUTC(e.at))) + " · " + esc(rel(parseUTC(e.at))) + (e.by ? " · " + esc(e.by) : "") + "</span>" +
      '<span class="what"><b>' + esc(kindLabel(e.kind)) + "</b> " + linkify(e.text) + "</span></div>").join("") : "";
  });
}
const kindLabel = (k) => ({ stage: "Stage", follow_up: "Follow-up", summary: "Claude", draft: "Email draft", invite: "Invite link",
  details: "Details", note: "Note" })[k] || k;
function linkify(text) {
  return esc(text).replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
}

function claudeText(d) {
  const lines = [];
  const who = [d.first, d.last].filter(Boolean).join(" ") || d.dm_name || "";
  lines.push("LEAD: " + (d.company || "") + (who ? " / " + who : "") + (d.title ? ", " + d.title : ""));
  lines.push("Phone " + fmtPhone(d.phone) + (d.email ? " · Email " + d.email : "") + (d.city || d.state ? " · " + [d.city, d.state].filter(Boolean).join(", ") : ""));
  if (d.website) lines.push("Website " + d.website);
  if (d.process || d.industry) lines.push("Makes: " + [d.process, d.industry].filter(Boolean).join(" · "));
  lines.push("Pipeline stage: " + (LABEL[d.stage] || "none") + (d.follow_up_at ? " · follow up " + d.follow_up_at + " UTC" + (d.follow_up_note ? " (" + d.follow_up_note + ")" : "") : ""));
  if (d.pain) lines.push("Pain they named: " + d.pain);
  (d.calls || []).forEach((c, i) => {
    lines.push("");
    lines.push("CALL " + (i + 1) + ": " + c.at + " UTC · " + outcome(c.disposition).label + (c.duration ? " · " + fmtClock(c.duration) : ""));
    if (c.notes) lines.push("My notes: " + c.notes);
    if (c.ai_summary) lines.push("Summary: " + c.ai_summary);
    if (c.transcript) lines.push("Transcript:\n" + c.transcript);
  });
  const tl = (d.timeline || []).slice(0, 12);
  if (tl.length) {
    lines.push("");
    lines.push("TIMELINE (newest first):");
    tl.forEach((e) => lines.push("- " + e.at + " " + kindLabel(e.kind) + ": " + e.text));
  }
  return lines.join("\n");
}

function copyForClaude() {
  if (!S.cur) { toast("info", "Open a lead first."); return; }
  const go = (d) => copyText(claudeText(d), "Copied for Claude: lead, calls, transcripts and timeline. Paste it into a chat.");
  if (detail && detail.phone === S.cur.phone) go(detail);
  else api("/api/lead/detail?phone=" + encodeURIComponent(S.cur.phone)).then((d) => {
    if (d.error) toast("error", esc(d.error)); else go(d.lead);
  });
}

/* ---- Pipeline tab ------------------------------------------------------------------- */

let board = null;
export function refreshPipeline() {
  return api("/api/pipeline" + (filter ? "?stage=" + filter : "")).then((d) => {
    if (d.error) return;
    board = d;
    $("n-pipe").textContent = d.due ? d.due : "";
    $("n-pipe").classList.toggle("due", d.due > 0);
    renderPipeline();
  }).catch(() => {});
}

function renderPipeline() {
  if (!board) return;
  const total = board.stages.reduce((a, s) => a + s.count, 0);
  $("pipe-stages").innerHTML = '<button data-st="" aria-pressed="' + (!filter) + '">All <b>' + total + "</b></button>" +
    '<button data-st="due" aria-pressed="' + (filter === "due") + '" class="' + (board.due ? "late" : "") + '">Due <b>' + board.due + "</b></button>" +
    board.stages.filter((s) => s.count || filter === s.key).map((s) =>
      '<button data-st="' + s.key + '" aria-pressed="' + (filter === s.key) + '">' + esc(s.label) + " <b>" + s.count + "</b></button>").join("");
  const now = new Date();
  $("l-pipeline").innerHTML = board.leads.map((l) => {
    const name = [l.first, l.last].filter(Boolean).join(" ") || l.dm_name || "";
    const fu = parseUTC(l.follow_up_at), late = fu && fu <= now;
    return '<button class="row" data-open="' + esc(l.phone) + '">' +
      '<span class="main"><span class="t1">' + esc(l.company || fmtPhone(l.phone)) + '</span><span class="t2">' +
      esc(LABEL[l.stage] || "") + (name ? " · " + esc(name) : "") + (l.email ? " · " + esc(l.email) : "") + "</span></span>" +
      '<span class="meta">' + (fu ? '<span class="num' + (late ? " late" : "") + '">' + (late ? "due " : "") + esc(rel(fu)) + "</span>" : "") +
      esc(l.state || "") + "</span>" +
      (l.follow_up_note ? '<span class="quote">' + esc(l.follow_up_note) + "</span>" : "") + "</button>";
  }).join("") || '<div class="list-note"><b>' + (filter ? "Nothing here" : "Pipeline is empty") + "</b>" +
    (filter ? "No leads in this stage." : "Leads land here when a call goes well (Resonated or Booked), when you set a stage on the lead, or when Claude processes a call.") + "</div>";
}

/* ---- wiring ------------------------------------------------------------------------- */

export function wirePipeline() {
  registerTab("pipeline", refreshPipeline);
  $("pipe-save").addEventListener("click", savePipe);
  $("pipe-note").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); savePipe(); } });
  $("b-copy-claude").addEventListener("click", copyForClaude);
  $("pipe-stages").addEventListener("click", (e) => {
    const b = e.target.closest("[data-st]"); if (!b) return;
    filter = b.dataset.st; refreshPipeline();
  });
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-retry-tx]"); if (!b) return;
    api("/api/transcript/retry", { id: Number(b.dataset.retryTx) }).then((d) => {
      toast(d.error ? "error" : "success", d.error ? esc(d.error) : "Transcribing again. It shows here in a minute.");
    });
  });
  on("lead", () => { renderLeadPipe(); loadEvents(); });
  on("saved", () => { refreshPipeline(); });
  on("campaign", refreshPipeline);
  on("minute", () => { if (S.cur) renderLeadPipe(); });
  actions.copyForClaude = copyForClaude;
  refreshPipeline();
}
