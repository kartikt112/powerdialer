/* Powerdialer agent cockpit: state machine, power session, calling, lead
 * pane, keyboard, boot.
 *
 * The server owns the queue, caps, retries, calling hours and DNC. This file
 * owns the agent's flow.  IDLE -> READY -> DIALING -> LIVE -> WRAP -> next.
 * With no carrier credentials on the server, calls are simulated.
 */
import { $, icon, esc, fmtPhone, fmtClock, fmtHMS, toServer, leadClock, localHour, localWeekday, keysHTML, store } from "./util.js";
import { S, on, emit, actions, outcome, busy } from "./state.js";
import { api, withAgent } from "./api.js";
import { toast, say, banner, modal, closeModal, closeMenus, menuOpen, toggleMenu, copyText } from "./ui.js";
import { simulatorCarrier, twilioCarrier } from "./carrier.js";
import { renderScript, renderTimeline, wireScript, scriptKey, openObjections, closeObjections, objectionsOpen, setFlow, emailFor, copyEmail, currentStepId, currentVersion } from "./script.js";
import { openScriptEditor } from "./editor.js";
import { renderOutcomes, setSuggested, resetWrap, wrapKey, getWrapMode, backToOutcomes, undo, wireWrap, draftKey, submitOutcome, freshCall } from "./wrap.js";
import { refreshStats, statsSheet, wireFunnel } from "./funnel.js";
import { sessionStartModal, sessionEndCard } from "./session.js";
import { selectTab, refreshQueue, refreshCallbacks, refreshCalls, refreshInbox, refreshBookings, refreshNumbers, wireRails } from "./rails.js";
import { agentPicker, manualModal, shortcutsModal, logModal, settingsModal, applyTheme } from "./modals.js";
import { wireCampaigns, loadListModal, manageModal, deleteLeads, refreshTarget } from "./campaigns.js";
import { openAnalytics } from "./analytics.js";
import { wirePipeline } from "./pipeline.js";

let carrier = simulatorCarrier();
let activeCall = null, incoming = null;
let idleTimer = null;
const cd = { left: 0, timer: null };

/* ================================================================ stats */

function renderStats(s) {
  if (!s) return;
  S.stats = s;
  renderCap();
  renderWindowLine(s.window);
  const late = s.callbacks_overdue || 0, due = s.callbacks_due || 0;
  $("k-cb-btn").hidden = !(late || due);
  $("k-cb-btn").className = "btn quiet" + (late ? " alert" : "");
  $("k-cb").textContent = late ? late + " callback" + (late > 1 ? "s" : "") + " overdue" : due + " callback" + (due > 1 ? "s" : "") + " due";
  $("h-queue").textContent = s.queue == null ? "" : s.queue;
  $("h-calls").textContent = (s.today && s.today.dials) || "";
  const caps = s.caps || [], open = caps.filter((c) => c.used < c.cap && (s.parked || []).indexOf(c.number) < 0);
  if (caps.length && !open.length) {
    banner("danger", "<b>Every caller ID is capped or parked for today.</b> The queue is closed until tomorrow. Pushing past the cap is how numbers get labelled as spam.");
  } else if ((s.parked || []).length) {
    banner("warn", "<b>" + s.parked.length + " caller ID" + (s.parked.length > 1 ? "s are" : " is") + " parked</b> for a low pickup rate. Check Numbers before adding more dials to it.");
  } else banner("", "");
  emit("stats", s);
}

/* Cap meter for the caller ID the current lead will see. */
function renderCap() {
  const s = S.stats; if (!s) return;
  const caps = s.caps || [];
  const mine = (S.cur && caps.find((c) => c.number === S.cur.caller_id)) || caps[0];
  if (!mine) return;
  $("k-dials").textContent = mine.used + " of " + mine.cap + " today";
  $("k-dials").className = mine.used >= mine.cap ? "cb-note rec" : "muted";
  $("k-dials").title = "Dials today from " + fmtPhone(mine.number) + ". Cap " + mine.cap + (mine.cap < 100 ? " while the number warms up." : ".");
}

