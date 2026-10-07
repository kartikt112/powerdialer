/* Script rail: the call as a tree you walk with the arrow keys.
   The current step is big, the next two are small. Branches jump. The Book
   step carries the date picker and the three mini-contracts. Objections are
   a searchable panel (o). Every string comes from config.yaml
   (dialer.scripts), picked by the session's script version. */
import { $, icon, esc, fmtPhone, fmtClock, parseUTC, leadDay, leadClock, myClock, firstName, pad } from "./util.js";
import { S, on, emit, outcome, campaignScript } from "./state.js";
import { api } from "./api.js";
import { toast, copyText } from "./ui.js";

let flow = "call";            // call | voicemail | inbound
let idx = 0;
let objOpen = false, objQuery = "", objEdit = null;
let lastEmail = null;

const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/* ---- tokens ------------------------------------------------------------------ */

function bookedWhen(local) {
  const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/.exec(local || "");
  if (!m) return "";
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])), h = +m[4];
  return DOW[d.getUTCDay()] + " " + (+m[3]) + " " + MON[+m[2] - 1] + " at " + ((h % 12) || 12) + ":" + m[5] + (h < 12 ? "am" : "pm");
}

export function leadVars(l, call) {
  const c = call || S.call || {};
  const contact = ((l.first || "") + " " + (l.last || "")).trim();
  const dm = c.dm_name || l.dm_name || contact;
  return {
    first: l.first || firstName(dm) || "there", last: l.last || "", dm_name: dm, dm_first: firstName(dm) || "there",
    company: l.co || l.company || "your company", title: l.title || "", agent: S.agentName || "me",
    city: l.city || "", state: l.state || "", process: c.process || l.process || "", oem: c.oem || l.oem || "",
    email: c.email || l.email || "", pain: c.pain || l.pain || "",
    callback_number: l.caller_id_spoken || fmtPhone(l.caller_id || ""),
    calendly: S.cfg.calendly_url || "", booked_when: bookedWhen(c.booked_for_local)
  };
}

function expand(tpl, vars) {
  return String(tpl || "").replace(/\{([?!])(\w+)\}([\s\S]*?)\{\/\2\}/g, (_, op, k, body) => ((op === "?") === !!vars[k]) ? body : "");
}
/* {token}, {?token}shown when set{/token}, {!token}shown when empty{/token}, **bold** */
export function renderTpl(tpl, vars) {
  return esc(expand(tpl, vars)).replace(/\{(\w+)\}/g, (m, k) =>
    Object.prototype.hasOwnProperty.call(vars, k) ? '<span class="tok">' + esc(vars[k]) + "</span>" : m)
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
}
export function renderPlain(tpl, vars) {
  return expand(tpl, vars).replace(/\{(\w+)\}/g, (m, k) => Object.prototype.hasOwnProperty.call(vars, k) ? vars[k] : m).replace(/\*\*/g, "");
}

/* ---- the tree -------------------------------------------------------------------- */

function version() {
  const tree = (S.cfg.scripts && S.cfg.scripts.tree) || {};
  const want = S.session.script || campaignScript() || S.cfg.default_script_version;
  return tree[want] ? want : Object.keys(tree)[0];
}
function tree() { return ((S.cfg.scripts && S.cfg.scripts.tree) || {})[version()] || { order: {}, steps: {} }; }

function visible(step, vars) {
  if (step.when && !vars[step.when]) return false;
  if (step.unless && vars[step.unless]) return false;
  if (step.attempts && S.cur && step.attempts.indexOf(S.cur.attempt_no || 1) < 0) return false;
  return true;
}
function steps() {
  if (!S.cur) return [];
  const t = tree(), vars = leadVars(S.cur);
  return (t.order[flow] || []).map((id) => Object.assign({ id }, t.steps[id] || {})).filter((s) => visible(s, vars));
}

export const currentVersion = () => version();
export function currentStepId() { const l = steps(); return l.length ? l[Math.max(0, Math.min(l.length - 1, idx))].id : ""; }

