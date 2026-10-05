/* India dialer cockpit: the call state machine, the hand-off to the phone,
   power mode, the keyboard and boot.

   idle -> ready -> ringing -> connected -> wrap -> next lead
   The laptop never places a call. `space` opens the tel: link (Continuity or
   Phone Link rings it through the phone) and shows a QR code to scan; the
   timer runs from that moment. `c` marks the pickup, so talk time is real. */
import { $, esc, fmtClock, prettyPhone, parseUTC, istWhen, store } from "./util.js";
import { S, on, emit, actions, outcome, onCall, busy } from "./state.js";
import { api, withAgent } from "./api.js";
import { toast, banner, openModal, closeModal, modal, closeMenus, menuOpen, toggleMenu } from "./ui.js";
import { renderLead, renderNumber, renderNumbers, activePhone, firstDialable, nextDialable, freshCall, wireLead, syncDiscovery } from "./lead.js";
import { openWrap, closeWrap, pick, save, undo, level2Key, level2Open, wireWrap, suggest } from "./wrap.js";
import { wireRails, refreshAll, selectTab } from "./rails.js";
import { wireScript, scriptKey, jumpTo, objectionsOpen, closeObjections } from "./script.js";
import { wireFunnel, refreshStats } from "./funnel.js";
import { wireFollowups, followKey } from "./follow.js";
import { wireModals, helpModal, sessionModal, sessionEndCard, pauseReasons } from "./modals.js";
import { wireSamples } from "./samples.js";
import { wireEditor } from "./editor.js";

let countdown = null;
const settings = { openTel: store.get("in_open_tel", "1") === "1" };
actions.settings = settings;

/* ---------------------------------------------------------------- state -- */

const LABEL = { idle: "Idle", ready: "Ready", ringing: "Ringing", connected: "On the call", wrap: "Wrap-up" };

function setState(s) {
  S.state = s;
  document.body.setAttribute("data-state", s);
  const ready = s === "ready", ringing = s === "ringing", connected = s === "connected";
  $("lamp-text").textContent = S.session.paused && s === "idle" ? "Paused" : LABEL[s];
  $("b-dial").hidden = !ready;
  $("b-skip").hidden = !ready;
  $("b-connected").hidden = !ringing;
  $("b-noanswer").hidden = !ringing;
  $("b-next-number").hidden = !ringing || nextDialable() < 0;
  $("b-end").hidden = !(ringing || connected);
  $("dial-acts").hidden = s === "wrap" || s === "idle";
  if (!ringing && !connected) $("timer").textContent = s === "wrap" && S.dialedAt ? fmtClock(((S.endedAt || new Date()) - (S.connectedAt || S.dialedAt)) / 1000) : "00:00";
  $("timer-label").textContent = ready ? (S.cur && S.cur.dial_block ? "Cannot dial yet" : "Ready") : ringing ? "Ringing" : connected ? "Talking" : s === "wrap" ? "Call ended" : "";
  if (s !== "ringing") $("dial-hint").textContent = S.cur && s === "ready" ? S.cur.dial_block || "" : "";
  renderSessionLine();
  postLive();
  emit("state", s);
}
actions.setState = setState;

setInterval(() => {
  if (S.state === "ringing" && S.dialedAt) {
    const secs = (Date.now() - S.dialedAt) / 1000;
    $("timer").textContent = fmtClock(secs);
    const limit = +((S.cfg.dialing || {}).ring_no_answer_seconds || 40);
    if (secs >= limit && !$("dial-hint").dataset.nudged) {
      $("dial-hint").dataset.nudged = "1";
      $("dial-hint").textContent = "No pickup for " + limit + " seconds. Press n for no answer" + (nextDialable() >= 0 ? ", or t to try the next number." : ".");
    }
  } else if (S.state === "connected" && S.connectedAt) {
    $("timer").textContent = fmtClock((Date.now() - S.connectedAt) / 1000);
  }
  const ss = S.session;
  if (ss.on) { if (ss.paused) ss.pauseSec++; else ss.activeSec++; if (ss.activeSec % 5 === 0 || ss.paused) renderSessionLine(); }
}, 1000);