function renderWindowLine(w) {
  const el = $("window-line");
  if (!w) { el.textContent = ""; return; }
  if (!w.enforced) { el.className = "window-line"; el.innerHTML = "Simulator, windows open · " + w.eligible + " eligible"; return; }
  if (w.open) {
    el.className = "window-line " + w.tier;
    el.innerHTML = "<b>" + w.zones.map((z) => esc(z.label)).join(" + ") + " " + w.tier + " window</b> · " + w.eligible + " eligible";
  } else {
    el.className = "window-line closed";
    el.innerHTML = w.next_zone ? "Windows closed · next <b>" + esc(w.next_zone) + " " + esc(w.next_tier || "") + "</b> " + esc(w.next_local || "") + " their time"
      : "No lead is inside a calling window";
  }
}

/* ================================================================= lead */

function inHard(date, off) {
  const w = S.cfg.windows.hard, h = localHour(date, off), d = localWeekday(date, off);
  if (S.cfg.windows.weekdays_only && (d === 0 || d === 6)) return false;
  return h >= w[0] && h < w[1];
}

const hm = (t) => { const p = String(t).split(":"); return +p[0] + (+p[1] || 0) / 60; };
function tierOf(date, off) {
  const w = S.cfg.windows, h = localHour(date, off);
  if (!inHard(date, off)) return null;
  for (const name of ["power", "secondary"]) if ((w[name] || []).some((s) => h >= hm(s[0]) && h < hm(s[1]))) return name;
  return "gap";                                           // inside the day, outside every window: lunch and the edges
}

function renderLocal() {
  const l = S.cur;
  if (!l) return;
  const now = new Date(), tier = tierOf(now, l.tz_offset), open = !S.cfg.windows.enforced || tier !== null;
  const place = l.city ? l.city + (l.state ? ", " + l.state : "") : (l.state || "");
  const label = { power: "power window", secondary: "secondary window", gap: "between windows, callbacks only" }[tier] || "outside calling hours";
  $("c-local").className = "fact-line" + (open ? "" : " closed");
  $("c-local").innerHTML = [place, leadClock(now, l.tz_offset) + (l.zone ? " " + l.zone : ""), label].filter(Boolean).map((x) => "<span>" + esc(x) + "</span>").join("");
}

