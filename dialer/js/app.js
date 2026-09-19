/* Powerdialer agent cockpit: state machine, power session, calling, lead
 * pane, keyboard, boot.
 *
 * The server owns the queue, caps, retries, calling hours and DNC. This file
 * owns the agent's flow.  IDLE -> READY -> DIALING -> LIVE -> WRAP -> next.
 * With no carrier credentials on the server, calls are simulated.
 */
import { $, icon, esc, fmtPhone, fmtClock, fmtHMS, fmtTalk, parseUTC, leadClock, localHour, localWeekday, keysHTML, store } from "./util.js";
import { S, on, emit, actions, outcome, busy } from "./state.js";
import { api, withAgent } from "./api.js";
import { toast, say, banner, modal, closeModal, closeMenus, menuOpen, toggleMenu, copyText } from "./ui.js";
import { simulatorCarrier, twilioCarrier } from "./carrier.js";
import { renderScript, renderTimeline, setScriptTab, wireScript } from "./script.js";
import { renderOutcomes, setSuggested, resetWrap, wrapKey, getWrapMode, backToOutcomes, undo, wireWrap, draftKey, submitOutcome } from "./wrap.js";
import { selectTab, refreshQueue, refreshCallbacks, refreshCalls, refreshInbox, wireRails } from "./rails.js";
import { agentPicker, manualModal, shortcutsModal, logModal, settingsModal, applyTheme, pickList, uploadList } from "./modals.js";

let carrier = simulatorCarrier();
let activeCall = null, incoming = null;
let idleTimer = null;
const cd = { left: 0, timer: null };

/* ================================================================ stats */

function renderStats(s) {
  if (!s) return;
  S.stats = s;
  const mine = s.my_dials == null ? s.dials_today : s.my_dials;
  const conn = s.my_connects == null ? s.connects_today : s.my_connects;
  $("k-dials").innerHTML = s.dials_today + "<small>/ " + s.cap + "</small>";
  $("k-dials").className = s.dials_today >= s.cap ? "over" : "";
  $("k-dials").title = "Dials on this caller ID today. Cap " + s.cap + ".";
  $("k-goal").style.width = Math.min(100, mine / (S.cfg.daily_goal || 100) * 100) + "%";
  $("k-conn").innerHTML = conn + "<small>" + (mine ? Math.round(conn / mine * 100) + "%" : "-") + "</small>";
  $("k-int").textContent = (s.outcomes && s.outcomes.INTERESTED) || s.interested_today || 0;
  $("k-talk").textContent = fmtTalk(s.talk_seconds);
  let pace = "-";
  if (s.first_dial_at && s.server_now) {
    const hrs = (parseUTC(s.server_now) - parseUTC(s.first_dial_at)) / 3600000;
    if (hrs >= 0.1) pace = Math.round(mine / hrs);
  }
  $("k-pace").innerHTML = pace + "<small>/hr</small>";
  const late = s.callbacks_overdue || 0;
  $("k-cb").innerHTML = (late || s.callbacks_due) + "<small>" + (late ? "overdue" : "due") + "</small>";
  $("k-cb").className = late ? "alert" : "";
  $("n-queue").textContent = s.queue == null ? "" : s.queue;
  $("n-calls").textContent = mine || "";
  if (s.dials_today >= s.cap) {
    banner("danger", "<b>Daily cap reached on this number.</b> The queue is closed until tomorrow. " +
           "Pushing past " + s.cap + " dials a day is how numbers get labelled as spam.");
  }
  emit("stats", s);
}

/* ================================================================= lead */

function inHard(date, off) {
  const w = S.cfg.windows.hard, h = localHour(date, off), d = localWeekday(date, off);
  if (S.cfg.windows.weekdays_only && (d === 0 || d === 6)) return false;
  return h >= w[0] && h < w[1];
}

function renderLocal() {
  const l = S.cur;
  if (!l) return;
  const now = new Date(), open = !S.cfg.windows.enforced || inHard(now, l.tz_offset);
  const place = l.city ? l.city + (l.state ? ", " + l.state : "") : (l.state || "");
  $("c-local").className = "localtime" + (open ? "" : " closed");
  $("c-local").innerHTML = icon(open ? "clock" : "alert", "sm") + "<b>" + leadClock(now, l.tz_offset) + "</b>" +
    (place ? "<span>" + esc(place) + "</span>" : "<span>their time</span>") + (open ? "" : "<span>· outside calling hours</span>");
}

