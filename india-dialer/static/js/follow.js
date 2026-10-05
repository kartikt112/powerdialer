/* Follow-ups, always sent by hand. w or e opens the message with the right
   template filled in; the button opens WhatsApp (wa.me) or the mail app
   (mailto:), where Pawan reads it and presses send himself. The app never
   sends anything. Each open is logged on the lead. The Follow-ups rail
   lists who is owed a nudge, and the leads to reach only in writing. */
import { $, esc, parseUTC, rel, prettyPhone, firstName, istDay } from "./util.js";
import { S, on, emit } from "./state.js";
import { api } from "./api.js";
import { toast, openModal, closeModal, closeX } from "./ui.js";
import { registerTab } from "./rails.js";
import { leadVars, renderPlain, spoken } from "./script.js";

const templates = () => (S.cfg.scripts && S.cfg.scripts.templates) || {};

/* 91XXXXXXXXXX for wa.me, from whatever the lead or this call has. */
function waDigits(lead, call) {
  const pick = [(call || {}).whatsapp, lead.whatsapp, (call || {}).dm_mobile, lead.dm_mobile]
    .concat((lead.phones || []).filter((p) => p.kind === "mobile" && !p.bad).map((p) => p.e164)).concat([lead.mobile]);
  for (const raw of pick) {
    let d = String(raw || "").replace(/\D/g, "");
    if (d.length === 11 && d[0] === "0") d = d.slice(1);
    if (d.length === 10 && /^[6-9]/.test(d)) d = "91" + d;
    if (d.length === 12 && d.startsWith("91") && /^[6-9]/.test(d[2])) return d;
  }
  return "";
}
const emailOf = (lead, call) => ((call || {}).email || lead.email || "").trim();

/* Which template fits this lead right now. */
function defaultTemplate(lead, hint) {
  if (hint) return hint;
  const card = (lead.samples || []).find((c) => c.stage !== "won" && c.stage !== "lost");
  if (card && (card.stage === "delivered" || card.stage === "feedback")) return "sample_delivered";
  if (card && card.stage === "asked") return "sample_request";
  return "after_call_intro";
}

function message(kind, lead, call) {
  const t = templates()[kind] || {};
  const vars = leadVars(lead, call);
  const named = !!firstName(vars.dm_name);
  return {
    whatsapp: renderPlain(t.whatsapp, vars).trim(),
    subject: named ? renderPlain(t.subject, vars).trim() : spoken(lead.company),
    email: renderPlain(t.email, vars).trim(),
    named
  };
}

/* ---- the dialog ------------------------------------------------------------------ */

