/* Wrap-up, two levels.
   Level 1: what happened (keys 1-9, 0). A no-pickup outcome saves at once.
   Level 2: only when someone picked up. Objections heard, whether the
            meeting was offered, who the DM is, their pain in their words,
            and for a booking the time on the prospect's clock.
   Then save, with a few seconds of undo. */
import { $, icon, esc, fmtPhone, parseUTC, toServer, shifted, leadClock, leadDay, localHour, pad, store } from "./util.js";
import { S, emit, actions, outcome, campaignScript } from "./state.js";
import { api } from "./api.js";
import { toast, say } from "./ui.js";

let wrapMode = "";            // "" | "l2" | "dnc"
let chosen = null;            // outcome picked at level 1
let suggested = "";
let submitting = false;
let lastUndo = null;

export const getWrapMode = () => wrapMode;
export function setSuggested(key) { suggested = key || ""; }
export const draftKey = (phone) => "pd_draft:" + phone;

/* Per-call capture. The script rail and the lead pane write here too. */
export function freshCall(lead) {
  const name = ((lead.first || "") + " " + (lead.last || "")).trim();
  return { objections: [], other: "", offered: false, dm_name: lead.dm_name || name, email: lead.email || "",
           mobile: lead.mobile || "", pain: lead.pain || "", ppap_per_year: lead.ppap_per_year || "",
           oem: lead.oem || "", booked_for_local: "", callback_at: "" };
}

export function renderOutcomes() {
  $("outcomes").innerHTML = S.cfg.outcomes.map((o, i) => {
    const key = i < 9 ? String(i + 1) : i === 9 ? "0" : "";
    return '<button class="outcome ' + esc(o.tone || "plain") + (o.key === suggested ? " suggested" : "") +
           '" data-k="' + esc(o.key) + '">' + (key ? "<kbd>" + key + "</kbd>" : "") + esc(o.label) + "</button>";
  }).join("");
}

/* ---- time helpers, shared with the reschedule dialog -------------------- */

function inHardHours(date, off) {
  const w = S.cfg.windows.hard, h = localHour(date, off);
  return h >= w[0] && h < w[1];
}
function nextBusinessDay(off, hour, minute) {
  const l = shifted(new Date(), off);
  do { l.setUTCDate(l.getUTCDate() + 1); } while (l.getUTCDay() === 0 || l.getUTCDay() === 6);
  l.setUTCHours(hour, minute || 0, 0, 0);
  return new Date(l.getTime() - (off == null ? -5 : off) * 3600000);
}
function cbOptions(off) {
  const opts = [
    { label: "In 1 hour", when: new Date(Date.now() + 3600000) },
    { label: "In 3 hours", when: new Date(Date.now() + 3 * 3600000) },
    { label: "Next business day, 8:30am", when: nextBusinessDay(off, 8, 30) },
    { label: "Next business day, 4pm", when: nextBusinessDay(off, 16) }
  ];
  opts.forEach((o) => {
    o.after = !inHardHours(o.when, off);
    o.sub = leadDay(o.when, off) + " " + leadClock(o.when, off) + " their time" + (o.after ? " · after hours, rings next open" : "");
  });
  return opts;
}
export function cbGridHTML(off) {
  return cbOptions(off).map((o, i) =>
    '<button class="outcome cb-opt" aria-pressed="false" data-when="' + toServer(o.when) + '"><span class="l1"><kbd>' + (i + 1) + "</kbd>" +
    esc(o.label) + '</span><span class="l2' + (o.after ? " after" : "") + '">' + esc(o.sub) + "</span></button>").join("");
}
export function localInputMin() {
  return new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
/* A Date -> "YYYY-MM-DDTHH:MM" on the prospect's wall clock. */
function leadLocalInput(date, off) {
  const l = shifted(date, off);
  return l.getUTCFullYear() + "-" + pad(l.getUTCMonth() + 1) + "-" + pad(l.getUTCDate()) + "T" + pad(l.getUTCHours()) + ":" + pad(l.getUTCMinutes());
}
/* "YYYY-MM-DDTHH:MM" on the prospect's wall clock -> Date. */
function fromLeadLocal(value, off) {
  const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/.exec(value || "");
  if (!m) return null;
  return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) - (off == null ? -5 : off) * 3600000);
}
function bookOptions(off) {
  return [[10, 0, "10am"], [14, 0, "2pm"], [16, 0, "4pm"]].map((t) => {
    const when = nextBusinessDay(off, t[0], t[1]);
    return { label: leadDay(when, off) + " " + t[2], local: leadLocalInput(when, off), when };
  });
}
function myTimeHint(date) {
  return "= " + date.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) + " your time";
}