function renderLead() {
  const l = S.cur;
  $("lead").hidden = !l;
  $("empty").hidden = !!l;
  if (!l) { renderScript(); renderTimeline(); return; }

  const tags = [];
  if (S.inbound) tags.push('<span class="pill good">' + icon("phone", "sm") + "Inbound</span>");
  if (l.list_id === "manual") tags.push('<span class="pill">Manual dial</span>');
  if (l.rank) tags.push('<span class="pill' + (l.rank >= 80 ? " good" : "") + '">Rank <b>' + esc(l.rank) + "</b></span>");
  tags.push(l.attempts ? '<span class="pill">Attempt <b>' + (l.attempts + 1) + "</b> of " + S.cfg.max_attempts + "</span>"
                       : '<span class="pill info">Fresh</span>');
  if (l.callback_at) tags.push('<span class="pill warn">' + icon("calendar", "sm") + "Callback due</span>");
  if (l.last_disposition) tags.push('<span class="pill">Last: <b>' + esc(outcome(l.last_disposition).label) + "</b></span>");
  $("c-tags").innerHTML = tags.join("");

  $("c-company").textContent = l.co || "Unknown company";
  const name = ((l.first || "") + " " + (l.last || "")).trim();
  $("c-person").innerHTML = (name ? "<b>" + esc(name) + "</b>" : "") + (l.title ? (name ? " · " : "") + esc(l.title) : "") ||
                            '<span class="muted">No contact name on file</span>';
  $("c-phone").textContent = fmtPhone(l.phone);
  renderLocal();

  const chips = [];
  if (l.process) chips.push('<span class="pill info">' + icon("factory", "sm") + "<b>" + esc(l.process) + "</b></span>");
  if (l.oem) chips.push('<span class="pill">Supplies <b>' + esc(l.oem) + "</b></span>");
  if (l.size) chips.push('<span class="pill"><b>' + esc(l.size) + "</b> employees</span>");
  if (l.li_status === "accepted") chips.push('<span class="pill good">LinkedIn accepted</span>');
  $("c-chips").innerHTML = chips.join("");
  $("c-chips").hidden = !chips.length;

  const q = encodeURIComponent, links = [];
  if (l.co) {
    if (l.website) links.push(["Website", /^https?:/.test(l.website) ? l.website : "https://" + l.website]);
    links.push(["Google", "https://www.google.com/search?q=" + q(l.co + " PPAP")]);
    links.push(["LinkedIn", "https://www.linkedin.com/search/results/all/?keywords=" + q((name + " " + l.co).trim())]);
  }
  $("c-links").innerHTML = links.map((x) =>
    '<a class="btn sm quiet" target="_blank" rel="noopener noreferrer" href="' + esc(x[1]) + '">' + esc(x[0]) + icon("external", "sm") + "</a>").join("");
  $("c-links").hidden = !links.length;

  const last = (l.history || []).find((h) => h.notes);
  $("c-note").hidden = !last;
  if (last) {
    $("c-note").innerHTML = "<div><q>" + esc(last.notes) + '</q><span class="by">' + esc(last.agent || "agent") +
      " · " + esc(outcome(last.disposition).label) + " · " + esc((last.at || "").slice(0, 10)) + "</span></div>";
  }
  emit("lead", l);
}

function renderEmpty(kind, text) {
  S.cur = null;
  renderLead();
  emit("lead", null);
  let glyph = "inbox", title = "No lead to dial", acts = "";
  if (kind === "paused") {
    glyph = "coffee"; title = "Paused" + (S.session.paused ? " · " + S.session.paused : "");
    text = "Your lead went back to the queue. Inbound calls still ring here. Callbacks and the inbox stay open on the left.";
    acts = '<button class="btn primary" data-act="resume">' + icon("play") + 'Resume <kbd class="onfill">p</kbd></button>';
  } else if (kind === "connecting") {
    glyph = "bolt"; title = "Connecting";
  } else {
    acts = '<button class="btn" data-act="load">' + icon("upload") + "Load a list</button>" +
           '<button class="btn" data-act="manual">' + icon("keypad") + "Dial a number</button>" +
           '<button class="btn quiet" data-act="refresh">Check again</button>';
  }
  $("e-glyph").innerHTML = icon(glyph, "lg");
  $("e-title").textContent = title;
  $("e-text").textContent = text || "";
  $("e-acts").innerHTML = acts;
}

/* ======================================================== state machine */

const lamp = (text) => { $("lamp-text").textContent = text; };