export function openFollowup(lead, opts) {
  opts = opts || {};
  if (!lead) return;
  if (lead.is_dnc) { toast("error", "On the do-not-call list: no messages either."); return; }
  const call = S.cur && S.cur.id === lead.id ? S.call : {};
  let kind = defaultTemplate(lead, opts.template);
  let channel = opts.channel || "whatsapp";
  const sampleId = opts.sampleId || ((lead.samples || []).find((c) => c.stage !== "won" && c.stage !== "lost") || {}).id || null;

  const draw = () => {
    const m = message(kind, lead, call), wa = waDigits(lead, call), to = emailOf(lead, call);
    const kinds = Object.keys(templates());
    const target = channel === "whatsapp"
      ? (wa ? "To " + prettyPhone("+" + wa, "mobile") + " on WhatsApp" : "No mobile number for WhatsApp yet. Add one below.")
      : (to ? "To " + to : "No email yet. Add one below.");
    openModal('<div class="dh"><h2>' + esc(spoken(lead.company)) + "</h2>" + closeX() + "</div>" +
      '<div class="fu-tabs"><span class="tabs-text" id="fu-ch"><button data-ch="whatsapp" aria-selected="' + (channel === "whatsapp") + '">WhatsApp <kbd>w</kbd></button>' +
      '<button data-ch="email" aria-selected="' + (channel === "email") + '">Email <kbd>e</kbd></button></span>' +
      '<span class="tabs-text" id="fu-kind">' + kinds.map((k) => '<button data-k="' + k + '" aria-selected="' + (k === kind) + '">' + esc(templates()[k].label || k) + "</button>").join("") + "</span></div>" +
      '<p class="sub">' + esc(target) + (channel === "email" && !m.named ? " Subject is the company until you know their first name." : "") + "</p>" +
      (channel === "whatsapp" && !wa ? '<div class="grid2"><div><label class="lbl" for="fu-to">WhatsApp number</label><input class="field" id="fu-to" type="tel" maxlength="20"></div></div>' : "") +
      (channel === "email" && !to ? '<div class="grid2"><div><label class="lbl" for="fu-to">Email</label><input class="field" id="fu-to" type="email" maxlength="80"></div></div>' : "") +
      (channel === "email" ? '<label class="lbl" for="fu-subject">Subject</label><input class="field" id="fu-subject" value="' + esc(m.subject) + '">' : "") +
      '<label class="lbl" for="fu-text">Message <span class="muted">change anything; it opens as written</span></label>' +
      '<textarea class="field" id="fu-text" rows="9">' + esc(channel === "whatsapp" ? m.whatsapp : m.email) + "</textarea>" +
      '<p class="err" id="fu-err"></p><div class="fu-go"><button class="btn primary" id="fu-go">' + (channel === "whatsapp" ? "Open in WhatsApp" : "Open in mail") + "</button>" +
      '<span class="muted small">You press send there. Nothing is sent from here.</span></div>', { wide: true });
    $("fu-ch").addEventListener("click", (e) => { const b = e.target.closest("[data-ch]"); if (b) { channel = b.getAttribute("data-ch"); draw(); } });
    $("fu-kind").addEventListener("click", (e) => { const b = e.target.closest("[data-k]"); if (b) { kind = b.getAttribute("data-k"); draw(); } });
    $("fu-go").addEventListener("click", go);
  };

  const go = () => {
    const text = $("fu-text").value;
    let url, to;
    if (channel === "whatsapp") {
      to = waDigits(lead, call) || waDigits({ whatsapp: $("fu-to") ? $("fu-to").value : "" });
      if (!to) { $("fu-err").textContent = "That does not read as an Indian mobile number."; return; }
      url = "https://wa.me/" + to + "?text=" + encodeURIComponent(text);
    } else {
      to = emailOf(lead, call) || ($("fu-to") ? $("fu-to").value.trim() : "");
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(to)) { $("fu-err").textContent = "That email does not look right."; return; }
      url = "mailto:" + encodeURIComponent(to) + "?subject=" + encodeURIComponent($("fu-subject").value) + "&body=" + encodeURIComponent(text);
    }
    api("/api/followup", { lead_id: lead.id, channel, template: kind, to, sample_id: sampleId }).then((d) => {
      if (d.error) { toast("error", esc(d.error)); return; }
      lead.followups = [{ channel, template: kind, at: new Date().toISOString().slice(0, 19).replace("T", " ") }].concat(lead.followups || []);
      if (S.cur && S.cur.id === lead.id) emit("lead", S.cur);
      emit("followup", { lead, channel, kind });
      refreshFollowups();
    });
    if (channel === "whatsapp") window.open(url, "_blank", "noopener");
    else window.location.href = url;
    closeModal();
    toast("ok", (channel === "whatsapp" ? "WhatsApp" : "Mail") + " opened for <b>" + esc(spoken(lead.company)) + "</b>. Logged. Press send there.");
  };
  draw();
}

export function followLead(id, opts) {
  if (S.cur && S.cur.id === id) { openFollowup(S.cur, opts); return; }
  api("/api/lead?id=" + id).then((d) => { if (d.lead) openFollowup(d.lead, opts); });
}

/* ---- the rail ---------------------------------------------------------------------- */