/* ----------------------------------------------------------- lead flow -- */

function loadLead(lead, opts) {
  opts = opts || {};
  cancelCountdown();
  S.cur = lead;
  S.phoneIdx = firstDialable(lead);
  S.call = freshCall(lead);
  S.dialedAt = S.connectedAt = S.endedAt = null;
  S.picked = !!opts.picked;
  renderLead();
  if (opts.notes) $("notes").value = opts.notes;
  if (opts.wrap) { S.endedAt = new Date(); setState("wrap"); openWrap("Undone. Pick the right outcome"); return; }
  closeWrap();
  setState("ready");
  $("centre").scrollTop = 0;
  if (S.session.on && !S.session.paused && !S.picked && !lead.dial_block) startCountdown();
}
actions.loadLead = loadLead;

function showEmpty(reason, hasLeads) {
  S.cur = null;
  closeWrap();
  $("lead").hidden = true; $("empty").hidden = false;
  $("e-title").textContent = S.session.paused ? "Paused: " + S.session.paused.toLowerCase() : hasLeads === false ? "No leads yet" : "Nothing in the queue right now";
  $("e-text").textContent = S.session.paused ? "Your lead went back to the queue. Callbacks and follow-ups are on the left." : reason || "";
  $("e-acts").innerHTML = S.session.paused ? '<button class="btn primary" data-act="resume">Resume <kbd>p</kbd></button>'
    : hasLeads === false ? '<button class="btn primary" data-act="import">Import a list</button><button class="btn quiet" data-act="lead">Add a lead</button>'
    : '<button class="btn" data-act="refresh">Check again</button><button class="btn quiet" data-act="list">Show the list</button>';
  setState("idle");
  emit("lead", null);
}

function nextLead() {
  if (S.session.paused) { showEmpty(); return; }
  api(withAgent("/api/next")).then((d) => {
    renderClock(d.window);
    if (d.lead) loadLead(d.lead); else showEmpty(d.reason, d.has_leads);
    refreshAll();
  }).catch(() => { showEmpty("The server is not answering. Is serve.py still running?"); });
}
actions.nextLead = nextLead;

function openLead(id) {
  if (busy()) { toast("error", "Finish this call first."); return; }
  const release = S.cur && S.state === "ready" && S.cur.id !== id ? api("/api/release", { lead_id: S.cur.id }) : Promise.resolve();
  release.then(() => api("/api/checkout", { lead_id: id })).then((d) => {
    if (d.error) { toast("error", esc(d.error)); return; }
    loadLead(d.lead, { picked: true });
    document.body.classList.remove("rail-open");
  });
}
actions.openLead = openLead;

function skip() {
  if (S.state !== "ready" || !S.cur) return;
  const id = S.cur.id;
  cancelCountdown();
  api("/api/skip", { lead_id: id }).then(nextLead);
}

function afterSave(o) {
  closeWrap();
  S.cur = null;
  const ss = S.session;
  if (ss.on) ss.dials++;
  refreshStats();
  if (ss.on && (ss.dials >= ss.target || ss.activeSec >= ss.minutes * 60)) { endSession("Target reached: " + ss.dials + " dials."); nextLead(); return; }
  if (ss.pausePending) { doPause(ss.pausePending); return; }
  if (o && o.kind === "callback" && window.Notification && Notification.permission === "default") Notification.requestPermission();
  nextLead();
}
actions.afterSave = afterSave;

/* ------------------------------------------------------------- hand-off -- */

/* Runs from the Dial link's click. The link's own default opens tel: (a
   real user gesture, so the browser allows it); QR-only mode stops it. */