/* ---- level 1 -------------------------------------------------------------- */

export function pickOutcome(key) {
  if (S.state !== "WRAP" || submitting) return;
  const o = outcome(key);
  if (o.kind === "dnc") {
    wrapMode = "dnc"; chosen = o;
    $("l2").hidden = true; $("dnc-panel").hidden = false;
    $("dnc-yes").focus();
    return;
  }
  if (!o.connect) { chosen = o; save(); return; }      // nobody picked up: one key, done
  openLevel2(o);
}

function openLevel2(o) {
  wrapMode = "l2"; chosen = o;
  const c = S.call, off = S.cur.tz_offset;
  $("dnc-panel").hidden = true; $("l2").hidden = false;
  $("l2-err").textContent = "";
  $("l2-outcome").className = o.tone === "good" ? "good" : "";
  $("l2-outcome").textContent = o.label;

  const isCb = o.kind === "callback", isBook = !!o.booked;
  $("l2-when").hidden = !isCb;
  $("l2-book").hidden = !isBook;
  $("l2-email-req").hidden = !isBook;
  if (isCb) {
    $("cb-grid").innerHTML = cbGridHTML(off);
    $("cb-custom").min = localInputMin(); $("cb-custom").value = "";
    $("cb-hint").textContent = "Or pick a time. Your local time.";
    c.callback_at = "";
  }
  if (isBook) {
    const place = S.cur.city ? S.cur.city + (S.cur.state ? ", " + S.cur.state : "") : (S.cur.state || "their time zone");
    $("l2-book-place").textContent = place;
    $("bk-grid").innerHTML = bookOptions(off).map((b, i) =>
      '<button class="outcome cb-opt" aria-pressed="' + (c.booked_for_local === b.local) + '" data-local="' + b.local + '"><span class="l1"><kbd>' + (i + 1) +
      "</kbd>" + esc(b.label) + '</span><span class="l2">' + esc(myTimeHint(b.when)) + "</span></button>").join("");
    $("l2-booked").value = c.booked_for_local || "";
    bookedHint();
  }

  $("l2-obj").innerHTML = (S.cfg.objection_tags || []).map((t) =>
    '<button class="chip-t" type="button" aria-pressed="' + (c.objections.indexOf(t.key) >= 0) + '" data-obj="' + esc(t.key) + '">' + esc(t.label) + "</button>").join("");
  $("l2-other").hidden = c.objections.indexOf("OTHER") < 0;
  $("l2-other").value = c.other || "";
  $("l2-offered").checked = !!(o.offered || c.offered);
  $("l2-offered").disabled = !!o.offered;                  // the outcome already says so
  $("l2-dm").value = c.dm_name || ""; $("l2-email").value = c.email || "";
  $("l2-mobile").value = c.mobile || ""; $("l2-pain").value = c.pain || "";
  $("l2").scrollIntoView({ block: "nearest", behavior: "smooth" });
  // Keep focus off the inputs so 1-4 pick a time and enter saves. Only a missing email pulls focus.
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  if (isBook && c.booked_for_local && !c.email) $("l2-email").focus();
}