function setState(s) {
  S.state = s;
  const inCall = s === "LIVE", ringing = s === "DIALING", wrap = s === "WRAP", ready = s === "READY";
  document.body.setAttribute("data-state",
    S.session.paused && s === "IDLE" ? "paused" : { IDLE: "idle", READY: "ready", DIALING: "dialing", LIVE: "live", WRAP: "wrap" }[s]);

  $("b-dial").hidden = inCall || ringing;
  $("b-hangup").hidden = !(inCall || ringing);
  $("b-dial").disabled = !ready;
  $("b-skip").disabled = !ready;
  $("b-mute").disabled = !inCall;
  $("b-keypad").disabled = !inCall;
  $("b-vmdrop").disabled = !inCall || S.inbound;
  if (!inCall) { setMuted(false); toggleKeypad(false); $("quality").hidden = true; }
  $("rec").hidden = !(inCall && S.cfg && S.cfg.recording);

  $("wrap").hidden = !wrap;
  if (!wrap) resetWrap();

  $("cb-label").textContent = { IDLE: S.session.paused ? "Paused" : "Standing by", READY: "Ready", DIALING: "Dialing", LIVE: "On call", WRAP: "Wrap-up" }[s];
  $("cb-timer").className = "timer num" + (inCall || ringing || wrap ? "" : " dim");
  if (s === "IDLE" || s === "READY") $("cb-timer").textContent = "00:00";
  lamp(S.session.paused && s === "IDLE" ? "Paused" : { IDLE: "Idle", READY: "Ready", DIALING: "Dialing", LIVE: "On a call", WRAP: "Wrap-up" }[s]);
  renderSession();
  emit("state", s);
}

/* One clock drives every counter. */
setInterval(() => {
  if (S.state === "DIALING") { S.ringSec++; $("cb-timer").textContent = fmtClock(S.ringSec); }
  else if (S.state === "LIVE") { S.callSec++; $("cb-timer").textContent = fmtClock(S.callSec); }
  else if (S.state === "WRAP") {
    S.wrapSec++; $("cb-timer").textContent = fmtClock(S.wrapSec);
    $("cb-label").textContent = "Wrap-up" + (S.callSec ? " · call " + fmtClock(S.callSec) : "");
  }
  const ss = S.session;
  if (ss.on) {
    if (ss.paused) ss.pauseSec++; else ss.activeSec++;
    $("session-clock").textContent = ss.paused ? "paused " + fmtClock(ss.pauseSec) : fmtHMS(ss.activeSec);
  }
  emit("tick");
  if (new Date().getSeconds() === 0) { renderLocal(); emit("minute"); }
}, 1000);

/* ============================================================== session */

function renderSession() {
  const ss = S.session, b = $("b-session");
  b.hidden = ss.on && !ss.paused;
  b.textContent = ss.paused ? "Resume" : "Start session";
  b.title = ss.paused ? "Resume dialing (p)" : "Auto-dial through the queue (p)";
  $("pause-wrap").hidden = !(ss.on && !ss.paused);
  $("b-pause").innerHTML = (ss.pausePending ? "Pausing after call" : "Pause") + icon("chevron", "sm");
  $("session-clock").hidden = !ss.on;
}

const agentEvent = (event, reason) => api("/api/agent-event", { agent: S.agent, event, reason: reason || "" });

function autodialDelay() {
  const v = S.settings.delay !== "" ? Number(S.settings.delay) : Number(S.cfg.autodial_delay_sec);
  return isFinite(v) ? v : 3;
}

function startSession() {
  const ss = S.session;
  if (ss.on) return;
  ss.on = true; ss.paused = null; ss.pausePending = null; ss.activeSec = 0;
  agentEvent("SESSION_START");
  say("Session started: leads dial themselves after wrap-up");
  toast("info", "<b>Session on.</b> Each lead dials itself " + autodialDelay() + "s after it loads. <kbd>esc</kbd> holds one.");
  renderSession();
  if (S.state === "READY" && S.cur && !S.picked) startCountdown();
  else if (S.state === "IDLE") nextLead();
}

function requestPause(reason) {
  closeMenus();
  if (!S.session.on) return;
  if (busy()) {
    S.session.pausePending = reason;
    toast("info", "Pausing for <b>" + esc(reason) + "</b> once this call is wrapped up.");
    renderSession();
    return;
  }
  doPause(reason);
}

function doPause(reason) {
  const ss = S.session;
  ss.paused = reason; ss.pausePending = null; ss.pauseSec = 0;
  cancelCountdown();
  agentEvent("PAUSE", reason);
  say("Paused: " + esc(reason));
  if (S.cur && S.state === "READY") api("/api/release", { phone: S.cur.phone });
  clearTimeout(idleTimer);
  setState("IDLE");
  renderEmpty("paused");
  refreshQueue();
}