function renderLead() {
  const l = S.cur;
  $("lead").hidden = !l;
  $("empty").hidden = !!l;
  if (!l) { renderScript(); renderTimeline(); return; }

  // One quiet line above the name. Only what changes how you open the call is coloured.
  const flags = [];
  if (S.inbound) flags.push('<span class="pill good">Inbound call</span>');
  if (l.callback_at) flags.push('<span class="pill warn">Callback due</span>');
  if (l.list_id === "manual") flags.push('<span class="pill">Dialed by hand</span>');
  flags.push('<span class="pill">' + (l.attempts ? "Try " + (l.attempts + 1) + " of " + S.cfg.max_attempts : "First call") + "</span>");
  if (l.rank) flags.push('<span class="pill">Rank ' + esc(l.rank) + "</span>");
  if (l.last_disposition) flags.push('<span class="pill">Last time: ' + esc(outcome(l.last_disposition).label.toLowerCase()) + "</span>");
  if (l.vm_allowed === false && !S.inbound) flags.push('<span class="pill warn" title="Messages go out on tries ' + (S.cfg.voicemail_attempts || []).join(", ") + ' only">No voicemail this try</span>');
  (l.tags || []).forEach((t) => flags.push('<span class="pill warn">' + esc(t.replace(/_/g, " ")) + "</span>"));
  $("c-tags").innerHTML = flags.join("");

  $("c-company").textContent = l.co || "Unknown company";
  const name = ((l.first || "") + " " + (l.last || "")).trim();
  $("c-person").innerHTML = (name ? "<b>" + esc(name) + "</b>" : "") + (l.title ? (name ? ", " : "") + esc(l.title) : "") ||
                            "No contact name on file";
  $("c-phone").textContent = fmtPhone(l.phone);
  renderLocal();

  const facts = [];
  if (l.process) facts.push(esc(l.process.replace(/^./, (c) => c.toUpperCase())));
  if (l.oem) facts.push("supplies " + esc(l.oem));
  if (l.size) facts.push(esc(l.size) + " people");
  if (l.ppap_per_year) facts.push(esc(l.ppap_per_year) + " PPAPs a year");
  if (l.li_status) facts.push("LinkedIn " + esc(l.li_status));
  $("c-chips").innerHTML = facts.map((x) => "<span>" + x + "</span>").join("");

  const q = encodeURIComponent, links = [];
  if (l.co) {
    if (l.website) links.push(["Website", /^https?:/.test(l.website) ? l.website : "https://" + l.website]);
    links.push(["LinkedIn", l.linkedin_url || "https://www.linkedin.com/search/results/all/?keywords=" + q((name + " " + l.co).trim())]);
    links.push(["Google", "https://www.google.com/search?q=" + q(l.co + " PPAP")]);
    links.push(["Thomasnet", "https://www.google.com/search?q=" + q('site:thomasnet.com "' + l.co + '"')]);
  }
  $("c-links").innerHTML = links.map((x) => '<a target="_blank" rel="noopener noreferrer" href="' + esc(x[1]) + '">' + esc(x[0]) + "</a>").join("");

  const last = (l.history || []).find((h) => h.notes);
  const painLine = l.pain ? '<span class="eyebrow">Their pain, last time</span><q>' + esc(l.pain) + "</q>" : "";
  $("c-note").hidden = !last && !painLine;
  $("c-note").innerHTML = (painLine ? "<div>" + painLine + "</div>" : "") + (last ? '<div' + (painLine ? ' style="margin-top:8px"' : "") + '><q>' + esc(last.notes) +
    '</q><span class="by">' + esc(outcome(last.disposition).label) + ", " + esc((last.at || "").slice(0, 10)) + "</span></div>" : "");

  if (l.caller_id) {
    $("cid").textContent = "From " + fmtPhone(l.caller_id);
    $("cid").title = "Chosen for this lead by " + (l.caller_id_reason || "default") + ".";
  }
  renderCap();
  emit("lead", l);
}

function renderEmpty(kind, text) {
  S.cur = null;
  renderLead();
  emit("lead", null);
  let title = "Nothing to dial", acts = "";
  if (kind === "paused") {
    title = "Paused" + (S.session.paused ? ", " + S.session.paused.toLowerCase() : "");
    text = "Your lead went back to the queue. Inbound calls still ring here.";
    acts = '<button class="btn primary" data-act="resume">Resume <kbd>p</kbd></button>';
  } else if (kind === "connecting") {
    title = "Connecting";
  } else {
    acts = '<button class="btn" data-act="load">Load a list</button><button class="btn" data-act="manual">Dial a number</button>' +
           '<button class="btn quiet" data-act="refresh">Check again</button>';
  }
  $("e-title").textContent = title;
  $("e-text").textContent = text || "";
  $("e-acts").innerHTML = acts;
}

/* Discovery fields under the notes mirror the per-call capture, which the
   script's Qualify step and the wrap-up also edit. */
function syncDiscovery() {
  document.querySelectorAll("#discovery [data-call]").forEach((el) => {
    if (document.activeElement !== el) el.value = (S.call && S.call[el.getAttribute("data-call")]) || "";
  });
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
  const vmOk = !S.cur || S.cur.vm_allowed !== false;
  $("b-vmdrop").disabled = !inCall || S.inbound || !vmOk;
  $("b-vmdrop").title = vmOk ? "Play your recorded voicemail and move on" : "No voicemail on this attempt. Messages go out on tries " + (S.cfg.voicemail_attempts || [1, 3, 5]).join(", ") + " only.";
  if (!inCall) { setMuted(false); toggleKeypad(false); $("quality").hidden = true; }
  $("rec").hidden = !(inCall && S.call.recording_sid);
  $("b-record").hidden = !(inCall && S.call.recording_held && !S.call.recording_sid);

  $("wrap").hidden = !wrap;
  if (!wrap) resetWrap();

  $("cb-label").textContent = { IDLE: S.session.paused ? "Paused" : "Standing by", READY: "Ready", DIALING: "Dialing", LIVE: "On call", WRAP: "Wrap-up" }[s];
  $("cb-timer").className = "timer" + (inCall || ringing || wrap ? "" : " dim");
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
  if (ss.on && ss.activeSec % 5 === 0) renderEta();
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
  $("b-pause").textContent = ss.pausePending ? "Pausing after this call" : "Pause";
  $("session-clock").hidden = !ss.on;
  $("session-eta").hidden = !ss.on;
  renderEta();
}