function row(x, kind) {
  const since = parseUTC(x.since || x.last_called_at);
  const nudge = parseUTC(x.last_nudge);
  const wa = waDigits(x), mail = x.email;
  const t2 = kind === "due"
    ? [x.dm_name, x.city, nudge ? "nudged " + rel(nudge) : "no nudge yet"].filter(Boolean).map(esc).join(" · ")
    : [x.dm_name, x.city, x.attempts + " tries, no DM", nudge ? "messaged " + rel(nudge) : ""].filter(Boolean).map(esc).join(" · ");
  const q = encodeURIComponent;
  return '<div class="row" data-lead="' + x.id + '"><span class="t1">' + esc(x.company) + '</span><span class="r">' + (since ? esc(kind === "due" ? rel(since) : istDay(since)) : "") + "</span>" +
    '<span class="t2 full">' + (kind === "due" ? "<b>" + esc(x.label) + "</b> · " : "") + t2 + "</span>" +
    '<span class="acts"><button class="btn sm" data-fu="whatsapp" data-id="' + x.id + '" data-t="' + esc(x.template || "after_call_intro") + '"' + (x.sample_id ? ' data-s="' + x.sample_id + '"' : "") + ">WhatsApp" + (wa ? "" : ", add number") + "</button>" +
    '<button class="btn quiet sm" data-fu="email" data-id="' + x.id + '" data-t="' + esc(x.template || "after_call_intro") + '"' + (x.sample_id ? ' data-s="' + x.sample_id + '"' : "") + ">Email" + (mail ? "" : ", add address") + "</button>" +
    (kind === "written" ? '<a class="btn quiet sm" target="_blank" rel="noopener noreferrer" href="https://www.linkedin.com/search/results/people/?keywords=' + q(x.company + " quality") + '">LinkedIn</a>' : "") +
    "</span></div>";
}

let due = [], written = [];
function render() {
  $("n-fu").textContent = due.length || "";
  $("n-fu").classList.toggle("due", due.length > 0);
  let h = '<p class="stage-h">Due for a nudge<span>' + due.length + "</span></p>" +
    (due.length ? due.map((x) => row(x, "due")).join("") : '<p class="note">Nobody is owed a nudge. Samples not in after 24 hours, delivered with no feedback after 48, and interested with no next step after 3 days show here.</p>');
  h += '<p class="stage-h">WhatsApp or LinkedIn only<span>' + written.length + "</span></p>" +
    (written.length ? written.map((x) => row(x, "written")).join("") : '<p class="note">Leads that used all 5 tries without reaching the decision maker land here.</p>');
  $("l-followups").innerHTML = h;
}

export function refreshFollowups() {
  if (!$("l-followups").children.length) $("l-followups").innerHTML = '<div class="skel"></div>'.repeat(4);
  return api("/api/followups").then((d) => { due = d.due || []; written = d.written_only || []; render(); });
}
registerTab("followups", refreshFollowups);

/* ---- keys and wiring ------------------------------------------------------------------ */

export function followKey(e, k) {
  if ((k === "w" || k === "e") && S.cur) { e.preventDefault(); openFollowup(S.cur, { channel: k === "w" ? "whatsapp" : "email" }); return true; }
  return false;
}

export function wireFollowups() {
  $("l-followups").addEventListener("click", (e) => {
    const b = e.target.closest("[data-fu]"); if (!b) return;
    followLead(+b.getAttribute("data-id"), { channel: b.getAttribute("data-fu"), template: b.getAttribute("data-t"), sampleId: +b.getAttribute("data-s") || null });
  });
  $("c-links").addEventListener("click", (e) => {
    const b = e.target.closest("[data-fu-here]"); if (b && S.cur) openFollowup(S.cur, { channel: b.getAttribute("data-fu-here") });
  });
  $("l-samples").addEventListener("click", (e) => {
    const b = e.target.closest("[data-fu-card]"); if (!b) return;
    followLead(+b.getAttribute("data-lead-id"), { channel: "whatsapp", template: b.getAttribute("data-t"), sampleId: +b.getAttribute("data-fu-card") });
  });
  on("saved", (p) => {
    refreshFollowups();
    const o = p && p.outcome;
    if (!o || !p.lead) return;
    const tpl = o.sample ? "sample_request" : o.demo || o.key === "INTERESTED_NO" ? "after_call_intro" : null;
    if (!tpl) return;
    const lead = Object.assign({}, p.lead, { whatsapp: p.call.whatsapp || p.lead.whatsapp, email: p.call.email || p.lead.email, dm_name: p.call.dm_name || p.lead.dm_name });
    toast("ok", "<b>Send the " + esc((templates()[tpl] || {}).label || "follow-up").toLowerCase() + " while it's warm.</b>", {
      ms: 30000, actions: [{ html: "WhatsApp", run: () => openFollowup(lead, { channel: "whatsapp", template: tpl }) },
                           { html: "Email", run: () => openFollowup(lead, { channel: "email", template: tpl }) }] });
  });
  refreshFollowups();
}