export function goStep(target) {
  const list = steps();
  if (typeof target === "string") {
    let i = list.findIndex((s) => s.id === target);
    if (i < 0) {                                        // the step lives in another flow
      const t = tree();
      const home = Object.keys(t.order).find((f) => t.order[f].indexOf(target) >= 0);
      if (!home) return;
      flow = home;
      i = Math.max(0, steps().findIndex((s) => s.id === target));
    }
    idx = i;
  } else idx = Math.max(0, Math.min(list.length - 1, idx + target));
  closeObjections(true);
  renderScript();
}
export function setFlow(f) { flow = f; idx = 0; closeObjections(true); renderScript(); }

function stepHTML(s, vars) {
  const say = s.say || [];
  let h = '<div class="step-h"><h3>' + esc(s.title || s.id) + "</h3></div>";
  if (s.id === "permission" && S.cfg.recording && S.call && S.call.recording_held && !S.call.recording_sid)
    h += '<div class="must">' + icon("alert") + "<span>Not recording yet. Say: &ldquo;" + esc(S.cfg.disclosure) + "&rdquo; then press <kbd>r</kbd>.</span></div>";
  if (flow === "voicemail" && S.cur.caller_id) h += '<div class="cid-line">' + icon("phone", "sm") + "Caller ID in use: <b>" + esc(fmtPhone(S.cur.caller_id)) + "</b></div>";
  if (say[0]) h += '<p class="say">' + renderTpl(say[0], vars) + "</p>";
  say.slice(1).forEach((line) => { h += '<p class="say alt">' + renderTpl(line, vars) + "</p>"; });
  if (s.cue) h += '<p class="cue">' + renderTpl(s.cue, vars) + "</p>";

  if (s.capture) {
    h += '<div class="capture"><label class="lbl" for="sc-pain">Pain, in their words</label><input class="field" id="sc-pain" data-bind="pain" maxlength="300" value="' + esc(S.call.pain || "") + '" placeholder="Type exactly what they said">' +
      '<div class="formrow" style="margin-top:8px"><div><label class="lbl" for="sc-ppap">PPAPs per year</label><input class="field num" id="sc-ppap" data-bind="ppap_per_year" maxlength="12" value="' + esc(S.call.ppap_per_year || "") + '"></div>' +
      '<div><label class="lbl" for="sc-oem">Which OEMs</label><input class="field" id="sc-oem" data-bind="oem" maxlength="80" value="' + esc(S.call.oem || "") + '"></div></div></div>';
  }
  if ((s.prompts || []).length) h += '<div class="prompts"><span class="eyebrow">Pain questions</span>' + s.prompts.map((p) => '<p class="prompt">' + renderTpl(p, vars) + "</p>").join("") + "</div>";

  if (s.action === "book") {
    h += '<div class="capture"><label class="lbl" for="sc-when">When, on THEIR clock' + (S.cur.city ? " (" + esc(S.cur.city) + ")" : "") + '</label>' +
      '<input class="field" type="datetime-local" id="sc-when" data-bind="booked_for_local" value="' + esc(S.call.booked_for_local || "") + '">' +
      '<div class="contracts">' + (s.contracts || []).map((c, i) =>
        '<label class="check"><input type="checkbox" data-contract="' + i + '"' + ((S.call.contracts || [])[i] ? " checked" : "") + "><span>" + renderTpl(c, vars) + "</span></label>").join("") + "</div>" +
      '<label class="lbl" for="sc-email" style="margin-top:10px">Their email, for the invite</label><input class="field" id="sc-email" type="email" data-bind="email" maxlength="70" value="' + esc(S.call.email || "") + '">' +
      '<p class="tip">When you hang up, press <kbd>1</kbd> Booked call. The time and email carry over.</p></div>';
  }
  if ((s.chips || []).length) h += '<div class="qa">' + s.chips.map((c, i) =>
    '<details class="obj"><summary>' + esc(c.q) + icon("chevron", "sm") + "</summary><p>" + renderTpl(c.a, vars) + "</p></details>").join("") + "</div>";
  if ((s.branches || []).length) h += '<div class="branches">' + s.branches.map((b, i) =>
    '<button class="btn sm" data-branch="' + i + '">' + esc(b.label) + "</button>").join("") + "</div>";
  if ((s.rules || []).length) h += '<ul class="rules">' + s.rules.map((r) => "<li>" + renderTpl(r, vars) + "</li>").join("") + "</ul>";
  return h;
}