function bookedHint() {
  const d = fromLeadLocal($("l2-booked").value, S.cur && S.cur.tz_offset);
  $("l2-booked-hint").textContent = d ? myTimeHint(d) : "Type it as they said it: their clock, not yours.";
}

export function backToOutcomes() {
  wrapMode = ""; chosen = null;
  $("l2").hidden = true; $("dnc-panel").hidden = true;
}
export function resetWrap() { backToOutcomes(); }

/* ---- save ------------------------------------------------------------------ */

function readLevel2() {
  const c = S.call;
  c.dm_name = $("l2-dm").value.trim(); c.email = $("l2-email").value.trim();
  c.mobile = $("l2-mobile").value.trim(); c.pain = $("l2-pain").value.trim();
  c.other = $("l2-other").value.trim(); c.offered = $("l2-offered").checked;
  c.booked_for_local = $("l2-booked").value;
}

function save() {
  if (!S.cur || submitting || !chosen) return;
  const o = chosen, lead = S.cur, c = S.call, duration = S.callSec;
  const payload = { agent: S.agent, phone: lead.phone, company: lead.co || "", disposition: o.key,
                    notes: $("notes").value.trim(), duration,
                    script_version: S.session.script || campaignScript() || S.cfg.default_script_version || "",
                    number_used: (lead.caller_id || $("cid").textContent || "").replace(/[^+\d]/g, ""),
                    session_id: S.session.id || null,
                    call_sid: c.call_sid || "", recording_sid: c.recording_sid || "" };
  if (o.connect && o.kind !== "dnc") {
    readLevel2();
    if (o.kind === "callback") {
      if (!c.callback_at) { $("l2-err").textContent = "Pick when to call back."; return; }
      payload.callback_at = c.callback_at;
    }
    if (o.booked) {
      if (!c.booked_for_local) { $("l2-err").textContent = "Set the date and time you booked, on their clock."; $("l2-booked").focus(); return; }
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(c.email)) { $("l2-err").textContent = "Their email is needed for the invite."; $("l2-email").focus(); return; }
      payload.booked_for_local = c.booked_for_local;
    }
    payload.objections = c.objections.map((k) => (k === "OTHER" && c.other ? "OTHER:" + c.other : k));
    payload.offered = c.offered;
    ["dm_name", "email", "mobile", "pain", "ppap_per_year", "oem"].forEach((k) => { if (c[k]) payload[k] = c[k]; });
  }
  submitting = true;
  api("/api/disposition", payload).then((d) => {
    submitting = false;
    if (d.error) {
      if (wrapMode === "l2") $("l2-err").textContent = d.error;
      toast("error", "<b>Not saved.</b> " + esc(d.error));
      return;
    }
    store.del(draftKey(lead.phone));
    actions.renderStats(d.stats);
    const w = payload.callback_at ? parseUTC(payload.callback_at) : o.booked ? fromLeadLocal(c.booked_for_local, lead.tz_offset) : null;
    const when = w ? " · " + leadDay(w, lead.tz_offset) + " " + leadClock(w, lead.tz_offset) + " their time" : "";
    say("<b>" + esc(o.label) + "</b>: " + esc(lead.co || fmtPhone(lead.phone)) + esc(when));
    offerUndo(d.id, o, lead, duration, when);
    emit("saved", { lead, outcome: o, id: d.id, call: Object.assign({}, c), webhook: d.webhook });
    actions.afterSave(o);
  }).catch(() => {
    submitting = false;
    toast("error", "<b>Not saved: the server is unreachable.</b> Your notes are still here. Save again.");
  });
}

/* Used by voicemail drop: save a no-pickup outcome without the grid. */
export function submitOutcome(key) {
  chosen = outcome(key);
  save();
}

/* ---- undo ------------------------------------------------------------------ */