/* "Time and a half": dials left x average seconds per dial x 1.5. */
function renderEta() {
  const ss = S.session;
  if (!ss.on) return;
  const left = Math.max(0, ss.targetDials - ss.dials);
  const perDial = ss.dials >= 3 ? ss.activeSec / ss.dials : 45;
  const byDials = left * perDial * 1.5, byClock = Math.max(0, ss.targetMinutes * 60 - ss.activeSec);
  const eta = Math.min(byDials, byClock), m = Math.round(eta / 60);
  $("session-eta").innerHTML = "<b>" + ss.dials + "</b>/" + ss.targetDials + " · " + esc(ss.script || "") +
    " · ETA " + (m >= 60 ? Math.floor(m / 60) + "h " + (m % 60) + "m" : m + "m");
  $("session-eta").title = "Estimate = dials left x average seconds per dial x 1.5, or the minutes target, whichever is sooner.";
}

const agentEvent = (event, reason) => api("/api/agent-event", { agent: S.agent, event, reason: reason || "" });

function autodialDelay() {
  const v = S.settings.delay !== "" ? Number(S.settings.delay) : Number(S.cfg.autodial_delay_sec);
  return isFinite(v) ? v : 3;
}

function startSession() {
  const ss = S.session;
  if (ss.on || modal.open) return;
  sessionStartModal().then((opts) => {
    if (!opts) return;
    return api("/api/session/start", Object.assign({ agent: S.agent }, opts)).then((d) => {
      ss.on = true; ss.paused = null; ss.pausePending = null; ss.activeSec = 0; ss.dials = 0;
      ss.id = d.id; ss.script = d.script_version || opts.script_version;
      ss.targetDials = opts.target_dials; ss.targetMinutes = opts.target_minutes;
      ss.startedAt = toServer(new Date());
      agentEvent("SESSION_START", ss.script);
      say("Session started: " + ss.targetDials + " dials or " + ss.targetMinutes + " min, script " + esc(ss.script));
      toast("info", "<b>Session on.</b> Each lead dials itself " + autodialDelay() + "s after it loads. <kbd>esc</kbd> holds one.");
      renderSession();
      emit("session");
      if (S.state === "READY" && S.cur && !S.picked) startCountdown();
      else if (S.state === "IDLE") nextLead();
    });
  });
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

function endSession(why) {
  closeMenus();
  const ss = S.session;
  if (!ss.on) return;
  cancelCountdown();
  agentEvent("SESSION_END");
  say("Session ended after " + fmtHMS(ss.activeSec));
  const id = ss.id, active = ss.activeSec;
  ss.on = false; ss.paused = null; ss.pausePending = null; ss.id = null;
  setState(S.state);
  emit("session");
  if (id) api("/api/session/end", { id, active_seconds: active, agent: S.agent }).then((d) => { if (!d.error) sessionEndCard(d, typeof why === "string" ? why : "You ended it."); });
}

/* Called after every save inside a session: count it, stop at the target. */
function sessionTargetHit() {
  const ss = S.session;
  if (!ss.on) return "";
  if (ss.dials >= ss.targetDials) return "Target of " + ss.targetDials + " dials reached.";
  if (ss.activeSec >= ss.targetMinutes * 60) return ss.targetMinutes + " minutes are up.";
  return "";
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
  S.callSec = 0; S.ringSec = 0; S.call = freshCall(lead);
  setSuggested("");
  $("notes").value = opts.notes != null ? opts.notes : store.get(draftKey(lead.phone), "");
  $("notes-saved").textContent = opts.notes == null && $("notes").value ? "Draft restored" : "";
  renderLead();
  syncDiscovery();
  if (opts.state) { setState(opts.state); return; }
  setState("READY");
  $("lead-pane").scrollTop = 0;
  if (S.session.on && !S.session.paused && !S.picked) {
    if (S.holdNext) { S.holdNext = false; $("cb-label").textContent = "Held · send the follow-up, space to dial"; }
    else startCountdown();
  }
}

function nextLead() {
  if (S.session.paused) { setState("IDLE"); renderEmpty("paused"); return; }
  clearTimeout(idleTimer);
  api(withAgent("/api/next")).then((d) => {
    if (d.caller_id && !(d.lead && d.lead.caller_id)) $("cid").textContent = d.caller_id;
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
function openLead(phone, route, extra) {
  if (busy()) { toast("warn", "Finish this call first, then open that lead."); return Promise.resolve(false); }
  cancelCountdown();
  const release = S.cur && S.state === "READY" && S.cur.phone !== phone ? api("/api/release", { phone: S.cur.phone }) : Promise.resolve();
  return release.then(() => api(route || "/api/checkout", Object.assign({ phone, agent: S.agent }, extra || {}))).then((d) => {
    if (d.error && d.can_force) {
      toast("warn", esc(d.error) + " You can still call them.", {
        ms: 12000, actions: [{ html: "Dial anyway", run: () => openLead(phone, route, Object.assign({}, extra || {}, { force: true })) }]
      });
      if (!S.cur) nextLead();
      return false;
    }
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

function afterSave(o) {
  S.holdNext = !!(o && (o.booked || o.key === "RESONATED_NO"));
  S.cur = null; S.inbound = false; $("notes").value = ""; $("notes-saved").textContent = "";
  const ss = S.session;
  if (ss.on) { ss.dials++; renderEta(); }
  const hit = sessionTargetHit();
  if (hit) { endSession(hit); setState("IDLE"); nextLead(); return; }
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
  $("mode-pill").textContent = "Simulator";
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
  maybeRecord();
}

/* Recording. One-party-consent states: starts at pickup. All-party-consent
   states (or no state on file): held until you've told them and press R. */
function needsConsent(lead) {
  const states = S.cfg.all_party_states || [];             // empty unless compliance.hold_recording_in_all_party_states
  if (!states.length) return false;
  const st = String((lead && lead.state) || "").trim().toUpperCase();
  return !st || states.indexOf(st) >= 0;
}
function maybeRecord() {
  if (activeCall && activeCall.sid) S.call.call_sid = activeCall.sid();
  if (!S.cfg.recording || S.inbound || !activeCall || !activeCall.sid || !activeCall.sid()) return;
  if (needsConsent(S.cur)) {
    S.call.recording_held = true;
    setState(S.state);
    renderScript();
    toast("info", "<b>Not recording yet.</b> " + esc((S.cur && S.cur.state) || "Their state") +
      " needs everyone's consent. Say &ldquo;" + esc(S.cfg.disclosure) + "&rdquo;, then press <kbd>r</kbd>.", { ms: 12000 });
    return;
  }
  startRecording();
}
function startRecording() {
  if (S.state !== "LIVE" || S.call.recording_sid || !activeCall || !activeCall.sid()) return;
  api("/api/record/start", { call_sid: activeCall.sid() }).then((d) => {
    if (d.error) { toast("error", "<b>Recording didn't start.</b> " + esc(d.error)); return; }
    S.call.recording_sid = d.recording_sid;
    S.call.call_sid = activeCall ? activeCall.sid() : S.call.call_sid;
    setState(S.state);
    say("Recording");
  });
}

function onEnded(reason) {
  setSuggested(reason === "voicemail" ? (S.cur && S.cur.vm_allowed === false ? "NO_ANSWER" : "VOICEMAIL")
    : reason === "no_answer" ? "NO_ANSWER" : (S.call.suggest || ""));
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
  $("mute-t").textContent = v ? "Unmute" : "Mute";
}

function vmDrop() {
  if (S.state !== "LIVE" || !S.cur || S.inbound) return;
  const sid = activeCall && activeCall.sid();
  if (!carrier.real || !activeCall) {                      // simulator
    say("Dropped voicemail (simulated)");
    const c = activeCall; activeCall = null; if (c) c.hangup();
    submitOutcome("VOICEMAIL");
    return;
  }
  if (!sid) { toast("warn", "No call id yet. Try again in a second."); return; }
  $("b-vmdrop").disabled = true;
  api("/api/vmdrop", { call_sid: sid }).then((d) => {
    if (d.ok || d.sim) {
      say("Dropped voicemail message");
      const c = activeCall; activeCall = null; if (c) c.hangup();
      submitOutcome("VOICEMAIL");                   // message is playing; move on
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
  setFlow("inbound");
  emit("inbound-answered", lead);
}

function declineIncoming() {
  if (incoming) incoming.reject();
  $("incoming").hidden = true;
  incoming = null;
}

/* ============================================================= keyboard */

function utcToLeadLocal(stamp, off) {
  const d = stamp && new Date(String(stamp).replace(" ", "T") + "Z");
  return d ? new Date(d.getTime() + (off == null ? -5 : off) * 3600000).toISOString().slice(0, 16) : "";
}

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
    if (objectionsOpen() && !e.target.matches("textarea")) { closeObjections(); return; }
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
  if (k === "r" && S.state === "LIVE" && S.call.recording_held && !S.call.recording_sid) { startRecording(); return; }
  else if (k === "k" && S.state === "LIVE") toggleKeypad();
  else if (k === "v" && S.state === "LIVE") vmDrop();
  else if (k === "s" && S.state === "READY") skip();
  else if (k === "n" && S.cur) { e.preventDefault(); $("notes").focus(); }
  else if (k === "/") { e.preventDefault(); selectTab("queue"); document.body.classList.add("rail-open"); $("q").focus(); }
  else if (k === "d") { e.preventDefault(); manualModal(); }
  else if (k === "c" && S.cur) copyNumber();
  else if (scriptKey(e, e.key.length === 1 ? k : e.key)) return;
  else if (k === "z") undo();
  else if (k === "t") statsSheet();
  else if (k === "?") shortcutsModal();
  else if (k === "p") {
    if (!S.session.on) startSession(); else if (S.session.paused) resume(); else toggleMenu("pause-menu", "b-pause");
  }
});

/* =============================================================== wiring */

function hydrateIcons() {
  document.querySelectorAll("[data-ic]").forEach((n) => { n.outerHTML = icon(n.getAttribute("data-ic"), n.getAttribute("data-cls") || ""); });
  $("keys").innerHTML = keysHTML("data-tone");
}

function renderPauseMenu() {
  $("pause-menu").innerHTML = (S.cfg.pause_reasons || ["Break"]).map((r) =>
    '<button role="menuitem" data-pause="' + esc(r) + '">' + esc(r) + "</button>").join("") +
    '<hr><button role="menuitem" data-end="1">End session</button>';
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
  $("b-record").addEventListener("click", startRecording);
  $("b-keypad").addEventListener("click", () => toggleKeypad());
  $("b-vmdrop").addEventListener("click", vmDrop);
  $("b-skip").addEventListener("click", skip);
  $("b-copy").addEventListener("click", copyNumber);
  $("b-accept").addEventListener("click", acceptIncoming);
  $("b-decline").addEventListener("click", declineIncoming);
  $("b-manual").addEventListener("click", manualModal);
  $("b-keys").addEventListener("click", shortcutsModal);
  $("b-edit-script").addEventListener("click", () => openScriptEditor(currentStepId(), currentVersion()));
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
    } else if (act === "load") loadListModal();
    else if (act === "campaigns") manageModal();
    else if (act === "analytics") openAnalytics();
    else if (act === "log") logModal();
    else if (act === "scripts") openScriptEditor(currentStepId(), currentVersion());
    else if (act === "settings") settingsModal();
  });
  document.addEventListener("click", (e) => {
    // A mouse click leaves focus on the button, and then Space would press it
    // again instead of dialing. Keyboard focus (Tab) is left alone.
    if (e.detail > 0 && !modal.open) { const b = e.target.closest("button, summary"); if (b) b.blur(); }
    if (!e.target.closest(".menu-wrap")) closeMenus();
    if (!$("keypad").hidden && !e.target.closest("#keypad, #b-keypad")) toggleKeypad(false);
  });

  $("e-acts").addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    const act = b.getAttribute("data-act");
    if (act === "resume") resume(); else if (act === "load") loadListModal();
    else if (act === "manual") manualModal(); else if (act === "refresh") nextLead();
  });
  $("f-list").addEventListener("change", function () { if (this.files[0]) loadListModal(this.files[0]); this.value = ""; });
  $("sb-analytics").addEventListener("click", () => openAnalytics());
  $("b-del-lead").addEventListener("click", () => {
    if (!S.cur) return;
    if (busy()) { toast("warn", "Finish this call first, then remove the lead."); return; }
    deleteLeads([S.cur.phone], S.cur.co || fmtPhone(S.cur.phone));
  });
  on("campaign", () => {
    renderScript();
    refreshStats();
    if (!busy() && !S.session.paused) nextLead();
  });
  on("campaign-edited", renderScript);
  on("campaigns-loaded", renderScript);
  on("leads-removed", (phones) => {
    if (S.cur && (phones || []).indexOf(S.cur.phone) >= 0 && !busy()) nextLead();
    refreshStats(); refreshTarget();
  });

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

  $("discovery").addEventListener("input", (e) => {
    const k = e.target.getAttribute("data-call");
    if (k) { S.call[k] = e.target.value.trim(); emit("call"); }
  });
  $("discovery").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); e.target.blur(); } });
  on("call", syncDiscovery);

  $("modal").addEventListener("click", (e) => { if (e.target === $("modal") || e.target.closest("[data-close]")) closeModal(); });

  window.addEventListener("beforeunload", (e) => {
    if (S.state === "LIVE" || S.state === "DIALING") { e.preventDefault(); e.returnValue = ""; return; }
    if (S.cur && S.state === "READY") navigator.sendBeacon("/api/release", JSON.stringify({ phone: S.cur.phone }));
  });
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", applyTheme);

  wireScript(); wireWrap(); wireRails(); wireFunnel();
}