export function renderScript() {
  $("flow-tabs").querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", b.getAttribute("data-f") === flow ? "true" : "false"));
  $("script-ver").textContent = S.cur ? version() : "";
  if (objOpen) { renderObjections(); return; }
  $("objpanel").hidden = true; $("treebox").hidden = false;
  if (!S.cur) {
    $("crumbs").innerHTML = ""; $("step-next").innerHTML = ""; $("step-nav").hidden = true;
    $("step").innerHTML = '<p class="muted">The script fills in with the lead\'s name, company and process once one is on screen.</p>';
    return;
  }
  const list = steps(), vars = leadVars(S.cur);
  $("step-nav").hidden = list.length < 2;
  if (!list.length) {
    $("crumbs").innerHTML = ""; $("step-next").innerHTML = "";
    $("step").innerHTML = flow === "voicemail"
      ? '<div class="step-h"><h3>No message on this try</h3></div><p class="cue">This is attempt ' + (S.cur.attempt_no || 1) + ". Messages go out on tries " +
        (S.cfg.voicemail_attempts || [1, 3, 5]).join(", ") + " only. Hang up before the beep and save No answer.</p>"
      : '<p class="muted">No steps in this flow.</p>';
    return;
  }
  idx = Math.max(0, Math.min(list.length - 1, idx));
  $("crumbs").innerHTML = list.map((s, i) => '<button class="crumb' + (i === idx ? " on" : i < idx ? " done" : "") + '" data-go="' + esc(s.id) + '" title="' + esc(s.title || s.id) + '">' +
    esc((s.title || s.id).split(",")[0]) + "</button>").join("");
  $("step").innerHTML = stepHTML(list[idx], vars);
  $("step-next").innerHTML = list.slice(idx + 1, idx + 3).map((s) =>
    '<button class="upnext" data-go="' + esc(s.id) + '"><span class="eyebrow">' + esc(s.title || s.id) + "</span><span>" +
    esc(renderPlain((s.say || [""])[0], vars)) + "</span></button>").join("");
  $("b-prev").disabled = idx === 0; $("b-next").disabled = idx >= list.length - 1;
  $("step").scrollTop = 0;
}

function takeBranch(i) {
  const b = (steps()[idx].branches || [])[i];
  if (!b) return;
  if (b.tag && S.call.objections.indexOf(b.tag) < 0) S.call.objections.push(b.tag);
  if (b.outcome) {
    S.call.suggest = b.outcome;
    toast("info", "After you hang up, <kbd>enter</kbd> saves <b>" + esc(outcome(b.outcome).label) + "</b>.");
  }
  if (b.to) goStep(b.to);
}

/* ---- objections panel -------------------------------------------------------------- */

function objections() { return (S.cfg.scripts && S.cfg.scripts.objections) || []; }

export function openObjections(focusKey) {
  objOpen = true; objQuery = ""; objEdit = null;
  renderObjections();
  const q = $("obj-q"); if (q) q.focus();
  if (focusKey) {
    const card = $("objpanel").querySelector('[data-tag="' + focusKey + '"], [data-okey="' + focusKey + '"]');
    if (card) { card.open = true; card.scrollIntoView({ block: "nearest" }); }
  }
}
export function closeObjections(silent) { if (!objOpen) return false; objOpen = false; if (!silent) renderScript(); return true; }
export const objectionsOpen = () => objOpen;

