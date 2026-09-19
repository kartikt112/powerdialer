/* Wrap-up: outcome grid, callback picker, do-not-call confirm, save, undo. */
import { $, icon, esc, fmtPhone, parseUTC, toServer, shifted, leadClock, leadDay, localHour, store } from "./util.js";
import { S, emit, actions, outcome } from "./state.js";
import { api } from "./api.js";
import { toast, say } from "./ui.js";

let wrapMode = "";            // "" | "cb" | "dnc"
let suggested = "";
let submitting = false;
let lastUndo = null;

export const getWrapMode = () => wrapMode;
export function setSuggested(key) { suggested = key || ""; }
export const draftKey = (phone) => "pd_draft:" + phone;

export function renderOutcomes() {
  $("outcomes").innerHTML = S.cfg.outcomes.map((o, i) => {
    const key = i < 9 ? String(i + 1) : i === 9 ? "0" : "";
    return '<button class="outcome ' + esc(o.tone || "plain") + (o.key === suggested ? " suggested" : "") +
           '" data-k="' + esc(o.key) + '">' + (key ? "<kbd>" + key + "</kbd>" : "") + esc(o.label) + "</button>";
  }).join("");
}

/* ---- callback options, shared with the reschedule dialog --------------- */

function inHardHours(date, off) {
  const w = S.cfg.windows.hard, h = localHour(date, off);
  return h >= w[0] && h < w[1];
}
function nextBusinessDay(off, hour) {
  const l = shifted(new Date(), off);
  do { l.setUTCDate(l.getUTCDate() + 1); } while (l.getUTCDay() === 0 || l.getUTCDay() === 6);
  l.setUTCHours(hour, 0, 0, 0);
  return new Date(l.getTime() - (off == null ? -5 : off) * 3600000);
}
function cbOptions(off) {
  const opts = [
    { label: "In 1 hour", when: new Date(Date.now() + 3600000) },
    { label: "In 3 hours", when: new Date(Date.now() + 3 * 3600000) },
    { label: "Next business day, 10am", when: nextBusinessDay(off, 10) },
    { label: "Next business day, 2pm", when: nextBusinessDay(off, 14) }
  ];
  opts.forEach((o) => {
    o.after = !inHardHours(o.when, off);
    o.sub = leadDay(o.when, off) + " " + leadClock(o.when, off) + " their time" + (o.after ? " · after hours, rings next open" : "");
  });
  return opts;
}
export function cbGridHTML(off) {
  return cbOptions(off).map((o, i) =>
    '<button class="outcome cb-opt" data-when="' + toServer(o.when) + '"><span class="l1"><kbd>' + (i + 1) + "</kbd>" +
    esc(o.label) + '</span><span class="l2' + (o.after ? " after" : "") + '">' + esc(o.sub) + "</span></button>").join("");
}
export function localInputMin() {
  return new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

/* ---- picking ------------------------------------------------------------ */

export function pickOutcome(key) {
  if (S.state !== "WRAP" || submitting) return;
  const o = outcome(key);
  if (o.kind === "callback") {
    wrapMode = "cb";
    $("dnc-panel").hidden = true; $("cb-panel").hidden = false;
    $("cb-grid").innerHTML = cbGridHTML(S.cur.tz_offset);
    $("cb-custom").min = localInputMin(); $("cb-custom").value = "";
    $("cb-hint").textContent = "Your local time.";
    $("cb-panel").scrollIntoView({ block: "nearest", behavior: "smooth" });
    return;
  }
  if (o.kind === "dnc") {
    wrapMode = "dnc";
    $("cb-panel").hidden = true; $("dnc-panel").hidden = false;
    $("dnc-yes").focus();
    return;
  }
  submitOutcome(key, null);
}
export function backToOutcomes() { wrapMode = ""; $("cb-panel").hidden = true; $("dnc-panel").hidden = true; }
export function resetWrap() { backToOutcomes(); }

export function submitOutcome(key, callbackAt) {
  if (!S.cur || submitting) return;
  submitting = true;
  const lead = S.cur, o = outcome(key), duration = S.callSec;
  const payload = { agent: S.agent, phone: lead.phone, company: lead.co || "", disposition: key,
                    notes: $("notes").value.trim(), duration };
  if (callbackAt) payload.callback_at = callbackAt;

  api("/api/disposition", payload).then((d) => {
    submitting = false;
    if (d.error) { toast("error", "<b>Not saved.</b> " + esc(d.error)); return; }
    store.del(draftKey(lead.phone));
    actions.renderStats(d.stats);
    const w = callbackAt && parseUTC(callbackAt);
    const when = w ? " · " + leadDay(w, lead.tz_offset) + " " + leadClock(w, lead.tz_offset) + " their time" : "";
    say("<b>" + esc(o.label) + "</b>: " + esc(lead.co || fmtPhone(lead.phone)) + esc(when));
    offerUndo(d.id, o, lead, duration, when);
    emit("saved", { lead, outcome: o, id: d.id });
    actions.afterSave();
  }).catch(() => {
    submitting = false;
    toast("error", "<b>Not saved: the server is unreachable.</b> Your notes are still here. Pick the outcome again.");
  });
}

/* ---- undo --------------------------------------------------------------- */

function offerUndo(id, o, lead, duration, when) {
  if (lastUndo && lastUndo.toast) lastUndo.toast.close();
  const entry = { id, duration };
  lastUndo = entry;
  entry.toast = toast(o.kind === "dnc" ? "warn" : "success",
    "<b>" + esc(o.label) + "</b> · " + esc(lead.co || fmtPhone(lead.phone)) + esc(when || ""),
    { ms: 6500, action: id ? { html: icon("undo", "sm") + "Undo <kbd>z</kbd>", run: undo } : null,
      onClose() { if (lastUndo === entry) lastUndo = null; } });
}

export function undo() {
  const u = lastUndo;
  if (!u || !u.id) return;
  if (S.state === "DIALING" || S.state === "LIVE" || S.state === "WRAP") {
    toast("warn", "Finish this call first, then fix that one from its history."); return;
  }
  lastUndo = null;
  if (u.toast) u.toast.close();
  actions.cancelCountdown();
  const release = S.cur && S.state === "READY" ? api("/api/release", { phone: S.cur.phone }) : Promise.resolve();
  release.then(() => api("/api/undo", { id: u.id, agent: S.agent })).then((d) => {
    if (d.error) { toast("error", esc(d.error)); if (!S.cur) actions.nextLead(); return; }
    actions.renderStats(d.stats);
    actions.loadLead(d.lead, { handPicked: true, notes: d.notes || "", state: "WRAP" });
    S.callSec = u.duration || 0; S.wrapSec = 0; suggested = "";
    $("wrap-title").textContent = "Undone. Pick the right outcome";
    renderOutcomes();
    say("Undid the last outcome on <b>" + esc(d.lead.co || fmtPhone(d.lead.phone)) + "</b>");
    emit("saved", {});
    actions.refreshQueue();
  });
}

/* ---- keyboard + clicks -------------------------------------------------- */

/* Returns true when the key was consumed. */
export function wrapKey(e, k) {
  if (S.state !== "WRAP") return false;
  if (wrapMode === "dnc") { if (k === "Enter") { e.preventDefault(); submitOutcome("DNC", null); } return true; }
  if (wrapMode === "cb") {
    const opts = $("cb-grid").querySelectorAll("[data-when]");
    if (/^[1-4]$/.test(k) && opts[+k - 1]) { e.preventDefault(); submitOutcome("CALLBACK", opts[+k - 1].getAttribute("data-when")); }
    return true;
  }
  if (/^[0-9]$/.test(k)) {
    const idx = k === "0" ? 9 : +k - 1;
    if (S.cfg.outcomes[idx]) { e.preventDefault(); pickOutcome(S.cfg.outcomes[idx].key); }
    return true;
  }
  if (k === "Enter" && suggested && !e.target.closest("button, a")) { e.preventDefault(); pickOutcome(suggested); return true; }
  return false;
}

export function wireWrap() {
  $("outcomes").addEventListener("click", (e) => { const b = e.target.closest("[data-k]"); if (b) pickOutcome(b.getAttribute("data-k")); });
  $("cb-grid").addEventListener("click", (e) => { const b = e.target.closest("[data-when]"); if (b) submitOutcome("CALLBACK", b.getAttribute("data-when")); });
  $("cb-back").addEventListener("click", backToOutcomes);
  $("cb-custom").addEventListener("input", function () {
    const v = this.value && new Date(this.value);
    if (!v || !S.cur) { $("cb-hint").textContent = "Your local time."; return; }
    $("cb-hint").textContent = "= " + leadDay(v, S.cur.tz_offset) + " " + leadClock(v, S.cur.tz_offset) + " their time" +
      (inHardHours(v, S.cur.tz_offset) ? "" : " · after hours, rings next open");
  });
  $("cb-custom-go").addEventListener("click", () => {
    const v = $("cb-custom").value;
    if (!v || new Date(v) <= new Date()) { toast("warn", "Pick a callback time in the future."); return; }
    submitOutcome("CALLBACK", toServer(new Date(v)));
  });
  $("dnc-yes").addEventListener("click", () => submitOutcome("DNC", null));
  $("dnc-no").addEventListener("click", backToOutcomes);
}