function startDial(e) {
  const p = activePhone();
  if (S.state !== "ready" || !S.cur) { if (e) e.preventDefault(); return; }
  if (S.cur.dial_block || !p || p.blocked) {
    if (e) e.preventDefault();
    toast("error", esc(S.cur.dial_block || (p && p.blocked) || "No number to dial."));
    return;
  }
  if (e && !settings.openTel) e.preventDefault();
  cancelCountdown();
  S.dialedAt = new Date(); S.connectedAt = S.endedAt = null;
  $("dial-hint").textContent = ""; delete $("dial-hint").dataset.nudged;
  setState("ringing");
  api("/api/dial", { lead_id: S.cur.id, phone_id: p.id }).then((d) => {
    if (d.error) { toast("error", esc(d.error)); S.dialedAt = null; setState("ready"); }
  });
}

/* The countdown is not a click, so the browser may refuse to open tel:.
   The timer starts anyway, the QR code stays up, and the hint carries a
   real link one click away. */
function autoDial() {
  const p = activePhone();
  $("b-dial").click();
  if (S.state !== "ringing" || !p) return;
  $("dial-hint").innerHTML = 'Phone not ringing? Scan the code, or <a href="tel:' + esc(p.dial) + '">open it on the phone</a>.';
}

function connected() {
  if (S.state !== "ringing") return;
  S.connectedAt = new Date();
  $("dial-hint").textContent = "";
  setState("connected");
}

function endCall() {
  if (!onCall()) return;
  S.endedAt = new Date();
  setState("wrap");
  openWrap(S.connectedAt ? "How did it go?" : "No pickup?", S.connectedAt ? (S.call.suggest || "") : "NO_ANSWER");
}

function noAnswer() {
  if (S.state !== "ringing") return;
  S.endedAt = new Date();
  setState("wrap");
  save({ outcome: "NO_ANSWER" });
}

function tryNext() {
  if (S.state === "ready") {
    const i = nextDialable();
    if (i >= 0) { S.phoneIdx = i; renderNumber(); renderNumbers(); postLive(); }
    return;
  }
  if (S.state !== "ringing") return;
  const i = nextDialable();
  if (i < 0) { toast("error", "No other number on this lead. Press n for no answer."); return; }
  const nextId = S.cur.phones[i].id;
  S.endedAt = new Date();
  save({ outcome: "NO_ANSWER", continued: true }).then((d) => {
    if (!d) return;
    S.cur = d.lead;
    S.phoneIdx = Math.max(0, S.cur.phones.findIndex((p) => p.id === nextId));
    renderLead();
    S.dialedAt = S.connectedAt = S.endedAt = null;
    setState("ready");
    $("b-dial").click();                         // still inside the key press's user gesture
  });
}

/* ------------------------------------------------------------ power mode -- */

function startCountdown() {
  cancelCountdown();
  let left = +((S.cfg.dialing || {}).power_countdown_seconds || 5);
  const show = () => { $("timer-label").textContent = "Dialling in " + left + " s · esc holds · space dials now"; };
  show();
  countdown = setInterval(() => {
    left--;
    if (left <= 0) { cancelCountdown(); if (S.state === "ready") autoDial(); return; }
    show();
  }, 1000);
}
function cancelCountdown(hold) {
  const was = !!countdown;
  if (countdown) { clearInterval(countdown); countdown = null; }
  if (was && S.state === "ready") $("timer-label").textContent = hold ? "Held · space to dial" : "Ready";
  return was;
}
actions.cancelCountdown = cancelCountdown;

function renderSessionLine() {
  const ss = S.session;
  $("b-session").hidden = ss.on && !ss.paused;
  $("b-session").textContent = ss.paused ? "Resume" : "Start session";
  $("pause-wrap").hidden = !(ss.on && !ss.paused);
  $("b-pause").textContent = ss.pausePending ? "Pausing after this call" : "Pause";
  $("session-line").hidden = !ss.on;
  if (!ss.on) return;
  const left = Math.max(0, ss.target - ss.dials), perDial = ss.dials >= 3 ? ss.activeSec / ss.dials : 60;
  const eta = Math.min(left * perDial, Math.max(0, ss.minutes * 60 - ss.activeSec)), m = Math.round(eta / 60);
  $("session-line").innerHTML = ss.paused ? "Paused: " + esc(ss.paused.toLowerCase()) + " " + fmtClock(ss.pauseSec)
    : "<b>" + ss.dials + "</b> of " + ss.target + " dials · " + fmtClock(ss.activeSec) + " · about " + (m >= 60 ? Math.floor(m / 60) + " h " + (m % 60) + " min" : m + " min") + " left · script " + esc(ss.script);
}

