/* Wrap-up. Level 1: what happened (1-9, 0); nobody-picked-up outcomes save at
   once. Level 2 opens only when someone picked up: objections, what you
   learned, how to reach them, and the time for a demo or callback. */
import { $, esc, istInput, fromIstInput, istWhen, toServer, prettyPhone } from "./util.js";
import { S, emit, actions, outcome } from "./state.js";
import { api } from "./api.js";
import { toast } from "./ui.js";
import { activePhone } from "./lead.js";

let chosen = null, suggested = "", saving = false, lastUndo = null;

export const wrapOpen = () => S.state === "wrap";
export const level2Open = () => !!chosen && !$("l2").hidden;
export function suggest(key) { suggested = key || ""; renderOutcomes(); }

export function openWrap(title, suggestion) {
  chosen = null;
  suggested = suggestion || "";
  $("wrap").hidden = false;
  $("l2").hidden = true;
  $("wrap-title").textContent = title || "How did it go?";
  $("wrap-sub").textContent = S.connectedAt ? "Talked " + Math.round((S.endedAt - S.connectedAt) / 1000) + " s" : "No pickup logged";
  renderOutcomes();
  requestAnimationFrame(() => $("wrap").scrollIntoView({ block: "nearest" }));
}
export function closeWrap() { chosen = null; $("wrap").hidden = true; $("l2").hidden = true; }

function renderOutcomes() {
  $("outcomes").innerHTML = (S.cfg.outcomes || []).map((o, i) => {
    const k = i < 9 ? String(i + 1) : i === 9 ? "0" : "";
    return '<button class="outcome ' + (o.tone || "") + (o.key === suggested ? " suggested" : "") + '" data-k="' + o.key + '">' +
      (k ? "<kbd>" + k + "</kbd>" : "") + esc(o.label) + "</button>";
  }).join("");
}

export function pick(key) {
  if (S.state !== "wrap" || saving) return;
  const o = outcome(key);
  chosen = o;
  if (!o.connect) { save(); return; }
  renderLevel2(o);
}

/* ---- level 2 ------------------------------------------------------------ */

const field = (id, label, value, attrs) => '<div><label class="lbl" for="' + id + '">' + label + "</label>" +
  '<input class="field" id="' + id + '" value="' + esc(value || "") + '" ' + (attrs || "") + "></div>";

function nextWorkday(hour, minute) {
  const d = new Date(Date.now() + 330 * 60000);
  do { d.setUTCDate(d.getUTCDate() + 1); } while (d.getUTCDay() === 0);
  d.setUTCHours(hour, minute || 0, 0, 0);
  return new Date(d.getTime() - 330 * 60000);
}

function timePicks(kind) {
  const picks = kind === "demo"
    ? [["Tomorrow 11:00", nextWorkday(11)], ["Tomorrow 15:00", nextWorkday(15)], ["Day after 11:00", new Date(nextWorkday(11).getTime() + 86400000)]]
    : [["In 1 hour", new Date(Date.now() + 3600000)], ["In 3 hours", new Date(Date.now() + 3 * 3600000)], ["Tomorrow 10:30", nextWorkday(10, 30)], ["Tomorrow 15:00", nextWorkday(15)]];
  return '<div class="acts" data-picks="' + kind + '">' + picks.map((p, i) =>
    '<button class="btn sm" data-when="' + istInput(p[1]) + '">' + (i + 1) + ". " + esc(p[0]) + "</button>").join("") + "</div>";
}

function renderLevel2(o) {
  const c = S.call, tags = S.cfg.objection_tags || [];
  let h = '<div class="l2-head"><h3 class="' + (o.tone === "good" ? "good" : "") + '">' + esc(o.label) + '</h3><button class="btn quiet sm" data-back>Change <kbd>esc</kbd></button></div>';
  if (o.kind === "callback") {
    h += '<div><span class="lbl">Call back when (IST)</span>' + timePicks("callback") +
      '<input class="field" type="datetime-local" id="w-callback" value="' + esc(c.callback_at) + '" style="max-width:240px;margin-top:6px"></div>';
  }
  if (o.demo) {
    h += '<div><span class="lbl">Demo, 15 minutes (IST)</span>' + timePicks("demo") +
      '<input class="field" type="datetime-local" id="w-demo" value="' + esc(c.demo_at) + '" style="max-width:240px;margin-top:6px"></div>';
  }
  h += '<div><span class="lbl">Objections heard</span><div class="chips" id="w-obj">' + tags.map((t) =>
    '<button class="chip" type="button" data-obj="' + t.key + '" aria-pressed="' + (c.objections.indexOf(t.key) >= 0) + '">' + esc(t.label) + "</button>").join("") +
    '</div><input class="field" id="w-other" placeholder="The other objection, in their words" maxlength="80" value="' + esc(c.other) + '"' + (c.objections.indexOf("OTHER") < 0 ? " hidden" : "") + ' style="margin-top:6px"></div>';
  h += '<div class="grid3">' + field("w-dm", "Decision maker", c.dm_name, 'maxlength="60"') +
    field("w-mobile", "DM mobile", c.dm_mobile, 'type="tel" maxlength="20"') +
    field("w-wa", "WhatsApp" + (o.sample ? " or email needed" : ""), c.whatsapp ? prettyPhone(c.whatsapp, "mobile") : "", 'type="tel" maxlength="20"') + "</div>";
  h += '<div class="grid3">' + field("w-email", "Email", c.email, 'type="email" maxlength="80"') + "</div>";
  h += '<p class="muted small">Drawings a month, method, software and pain are saved from the fields above.</p>';
  h += '<p class="err" id="w-err"></p><div class="acts"><button class="btn primary" id="w-save">Save and next <kbd>enter</kbd></button>' +
    '<button class="btn quiet" id="w-referral">Add a referral</button></div>';
  $("l2").innerHTML = h;
  $("l2").hidden = false;
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  $("l2").scrollIntoView({ block: "nearest" });
}