function objForm(o) {
  const f = (k, label, rows) => '<label class="lbl" for="oe-' + k + '">' + label + '</label><textarea class="field" id="oe-' + k + '" rows="' + rows + '">' + esc(o[k] || "") + "</textarea>";
  return '<div class="objform">' + (o.key ? "" : '<label class="lbl" for="oe-title">What they say</label><input class="field" id="oe-title" maxlength="80" placeholder="e.g. We use a consultant">') +
    f("anchor", "1 · Anchor (agree, lower the guard)", 2) + f("disrupt", "2 · Pattern disrupt", 2) + f("question", "3 · Question (hand it back)", 2) +
    '<p class="err" id="oe-err"></p><div class="acts"><button class="btn sm" data-oe-cancel>Cancel</button><button class="btn sm primary" data-oe-save="' + esc(o.key || "") + '">Save rebuttal</button></div></div>';
}

function renderObjections() {
  $("treebox").hidden = true; $("objpanel").hidden = false;
  $("obj-rule").textContent = (S.cfg.scripts && S.cfg.scripts.objection_rule) || "";
  const vars = S.cur ? leadVars(S.cur) : { company: "your company", oem: "", dm_first: "there" };
  const q = objQuery.toLowerCase();
  const list = objections().filter((o) => !q || [o.title, o.anchor, o.disrupt, o.question, o.tag].join(" ").toLowerCase().indexOf(q) >= 0);
  const heard = (S.call && S.call.objections) || [];
  const cards = list.map((o) => {
    if (objEdit === o.key) return '<div class="obj open-edit"><div class="obj-t">' + esc(o.title) + "</div>" + objForm(o) + "</div>";
    const on = o.tag && heard.indexOf(o.tag) >= 0;
    return '<details class="obj" data-okey="' + esc(o.key) + '"' + (o.tag ? ' data-tag="' + esc(o.tag) + '"' : "") + (list.length <= 2 || q ? " open" : "") + "><summary>" + esc(o.title) +
      (o.edited ? '<span class="pill">edited</span>' : "") + "</summary>" +
      '<ol class="three"><li><span class="eyebrow">Anchor</span>' + renderTpl(o.anchor, vars) + '</li><li><span class="eyebrow">Disrupt</span>' + renderTpl(o.disrupt, vars) +
      '</li><li><span class="eyebrow">Question</span>' + renderTpl(o.question, vars) + "</li></ol>" + (o.note ? '<p class="cue" style="padding:0 12px 10px">' + renderTpl(o.note, vars) + "</p>" : "") +
      '<div class="obj-acts">' + (o.tag && S.cur ? '<button class="chip-t" aria-pressed="' + on + '" data-heard="' + esc(o.tag) + '">' + (on ? "Heard it, logged" : "Heard it") + "</button>" : "") +
      '<button class="btn sm quiet" data-oe="' + esc(o.key) + '">Edit</button></div></details>';
  }).join("");
  $("obj-list").innerHTML = (cards || '<p class="muted">Nothing matches that search.</p>') +
    (objEdit === "__new" ? '<div class="obj open-edit"><div class="obj-t">New rebuttal</div>' + objForm({}) + "</div>"
      : '<button class="btn sm" data-oe="__new" style="margin-top:12px">Add a rebuttal</button>');
}

function saveObjection(key) {
  const get = (k) => ($("oe-" + k) ? $("oe-" + k).value.trim() : "");
  const old = objections().find((o) => o.key === key) || {};
  const body = { key, title: key ? old.title : get("title"), anchor: get("anchor"), disrupt: get("disrupt"), question: get("question"), note: old.note || "", tag: old.tag || "" };
  api("/api/objections", body).then((d) => {
    if (d.error) { $("oe-err").textContent = d.error; return; }
    S.cfg.scripts.objections = d.objections; objEdit = null; renderObjections();
    toast("success", "Rebuttal saved. It overrides config.yaml on this server.");
  });
}

/* ---- follow-up emails ---------------------------------------------------------------- */