function offerUndo(id, o, lead, duration, when) {
  if (lastUndo && lastUndo.toast) lastUndo.toast.close();
  const entry = { id, duration };
  lastUndo = entry;
  entry.toast = toast(o.kind === "dnc" ? "warn" : "success",
    "<b>" + esc(o.label) + "</b> · " + esc(lead.co || fmtPhone(lead.phone)) + esc(when || ""),
    { ms: 6500, action: id ? { html: "Undo <kbd>z</kbd>", run: undo } : null,
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

/* ---- keyboard + clicks ------------------------------------------------------ */

function selectIn(gridId, btn) {
  $(gridId).querySelectorAll(".cb-opt").forEach((b) => b.setAttribute("aria-pressed", b === btn ? "true" : "false"));
}
function chooseCallback(btn) { S.call.callback_at = btn.getAttribute("data-when"); selectIn("cb-grid", btn); $("cb-custom").value = ""; $("l2-err").textContent = ""; }
function chooseBooked(btn) { $("l2-booked").value = btn.getAttribute("data-local"); S.call.booked_for_local = $("l2-booked").value; selectIn("bk-grid", btn); bookedHint(); $("l2-err").textContent = ""; }

/* Returns true when the key was consumed. Called for keys outside inputs. */
export function wrapKey(e, k) {
  if (S.state !== "WRAP") return false;
  if (wrapMode === "dnc") { if (k === "Enter") { e.preventDefault(); save(); } return true; }
  if (wrapMode === "l2") {
    if (k === "Enter") { e.preventDefault(); save(); return true; }
    if (/^[1-4]$/.test(k)) {
      const grid = chosen && chosen.kind === "callback" ? "cb-grid" : chosen && chosen.booked ? "bk-grid" : null;
      const btn = grid && $(grid).querySelectorAll(".cb-opt")[+k - 1];
      if (btn) { e.preventDefault(); (grid === "cb-grid" ? chooseCallback : chooseBooked)(btn); }
    }
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
  $("cb-grid").addEventListener("click", (e) => { const b = e.target.closest("[data-when]"); if (b) chooseCallback(b); });
  $("bk-grid").addEventListener("click", (e) => { const b = e.target.closest("[data-local]"); if (b) chooseBooked(b); });
  $("cb-custom").addEventListener("input", function () {
    const v = this.value && new Date(this.value);
    if (!v || !S.cur) { $("cb-hint").textContent = "Or pick a time. Your local time."; S.call.callback_at = ""; return; }
    selectIn("cb-grid", null);
    S.call.callback_at = v > new Date() ? toServer(v) : "";
    $("cb-hint").textContent = v > new Date()
      ? "= " + leadDay(v, S.cur.tz_offset) + " " + leadClock(v, S.cur.tz_offset) + " their time" + (inHardHours(v, S.cur.tz_offset) ? "" : " · after hours, rings next open")
      : "That time is in the past.";
  });
  $("l2-booked").addEventListener("input", () => { S.call.booked_for_local = $("l2-booked").value; selectIn("bk-grid", null); bookedHint(); emit("call"); });
  $("l2-obj").addEventListener("click", (e) => {
    const b = e.target.closest("[data-obj]"); if (!b) return;
    const key = b.getAttribute("data-obj"), list = S.call.objections, i = list.indexOf(key);
    if (i >= 0) list.splice(i, 1); else list.push(key);
    b.setAttribute("aria-pressed", i < 0 ? "true" : "false");
    $("l2-other").hidden = list.indexOf("OTHER") < 0;
    if (key === "OTHER" && i < 0) $("l2-other").focus();
  });
  ["l2-dm", "l2-email", "l2-mobile", "l2-pain", "l2-other"].forEach((id) => {
    $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } });
    $(id).addEventListener("input", () => { readLevel2(); emit("call"); });
  });
  $("l2-booked").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } });
  $("l2-save").addEventListener("click", save);
  $("l2-back").addEventListener("click", backToOutcomes);
  $("dnc-yes").addEventListener("click", save);
  $("dnc-no").addEventListener("click", backToOutcomes);
}