function resume() {
  const ss = S.session;
  if (!ss.paused) return;
  agentEvent("RESUME", ss.paused);
  say("Resumed after " + fmtClock(ss.pauseSec));
  ss.paused = null;
  renderSession();
  if (S.state === "IDLE") nextLead(); else setState(S.state);
}

function endSession() {
  closeMenus();
  const ss = S.session;
  if (!ss.on) return;
  cancelCountdown();
  agentEvent("SESSION_END");
  say("Session ended after " + fmtHMS(ss.activeSec));
  toast("info", "Session ended: " + fmtHMS(ss.activeSec) + " active. Dial by hand with <kbd>space</kbd>.");
  ss.on = false; ss.paused = null; ss.pausePending = null;
  setState(S.state);
}

function startCountdown() {
  cancelCountdown();
  const secs = autodialDelay();
  if (!S.cur || S.state !== "READY") return;
  if (S.cfg.windows.enforced && !inHard(new Date(), S.cur.tz_offset)) return;
  if (secs <= 0) { dial(); return; }
  cd.left = secs;
  $("cd-n").textContent = secs;
  $("cb-state").hidden = true;
  $("countdown").hidden = false;
  const ring = $("ring");
  ring.classList.remove("run"); void ring.offsetWidth;
  ring.style.setProperty("--secs", secs + "s");
  ring.classList.add("run");
  cd.timer = setInterval(() => {
    cd.left--;
    $("cd-n").textContent = Math.max(cd.left, 0);
    if (cd.left <= 0) { cancelCountdown(); dial(); }
  }, 1000);
}

function cancelCountdown(hold) {
  const was = !!cd.timer;
  if (cd.timer) { clearInterval(cd.timer); cd.timer = null; }
  $("countdown").hidden = true;
  $("cb-state").hidden = false;
  $("ring").classList.remove("run");
  if (hold && was) $("cb-label").textContent = "Held · space to dial";
  return was;
}

/* ================================================================ queue */

function loadLead(lead, opts) {
  opts = opts || {};
  clearTimeout(idleTimer);
  S.cur = lead; S.picked = !!opts.handPicked; S.inbound = !!opts.inbound;
  S.callSec = 0; S.ringSec = 0; S.call = {};
  setSuggested("");
  $("notes").value = opts.notes != null ? opts.notes : store.get(draftKey(lead.phone), "");
  $("notes-saved").textContent = opts.notes == null && $("notes").value ? "Draft restored" : "";
  renderLead();
  if (opts.state) { setState(opts.state); return; }
  setState("READY");
  $("lead-pane").scrollTop = 0;
  if (S.session.on && !S.session.paused && !S.picked) startCountdown();
}

function nextLead() {
  if (S.session.paused) { setState("IDLE"); renderEmpty("paused"); return; }
  clearTimeout(idleTimer);
  api(withAgent("/api/next")).then((d) => {
    if (d.caller_id) $("cid").textContent = d.caller_id;
    renderStats(d.stats);
    if (d.lead) {
      loadLead(d.lead);
    } else {
      setState("IDLE");
      renderEmpty("empty", d.reason || d.error || "Queue empty.");
      say(esc(d.reason || "Queue empty"));
      idleTimer = setTimeout(() => { if (S.state === "IDLE" && !S.session.paused) nextLead(); }, 120000);
    }
    refreshQueue();
  }).catch(() => {
    toast("error", "<b>The server is unreachable.</b> Trying again in 15 seconds.");
    setState("IDLE"); renderEmpty("empty", "Server unreachable.");
    idleTimer = setTimeout(nextLead, 15000);
  });
}

/* Hand-pick a lead: queue row, callback, call log, inbox, typed number. */
function openLead(phone, route) {
  if (busy()) { toast("warn", "Finish this call first, then open that lead."); return Promise.resolve(false); }
  cancelCountdown();
  const release = S.cur && S.state === "READY" && S.cur.phone !== phone ? api("/api/release", { phone: S.cur.phone }) : Promise.resolve();
  return release.then(() => api(route || "/api/checkout", { phone, agent: S.agent })).then((d) => {
    if (d.error || !d.lead) { toast("error", esc(d.error || "Could not open that lead.")); if (!S.cur) nextLead(); return false; }
    renderStats(d.stats);
    loadLead(d.lead, { handPicked: true });
    document.body.classList.remove("rail-open");
    if (d.tz_known === false) toast("warn", "Could not work out the local time for that area code. Check it is a sensible hour before dialing.");
    refreshQueue();
    return true;
  }).catch(() => { toast("error", "The server is unreachable."); return false; });
}