let starting = false;
function startSession() {
  if (S.session.on || modal.open || starting) return;
  sessionModal().then((opts) => {
    if (!opts) return;
    starting = true;
    api("/api/session/start", opts).finally(() => { starting = false; }).then((d) => {
      Object.assign(S.session, { on: true, paused: null, pausePending: null, id: d.id, activeSec: 0, pauseSec: 0, dials: 0,
                                 target: opts.target_dials, minutes: opts.target_minutes, script: d.script_version });
      api("/api/agent-event", { event: "SESSION_START", reason: d.script_version });
      emit("session");
      renderSessionLine();
      if (S.state === "ready" && S.cur && !S.picked && !S.cur.dial_block) startCountdown();
      else if (S.state === "idle") nextLead();
    });
  });
}

function requestPause(reason) {
  closeMenus();
  if (onCall() || S.state === "wrap") { S.session.pausePending = reason; renderSessionLine(); toast("ok", "Pausing for " + esc(reason.toLowerCase()) + " after this call."); return; }
  doPause(reason);
}
function doPause(reason) {
  const ss = S.session;
  ss.paused = reason; ss.pausePending = null; ss.pauseSec = 0;
  cancelCountdown();
  api("/api/agent-event", { event: "PAUSE", reason });
  if (S.cur && S.state === "ready") api("/api/release", { lead_id: S.cur.id });
  showEmpty();
}
function resume() {
  if (!S.session.paused) return;
  api("/api/agent-event", { event: "RESUME", reason: S.session.paused });
  S.session.paused = null;
  nextLead();
}
function endSession(why) {
  closeMenus();
  const ss = S.session;
  if (!ss.on) return;
  cancelCountdown();
  const id = ss.id, active = ss.activeSec, script = ss.script, started = ss.startedAt;
  ss.on = false; ss.paused = null; ss.pausePending = null; ss.id = null;
  api("/api/session/end", { id, active_seconds: active });
  api("/api/agent-event", { event: "SESSION_END" });
  renderSessionLine();
  emit("session");
  sessionEndCard(id, why || "You ended the session.", script, started);
}

/* ------------------------------------------------------------ the clock -- */

function renderClock(w) {
  if (!w) return;
  const cls = w.tier === "power" || w.tier === "soft" ? "" : w.legal ? "warn" : "bad";
  const parts = ["<b>" + esc(w.ist) + " IST</b>", '<span class="' + cls + '">' + esc(w.label) + "</span>"];
  if (w.next_open) parts.push("cold calls open " + esc(w.next_open));
  if (w.day === "saturday" || w.day === "holiday" || w.day === "sunday") parts.push('<span class="warn">' + esc(w.day_note) + "</span>");
  if (S.data.queueCount != null) parts.push(S.data.queueCount + " to call now" + (S.data.listTotal ? " of " + S.data.listTotal + " leads" : ""));
  $("clock-line").innerHTML = parts.join(" · ");
}
actions.renderClock = renderClock;
setInterval(() => { if (!document.hidden) api("/api/window").then(renderClock).catch(() => {}); }, 60000);

/* --------------------------------------------------- phone companion (/m) -- */