export function emailFor(kind, lead, call) {
  const tpl = ((S.cfg.scripts || {}).emails || {})[kind];
  if (!tpl) return null;
  const vars = leadVars(lead, call);
  return { to: vars.email, subject: renderPlain(tpl.subject, vars).trim(), body: renderPlain(tpl.body, vars).trim() };
}
export function copyEmail(mail) {
  const m = mail || lastEmail;
  if (!m) { toast("info", "The follow-up email appears after a Booked call or a Resonated, no book."); return; }
  copyText("Subject: " + m.subject + "\n\n" + m.body, "Follow-up copied" + (m.to ? " for <b>" + esc(m.to) + "</b>" : "") + ". Subject line is their first name.");
}
export function emailKindFor(o) { return o && o.booked ? "booked_confirm" : o && o.key === "RESONATED_NO" ? "no_book_intrigue" : null; }

/* ---- history ----------------------------------------------------------------------------- */

function tagsOf(e) {
  let list = [];
  try { list = JSON.parse(e.objections || "[]"); } catch (x) {}
  const labels = {};
  (S.cfg.objection_tags || []).forEach((t) => { labels[t.key] = t.label; });
  return list.map((k) => { const p = String(k).split(":"); return p[0] === "OTHER" && p[1] ? p.slice(1).join(":") : (labels[p[0]] || p[0]); }).join(", ");
}

export function renderTimeline() {
  const cur = S.cur, h = (cur && cur.history) || [];
  $("h-count").textContent = h.length ? h.length + (h.length === 1 ? " call" : " calls") : "";
  if (!cur) { $("timeline").innerHTML = ""; return; }
  $("timeline").innerHTML = h.map((e) => {
    const o = outcome(e.disposition), at = parseUTC(e.at), bf = e.booked_for && parseUTC(e.booked_for);
    return '<div class="ev"><i class="node ' + esc(o.tone) + '"></i><div>' +
      '<div class="l1">' + esc(o.label) + "<span>" + (at ? leadDay(at, cur.tz_offset) + " · " + myClock(at) : "") +
      (e.duration ? " · " + fmtClock(e.duration) : "") + (e.attempt_no ? " · try " + e.attempt_no : "") + (e.script_version ? " · " + esc(e.script_version) : "") + "</span></div>" +
      (bf ? '<div class="l2"><b>Booked for:</b> ' + leadDay(bf, cur.tz_offset) + " " + leadClock(bf, cur.tz_offset) + " their time" + (e.show_status ? " · " + esc(e.show_status.toLowerCase().replace("_", " ")) : "") + "</div>" : "") +
      (e.pain ? '<div class="l2"><b>Pain:</b> ' + esc(e.pain) + "</div>" : "") +
      (tagsOf(e) ? '<div class="l2"><b>Objections:</b> ' + esc(tagsOf(e)) + "</div>" : "") +
      (e.notes ? '<div class="l2">' + esc(e.notes) + "</div>" : "") +
      (e.recording_sid ? '<div class="l2"><a href="/api/recording/' + esc(e.recording_sid) + '.mp3" target="_blank" rel="noopener">Play recording</a></div>' : "") +
      (e.ai_summary ? '<div class="l2"><b>Claude:</b> ' + esc(e.ai_summary) + "</div>" : "") +
      (e.transcript_status === "done" && e.transcript
        ? '<details class="transcript"><summary>Transcript</summary><pre>' + esc(e.transcript) + "</pre></details>"
        : e.transcript_status === "queued" || e.transcript_status === "working" ? '<div class="l2 muted">Transcribing…</div>'
        : e.transcript_status === "failed" ? '<div class="l2 muted">Transcript failed' + (e.transcript_error ? ": " + esc(e.transcript_error) : "") +
          ' <button class="btn quiet sm" data-retry-tx="' + e.id + '">Try again</button></div>' : "") + "</div></div>";
  }).join("") || '<p class="muted">First time anyone has called this lead.</p>';
}

/* ---- keys + wiring ------------------------------------------------------------------------ */