function skip() {
  if (S.state !== "READY" || !S.cur) return;
  cancelCountdown();
  say("Skipped " + esc(S.cur.co));
  const p = api("/api/skip", { phone: S.cur.phone, agent: S.agent });
  store.del(draftKey(S.cur.phone));
  S.cur = null;
  p.then(nextLead, nextLead);
}

function afterSave() {
  S.cur = null; S.inbound = false; $("notes").value = ""; $("notes-saved").textContent = "";
  const ss = S.session;
  if (ss.pausePending) doPause(ss.pausePending);
  else if (ss.paused) { setState("IDLE"); renderEmpty("paused"); }
  else { setState("IDLE"); nextLead(); }
}

/* ============================================================== calling */

const QUALITY = { "high-rtt": "High latency", "high-jitter": "Choppy audio", "high-packet-loss": "Dropping audio",
                  "low-mos": "Poor call quality", "constant-audio-input-level": "Mic looks silent",
                  "constant-audio-output-level": "No audio coming in", "ice-connectivity-lost": "Connection lost, retrying" };

function simMode() {
  carrier = simulatorCarrier(); S.live = false;
  $("mode-pill").hidden = false;
  $("mode-pill").innerHTML = icon("info", "sm") + "Simulator";
  $("mode-pill").title = "No carrier credentials on the server. Calls are simulated, outcomes are saved for real.";
}

function applyAudio() {
  if (S.settings.mic) carrier.audio.setInput(S.settings.mic);
  if (S.settings.speaker && carrier.audio.outputSupported) carrier.audio.setOutput(S.settings.speaker);
}

function attachCarrier(token) {
  twilioCarrier(token, {
    onError: (msg) => toast("error", "Carrier: " + esc(msg)),
    onTokenExpiring: () => api(withAgent("/api/token")).then((d) => { if (d.token) carrier.refresh(d.token); }),
    onReady: applyAudio,
    onIncoming
  }).then((c) => {
    carrier = c; S.live = true;
    $("mode-pill").hidden = true;
    say("Carrier connected (" + c.name + "): <b>real calls enabled</b>, inbound rings here");
  }).catch((e) => { toast("error", esc(e.message) + ". Running as a simulator."); simMode(); });
}

function wireCall(call) {
  activeCall = call;
  call.on("ringing", () => { if (S.state === "DIALING") { $("cb-label").textContent = "Ringing"; lamp("Ringing"); } });
  call.on("answered", () => { if (S.state === "DIALING" || S.inbound) onAnswered(); });
  call.on("ended", (reason) => { activeCall = null; if (S.state === "LIVE" || S.state === "DIALING") onEnded(reason); });
  call.on("error", (msg) => toast("error", "Call error: " + esc(msg || "unknown")));
  call.on("warning", (name) => { $("quality").hidden = false; $("quality-t").textContent = QUALITY[name] || "Poor connection"; });
  call.on("warning-cleared", () => { $("quality").hidden = true; });
}

function dial() {
  if (S.state !== "READY" || !S.cur) return;
  cancelCountdown();
  if (S.cfg.windows.enforced && !inHard(new Date(), S.cur.tz_offset)) {
    toast("error", "It is <b>" + leadClock(new Date(), S.cur.tz_offset) + "</b> for this lead, outside calling hours. Skip it or set a callback.");
    return;
  }
  S.inbound = false; S.callSec = 0; S.ringSec = 0;
  setState("DIALING");
  say("Dialing <b>" + esc(S.cur.co || fmtPhone(S.cur.phone)) + "</b>");
  const from = (S.cur.caller_id || $("cid").textContent).replace(/[^+\d]/g, "");
  carrier.connect(S.cur.phone, from).then(wireCall).catch((e) => {
    const denied = /permission|denied|NotAllowed/i.test((e && e.message) || "");
    toast("error", denied ? "<b>Microphone blocked.</b> Allow mic access for this site, then dial again."
                          : "Could not start the call: " + esc(e && e.message));
    setState("READY");
  });
}

function hangup() {
  if (activeCall) activeCall.hangup();
  else if (S.state === "DIALING") onEnded("no_answer");
  else if (S.state === "LIVE") onEnded("hangup");
}

function onAnswered() {
  S.callSec = 0;
  setState("LIVE");
  say("Connected: <b>" + esc(S.cur ? (S.cur.co || fmtPhone(S.cur.phone)) : "caller") + "</b>");
  emit("call-answered");
}