let liveTimer = null;
function postLive() {
  if (!S.cfg || !S.cfg.lan || !S.cfg.lan.on) return;
  clearTimeout(liveTimer);
  liveTimer = setTimeout(() => {
    const p = S.cur && activePhone();
    api("/api/m/live", {
      state: S.state, company: S.cur ? S.cur.company : "", person: S.cur ? (S.cur.dm_name || S.cur.ask_for || "") : "",
      number: p ? prettyPhone(p.e164, p.kind) : "", dial: p && !p.blocked && S.cur && !S.cur.dial_block ? p.dial : "", kind: p ? p.kind : "",
      others: S.cur ? S.cur.phones.filter((x) => x !== p && !x.blocked).map((x) => ({ number: prettyPhone(x.e164, x.kind), dial: x.dial, kind: x.kind })) : [],
      timer_from: S.connectedAt ? S.connectedAt.toISOString() : S.dialedAt ? S.dialedAt.toISOString() : ""
    });
  }, 150);
}
on("number", postLive);

/* ------------------------------------------------- callback notifications -- */

const notified = new Set();
function checkCallbacks() {
  const mins = +((S.cfg.dialing || {}).callback_notify_minutes || 5);
  (S.data.callbacks || []).forEach((c) => {
    const at = parseUTC(c.next_action_at);
    if (!at || notified.has(c.id + c.next_action_at)) return;
    const lead = at - Date.now();
    if (lead <= mins * 60000 && lead > -30 * 60000) {
      notified.add(c.id + c.next_action_at);
      const text = c.company + (c.dm_name ? ", " + c.dm_name : "") + " at " + istWhen(at);
      toast("ok", "<b>Callback soon:</b> " + esc(text), { ms: 20000, actions: [{ html: "Open", run: () => openLead(c.id) }] });
      if (window.Notification && Notification.permission === "granted") {
        try { const n = new Notification("Callback in " + Math.max(0, Math.round(lead / 60000)) + " min", { body: text, tag: "cb" + c.id }); n.onclick = () => { window.focus(); openLead(c.id); }; } catch (e) {}
      }
    }
  });
}
on("callbacks", checkCallbacks);
setInterval(checkCallbacks, 30000);

/* ------------------------------------------------------------- keyboard -- */

document.addEventListener("keydown", (e) => {
  if (!e.target || !e.target.matches) return;
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (k === "Escape") {
    if (modal.open) { closeModal(); return; }
    if (menuOpen()) { closeMenus(); return; }
    if (e.target.matches("input, textarea, select")) { e.target.blur(); return; }
    if (objectionsOpen()) { closeObjections(); return; }
    if (level2Open()) { $("l2").hidden = true; return; }
    if (cancelCountdown(true)) return;
    document.body.classList.remove("rail-open", "script-open");
    return;
  }
  if (modal.open || e.metaKey || e.ctrlKey || e.altKey) return;
  if (level2Key(e, k)) return;
  if (e.target.matches("input, textarea, select")) return;

  if (k === " ") {
    if (e.target.closest("button, a, summary")) return;
    e.preventDefault();
    if (S.state === "ready") $("b-dial").click(); else if (onCall()) endCall();
    return;
  }
  if (S.state === "wrap") {
    if (/^[0-9]$/.test(k)) { const o = (S.cfg.outcomes || [])[k === "0" ? 9 : +k - 1]; if (o) { e.preventDefault(); pick(o.key); } return; }
    if (k === "Enter") { const s = document.querySelector(".outcome.suggested"); if (s) { e.preventDefault(); pick(s.getAttribute("data-k")); } return; }
  }
  if (k === "b" && (onCall() || S.state === "wrap")) {
    if (onCall()) { if (S.state === "ringing") connected(); endCall(); }
    return pick("DEMO_BOOKED");
  }
  if (k === "c" && S.state === "ringing") return connected();
  if (k === "n" && S.state === "ringing") return noAnswer();
  if (k === "t") return tryNext();
  if (k === "s" && S.state === "ready") return skip();
  if (k === "z") return undo();
  if (k === "?") return helpModal();
  if (k === "/") { e.preventDefault(); selectTab("queue"); document.body.classList.add("rail-open"); $("q").focus(); return; }
  if (k === "p") { if (!S.session.on) startSession(); else if (S.session.paused) resume(); else toggleMenu("pause-menu", "b-pause"); return; }
  if (scriptKey(e, k)) return;
  if (followKey(e, k)) return;
});