function readLevel2() {
  const v = (id) => ($(id) ? $(id).value.trim() : "");
  const c = S.call;
  c.dm_name = v("w-dm"); c.dm_mobile = v("w-mobile"); c.whatsapp = v("w-wa"); c.email = v("w-email");
  c.other = v("w-other");
  if ($("w-demo")) c.demo_at = $("w-demo").value;
  if ($("w-callback")) c.callback_at = $("w-callback").value;
}

export function level2Key(e, k) {
  if (!chosen || $("l2").hidden) return false;
  if (k === "Enter" && !e.target.matches("textarea")) { e.preventDefault(); save(); return true; }
  if (/^[1-4]$/.test(k) && !e.target.matches("input, textarea, select")) {
    const picks = $("l2").querySelectorAll("[data-when]");
    if (picks[+k - 1]) { e.preventDefault(); picks[+k - 1].click(); }
    return true;
  }
  return false;
}

/* ---- save ------------------------------------------------------------- */

export function save(opts) {
  opts = opts || {};
  const o = opts.outcome ? outcome(opts.outcome) : chosen;
  if (!o || saving || !S.cur) return Promise.resolve(false);
  if (o.connect && !opts.outcome) readLevel2();
  const c = S.call, p = opts.phone || activePhone();
  const body = {
    lead_id: S.cur.id, phone_id: p ? p.id : null, outcome: o.key, continued: !!opts.continued,
    dialed_at: S.dialedAt ? toServer(S.dialedAt) : null, connected_at: S.connectedAt ? toServer(S.connectedAt) : null,
    ended_at: toServer(S.endedAt || new Date()), notes: $("notes").value.trim(),
    script_version: S.session.script || S.cfg.default_script_version || "", session_id: S.session.id
  };
  if (o.connect) {
    Object.assign(body, {
      objections: c.objections.map((k) => (k === "OTHER" && c.other ? "OTHER:" + c.other : k)),
      dm_name: c.dm_name, dm_mobile: c.dm_mobile, whatsapp: c.whatsapp, email: c.email, language_pref: c.language_pref,
      actual_drawings_month: c.actual_drawings_month, current_method: c.current_method, software_used: c.software_used, pain: c.pain
    });
    if (o.demo) body.demo_at = c.demo_at;
    if (o.kind === "callback") body.callback_at = c.callback_at;
  }
  saving = true;
  return api("/api/call", body).then((d) => {
    saving = false;
    if (d.error) {
      if ($("w-err")) $("w-err").textContent = d.error;
      toast("error", "<b>Not saved.</b> " + esc(d.error));
      return false;
    }
    if (opts.continued) return d;
    const lead = S.cur;
    offerUndo(d.id, o, lead);
    emit("saved", { lead, outcome: o, call: Object.assign({}, c), id: d.id });
    actions.afterSave(o);
    return d;
  }).catch(() => { saving = false; toast("error", "Not saved: the server did not answer. Try again."); return false; });
}

function offerUndo(id, o, lead) {
  if (lastUndo) lastUndo.close();
  let when = "";
  if (o.demo && S.call.demo_at) when = " · demo " + istWhen(fromIstInput(S.call.demo_at));
  if (o.kind === "callback" && S.call.callback_at) when = " · call back " + istWhen(fromIstInput(S.call.callback_at));
  lastUndo = toast("ok", "<b>" + esc(o.label) + "</b> · " + esc(lead.company) + esc(when),
    { ms: 7000, actions: [{ html: "Undo <kbd>z</kbd>", run: () => undo(id) }], onClose: () => { lastUndo = null; } });
  lastUndo.id = id;
}

export function undo(id) {
  const target = id || (lastUndo && lastUndo.id);
  if (!target) return;
  if (S.state === "ringing" || S.state === "connected") { toast("error", "Finish this call first."); return; }
  if (lastUndo) lastUndo.close();
  actions.cancelCountdown();
  const release = S.cur && S.state === "ready" ? api("/api/release", { lead_id: S.cur.id }) : Promise.resolve();
  release.then(() => api("/api/undo", { id: target })).then((d) => {
    if (d.error) { toast("error", esc(d.error)); return; }
    actions.loadLead(d.lead, { picked: true, wrap: true, notes: d.notes });
    emit("saved", {});
  });
}

export function wireWrap() {
  $("outcomes").addEventListener("click", (e) => { const b = e.target.closest("[data-k]"); if (b) pick(b.getAttribute("data-k")); });
  $("l2").addEventListener("click", (e) => {
    let b;
    if ((b = e.target.closest("[data-obj]"))) {
      const key = b.getAttribute("data-obj"), list = S.call.objections, i = list.indexOf(key);
      if (i >= 0) list.splice(i, 1); else list.push(key);
      b.setAttribute("aria-pressed", String(i < 0));
      $("w-other").hidden = list.indexOf("OTHER") < 0;
      if (key === "OTHER" && i < 0) $("w-other").focus();
    } else if ((b = e.target.closest("[data-when]"))) {
      const input = b.parentNode.getAttribute("data-picks") === "demo" ? $("w-demo") : $("w-callback");
      input.value = b.getAttribute("data-when");
      b.parentNode.querySelectorAll(".btn").forEach((x) => x.classList.toggle("primary", x === b));
    } else if (e.target.closest("[data-back]")) {
      chosen = null; $("l2").hidden = true;
    } else if (e.target.id === "w-save") save();
    else if (e.target.id === "w-referral") actions.referral && actions.referral();
  });
}