function onEnded(reason) {
  setSuggested(reason === "voicemail" ? "VOICEMAIL" : reason === "no_answer" ? "NO_ANSWER" : "");
  S.wrapSec = 0;
  setState("WRAP");
  $("wrap-title").textContent = reason === "voicemail" ? "Reached voicemail" : reason === "no_answer" ? "No answer" : "How did it go?";
  renderOutcomes();
  say(reason === "hangup" ? "Call ended: " + fmtClock(S.callSec) : reason === "voicemail" ? "Answering machine" : "No answer");
  emit("call-ended", reason);
  requestAnimationFrame(() => $("wrap").scrollIntoView({ block: "nearest", behavior: "smooth" }));
}

function setMuted(v) {
  S.muted = v;
  if (activeCall) activeCall.mute(v);
  $("b-mute").classList.toggle("on", v);
  $("b-mute").setAttribute("aria-pressed", v ? "true" : "false");
  const svg = $("b-mute").querySelector("svg");
  if (svg) svg.outerHTML = icon(v ? "micoff" : "mic");
  $("mute-t").textContent = v ? "Unmute" : "Mute";
}

function vmDrop() {
  if (S.state !== "LIVE" || !S.cur || S.inbound) return;
  const sid = activeCall && activeCall.sid();
  if (!carrier.real || !activeCall) {                      // simulator
    say("Dropped voicemail (simulated)");
    const c = activeCall; activeCall = null; if (c) c.hangup();
    submitOutcome("VOICEMAIL", null);
    return;
  }
  if (!sid) { toast("warn", "No call id yet. Try again in a second."); return; }
  $("b-vmdrop").disabled = true;
  api("/api/vmdrop", { call_sid: sid }).then((d) => {
    if (d.ok || d.sim) {
      say("Dropped voicemail message");
      const c = activeCall; activeCall = null; if (c) c.hangup();
      submitOutcome("VOICEMAIL", null);             // message is playing; move on
    } else {
      $("b-vmdrop").disabled = false;
      toast("error", "Voicemail drop failed: " + esc(d.error || "unknown"));
    }
  });
}

/* ---- keypad ------------------------------------------------------------- */

function toggleKeypad(force) {
  const show = force == null ? $("keypad").hidden : force;
  if (show && S.state !== "LIVE") return;
  $("keypad").hidden = !show;
  $("b-keypad").setAttribute("aria-expanded", show ? "true" : "false");
  if (show) $("keypad-out").textContent = "";
}
function sendTone(d) {
  if (S.state !== "LIVE") return;
  if (activeCall) activeCall.digits(d);
  $("keypad-out").textContent = ($("keypad-out").textContent + d).slice(-18);
}

/* ============================================================== inbound */

function onIncoming(call) {
  if (S.state === "LIVE" || S.state === "DIALING") { call.reject(); return; }
  cancelCountdown(true);
  incoming = call;
  $("in-co").textContent = fmtPhone(call.from) || "Unknown caller";
  $("in-who").textContent = "Looking up";
  $("incoming").hidden = false;
  $("b-accept").focus();
  say("Incoming call from <b>" + esc(fmtPhone(call.from)) + "</b>");
  if (S.settings.notify && document.hidden && window.Notification && Notification.permission === "granted") {
    try { new Notification("Incoming call", { body: fmtPhone(call.from), tag: "pd-incoming" }); } catch (e) {}
  }
  api("/api/lookup?phone=" + encodeURIComponent(call.from)).then((d) => {
    if (incoming !== call) return;
    if (d.lead) {
      $("in-co").textContent = d.lead.co || fmtPhone(call.from);
      $("in-who").textContent = [((d.lead.first || "") + " " + (d.lead.last || "")).trim(), d.lead.title,
        d.lead.last_disposition ? "last: " + outcome(d.lead.last_disposition).label : ""].filter(Boolean).join(" · ");
      call.lead = d.lead;
    } else {
      $("in-who").textContent = fmtPhone(call.from) + " is not in the lead list.";
    }
    emit("incoming", call);
  });
  call.onCancel(() => {
    if (incoming !== call) return;
    $("incoming").hidden = true; incoming = null;
    toast("warn", "Missed call from <b>" + esc(fmtPhone(call.from)) + "</b>. It is in the Inbox.");
    setTimeout(refreshInbox, 65000);
  });
}

function acceptIncoming() {
  const call = incoming;
  if (!call) return;
  $("incoming").hidden = true;
  incoming = null;
  if (S.cur && S.state === "READY") api("/api/release", { phone: S.cur.phone });
  const lead = call.lead || { phone: call.from, co: "", first: "", last: "", title: "", city: "", state: "",
                              size: "", rank: 0, attempts: 0, history: [], tz_offset: -5, list_id: "" };
  loadLead(lead, { handPicked: true, inbound: true, state: "READY" });
  wireCall(call.accept());
  onAnswered();
  emit("inbound-answered", lead);
}