Object.assign(actions, {
  openLead, nextLead, loadLead, renderStats, afterSave, cancelCountdown, refreshQueue,
  autodialDelay, applyAudio, carrier: () => carrier, dial, hangup,
  openObjections, refreshStats,
  copyBookingEmail: (b) => copyEmail(emailFor("booked_confirm",
    { first: b.first, last: b.last, co: b.company, email: b.email, dm_name: b.dm_name, tz_offset: b.tz_offset },
    { pain: b.pain, dm_name: b.dm_name, email: b.email, booked_for_local: utcToLeadLocal(b.booked_for, b.tz_offset) })),
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
    wireCampaigns();
    wirePipeline();
    if (S.cfg.live) api(withAgent("/api/token")).then((d) => { if (d && d.token) attachCarrier(d.token); else simMode(); }).catch(simMode);
    else simMode();
    nextLead();
    refreshCallbacks(); refreshCalls(); refreshInbox(); refreshBookings(); refreshNumbers();
    setInterval(() => {
      if (document.hidden) return;
      refreshStats();
      refreshCallbacks();
    }, 60000);
    setInterval(() => { if (!document.hidden) refreshInbox(); }, 300000);
  }).catch(() => {
    renderEmpty("empty", "The dialer server is not answering. Start it with: python3.12 dialer/serve.py");
    banner("danger", "<b>Server unreachable.</b> Nothing can be dialed or saved until it is back.");
  });
}

boot();