/* Returns true when the key was consumed. Called for keys outside inputs. */
export function scriptKey(e, k) {
  if (!S.cur) return false;
  if (k === "o") { e.preventDefault(); if (objOpen) closeObjections(); else openObjections(); return true; }
  if (objOpen) return false;
  if (k === "ArrowRight") { e.preventDefault(); goStep(1); return true; }
  if (k === "ArrowLeft") { e.preventDefault(); goStep(-1); return true; }
  if (k === "b") { goStep("book"); const w = $("sc-when"); if (w) w.focus(); e.preventDefault(); return true; }
  if (k === "e") { copyEmail(); return true; }
  return false;
}

export function wireScript() {
  $("flow-tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-f]"); if (b) setFlow(b.getAttribute("data-f")); });
  $("b-next").addEventListener("click", () => goStep(1));
  $("b-prev").addEventListener("click", () => goStep(-1));
  $("b-obj").addEventListener("click", () => (objOpen ? closeObjections() : openObjections()));
  $("obj-close").addEventListener("click", () => closeObjections());
  $("obj-q").addEventListener("input", function () { objQuery = this.value; renderObjections(); });
  $("obj-q").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); closeObjections(); } });

  $("treebox").addEventListener("click", (e) => {
    let b;
    if ((b = e.target.closest("[data-go]"))) goStep(b.getAttribute("data-go"));
    else if ((b = e.target.closest("[data-branch]"))) takeBranch(+b.getAttribute("data-branch"));
  });
  on("call", () => {                                 // keep the Qualify / Book inputs in step with the lead pane and wrap-up
    $("treebox").querySelectorAll("[data-bind]").forEach((el) => { if (document.activeElement !== el) el.value = S.call[el.getAttribute("data-bind")] || ""; });
  });
  $("treebox").addEventListener("input", (e) => {
    const bind = e.target.getAttribute && e.target.getAttribute("data-bind");
    if (bind) { S.call[bind] = e.target.value.trim ? e.target.value.trim() : e.target.value; emit("call"); }
  });
  $("treebox").addEventListener("change", (e) => {
    const c = e.target.getAttribute && e.target.getAttribute("data-contract");
    if (c != null) { S.call.contracts = S.call.contracts || []; S.call.contracts[+c] = e.target.checked; }
  });
  $("treebox").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches("input")) { e.preventDefault(); e.target.blur(); } });

  $("obj-list").addEventListener("click", (e) => {
    let b;
    if ((b = e.target.closest("[data-heard]"))) {
      const tag = b.getAttribute("data-heard"), list = S.call.objections, i = list.indexOf(tag);
      if (i >= 0) list.splice(i, 1); else list.push(tag);
      renderObjections(); e.preventDefault();
    } else if ((b = e.target.closest("[data-oe]"))) { objEdit = b.getAttribute("data-oe"); renderObjections(); }
    else if (e.target.closest("[data-oe-cancel]")) { objEdit = null; renderObjections(); }
    else if ((b = e.target.closest("[data-oe-save]"))) saveObjection(b.getAttribute("data-oe-save"));
  });

  on("lead", () => { flow = S.inbound ? "inbound" : "call"; idx = 0; objOpen = false; renderScript(); renderTimeline(); });
  on("session", () => renderScript());
  on("scripts", () => renderScript());
  on("call-ended", (reason) => { if (reason === "voicemail") setFlow("voicemail"); });
  on("saved", (p) => {
    const kind = p && emailKindFor(p.outcome);
    if (!kind) return;
    lastEmail = emailFor(kind, p.lead, p.call);
    if (!lastEmail) return;
    const mailto = "mailto:" + encodeURIComponent(lastEmail.to || "") + "?subject=" + encodeURIComponent(lastEmail.subject) + "&body=" + encodeURIComponent(lastEmail.body);
    toast("info", "<b>Send the follow-up while it is warm.</b>" + (p.webhook === "queued" ? " Invite webhook fired." : ""), {
      ms: 45000, actions: [{ html: "Copy email <kbd>e</kbd>", run: () => copyEmail(), keep: true },
                           { html: "Open in mail", run: () => { window.location.href = mailto; }, keep: true }] });
  });
}