function declineIncoming() {
  if (incoming) incoming.reject();
  $("incoming").hidden = true;
  incoming = null;
}

/* ============================================================= keyboard */

function copyNumber() {
  if (S.cur) copyText(S.cur.phone, "Copied <b>" + esc(fmtPhone(S.cur.phone)) + "</b>");
}

document.addEventListener("keydown", (e) => {
  if (!e.target || !e.target.matches) return;
  if (e.key === "Escape") {
    if (!$("incoming").hidden) return;
    if (modal.open) { closeModal(); return; }
    if (menuOpen()) { closeMenus(); return; }
    if (!$("keypad").hidden) { toggleKeypad(false); return; }
    if (e.target.matches("input, textarea")) { e.target.blur(); return; }
    if (getWrapMode()) { backToOutcomes(); return; }
    if (cancelCountdown(true)) { e.preventDefault(); return; }
    document.body.classList.remove("rail-open");
    return;
  }
  if (modal.open || !$("incoming").hidden) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target.matches("input, textarea, select")) return;
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;

  if (getWrapMode() && wrapKey(e, k)) return;

  if (e.code === "Space" || e.key === " ") {
    if (e.target.closest("button, a, summary")) return;
    e.preventDefault();
    if (S.state === "READY") dial(); else if (S.state === "LIVE" || S.state === "DIALING") hangup();
    return;
  }
  if (S.state === "LIVE" && /^[0-9*#]$/.test(e.key)) { sendTone(e.key); return; }
  if (wrapKey(e, k)) return;

  if (k === "m" && S.state === "LIVE") setMuted(!S.muted);
  else if (k === "k" && S.state === "LIVE") toggleKeypad();
  else if (k === "v" && S.state === "LIVE") vmDrop();
  else if (k === "s" && S.state === "READY") skip();
  else if (k === "n" && S.cur) { e.preventDefault(); $("notes").focus(); }
  else if (k === "/") { e.preventDefault(); selectTab("queue"); document.body.classList.add("rail-open"); $("q").focus(); }
  else if (k === "d") { e.preventDefault(); manualModal(); }
  else if (k === "c" && S.cur) copyNumber();
  else if (k === "o" && S.cur) setScriptTab("objections");
  else if (k === "z") undo();
  else if (k === "?") shortcutsModal();
  else if (k === "p") {
    if (!S.session.on) startSession(); else if (S.session.paused) resume(); else toggleMenu("pause-menu", "b-pause");
  }
});

/* =============================================================== wiring */

function hydrateIcons() {
  document.querySelectorAll("[data-ic]").forEach((n) => { n.outerHTML = icon(n.getAttribute("data-ic"), n.getAttribute("data-cls") || ""); });
  $("b-rail").innerHTML = icon("list");
  $("b-keys").innerHTML = icon("keyboard");
  $("b-manual").innerHTML = icon("keypad");
  $("b-copy").innerHTML = icon("copy", "sm");
  $("brand-mark").innerHTML = icon("bolt", "sm");
  $("keys").innerHTML = keysHTML("data-tone");
}

function renderPauseMenu() {
  $("pause-menu").innerHTML = (S.cfg.pause_reasons || ["Break"]).map((r) =>
    '<button role="menuitem" data-pause="' + esc(r) + '">' + icon("coffee") + esc(r) + "</button>").join("") +
    '<hr><button role="menuitem" data-end="1">' + icon("x") + "End session</button>";
}

function renderAgent() {
  const n = S.agentName || S.agent;
  $("agent-ini").textContent = (n || "?").trim().slice(0, 1).toUpperCase();
  $("agent-name").textContent = n;
  $("am-name").textContent = n;
  $("am-seat").textContent = "Seat " + S.agent;
}

function wire() {
  $("b-dial").addEventListener("click", dial);
  $("b-hangup").addEventListener("click", hangup);
  $("b-mute").addEventListener("click", () => setMuted(!S.muted));
  $("b-keypad").addEventListener("click", () => toggleKeypad());
  $("b-vmdrop").addEventListener("click", vmDrop);
  $("b-skip").addEventListener("click", skip);
  $("b-copy").addEventListener("click", copyNumber);
  $("b-accept").addEventListener("click", acceptIncoming);
  $("b-decline").addEventListener("click", declineIncoming);
  $("b-manual").addEventListener("click", manualModal);
  $("b-keys").addEventListener("click", shortcutsModal);
  $("b-rail").addEventListener("click", () => document.body.classList.toggle("rail-open"));
  $("k-cb-btn").addEventListener("click", () => { selectTab("callbacks"); document.body.classList.add("rail-open"); });
  $("keys").addEventListener("click", (e) => { const b = e.target.closest("[data-tone]"); if (b) sendTone(b.getAttribute("data-tone")); });
  $("countdown").addEventListener("click", () => cancelCountdown(true));

  $("b-session").addEventListener("click", () => { if (S.session.paused) resume(); else startSession(); });
  $("b-pause").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("pause-menu", "b-pause"); });
  $("pause-menu").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.hasAttribute("data-end")) endSession(); else requestPause(b.getAttribute("data-pause"));
  });
  $("b-agent").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("agent-menu", "b-agent"); });
  $("agent-menu").addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    closeMenus();
    if (!b) return;
    const act = b.getAttribute("data-act");
    if (act === "switch") {
      if (busy()) toast("warn", "Finish this call before switching agent.");
      else agentPicker(false).then(() => { renderAgent(); renderScript(); });
    } else if (act === "load") pickList();
    else if (act === "log") logModal();
    else if (act === "keys") shortcutsModal();
    else if (act === "settings") settingsModal();
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".menu-wrap")) closeMenus();
    if (!$("keypad").hidden && !e.target.closest("#keypad, #b-keypad")) toggleKeypad(false);
  });

  $("e-acts").addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    const act = b.getAttribute("data-act");
    if (act === "resume") resume(); else if (act === "load") pickList();
    else if (act === "manual") manualModal(); else if (act === "refresh") nextLead();
  });
  $("f-list").addEventListener("change", function () { if (this.files[0]) uploadList(this.files[0]); this.value = ""; });

  let draftTimer;
  $("notes").addEventListener("input", () => {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => {
      if (!S.cur) return;
      const v = $("notes").value;
      if (v) store.set(draftKey(S.cur.phone), v); else store.del(draftKey(S.cur.phone));
      $("notes-saved").textContent = v ? "Draft saved" : "";
    }, 350);
  });

  $("modal").addEventListener("click", (e) => { if (e.target === $("modal") || e.target.closest("[data-close]")) closeModal(); });

  window.addEventListener("beforeunload", (e) => {
    if (S.state === "LIVE" || S.state === "DIALING") { e.preventDefault(); e.returnValue = ""; return; }
    if (S.cur && S.state === "READY") navigator.sendBeacon("/api/release", JSON.stringify({ phone: S.cur.phone }));
  });
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", applyTheme);

  wireScript(); wireWrap(); wireRails();
}