/* ---------------------------------------------------------------- wiring -- */

function wire() {
  $("b-dial").addEventListener("click", startDial);
  $("b-connected").addEventListener("click", connected);
  $("b-noanswer").addEventListener("click", noAnswer);
  $("b-next-number").addEventListener("click", tryNext);
  $("b-end").addEventListener("click", endCall);
  $("b-skip").addEventListener("click", skip);
  $("b-session").addEventListener("click", () => (S.session.paused ? resume() : startSession()));
  $("b-pause").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("pause-menu", "b-pause"); });
  $("pause-menu").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.hasAttribute("data-end")) endSession(); else requestPause(b.getAttribute("data-pause"));
  });
  $("b-menu").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("main-menu", "b-menu"); });
  $("b-help").addEventListener("click", helpModal);
  $("banner-x").addEventListener("click", () => { $("banner").hidden = true; });
  $("b-rail").addEventListener("click", () => { document.body.classList.remove("script-open"); document.body.classList.toggle("rail-open"); });
  $("b-script").addEventListener("click", () => { document.body.classList.remove("rail-open"); document.body.classList.toggle("script-open"); });
  $("e-acts").addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    const act = b.getAttribute("data-act");
    if (act === "resume") resume(); else if (act === "refresh") nextLead(); else if (act === "lead") actions.addLead();
    else if (act === "import") actions.importList();
    else if (act === "list") { selectTab("queue"); document.body.classList.add("rail-open"); }
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".menu-wrap")) closeMenus();
    if (e.detail > 0 && !modal.open) { const b = e.target.closest("button"); if (b) b.blur(); }
  });
  $("modal").addEventListener("click", (e) => { if (e.target === $("modal") || e.target.closest("[data-close]")) closeModal(); });
  window.addEventListener("beforeunload", (e) => {
    if (onCall()) { e.preventDefault(); e.returnValue = ""; return; }
    if (S.cur && S.state === "ready") navigator.sendBeacon("/api/release", JSON.stringify({ lead_id: S.cur.id, agent: S.agent }));
  });
  $("pause-menu").innerHTML = pauseReasons().map((r) => '<button role="menuitem" data-pause="' + esc(r) + '">' + esc(r) + "</button>").join("") +
    '<hr><button role="menuitem" data-end="1">End session</button>';
  wireEditor(); wireLead(); wireWrap(); wireSamples(); wireRails(); wireScript(); wireFunnel(); wireFollowups(); wireModals();
}

Object.assign(actions, { endSession, startSession, resume, openLead });

/* Rehearsal (INDIA_CLOCK_SHIFT_MIN on the server): move this page's clock by
   the same amount so timers, "in 2 h" and the server agree. */
function rehearsalClock(min) {
  const off = min * 60000, Real = Date;
  class Shifted extends Real {
    constructor(...a) { if (a.length) super(...a); else super(Real.now() + off); }
    static now() { return Real.now() + off; }
  }
  window.Date = Shifted;
  banner("", "<b>Rehearsal clock.</b> The time is moved " + Math.round(min) + " minutes so the calling windows can be tried. Restart the server without INDIA_CLOCK_SHIFT_MIN for real calls.");
}

fetch("/api/config").then((r) => r.json()).then((cfg) => {
  S.cfg = cfg;
  if (cfg.clock_shift_min) rehearsalClock(cfg.clock_shift_min);
  S.session.script = cfg.default_script_version || (cfg.script_versions || ["v1"])[0];
  wire();
  emit("cfg", cfg);
  nextLead();
  setInterval(() => { if (!document.hidden) { refreshStats(); refreshAll(true); } }, 60000);
}).catch(() => {
  $("e-title").textContent = "The server is not answering";
  $("e-text").textContent = "Start it with: python3 india-dialer/serve.py";
});