Object.assign(actions, {
  openLead, nextLead, loadLead, renderStats, afterSave, cancelCountdown, refreshQueue,
  autodialDelay, applyAudio, carrier: () => carrier, dial, hangup,
  submitOutcome
});

/* ================================================================= boot */

function boot() {
  hydrateIcons();
  S.cfg = { windows: { hard: [8, 21] }, outcomes: [], scripts: {}, max_attempts: 5 };
  wire();
  setState("IDLE");
  renderEmpty("connecting", "");

  fetch("/api/config").then((r) => { if (!r.ok) throw new Error(); return r.json(); }).then((c) => {
    if (!c || !c.outcomes) throw new Error();
    S.cfg = c;
    $("cid").textContent = c.caller_id || "-";
    renderPauseMenu();
    if (!S.agent || !S.agentName) {
      const known = (c.agents || []).find((a) => a.id === S.agent);
      if (S.agent && known) { S.agentName = known.name; store.set("pd_agent_name", S.agentName); }
      else return agentPicker(true);
    }
  }).then(() => {
    renderAgent();
    say("Signed in as " + esc(S.agentName) + " (" + esc(S.agent) + ")");
    emit("cfg", S.cfg);
    if (S.cfg.live) api(withAgent("/api/token")).then((d) => { if (d && d.token) attachCarrier(d.token); else simMode(); }).catch(simMode);
    else simMode();
    nextLead();
    refreshCallbacks(); refreshCalls(); refreshInbox();
    setInterval(() => {
      if (document.hidden) return;
      api(withAgent("/api/stats")).then(renderStats).catch(() => {});
      refreshCallbacks();
    }, 60000);
    setInterval(() => { if (!document.hidden) refreshInbox(); }, 300000);
  }).catch(() => {
    renderEmpty("empty", "The dialer server is not answering. Start it with: python3.12 dialer/serve.py");
    banner("danger", "<b>Server unreachable.</b> Nothing can be dialed or saved until it is back.");
  });
}

boot();
