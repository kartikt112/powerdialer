/* Powerdialer agent cockpit.
 *
 * The server owns the queue, caps, retries, calling hours and DNC; this file
 * owns the agent's flow: power session (auto-dial with a cancellable
 * countdown), call controls, notes, wrap-up with undo, and the side rails.
 *
 * States:  IDLE -> READY -> DIALING -> LIVE -> WRAP -> (next lead)
 * Without Twilio credentials the server says so and calls are simulated;
 * without a server at all the page runs on sample leads (preview mode).
 */
(function () {
  "use strict";

  /* ================================================================ icons */

  var ICONS = {
    phone: '<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/>',
    mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3"/>',
    micoff: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3M3 3l18 18"/>',
    keypad: '<g class="dots"><circle cx="5" cy="5" r="1.4"/><circle cx="12" cy="5" r="1.4"/><circle cx="19" cy="5" r="1.4"/><circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/><circle cx="5" cy="19" r="1.4"/><circle cx="12" cy="19" r="1.4"/><circle cx="19" cy="19" r="1.4"/></g>',
    voicemail: '<circle cx="6" cy="12" r="4"/><circle cx="18" cy="12" r="4"/><path d="M6 16h12"/>',
    skip: '<path d="M5 4l10 8-10 8V4zM19 5v14"/>',
    play: '<path d="M7 4l13 8-13 8V4z"/>',
    pause: '<path d="M8 5v14M16 5v14"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    x: '<path d="M18 6L6 18M6 6l12 12"/>',
    check: '<path d="M20 6L9 17l-5-5"/>',
    undo: '<path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/>',
    sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
    chevron: '<path d="M6 9l6 6 6-6"/>',
    external: '<path d="M15 3h6v6M10 14L21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    alert: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    user: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    keyboard: '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M6 9h.01M10 9h.01M14 9h.01M18 9h.01M6 13h.01M18 13h.01M9 13h6M8 16h8"/>',
    signal: '<path d="M2 20h.01M7 20v-4M12 20v-8M17 20V8M22 4v16"/>',
    missed: '<path d="M22 2l-6 6M16 2l6 6"/><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/>',
    coffee: '<path d="M17 8h1a4 4 0 0 1 0 8h-1M3 8h14v9a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4V8zM6 2v3M10 2v3M14 2v3"/>',
    inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    bolt: '<path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>'
  };
  function icon(name, cls) {
    var fill = name === "keypad" ? " fill" : "";
    return '<svg class="ic' + fill + (cls ? " " + cls : "") + '" viewBox="0 0 24 24" aria-hidden="true">' +
           (ICONS[name] || "") + "</svg>";
  }

  /* ================================================================ utils */

  var $ = function (id) { return document.getElementById(id); };
  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function fmtPhone(p) {
    var d = String(p || "").replace(/[^\d]/g, "").replace(/^1(?=\d{10}$)/, "");
    return d.length === 10 ? "(" + d.slice(0, 3) + ") " + d.slice(3, 6) + "-" + d.slice(6) : (p || "");
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function fmtClock(s) { s = Math.max(0, Math.floor(s)); return pad(Math.floor(s / 60)) + ":" + pad(s % 60); }
  function fmtHMS(s) {
    s = Math.max(0, Math.floor(s));
    return pad(Math.floor(s / 3600)) + ":" + pad(Math.floor(s / 60) % 60) + ":" + pad(s % 60);
  }
  function fmtTalk(s) {
    s = Math.floor(s || 0);
    var h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60;
    return h ? h + ":" + pad(m) + ":" + pad(s % 60) : m + ":" + pad(s % 60);
  }
  function fmtNum(n) { return Number(n).toLocaleString("en-US"); }
  function followers(raw) {
    var m = /\d+(?:\.\d+)?/.exec(String(raw || "").replace(/,/g, ""));
    return m ? Math.round(parseFloat(m[0])) : 0;
  }
  function parseUTC(s) { return s ? new Date(String(s).replace(" ", "T") + "Z") : null; }
  function toServer(d) { return d.toISOString().slice(0, 19).replace("T", " "); }

  /* Lead-local time without an IANA zone: shift the instant by the lead's
     UTC offset and read it back with the UTC getters. */
  function shifted(date, off) { return new Date(date.getTime() + (off == null ? -5 : off) * 3600000); }
  function leadClock(date, off) {
    var l = shifted(date, off), h = l.getUTCHours(), m = l.getUTCMinutes();
    return ((h % 12) || 12) + ":" + pad(m) + (h < 12 ? "am" : "pm");
  }
  var DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  var MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function leadDay(date, off) {
    var l = shifted(date, off), n = shifted(new Date(), off);
    var dayNo = function (d) { return Math.floor(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) / 86400000); };
    var diff = dayNo(l) - dayNo(n);
    if (diff === 0) return "Today";
    if (diff === 1) return "Tomorrow";
    if (diff === -1) return "Yesterday";
    return DOW[l.getUTCDay()] + " " + l.getUTCDate() + " " + MON[l.getUTCMonth()];
  }
  function inHours(date, off, win) {
    var l = shifted(date, off), h = l.getUTCHours() + l.getUTCMinutes() / 60;
    return h >= win[0] && h < win[1];
  }
  function rel(date) {
    var mins = Math.round((date.getTime() - Date.now()) / 60000), a = Math.abs(mins), txt;
    if (a < 1) return "now";
    if (a < 60) txt = a + "m";
    else if (a < 1440) txt = Math.floor(a / 60) + "h" + (a % 60 ? " " + (a % 60) + "m" : "");
    else txt = Math.floor(a / 1440) + "d";
    return mins < 0 ? txt + " ago" : "in " + txt;
  }
  function myClock(date) {
    var h = date.getHours();
    return ((h % 12) || 12) + ":" + pad(date.getMinutes()) + (h < 12 ? "am" : "pm");
  }
  function debounce(fn, ms) {
    var t; return function () { var a = arguments, me = this; clearTimeout(t); t = setTimeout(function () { fn.apply(me, a); }, ms); };
  }

  var store = {
    get: function (k, d) { try { var v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
    del: function (k) { try { localStorage.removeItem(k); } catch (e) {} }
  };

  /* ================================================================ state */

  var FALLBACK = {                       // preview mode only; the server's /api/config is the source of truth
    daily_goal: 100, autodial_delay_sec: 3, recording: false, agents: [],
    pause_reasons: ["Break", "Lunch", "Meeting", "Admin / follow-ups"],
    disclosure: "This call is being recorded for quality and training purposes.",
    windows: { weekday: [9, 20.5], weekend: [10, 18], callback: [8, 21] },
    max_attempts: 5, caller_id: "+1 917 555 0142", live: false,
    outcomes: [
      { key: "INTERESTED", label: "Interested", kind: "final", connect: true, tone: "good" },
      { key: "CALLBACK", label: "Callback", kind: "callback", connect: true, tone: "info" },
      { key: "NOT_INT", label: "Not interested", kind: "final", connect: true, tone: "plain" },
      { key: "GATEKEEPER", label: "Gatekeeper", kind: "retry", connect: true, tone: "plain" },
      { key: "VOICEMAIL", label: "Voicemail", kind: "retry", connect: false, tone: "plain" },
      { key: "NO_ANSWER", label: "No answer", kind: "retry", connect: false, tone: "plain" },
      { key: "BUSY", label: "Busy", kind: "retry", connect: false, tone: "plain" },
      { key: "WRONG_NUMBER", label: "Wrong number", kind: "final", connect: true, tone: "plain" },
      { key: "DISCONNECTED", label: "Disconnected", kind: "final", connect: false, tone: "plain" },
      { key: "DNC", label: "Do not call", kind: "dnc", connect: true, tone: "danger" }
    ],
    scripts: {
      opener: "Hi {first}, it's {agent} — I'll be quick. I saw **{company}**{?followers} has {followers} followers on TikTok but isn't running Shop against them yet.{/followers}{!followers} isn't running TikTok Shop yet.{/followers} That's what we do, end to end, for brands your size. Worth two minutes?",
      voicemail: "Hi {first}, this is {agent}. I was calling about **{company}**'s TikTok Shop. I'll try you again, or you can reach me back on this number. Thanks!",
      gatekeeper: "Hi, it's {agent} — could you put me through to {first}? It's about **{company}**'s TikTok channel.",
      objections: [{ q: "Send me an email", a: "Happy to. So I send the right thing — what's the bigger question for you?" }]
    }
  };

  var DEMO = [
    { rank: 90, first: "Stephen", last: "Bruner", co: "Corkcicle", title: "Co-founder & CMO", phone: "+14079249845",
      city: "Orlando", state: "FL", tt: "67600", size: "73", attempts: 0, tz_offset: -4, history: [] },
    { rank: 90, first: "Katie", last: "Kaps", co: "HigherDOSE", title: "Co-founder & Co-CEO", phone: "+16143399820",
      city: "New York", state: "NY", tt: "14500", size: "29", attempts: 1, tz_offset: -4, last_disposition: "NO_ANSWER",
      history: [{ disposition: "NO_ANSWER", notes: "", agent: "agent1", duration: 0, at: "2026-09-17 15:12:00" }] },
    { rank: 88, first: "Heidi", last: "Felix", co: "LifeVac", title: "VP of Sales", phone: "+15166412567",
      city: "Nesconset", state: "NY", tt: "33400", size: "30", attempts: 2, tz_offset: -4, last_disposition: "GATEKEEPER",
      history: [{ disposition: "GATEKEEPER", notes: "Ask for Heidi directly — assistant says mornings are best.", agent: "agent1", duration: 41, at: "2026-09-16 14:03:00" },
                { disposition: "VOICEMAIL", notes: "", agent: "agent1", duration: 0, at: "2026-09-12 18:40:00" }] }
  ];

  var cfg = FALLBACK;
  var state = "IDLE";
  var cur = null;                 // checked-out lead payload
  var picked = false;             // lead was chosen by hand -> never auto-dialled
  var inbound = false;
  var stats = null;
  var callSec = 0, ringSec = 0, wrapSec = 0;
  var wrapMode = "";              // "" | "cb" | "dnc"
  var suggested = "";             // outcome key hinted after a no-answer
  var submitting = false;
  var lastUndo = null;            // {id, duration, toast}
  var scriptTab = "opener";
  var activeTab = "queue";
  var idleTimer = null;
  var OFFLINE = false, demoIdx = 0, demoStats = null, demoCalls = [], rng = 7;

  var live = false, device = null, activeCall = null, incomingCall = null, muted = false;
  var AGENT = store.get("dialer_agent", "");
  var AGENT_NAME = store.get("pd_agent_name", "");

  var session = { on: false, paused: null, pausePending: null, activeSec: 0, pauseSec: 0 };
  var cd = { left: 0, timer: null };

  var settings = {
    theme: store.get("pd_theme", "system"),
    delay: store.get("pd_delay", ""),             // "" = use config
    mic: store.get("pd_mic", ""),
    speaker: store.get("pd_speaker", ""),
    notify: store.get("pd_notify", "0") === "1"
  };

  var data = { queue: [], callbacks: [], calls: [], missed: [], voicemails: [] };

  function outcome(key) {
    for (var i = 0; i < cfg.outcomes.length; i++) if (cfg.outcomes[i].key === key) return cfg.outcomes[i];
    return { key: key, label: String(key || "").replace(/_/g, " ").toLowerCase(), tone: "plain", kind: "final" };
  }
  function autodialDelay() {
    var v = settings.delay !== "" ? Number(settings.delay) : Number(cfg.autodial_delay_sec);
    return isFinite(v) ? v : 3;
  }

  /* ========================================================= toasts + log */

  var LOG = [];
  try { LOG = JSON.parse(sessionStorage.getItem("pd_log") || "[]"); } catch (e) {}
  function say(html) {
    LOG.unshift({ t: new Date().toTimeString().slice(0, 5), h: html });
    LOG.length = Math.min(LOG.length, 120);
    try { sessionStorage.setItem("pd_log", JSON.stringify(LOG)); } catch (e) {}
  }

  function toast(kind, html, opts) {
    opts = opts || {};
    var icons = { error: "alert", warn: "alert", success: "check", info: "info" };
    var el = document.createElement("div");
    el.className = "toast " + kind;
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    el.innerHTML = icon(icons[kind] || "info") + '<span class="msg">' + html + "</span>";
    if (opts.action) {
      var b = document.createElement("button");
      b.className = "btn sm";
      b.innerHTML = opts.action.html;
      b.addEventListener("click", function () { opts.action.run(); close(); });
      el.appendChild(b);
    }
    var x = document.createElement("button");
    x.className = "btn icon sm quiet"; x.setAttribute("aria-label", "Dismiss"); x.innerHTML = icon("x", "sm");
    x.addEventListener("click", close);
    el.appendChild(x);
    $("toasts").appendChild(el);
    var timer = setTimeout(close, opts.ms || (kind === "error" ? 7000 : 4200));
    function close() {
      clearTimeout(timer);
      if (!el.parentNode) return;
      el.classList.add("out");
      setTimeout(function () { if (el.parentNode) el.remove(); }, 180);
      if (opts.onClose) opts.onClose();
    }
    if (kind === "error" || kind === "warn") say(html);
    return { close: close };
  }

  /* =============================================================== server */

  function api(path, body) {
    if (OFFLINE) return Promise.resolve(demoApi(path, body));
    var opts = body !== undefined
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : undefined;
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return { error: "Server returned " + r.status }; });
    });
  }
  function withAgent(path) { return path + (path.indexOf("?") < 0 ? "?" : "&") + "agent=" + encodeURIComponent(AGENT); }

  function demoApi(path, body) {
    if (!demoStats) demoStats = { dials_today: 0, cap: 150, connects_today: 0, interested_today: 0, callbacks_due: 0,
      callbacks_overdue: 0, queue: DEMO.length, retry_pool: 0, outcomes: {}, my_dials: 0, my_connects: 0,
      talk_seconds: 0, first_dial_at: null, server_now: toServer(new Date()) };
    var route = path.split("?")[0];
    if (route === "/api/next") {
      return { lead: DEMO[demoIdx % DEMO.length], stats: demoStats, caller_id: FALLBACK.caller_id };
    }
    if (route === "/api/leads") {
      return { leads: DEMO.map(function (d) {
        return { phone: d.phone, first: d.first, last: d.last, company: d.co, state: d.state, city: d.city,
                 tz_offset: d.tz_offset, rank: d.rank, attempts: d.attempts, status: "NEW", in_window: true };
      }) };
    }
    if (route === "/api/calls") return { calls: demoCalls };
    if (route === "/api/disposition") {
      var o = outcome(body.disposition);
      demoIdx++; demoStats.dials_today++; demoStats.my_dials++;
      demoStats.outcomes[body.disposition] = (demoStats.outcomes[body.disposition] || 0) + 1;
      if (o.connect) { demoStats.my_connects++; demoStats.talk_seconds += body.duration || 0; }
      demoStats.first_dial_at = demoStats.first_dial_at || toServer(new Date());
      demoStats.server_now = toServer(new Date());
      demoCalls.unshift({ id: demoIdx, phone: body.phone, company: body.company, disposition: body.disposition,
                          notes: body.notes, agent: AGENT, duration: body.duration, at: toServer(new Date()) });
      return { ok: true, stats: demoStats };
    }
    if (route === "/api/skip") { demoIdx++; return { ok: true }; }
    if (route === "/api/checkout" || route === "/api/manual") {
      var hit = DEMO.filter(function (d) { return d.phone === body.phone; })[0];
      return hit ? { ok: true, lead: hit } : { error: "Preview mode only knows the sample leads." };
    }
    if (route === "/api/undo") return { error: "Undo needs the server — this is preview mode." };
    return { ok: true, sim: true, callbacks: [], voicemails: [], missed: [], history: [] };
  }

  /* ================================================================ stats */

  function renderStats(s) {
    if (!s) return;
    stats = s;
    var mine = s.my_dials == null ? s.dials_today : s.my_dials;
    var conn = s.my_connects == null ? s.connects_today : s.my_connects;
    $("k-dials").innerHTML = s.dials_today + "<small>/ " + s.cap + "</small>";
    $("k-dials").className = s.dials_today >= s.cap ? "over" : "";
    $("k-dials").title = "Floor-wide dials on this caller ID today. Cap " + s.cap + ".";
    $("k-goal").style.width = Math.min(100, mine / (cfg.daily_goal || 100) * 100) + "%";
    $("k-goal").parentNode.title = mine + " of your " + (cfg.daily_goal || 100) + "-dial goal";
    $("k-conn").innerHTML = conn + "<small>" + (mine ? Math.round(conn / mine * 100) + "%" : "—") + "</small>";
    $("k-int").textContent = (s.outcomes && s.outcomes.INTERESTED) || s.interested_today || 0;
    $("k-talk").textContent = fmtTalk(s.talk_seconds);
    var pace = "—";
    if (s.first_dial_at && s.server_now) {
      var hrs = (parseUTC(s.server_now) - parseUTC(s.first_dial_at)) / 3600000;
      if (hrs >= 0.1) pace = Math.round(mine / hrs);
    }
    $("k-pace").innerHTML = pace + "<small>/hr</small>";
    var late = s.callbacks_overdue || 0;
    $("k-cb").innerHTML = (late || s.callbacks_due) + "<small>" + (late ? "overdue" : "due") + "</small>";
    $("k-cb").className = late ? "alert" : "";
    $("n-queue").textContent = s.queue == null ? "" : s.queue;
    $("n-calls").textContent = mine || "";

    if (s.dials_today >= s.cap) {
      banner("danger", "<b>Daily cap reached on this number.</b> The queue is closed until tomorrow — " +
             "pushing past " + s.cap + " dials a day is how numbers get labelled as spam.");
    }
  }

  function banner(kind, html) {
    $("banner").hidden = !html;
    $("banner").className = "banner" + (kind === "danger" ? " danger" : "");
    $("banner-text").innerHTML = html || "";
  }

  /* ================================================================= lead */

  function leadVars(l) {
    var tt = followers(l.tt);
    return { first: l.first || "there", last: l.last || "", company: l.co || "your company", title: l.title || "",
             followers: tt ? fmtNum(tt) : "", agent: AGENT_NAME || "me", city: l.city || "", state: l.state || "" };
  }

  function renderTpl(tpl, vars) {
    var s = String(tpl || "").replace(/\{([?!])(\w+)\}([\s\S]*?)\{\/\2\}/g, function (_, op, k, body) {
      var has = !!vars[k];
      return (op === "?" ? has : !has) ? body : "";
    });
    s = esc(s).replace(/\{(\w+)\}/g, function (m, k) {
      return Object.prototype.hasOwnProperty.call(vars, k) ? '<span class="tok">' + esc(vars[k]) + "</span>" : m;
    });
    return s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  }

  function renderScript() {
    var tabs = $("script-tabs").querySelectorAll("button");
    for (var i = 0; i < tabs.length; i++) tabs[i].setAttribute("aria-selected", tabs[i].getAttribute("data-s") === scriptTab ? "true" : "false");
    var box = $("script");
    if (!cur) { box.innerHTML = '<p class="muted">The script fills in with the lead\'s name and company once one is on screen.</p>'; return; }
    var v = leadVars(cur), sc = cfg.scripts || {};
    if (scriptTab === "objections") {
      box.innerHTML = (sc.objections || []).map(function (o) {
        return '<details class="obj"><summary>' + esc(o.q) + icon("chevron", "sm") + "</summary><p>" + renderTpl(o.a, v) + "</p></details>";
      }).join("") || '<p class="muted">No objections configured. Add them under dialer.scripts.objections in config.yaml.</p>';
      return;
    }
    var html = "";
    if (scriptTab === "opener") {
      // Only when calls really are recorded (dialer.recording). Nothing is ever
      // played to the callee automatically; this is a prompt for the agent.
      if (cfg.recording) html += '<div class="must">' + icon("alert") + "<span>Say first: &ldquo;" + esc(cfg.disclosure) + "&rdquo;</span></div>";
      if (inbound) html += '<p style="margin-bottom:10px"><b>Return call.</b> They saw your missed call — pick up where the voicemail left off.</p>';
    }
    html += "<p>" + renderTpl(sc[scriptTab], v) + "</p>";
    if (scriptTab === "voicemail") html += '<p class="tip">Or press <kbd>v</kbd> on a live call to drop the recorded message and move on.</p>';
    box.innerHTML = html;
  }

  function renderTimeline() {
    var h = (cur && cur.history) || [];
    $("h-count").textContent = h.length ? h.length + (h.length === 1 ? " call" : " calls") : "";
    if (!cur) { $("timeline").innerHTML = ""; return; }
    $("timeline").innerHTML = h.map(function (e) {
      var o = outcome(e.disposition), at = parseUTC(e.at), off = cur.tz_offset;
      return '<div class="ev"><i class="node ' + esc(o.tone) + '"></i><div>' +
        '<div class="l1">' + esc(o.label) + "<span>" + (at ? leadDay(at, off) + " · " + myClock(at) : "") +
        (e.duration ? " · " + fmtClock(e.duration) : "") + (e.agent ? " · " + esc(e.agent) : "") + "</span></div>" +
        (e.notes ? '<div class="l2">' + esc(e.notes) + "</div>" : "") + "</div></div>";
    }).join("") || '<p class="muted">First time anyone has called this lead.</p>';
  }

  function renderLocal() {
    if (!cur) return;
    var el = $("c-local"), now = new Date(), open = inHours(now, cur.tz_offset, cfg.windows.callback);
    var place = cur.city ? cur.city + (cur.state ? ", " + cur.state : "") : (cur.state || "");
    el.className = "localtime" + (open ? "" : " closed");
    el.innerHTML = icon(open ? "clock" : "alert", "sm") + "<b>" + leadClock(now, cur.tz_offset) + "</b>" +
      (place ? "<span>" + esc(place) + "</span>" : "<span>their time</span>") +
      (open ? "" : "<span>· outside calling hours</span>");
  }

  function renderLead() {
    var l = cur;
    $("lead").hidden = !l;
    $("empty").hidden = !!l;
    if (!l) { renderScript(); renderTimeline(); return; }

    var tags = [];
    if (inbound) tags.push('<span class="pill good">' + icon("phone", "sm") + "Inbound</span>");
    if (l.list_id === "manual") tags.push('<span class="pill">Manual dial</span>');
    if (l.rank) tags.push('<span class="pill' + (l.rank >= 80 ? " good" : "") + '">Rank <b>' + esc(l.rank) + "</b></span>");
    tags.push(l.attempts ? '<span class="pill">Attempt <b>' + (l.attempts + 1) + "</b> of " + cfg.max_attempts + "</span>"
                         : '<span class="pill info">Fresh</span>');
    if (l.callback_at) tags.push('<span class="pill warn">' + icon("calendar", "sm") + "Callback due</span>");
    if (l.last_disposition) tags.push('<span class="pill">Last: <b>' + esc(outcome(l.last_disposition).label) + "</b></span>");
    $("c-tags").innerHTML = tags.join("");

    $("c-company").textContent = l.co || (l.list_id === "manual" ? "Unknown company" : "Unknown company");
    var name = ((l.first || "") + " " + (l.last || "")).trim();
    $("c-person").innerHTML = (name ? "<b>" + esc(name) + "</b>" : "") + (l.title ? (name ? " · " : "") + esc(l.title) : "") ||
                              '<span class="muted">No contact name on file</span>';
    $("c-phone").textContent = fmtPhone(l.phone);
    renderLocal();

    var chips = [], tt = followers(l.tt);
    if (tt) chips.push('<span class="pill good"><b>' + fmtNum(tt) + "</b> TikTok followers</span>");
    if (l.size) chips.push('<span class="pill"><b>' + esc(l.size) + "</b> employees</span>");
    $("c-chips").innerHTML = chips.join("");
    $("c-chips").hidden = !chips.length;

    var q = encodeURIComponent, links = [];
    if (l.co) {
      links.push(["Google", "https://www.google.com/search?q=" + q(l.co)]);
      links.push(["LinkedIn", "https://www.linkedin.com/search/results/all/?keywords=" + q((name + " " + l.co).trim())]);
      links.push(["TikTok", "https://www.tiktok.com/search?q=" + q(l.co)]);
    }
    $("c-links").innerHTML = links.map(function (x) {
      return '<a class="btn sm quiet" target="_blank" rel="noopener noreferrer" href="' + esc(x[1]) + '">' + esc(x[0]) + icon("external", "sm") + "</a>";
    }).join("");
    $("c-links").hidden = !links.length;

    var last = (l.history || []).filter(function (h) { return h.notes; })[0];
    $("c-note").hidden = !last;
    if (last) {
      $("c-note").innerHTML = "<div><q>" + esc(last.notes) + '</q><span class="by">' + esc(last.agent || "agent") +
        " · " + esc(outcome(last.disposition).label) + " · " + esc((last.at || "").slice(0, 10)) + "</span></div>";
    }
    renderScript();
    renderTimeline();
  }

  function renderEmpty(kind, text) {
    cur = null;
    renderLead();
    var glyph = "inbox", title = "No lead to dial", acts = "";
    if (kind === "paused") {
      glyph = "coffee"; title = "Paused" + (session.paused ? " · " + session.paused : "");
      text = "Your lead went back to the queue. Inbound calls still ring here. Callbacks and the inbox stay open on the left.";
      acts = '<button class="btn primary" data-act="resume">' + icon("play") + "Resume <kbd style=\"background:rgba(255,255,255,.2);border-color:transparent;color:inherit\">p</kbd></button>";
    } else if (kind === "connecting") {
      glyph = "bolt"; title = "Connecting…";
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

  function lamp(text) { $("lamp-text").textContent = text; }

  function setState(s) {
    state = s;
    var inCall = s === "LIVE", ringing = s === "DIALING", wrap = s === "WRAP", ready = s === "READY";
    document.body.setAttribute("data-state",
      session.paused && (s === "IDLE") ? "paused" : { IDLE: "idle", READY: "ready", DIALING: "dialing", LIVE: "live", WRAP: "wrap" }[s]);

    $("b-dial").hidden = inCall || ringing;
    $("b-hangup").hidden = !(inCall || ringing);
    $("b-dial").disabled = !ready;
    $("b-skip").disabled = !ready;
    $("b-mute").disabled = !inCall;
    $("b-keypad").disabled = !inCall;
    $("b-vmdrop").disabled = !inCall || inbound;
    if (!inCall) { setMuted(false); toggleKeypad(false); $("quality").hidden = true; }
    $("rec").hidden = !(inCall && cfg.recording);

    $("wrap").hidden = !wrap;
    if (!wrap) { wrapMode = ""; $("cb-panel").hidden = true; $("dnc-panel").hidden = true; }

    var label = { IDLE: session.paused ? "Paused" : "Standing by", READY: "Ready", DIALING: "Dialing", LIVE: "On call", WRAP: "Wrap-up" }[s];
    $("cb-label").textContent = label;
    $("cb-timer").className = "timer num" + (inCall || ringing || wrap ? "" : " dim");
    if (s === "IDLE" || s === "READY") $("cb-timer").textContent = "00:00";
    lamp(session.paused && s === "IDLE" ? "Paused" : { IDLE: "Idle", READY: "Ready", DIALING: "Dialing", LIVE: "On a call", WRAP: "Wrap-up" }[s]);
    renderSession();
  }

  /* Every second: the one clock that drives all the counters. */
  setInterval(function () {
    if (state === "DIALING") { ringSec++; $("cb-timer").textContent = fmtClock(ringSec); }
    else if (state === "LIVE") { callSec++; $("cb-timer").textContent = fmtClock(callSec); }
    else if (state === "WRAP") {
      wrapSec++; $("cb-timer").textContent = fmtClock(wrapSec);
      $("cb-label").textContent = "Wrap-up" + (callSec ? " · call " + fmtClock(callSec) : "");
    }
    if (session.on) {
      if (session.paused) session.pauseSec++; else session.activeSec++;
      $("session-clock").textContent = session.paused ? "paused " + fmtClock(session.pauseSec) : fmtHMS(session.activeSec);
    }
    if (new Date().getSeconds() === 0) { renderLocal(); if (activeTab === "callbacks") renderCallbacks(); }
  }, 1000);

  /* ============================================================== session */

  function renderSession() {
    var b = $("b-session");
    b.hidden = session.on && !session.paused;
    b.textContent = session.paused ? "Resume" : "Start session";
    b.title = session.paused ? "Resume dialing (p)" : "Auto-dial through the queue (p)";
    $("pause-wrap").hidden = !(session.on && !session.paused);
    $("b-pause").innerHTML = (session.pausePending ? "Pausing after call" : "Pause") + icon("chevron", "sm");
    $("session-clock").hidden = !session.on;
  }

  function agentEvent(event, reason) { if (!OFFLINE) api("/api/agent-event", { agent: AGENT, event: event, reason: reason || "" }); }

  function startSession() {
    if (session.on) return;
    session.on = true; session.paused = null; session.pausePending = null; session.activeSec = 0;
    agentEvent("SESSION_START");
    say("Session started — leads dial themselves after wrap-up");
    toast("info", "<b>Session on.</b> Each lead dials itself " + autodialDelay() + "s after it loads. <kbd>esc</kbd> holds one.");
    renderSession();
    if (state === "READY" && cur && !picked) startCountdown();
    else if (state === "IDLE") nextLead();
  }

  function requestPause(reason) {
    closeMenus();
    if (!session.on) return;
    if (state === "DIALING" || state === "LIVE" || state === "WRAP") {
      session.pausePending = reason;
      toast("info", "Pausing for <b>" + esc(reason) + "</b> once this call is wrapped up.");
      renderSession();
      return;
    }
    doPause(reason);
  }

  function doPause(reason) {
    session.paused = reason; session.pausePending = null; session.pauseSec = 0;
    cancelCountdown();
    agentEvent("PAUSE", reason);
    say("Paused — " + esc(reason));
    if (cur && state === "READY") { api("/api/release", { phone: cur.phone }); }
    clearTimeout(idleTimer);
    setState("IDLE");
    renderEmpty("paused");
    refreshQueue();
  }

  function resume() {
    if (!session.paused) return;
    agentEvent("RESUME", session.paused);
    say("Resumed after " + fmtClock(session.pauseSec));
    session.paused = null;
    renderSession();
    if (state === "IDLE") nextLead();
    else setState(state);
  }

  function endSession() {
    closeMenus();
    if (!session.on) return;
    cancelCountdown();
    agentEvent("SESSION_END");
    say("Session ended after " + fmtHMS(session.activeSec));
    toast("info", "Session ended — " + fmtHMS(session.activeSec) + " active. Dial by hand with <kbd>space</kbd>.");
    session.on = false; session.paused = null; session.pausePending = null;
    setState(state);
  }

  function startCountdown() {
    cancelCountdown();
    var secs = autodialDelay();
    if (!cur || state !== "READY") return;
    if (!inHours(new Date(), cur.tz_offset, cfg.windows.callback)) return;
    if (secs <= 0) { dial(); return; }
    cd.left = secs;
    $("cd-n").textContent = secs;
    $("cb-state").hidden = true;
    $("countdown").hidden = false;
    var ring = $("ring");
    ring.classList.remove("run"); void ring.offsetWidth;
    ring.style.setProperty("--secs", secs + "s");
    ring.classList.add("run");
    cd.timer = setInterval(function () {
      cd.left--;
      $("cd-n").textContent = Math.max(cd.left, 0);
      if (cd.left <= 0) { cancelCountdown(); dial(); }
    }, 1000);
  }

  function cancelCountdown(hold) {
    var was = !!cd.timer;
    if (cd.timer) { clearInterval(cd.timer); cd.timer = null; }
    $("countdown").hidden = true;
    $("cb-state").hidden = false;
    $("ring").classList.remove("run");
    if (hold && was) { $("cb-label").textContent = "Held · space to dial"; }
    return was;
  }

  /* ================================================================ queue */

  function draftKey(phone) { return "pd_draft:" + phone; }

  function loadLead(lead, opts) {
    opts = opts || {};
    clearTimeout(idleTimer);
    cur = lead; picked = !!opts.handPicked; inbound = !!opts.inbound;
    callSec = 0; ringSec = 0; suggested = "";
    scriptTab = "opener";
    $("notes").value = opts.notes != null ? opts.notes : store.get(draftKey(lead.phone), "");
    $("notes-saved").textContent = opts.notes == null && $("notes").value ? "Draft restored" : "";
    renderLead();
    if (opts.state) { setState(opts.state); return; }
    setState("READY");
    $("lead-pane").scrollTop = 0;
    if (session.on && !session.paused && !picked) startCountdown();
  }

  function nextLead() {
    if (session.paused) { setState("IDLE"); renderEmpty("paused"); return; }
    clearTimeout(idleTimer);
    api(withAgent("/api/next")).then(function (d) {
      if (d.caller_id) $("cid").textContent = d.caller_id;
      renderStats(d.stats);
      if (d.lead) {
        loadLead(d.lead);
      } else {
        setState("IDLE");
        renderEmpty("empty", d.reason || d.error || "Queue empty.");
        say(esc(d.reason || "Queue empty"));
        idleTimer = setTimeout(function () { if (state === "IDLE" && !session.paused) nextLead(); }, 120000);
      }
      refreshQueue();
    }).catch(function () {
      toast("error", "<b>Can't reach the server.</b> Trying again in 15 seconds.");
      setState("IDLE"); renderEmpty("empty", "Server unreachable.");
      idleTimer = setTimeout(nextLead, 15000);
    });
  }

  /* Hand-pick a lead: queue row, callback, call log, inbox, typed number. */
  function openLead(phone, route) {
    if (state === "DIALING" || state === "LIVE" || state === "WRAP") {
      toast("warn", "Finish this call first — then open that lead."); return Promise.resolve(false);
    }
    cancelCountdown();
    var release = cur && state === "READY" && cur.phone !== phone ? api("/api/release", { phone: cur.phone }) : Promise.resolve();
    return release.then(function () {
      return api(route || "/api/checkout", { phone: phone, agent: AGENT });
    }).then(function (d) {
      if (d.error || !d.lead) { toast("error", esc(d.error || "Could not open that lead.")); if (!cur) nextLead(); return false; }
      renderStats(d.stats);
      loadLead(d.lead, { handPicked: true });
      document.body.classList.remove("rail-open");
      if (d.tz_known === false) toast("warn", "Couldn't work out the local time for that area code — check it's a sensible hour before dialing.");
      refreshQueue();
      return true;
    }).catch(function () { toast("error", "Can't reach the server."); return false; });
  }

  function skip() {
    if (state !== "READY" || !cur) return;
    cancelCountdown();
    say("Skipped " + esc(cur.co));
    var p = api("/api/skip", { phone: cur.phone, agent: AGENT });
    store.del(draftKey(cur.phone));
    cur = null;
    p.then(nextLead, nextLead);
  }

  /* ============================================================== calling */

  function attachTwilio(token) {
    var s = document.createElement("script");
    s.src = "/twilio.min.js";                      // vendored @twilio/voice-sdk
    s.onload = function () {
      device = new Twilio.Device(token, { logLevel: "error" });
      device.on("error", function (e) { toast("error", "Twilio: " + esc(e.message)); });
      device.on("tokenWillExpire", function () {
        api(withAgent("/api/token")).then(function (d) { if (d.token) device.updateToken(d.token); });
      });
      device.on("incoming", onIncoming);
      device.on("registered", applyAudioDevices);
      device.register();
      live = true;
      $("mode-pill").hidden = true;
      say("Twilio connected — <b>real calls enabled</b>, inbound rings here");
    };
    s.onerror = function () { toast("error", "Twilio SDK failed to load — running as a simulator."); simMode(); };
    document.head.appendChild(s);
  }

  function simMode() {
    $("mode-pill").hidden = false;
    $("mode-pill").innerHTML = icon("info", "sm") + "Simulator";
    $("mode-pill").title = "No Twilio credentials on the server — calls are simulated, outcomes are saved for real.";
  }

  function applyAudioDevices() {
    if (!device || !device.audio) return;
    try {
      if (settings.mic) device.audio.setInputDevice(settings.mic).catch(function () {});
      if (settings.speaker && device.audio.isOutputSelectionSupported) device.audio.speakerDevices.set([settings.speaker]).catch(function () {});
    } catch (e) {}
  }

  var QUALITY = { "high-rtt": "High latency", "high-jitter": "Choppy audio", "high-packet-loss": "Dropping audio",
                  "low-mos": "Poor call quality", "constant-audio-input-level": "Mic looks silent",
                  "constant-audio-output-level": "No audio coming in", "ice-connectivity-lost": "Connection lost, retrying" };

  function wireCall(call) {
    activeCall = call;
    call.on("ringing", function () { if (state === "DIALING") { $("cb-label").textContent = "Ringing"; lamp("Ringing"); } });
    call.on("accept", function () { if (state === "DIALING" || inbound) onAnswered(); });
    call.on("disconnect", function () {
      activeCall = null;
      if (state === "LIVE") onEnded("HANGUP"); else if (state === "DIALING") onEnded("NO_ANSWER");
    });
    call.on("cancel", function () { activeCall = null; if (state === "DIALING") onEnded("NO_ANSWER"); });
    call.on("error", function (e) {
      activeCall = null;
      toast("error", "Call error: " + esc(e.message));
      if (state === "LIVE" || state === "DIALING") onEnded("NO_ANSWER");
    });
    call.on("warning", function (name) {
      $("quality").hidden = false;
      $("quality-t").textContent = QUALITY[name] || "Poor connection";
    });
    call.on("warning-cleared", function () { $("quality").hidden = true; });
  }

  function dial() {
    if (state !== "READY" || !cur) return;
    cancelCountdown();
    if (!inHours(new Date(), cur.tz_offset, cfg.windows.callback)) {
      toast("error", "It's <b>" + leadClock(new Date(), cur.tz_offset) + "</b> for this lead — outside calling hours. Skip it or set a callback.");
      return;
    }
    inbound = false; callSec = 0; ringSec = 0;
    setState("DIALING");
    say("Dialing <b>" + esc(cur.co || fmtPhone(cur.phone)) + "</b>");

    if (live && device) {
      device.connect({ params: { To: cur.phone, CallerId: $("cid").textContent.replace(/[^+\d]/g, "") } })
        .then(wireCall)
        .catch(function (e) {
          var denied = /permission|denied|NotAllowed/i.test(e && e.message || "");
          toast("error", denied ? "<b>Microphone blocked.</b> Allow mic access for this site, then dial again."
                                : "Could not start the call: " + esc(e.message));
          setState("READY");
        });
      return;
    }
    // Simulator: deterministic ~30% contact rate so demos are repeatable.
    setTimeout(function () { if (state === "DIALING") { $("cb-label").textContent = "Ringing"; lamp("Ringing"); } }, 600);
    setTimeout(function () {
      if (state !== "DIALING") return;
      rng = (rng * 7 + 3) % 10;
      if (rng < 3) onAnswered(); else onEnded(rng < 6 ? "VOICEMAIL" : "NO_ANSWER");
    }, 2100);
  }

  function hangup() {
    if (activeCall) activeCall.disconnect();
    else if (state === "DIALING") onEnded("NO_ANSWER");
    else if (state === "LIVE") onEnded("HANGUP");
  }

  function onAnswered() {
    callSec = 0;
    setState("LIVE");
    say("Connected — <b>" + esc(cur ? (cur.co || fmtPhone(cur.phone)) : "caller") + "</b>");
  }

  function onEnded(reason) {
    suggested = reason === "VOICEMAIL" ? "VOICEMAIL" : reason === "NO_ANSWER" ? "NO_ANSWER" : "";
    if (reason === "VOICEMAIL") { scriptTab = "voicemail"; renderScript(); }
    wrapSec = 0;
    setState("WRAP");
    $("wrap-title").textContent = reason === "VOICEMAIL" ? "Reached voicemail" : reason === "NO_ANSWER" ? "No answer" : "How did it go?";
    renderOutcomes();
    say(reason === "HANGUP" ? "Call ended — " + fmtClock(callSec) : reason === "VOICEMAIL" ? "Answering machine" : "No answer");
    requestAnimationFrame(function () { $("wrap").scrollIntoView({ block: "nearest", behavior: "smooth" }); });
  }

  function setMuted(on) {
    muted = on;
    if (activeCall) activeCall.mute(on);
    $("b-mute").classList.toggle("on", on);
    $("b-mute").setAttribute("aria-pressed", on ? "true" : "false");
    $("b-mute").querySelector("svg").outerHTML = icon(on ? "micoff" : "mic");
    $("mute-t").textContent = on ? "Unmute" : "Mute";
  }

  function vmDrop() {
    if (state !== "LIVE" || !cur || inbound) return;
    if (!activeCall) {                               // simulator
      say("Dropped voicemail (simulated)");
      submitOutcome("VOICEMAIL", null);
      return;
    }
    var sid = activeCall.parameters && activeCall.parameters.CallSid;
    if (!sid) { toast("warn", "No call id yet — try again in a second."); return; }
    $("b-vmdrop").disabled = true;
    api("/api/vmdrop", { call_sid: sid }).then(function (d) {
      if (d.ok || d.sim) {
        say("Dropped voicemail message");
        if (activeCall) { var c = activeCall; activeCall = null; c.disconnect(); }
        submitOutcome("VOICEMAIL", null);            // message is playing; agent moves on
      } else {
        $("b-vmdrop").disabled = false;
        toast("error", "Voicemail drop failed: " + esc(d.error || "unknown"));
      }
    });
  }

  /* --- keypad ----------------------------------------------------------- */

  var KEYS = [["1", ""], ["2", "ABC"], ["3", "DEF"], ["4", "GHI"], ["5", "JKL"], ["6", "MNO"],
              ["7", "PQRS"], ["8", "TUV"], ["9", "WXYZ"], ["*", ""], ["0", "+"], ["#", ""]];
  function keysHTML(attr) {
    return KEYS.map(function (k) {
      return '<button class="key" type="button" ' + attr + '="' + k[0] + '">' + k[0] + (k[1] ? "<small>" + k[1] + "</small>" : "") + "</button>";
    }).join("");
  }
  function toggleKeypad(force) {
    var show = force == null ? $("keypad").hidden : force;
    if (show && state !== "LIVE") return;
    $("keypad").hidden = !show;
    $("b-keypad").setAttribute("aria-expanded", show ? "true" : "false");
    if (show) $("keypad-out").textContent = "";
  }
  function sendTone(d) {
    if (state !== "LIVE") return;
    if (activeCall) activeCall.sendDigits(d);
    $("keypad-out").textContent = ($("keypad-out").textContent + d).slice(-18);
  }

  /* ============================================================== wrap-up */

  function renderOutcomes() {
    $("outcomes").innerHTML = cfg.outcomes.map(function (o, i) {
      var key = i < 9 ? String(i + 1) : i === 9 ? "0" : "";
      return '<button class="outcome ' + esc(o.tone || "plain") + (o.key === suggested ? " suggested" : "") +
             '" data-k="' + esc(o.key) + '">' + (key ? "<kbd>" + key + "</kbd>" : "") + esc(o.label) + "</button>";
    }).join("");
  }

  function nextBusinessDay(off, hour) {
    var l = shifted(new Date(), off);
    do { l.setUTCDate(l.getUTCDate() + 1); } while (l.getUTCDay() === 0 || l.getUTCDay() === 6);
    l.setUTCHours(hour, 0, 0, 0);
    return new Date(l.getTime() - (off == null ? -5 : off) * 3600000);
  }
  function cbOptions(off) {
    var opts = [
      { label: "In 1 hour", when: new Date(Date.now() + 3600000) },
      { label: "In 3 hours", when: new Date(Date.now() + 3 * 3600000) },
      { label: "Next business day, 10am", when: nextBusinessDay(off, 10) },
      { label: "Next business day, 2pm", when: nextBusinessDay(off, 14) }
    ];
    opts.forEach(function (o) {
      o.after = !inHours(o.when, off, cfg.windows.callback);
      o.sub = leadDay(o.when, off) + " " + leadClock(o.when, off) + " their time" + (o.after ? " · after hours, rings at 8am" : "");
    });
    return opts;
  }
  function cbGridHTML(off) {
    return cbOptions(off).map(function (o, i) {
      return '<button class="outcome cb-opt" data-when="' + toServer(o.when) + '"><span class="l1"><kbd>' + (i + 1) + "</kbd>" +
             esc(o.label) + '</span><span class="l2' + (o.after ? " after" : "") + '">' + esc(o.sub) + "</span></button>";
    }).join("");
  }
  function localInputMin() {
    var d = new Date(Date.now() - new Date().getTimezoneOffset() * 60000);
    return d.toISOString().slice(0, 16);
  }

  function pickOutcome(key) {
    if (state !== "WRAP" || submitting) return;
    var o = outcome(key);
    if (o.kind === "callback") {
      wrapMode = "cb";
      $("dnc-panel").hidden = true; $("cb-panel").hidden = false;
      $("cb-grid").innerHTML = cbGridHTML(cur.tz_offset);
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
  function backToOutcomes() { wrapMode = ""; $("cb-panel").hidden = true; $("dnc-panel").hidden = true; }

  function submitOutcome(key, callbackAt) {
    if (!cur || submitting) return;
    submitting = true;
    var lead = cur, o = outcome(key), duration = callSec;
    var payload = { agent: AGENT, phone: lead.phone, company: lead.co || "", disposition: key,
                    notes: $("notes").value.trim(), duration: duration };
    if (callbackAt) payload.callback_at = callbackAt;

    api("/api/disposition", payload).then(function (d) {
      submitting = false;
      if (d.error) { toast("error", "<b>Not saved.</b> " + esc(d.error)); return; }
      store.del(draftKey(lead.phone));
      renderStats(d.stats);
      var when = callbackAt ? " · " + leadDay(parseUTC(callbackAt), lead.tz_offset) + " " + leadClock(parseUTC(callbackAt), lead.tz_offset) + " their time" : "";
      say("<b>" + esc(o.label) + "</b> — " + esc(lead.co || fmtPhone(lead.phone)) + esc(when));
      offerUndo(d.id, o, lead, duration, when);

      cur = null; inbound = false; $("notes").value = ""; $("notes-saved").textContent = "";
      if (session.pausePending) doPause(session.pausePending);
      else if (session.paused) { setState("IDLE"); renderEmpty("paused"); }
      else { setState("IDLE"); nextLead(); }
      refreshCalls(); refreshCallbacks();
    }).catch(function () {
      submitting = false;
      toast("error", "<b>Not saved — can't reach the server.</b> Your notes are still here; pick the outcome again.");
    });
  }

  function offerUndo(id, o, lead, duration, when) {
    if (lastUndo && lastUndo.toast) lastUndo.toast.close();
    var entry = { id: id, duration: duration };
    lastUndo = entry;
    entry.toast = toast(o.kind === "dnc" ? "warn" : "success",
      "<b>" + esc(o.label) + "</b> · " + esc(lead.co || fmtPhone(lead.phone)) + esc(when || ""),
      { ms: 6500, action: id ? { html: icon("undo", "sm") + "Undo <kbd>z</kbd>", run: undo } : null,
        onClose: function () { if (lastUndo === entry) lastUndo = null; } });
  }

  function undo() {
    var u = lastUndo;
    if (!u || !u.id) return;
    if (state === "DIALING" || state === "LIVE" || state === "WRAP") { toast("warn", "Finish this call first, then fix that one from its history."); return; }
    lastUndo = null;
    if (u.toast) u.toast.close();
    cancelCountdown();
    clearTimeout(idleTimer);
    var release = cur && state === "READY" ? api("/api/release", { phone: cur.phone }) : Promise.resolve();
    release.then(function () { return api("/api/undo", { id: u.id, agent: AGENT }); }).then(function (d) {
      if (d.error) { toast("error", esc(d.error)); if (!cur) nextLead(); return; }
      renderStats(d.stats);
      loadLead(d.lead, { handPicked: true, notes: d.notes || "", state: "WRAP" });
      callSec = u.duration || 0; wrapSec = 0; suggested = "";
      $("wrap-title").textContent = "Undone — pick the right outcome";
      renderOutcomes();
      say("Undid the last outcome on <b>" + esc(d.lead.co || fmtPhone(d.lead.phone)) + "</b>");
      refreshCalls(); refreshCallbacks(); refreshQueue();
    });
  }

  /* ============================================================== inbound */

  function onIncoming(call) {
    if (state === "LIVE" || state === "DIALING") { call.reject(); return; }
    cancelCountdown(true);
    incomingCall = call;
    var from = (call.parameters && call.parameters.From) || "";
    $("in-co").textContent = fmtPhone(from) || "Unknown caller";
    $("in-who").textContent = "Looking up…";
    $("incoming").hidden = false;
    $("b-accept").focus();
    say("Incoming call from <b>" + esc(fmtPhone(from)) + "</b>");
    if (settings.notify && document.hidden && window.Notification && Notification.permission === "granted") {
      try { new Notification("Incoming call", { body: fmtPhone(from), tag: "pd-incoming" }); } catch (e) {}
    }
    api("/api/lookup?phone=" + encodeURIComponent(from)).then(function (d) {
      if (incomingCall !== call) return;
      if (d.lead) {
        $("in-co").textContent = d.lead.co || fmtPhone(from);
        $("in-who").textContent = [((d.lead.first || "") + " " + (d.lead.last || "")).trim(), d.lead.title,
          d.lead.last_disposition ? "last: " + outcome(d.lead.last_disposition).label : ""].filter(Boolean).join(" · ");
        call._lead = d.lead;
      } else {
        $("in-who").textContent = fmtPhone(from) + " is not in the lead list.";
      }
    });
    call.on("cancel", function () {
      if (incomingCall === call) {
        $("incoming").hidden = true; incomingCall = null;
        toast("warn", "Missed call from <b>" + esc(fmtPhone(from)) + "</b> — it's in the Inbox.");
        setTimeout(refreshInbox, 65000);
      }
    });
  }

  function acceptIncoming() {
    var call = incomingCall;
    if (!call) return;
    $("incoming").hidden = true;
    incomingCall = null;
    var from = (call.parameters && call.parameters.From) || "";
    var prev = cur && state === "READY" ? cur.phone : null;
    if (prev) api("/api/release", { phone: prev });
    var lead = call._lead || { phone: from, co: "", first: "", last: "", title: "", city: "", state: "", tt: "", size: "",
                               rank: 0, attempts: 0, history: [], tz_offset: -5, list_id: "" };
    loadLead(lead, { handPicked: true, inbound: true, state: "READY" });
    call.accept();
    wireCall(call);
    onAnswered();
  }

  function declineIncoming() {
    if (incomingCall) incomingCall.reject();
    $("incoming").hidden = true;
    incomingCall = null;
  }

  /* ============================================================ left rail */

  function selectTab(name) {
    activeTab = name;
    var tabs = document.querySelectorAll(".tab");
    for (var i = 0; i < tabs.length; i++) {
      var on = tabs[i].getAttribute("data-tab") === name;
      tabs[i].setAttribute("aria-selected", on ? "true" : "false");
      $("p-" + tabs[i].getAttribute("data-tab")).hidden = !on;
    }
    if (name === "queue") refreshQueue();
    if (name === "callbacks") refreshCallbacks();
    if (name === "calls") refreshCalls();
    if (name === "inbox") refreshInbox();
  }

  function note(title, text) { return '<div class="list-note"><b>' + title + "</b>" + text + "</div>"; }

  /* --- queue ------------------------------------------------------------ */

  function refreshQueue() {
    var q = $("q").value.trim();
    return api("/api/leads?q=" + encodeURIComponent(q)).then(function (d) {
      data.queue = d.leads || [];
      renderQueue(q);
    }).catch(function () {});
  }

  function renderQueue(q) {
    var now = new Date();
    $("l-queue").innerHTML = data.queue.map(function (l) {
      var name = ((l.first || "") + " " + (l.last || "")).trim();
      var blocked = l.status === "DNC";
      var tag = "";
      if (q) {
        if (l.status === "DNC") tag = '<span class="pill danger">DNC</span>';
        else if (l.status === "DONE") tag = '<span class="pill">' + esc(outcome(l.last_disposition).label) + "</span>";
        else if (l.status === "OUT") tag = '<span class="pill info">Open' + (l.checked_out_by ? " · " + esc(l.checked_out_by) : "") + "</span>";
        else if (l.callback_at) tag = '<span class="pill warn">Callback</span>';
      }
      return '<button class="row' + (l.in_window ? "" : " closed") + '" data-open="' + esc(l.phone) + '"' + (blocked ? " disabled" : "") + ">" +
        '<span class="rank' + (l.rank >= 80 ? " hot" : "") + '">' + esc(l.rank || "–") + "</span>" +
        '<span class="main"><span class="t1">' + esc(l.company || fmtPhone(l.phone)) + '</span><span class="t2">' +
        esc(name || fmtPhone(l.phone)) + (!l.attempts ? "" : l.status === "NEW" ? " · try " + (l.attempts + 1)
          : " · " + l.attempts + (l.attempts === 1 ? " call" : " calls")) + "</span></span>" +
        '<span class="meta">' + (tag || '<span class="num">' + leadClock(now, l.tz_offset) + "</span>" + esc(l.state || "") + (l.in_window ? "" : " · closed")) + "</span></button>";
    }).join("") || (q ? note("No match", "Nothing in any list matches &ldquo;" + esc(q) + "&rdquo;.")
                      : note("Queue is clear", "Load a list from the agent menu, or wait — retries and callbacks pull back in as they come due."));
  }

  /* --- callbacks -------------------------------------------------------- */

  function refreshCallbacks() {
    return api("/api/callbacks").then(function (d) { data.callbacks = d.callbacks || []; renderCallbacks(); }).catch(function () {});
  }

  function renderCallbacks() {
    var late = 0;
    $("l-callbacks").innerHTML = data.callbacks.map(function (c) {
      var at = parseUTC(c.callback_at), overdue = at <= new Date();
      if (overdue) late++;
      var name = ((c.first || "") + " " + (c.last || "")).trim();
      return '<div class="row card"><span class="dot">' + icon("calendar") + "</span>" +
        '<span class="main"><span class="t1">' + esc(c.company || fmtPhone(c.phone)) + '</span><span class="t2">' + esc(name || fmtPhone(c.phone)) + "</span></span>" +
        '<span class="meta"><span class="num' + (overdue ? " late" : "") + '">' + (overdue ? "due " : "") + rel(at) + "</span>" +
        leadDay(at, c.tz_offset) + " " + leadClock(at, c.tz_offset) + " theirs</span>" +
        (c.note ? '<span class="quote">&ldquo;' + esc(c.note) + "&rdquo;</span>" : "") +
        '<span class="row-actions"><button class="btn sm primary" data-open="' + esc(c.phone) + '">' + icon("phone", "sm") + "Call now</button>" +
        '<button class="btn sm" data-resched="' + esc(c.phone) + '">Reschedule</button>' +
        '<button class="btn sm quiet" data-uncb="' + esc(c.phone) + '" title="Drop the callback; the lead goes back to the normal queue">Remove</button></span></div>';
    }).join("") || note("No callbacks scheduled", "Pick <b style=\"display:inline\">Callback</b> in wrap-up and it shows here with the note you left.");
    $("n-cb").textContent = data.callbacks.length || "";
    $("n-cb").className = "count" + (late ? " hot" : "");
  }

  function rescheduleModal(phone) {
    var c = data.callbacks.filter(function (x) { return x.phone === phone; })[0];
    if (!c) return;
    openModal(
      '<div class="dh"><div><h2>Reschedule callback</h2><p class="sub">' + esc(c.company || fmtPhone(c.phone)) + " · currently " +
      leadDay(parseUTC(c.callback_at), c.tz_offset) + " " + leadClock(parseUTC(c.callback_at), c.tz_offset) + " their time</p></div>" + closeX() + "</div>" +
      '<div class="cb-grid" id="rs-grid">' + cbGridHTML(c.tz_offset) + "</div>" +
      '<div class="cb-custom"><input class="field" type="datetime-local" id="rs-custom" min="' + localInputMin() + '" aria-label="Custom time, your local time">' +
      '<button class="btn" id="rs-go">Set</button><span class="hint">Your local time.</span></div><p class="err" id="rs-err"></p>');
    function save(when) {
      api("/api/reschedule", { phone: phone, callback_at: when, agent: AGENT }).then(function (d) {
        if (d.error) { $("rs-err").textContent = d.error; return; }
        data.callbacks = d.callbacks || []; renderCallbacks(); renderStats(d.stats); closeModal();
        toast("success", "Callback moved to <b>" + leadDay(parseUTC(when), c.tz_offset) + " " + leadClock(parseUTC(when), c.tz_offset) + "</b> their time.");
      });
    }
    $("rs-grid").addEventListener("click", function (e) { var b = e.target.closest("[data-when]"); if (b) save(b.getAttribute("data-when")); });
    $("rs-go").addEventListener("click", function () {
      var v = $("rs-custom").value;
      if (!v || new Date(v) <= new Date()) { $("rs-err").textContent = "Pick a time in the future."; return; }
      save(toServer(new Date(v)));
    });
  }

  /* --- today's calls ---------------------------------------------------- */

  var TONE_COLOR = { good: "var(--good)", info: "var(--accent)", danger: "var(--danger)", plain: "var(--text-3)" };
  function refreshCalls() {
    return api(withAgent("/api/calls")).then(function (d) { data.calls = d.calls || []; renderCalls(); }).catch(function () {});
  }
  function renderCalls() {
    var counts = {}, order = [];
    data.calls.forEach(function (c) { if (!counts[c.disposition]) { counts[c.disposition] = 0; order.push(c.disposition); } counts[c.disposition]++; });
    order.sort(function (a, b) { return counts[b] - counts[a]; });
    var shades = ["", "color-mix(in srgb, var(--text-3) 75%, transparent)", "color-mix(in srgb, var(--text-3) 50%, transparent)",
                  "color-mix(in srgb, var(--text-3) 35%, transparent)", "color-mix(in srgb, var(--text-3) 25%, transparent)"];
    var plainN = 0;
    var colorOf = {};
    order.forEach(function (k) {
      var tone = outcome(k).tone || "plain";
      colorOf[k] = tone === "plain" ? (shades[Math.min(++plainN, shades.length - 1)]) : TONE_COLOR[tone];
    });
    $("breakdown").hidden = !data.calls.length;
    $("breakdown").innerHTML = '<div class="bar" role="img" aria-label="Outcome breakdown">' + order.map(function (k) {
      return '<i style="flex:' + counts[k] + ";background:" + colorOf[k] + '" title="' + esc(outcome(k).label) + ": " + counts[k] + '"></i>';
    }).join("") + '</div><div class="legend">' + order.map(function (k) {
      return '<span><i style="background:' + colorOf[k] + '"></i>' + esc(outcome(k).label) + " <b>" + counts[k] + "</b></span>";
    }).join("") + "</div>";

    $("l-calls").innerHTML = data.calls.map(function (c) {
      var o = outcome(c.disposition), at = parseUTC(c.at);
      var name = ((c.first || "") + " " + (c.last || "")).trim();
      return '<button class="row" data-open="' + esc(c.phone) + '" title="Open this lead">' +
        '<span class="dot" style="color:' + (TONE_COLOR[o.tone] || "var(--text-3)") + '">' + icon(o.connect ? "phone" : "missed") + "</span>" +
        '<span class="main"><span class="t1">' + esc(c.company || fmtPhone(c.phone)) + '</span><span class="t2">' + esc(o.label) +
        (name ? " · " + esc(name) : "") + "</span></span>" +
        '<span class="meta"><span class="num">' + (at ? myClock(at) : "") + "</span>" + (c.duration ? fmtClock(c.duration) : "") + "</span>" +
        (c.notes ? '<span class="quote">' + esc(c.notes) + "</span>" : "") + "</button>";
    }).join("") || note("No calls yet today", "Every outcome you save lands here, with its notes, and survives a reload.");
  }

  /* --- inbox: missed calls + voicemails --------------------------------- */

  function heardSet() { try { return JSON.parse(store.get("pd_heard", "[]")); } catch (e) { return []; } }
  function refreshInbox() {
    return Promise.all([api("/api/missed"), api("/api/voicemails")]).then(function (r) {
      data.missed = r[0].missed || []; data.voicemails = r[1].voicemails || [];
      renderInbox();
    }).catch(function () {});
  }
  function renderInbox() {
    var heard = heardSet(), unheard = 0, html = "";
    if (data.missed.length) {
      html += '<div class="group-h"><span class="eyebrow">Missed calls</span></div>' + data.missed.map(function (m) {
        return '<div class="row card"><span class="dot" style="color:var(--danger)">' + icon("missed") + "</span>" +
          '<span class="main"><span class="t1">' + esc(m.company || fmtPhone(m.phone)) + '</span><span class="t2">' +
          esc(m.name || (m.company ? fmtPhone(m.phone) : "Not in any list")) + "</span></span>" +
          '<span class="meta"><span class="num">' + rel(parseUTC(m.at)) + "</span></span>" +
          '<span class="row-actions">' + (m.dnc ? '<span class="pill danger">On the do-not-call list</span>'
            : '<button class="btn sm primary" data-callback="' + esc(m.phone) + '">' + icon("phone", "sm") + "Call back</button>") + "</span></div>";
      }).join("");
    }
    if (data.voicemails.length) {
      html += '<div class="group-h"><span class="eyebrow">Voicemails</span></div>' + data.voicemails.map(function (v) {
        var isNew = heard.indexOf(v.sid) < 0;
        if (isNew) unheard++;
        return '<div class="row card' + (isNew ? " unheard" : "") + '"><span class="dot">' + icon("voicemail") + "</span>" +
          '<span class="main"><span class="t1">' + esc(v.company || fmtPhone(v.from)) + '</span><span class="t2">' +
          (v.company ? esc(fmtPhone(v.from)) + " · " : "") + v.duration + "s" + (isNew ? " · new" : "") + "</span></span>" +
          '<span class="meta"></span><span class="row-actions"><button class="btn sm" data-vm="' + esc(v.sid) + '">' + icon("play", "sm") + "Play</button>" +
          (v.phone ? '<button class="btn sm primary" data-callback="' + esc(v.phone) + '">' + icon("phone", "sm") + "Call back</button>" : "") + "</span></div>";
      }).join("");
    }
    $("l-inbox").innerHTML = html || note("Inbox is empty", live ? "Missed calls and voicemails from people ringing you back show up here."
      : "Missed calls and voicemails appear here once Twilio is connected.");
    var n = data.missed.length + unheard;
    $("n-inbox").textContent = n || "";
  }

  /* =============================================================== modals */

  var modalState = { open: false, locked: false, opener: null };
  function closeX() { return '<button class="btn icon sm quiet" data-close aria-label="Close">' + icon("x") + "</button>"; }
  function openModal(html, opts) {
    opts = opts || {};
    modalState = { open: true, locked: !!opts.locked, opener: document.activeElement };
    $("modal-box").className = "dialog" + (opts.wide ? " wide" : "");
    $("modal-box").innerHTML = html;
    $("modal").hidden = false;
    var f = $("modal-box").querySelector("[autofocus], input, select, button:not([data-close])");
    if (f) f.focus();
  }
  function closeModal(force) {
    if (!modalState.open || (modalState.locked && !force)) return;
    $("modal").hidden = true; $("modal-box").innerHTML = "";
    var o = modalState.opener; modalState = { open: false, locked: false, opener: null };
    if (o && o.focus) o.focus();
  }

  function initials(name) {
    var p = String(name || "?").trim().split(/\s+/);
    return ((p[0] || "?")[0] + (p[1] ? p[1][0] : "")).toUpperCase();
  }
  function renderAgent() {
    $("agent-ini").textContent = initials(AGENT_NAME || AGENT);
    $("agent-name").textContent = AGENT_NAME || AGENT;
    $("am-name").textContent = AGENT_NAME || AGENT;
    $("am-seat").textContent = "Seat " + AGENT + (AGENT === "agent1" ? " · inbound rings here" : "");
  }

  function agentPicker(firstRun) {
    return new Promise(function (resolve) {
      var seats = cfg.agents || [];
      openModal(
        '<div class="dh"><div><h2>Who\'s dialing?</h2><p class="sub">Your name goes into the scripts and onto every call you log. ' +
        "The seat is your line — inbound calls ring seat <b>agent1</b>.</p></div>" + (firstRun ? "" : closeX()) + "</div>" +
        (seats.length ? '<div class="seat-grid">' + seats.map(function (a) {
          return '<button class="btn seat" data-seat="' + esc(a.id) + '" data-name="' + esc(a.name || a.id) + '"><span>' + esc(a.name || a.id) + "</span><small>" + esc(a.id) + "</small></button>";
        }).join("") + '</div><span class="eyebrow">Or someone else</span>' : "") +
        '<div class="formrow"><div><label class="lbl" for="ap-name">Your first name</label><input class="field" id="ap-name" maxlength="24" autocomplete="given-name" value="' + esc(AGENT_NAME) + '"></div>' +
        '<div><label class="lbl" for="ap-seat">Seat</label><input class="field num" id="ap-seat" maxlength="24" placeholder="agent1" value="' + esc(AGENT || "agent1") + '"></div></div>' +
        '<p class="err" id="ap-err"></p><div class="acts"><button class="btn primary" id="ap-go">Start dialing</button></div>',
        { locked: firstRun });
      function choose(id, name) {
        id = String(id || "").replace(/[^A-Za-z0-9_-]/g, "").slice(0, 24);
        name = String(name || "").trim().slice(0, 24);
        if (!id) { $("ap-err").textContent = "Seat can only use letters, numbers, dashes and underscores."; return; }
        if (!name) { $("ap-err").textContent = "Add your first name — the scripts use it."; return; }
        var changedSeat = AGENT && id !== AGENT;
        store.set("dialer_agent", id); store.set("pd_agent_name", name);
        AGENT = id; AGENT_NAME = name;
        closeModal(true);
        if (changedSeat) { location.reload(); return; }
        renderAgent(); renderScript();
        resolve();
      }
      $("modal-box").addEventListener("click", function (e) {
        var s = e.target.closest("[data-seat]");
        if (s) choose(s.getAttribute("data-seat"), s.getAttribute("data-name"));
      });
      $("ap-go").addEventListener("click", function () { choose($("ap-seat").value, $("ap-name").value); });
      $("ap-name").addEventListener("keydown", function (e) { if (e.key === "Enter") $("ap-go").click(); });
      $("ap-seat").addEventListener("keydown", function (e) { if (e.key === "Enter") $("ap-go").click(); });
    });
  }

  function manualModal() {
    if (state === "DIALING" || state === "LIVE" || state === "WRAP") { toast("warn", "Finish this call first."); return; }
    openModal(
      '<div class="dh"><div><h2>Dial a number</h2><p class="sub">US and Canada only. It is checked against the do-not-call list and the daily cap, ' +
      "then opens as a lead so the call is logged like any other.</p></div>" + closeX() + "</div>" +
      '<div><label class="lbl" for="md-num">Phone number</label><input class="field num" id="md-num" type="tel" inputmode="tel" autocomplete="off" placeholder="(415) 555-0134" style="height:44px;font-size:18px"></div>' +
      '<div class="keys" id="md-keys">' + keysHTML("data-d")
        .replace(/<button class="key" type="button" data-d="\*">\*<\/button>/, '<span></span>')
        .replace(/<button class="key" type="button" data-d="#">#<\/button>/,
                 '<button class="key" type="button" data-d="back" aria-label="Delete last digit">' + icon("undo") + "</button>") + "</div>" +
      '<p class="err" id="md-err"></p><div class="acts"><button class="btn" data-close>Cancel</button><button class="btn primary" id="md-go">Open lead</button></div>');
    var inp = $("md-num");
    $("md-keys").addEventListener("click", function (e) {
      var b = e.target.closest("[data-d]");
      if (!b) return;
      var d = b.getAttribute("data-d");
      inp.value = d === "back" ? inp.value.slice(0, -1) : inp.value + d;
      inp.focus();
    });
    function go() {
      var digits = inp.value.replace(/\D/g, "").replace(/^1(?=\d{10}$)/, "");
      if (digits.length !== 10) { $("md-err").textContent = "Enter a 10-digit US or Canadian number."; return; }
      $("md-go").disabled = true;
      openLead("+1" + digits, "/api/manual").then(function (ok) { if (ok) closeModal(); else { $("md-go").disabled = false; } });
    }
    $("md-go").addEventListener("click", go);
    inp.addEventListener("keydown", function (e) { if (e.key === "Enter") go(); });
  }

  function shortcutsModal() {
    var rows = [["Dial · hang up", "space"], ["Hold the auto-dial", "esc"], ["Start · pause · resume session", "p"], ["Skip lead", "s"],
      ["Mute", "m"], ["Keypad", "k"], ["Drop voicemail", "v"], ["Send touch-tones on a call", "0-9 * #"],
      ["Jump to notes", "n"], ["Pick an outcome in wrap-up", "1-9 0"], ["Accept the suggested outcome", "enter"], ["Undo last outcome", "z"],
      ["Search leads", "/"], ["Dial a number", "d"], ["Copy the number", "c"], ["Objections", "o"]];
    openModal('<div class="dh"><h2>Keyboard shortcuts</h2>' + closeX() + '</div><div class="keys-table">' + rows.map(function (r) {
      return "<div><span>" + r[0] + "</span><kbd>" + r[1] + "</kbd></div>";
    }).join("") + "</div>", { wide: true });
  }

  function logModal() {
    openModal('<div class="dh"><div><h2>Activity log</h2><p class="sub">This browser session only. Saved calls live in the Calls tab.</p></div>' + closeX() +
      '</div><div class="log">' + (LOG.map(function (e) { return "<div><time>" + esc(e.t) + "</time><span>" + e.h + "</span></div>"; }).join("") ||
      '<p class="muted">Nothing yet.</p>') + "</div>", { wide: true });
  }

  function applyTheme() {
    var t = settings.theme;
    if (t === "system") t = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", t);
  }

  function settingsModal() {
    var delays = [0, 2, 3, 5, 8, 12], curDelay = autodialDelay();
    if (delays.indexOf(curDelay) < 0) delays.push(curDelay);
    delays.sort(function (a, b) { return a - b; });
    openModal(
      '<div class="dh"><h2>Settings</h2>' + closeX() + "</div><div>" +
      '<div class="setting"><div class="what"><b>Theme</b><span>System follows your OS.</span></div><div class="seg" id="st-theme">' +
        ["system", "dark", "light"].map(function (t) { return '<button data-v="' + t + '" aria-selected="' + (settings.theme === t) + '">' + t[0].toUpperCase() + t.slice(1) + "</button>"; }).join("") + "</div></div>" +
      '<div class="setting"><div class="what"><b>Auto-dial delay</b><span>Breathing room between wrap-up and the next dial in a session.</span></div>' +
        '<select class="field" id="st-delay">' + delays.map(function (d) { return '<option value="' + d + '"' + (d === curDelay ? " selected" : "") + ">" + (d ? d + " seconds" : "Instant") + "</option>"; }).join("") + "</select></div>" +
      '<div class="setting"><div class="what"><b>Microphone</b><span id="st-mic-h">Used for calls from this browser.</span></div><select class="field" id="st-mic"><option value="">System default</option></select></div>' +
      '<div class="setting"><div class="what"><b>Speaker</b><span id="st-spk-h">Where the other side plays.</span></div><select class="field" id="st-spk"><option value="">System default</option></select></div>' +
      '<div class="setting"><div class="what"><b>Desktop alert on inbound</b><span>Only when this tab is in the background.</span></div>' +
        '<button class="btn" id="st-notify" aria-pressed="' + settings.notify + '">' + (settings.notify ? "On" : "Off") + "</button></div></div>");

    $("st-theme").addEventListener("click", function (e) {
      var b = e.target.closest("[data-v]"); if (!b) return;
      settings.theme = b.getAttribute("data-v"); store.set("pd_theme", settings.theme); applyTheme();
      var all = $("st-theme").querySelectorAll("button");
      for (var i = 0; i < all.length; i++) all[i].setAttribute("aria-selected", all[i] === b ? "true" : "false");
    });
    $("st-delay").addEventListener("change", function () { settings.delay = this.value; store.set("pd_delay", this.value); });
    $("st-notify").addEventListener("click", function () {
      var btn = this;
      function set(on) { settings.notify = on; store.set("pd_notify", on ? "1" : "0"); btn.textContent = on ? "On" : "Off"; btn.setAttribute("aria-pressed", on); }
      if (settings.notify) return set(false);
      if (!window.Notification) { toast("warn", "This browser doesn't support desktop notifications."); return; }
      Notification.requestPermission().then(function (p) {
        if (p === "granted") set(true); else toast("warn", "Notifications are blocked for this site — allow them in the browser's site settings.");
      });
    });

    function fill(sel, kind, chosen) {
      if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
      navigator.mediaDevices.enumerateDevices().then(function (list) {
        list.filter(function (d) { return d.kind === kind && d.deviceId && d.deviceId !== "default"; }).forEach(function (d, i) {
          var o = document.createElement("option");
          o.value = d.deviceId; o.textContent = d.label || (kind === "audioinput" ? "Microphone " : "Speaker ") + (i + 1);
          if (d.deviceId === chosen) o.selected = true;
          sel.appendChild(o);
        });
      }).catch(function () {});
    }
    fill($("st-mic"), "audioinput", settings.mic);
    fill($("st-spk"), "audiooutput", settings.speaker);
    if (!live) {
      $("st-mic").disabled = $("st-spk").disabled = true;
      $("st-mic-h").textContent = $("st-spk-h").textContent = "Available once Twilio is connected.";
    } else if (device && device.audio && !device.audio.isOutputSelectionSupported) {
      $("st-spk").disabled = true; $("st-spk-h").textContent = "This browser can't switch speakers — use the OS setting.";
    }
    $("st-mic").addEventListener("change", function () { settings.mic = this.value; store.set("pd_mic", this.value); applyAudioDevices(); });
    $("st-spk").addEventListener("change", function () { settings.speaker = this.value; store.set("pd_speaker", this.value); applyAudioDevices(); });
  }

  /* --- list upload ------------------------------------------------------ */

  function uploadList(file) {
    var t = toast("info", "Prepping <b>" + esc(file.name) + "</b> — validating, scoring and de-duping…", { ms: 300000 });
    say("Uploading <b>" + esc(file.name) + "</b>");
    fetch("/api/upload", { method: "POST", headers: { "X-Filename": file.name }, body: file })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        t.close();
        if (d.ok) {
          toast("success", "List loaded — <b>" + d.added + "</b> new leads, " + d.refreshed + " already known.");
          say("List prepped — " + d.added + " new, " + d.refreshed + " known");
          renderStats(d.stats);
          if (state === "IDLE" && !session.paused) nextLead(); else refreshQueue();
        } else {
          toast("error", "<b>List prep failed:</b> " + esc(d.error || "unknown error") + (d.log ? "<br><small>" + esc(String(d.log).split("\n").pop()) + "</small>" : ""), { ms: 15000 });
        }
      })
      .catch(function (e) { t.close(); toast("error", "Upload failed: " + esc(e.message)); });
  }

  /* ================================================================ menus */

  function closeMenus() {
    ["agent-menu", "pause-menu"].forEach(function (id) { $(id).hidden = true; });
    $("b-agent").setAttribute("aria-expanded", "false"); $("b-pause").setAttribute("aria-expanded", "false");
  }
  function toggleMenu(menuId, btnId) {
    var open = $(menuId).hidden;
    closeMenus();
    $(menuId).hidden = !open;
    $(btnId).setAttribute("aria-expanded", open ? "true" : "false");
    if (open) { var f = $(menuId).querySelector("button, a"); if (f) f.focus(); }
  }
  function renderPauseMenu() {
    $("pause-menu").innerHTML = (cfg.pause_reasons || ["Break"]).map(function (r) {
      return '<button role="menuitem" data-pause="' + esc(r) + '">' + icon("coffee") + esc(r) + "</button>";
    }).join("") + '<hr><button role="menuitem" data-end="1">' + icon("x") + "End session</button>";
  }

  /* ============================================================= keyboard */

  document.addEventListener("keydown", function (e) {
    if (!e.target || !e.target.matches) return;               // synthetic events aimed at document
    if (e.key === "Escape") {
      if (!$("incoming").hidden) return;
      if (modalState.open) { closeModal(); return; }
      if (!$("agent-menu").hidden || !$("pause-menu").hidden) { closeMenus(); return; }
      if (!$("keypad").hidden) { toggleKeypad(false); return; }
      if (e.target.matches("input, textarea")) { e.target.blur(); return; }
      if (wrapMode) { backToOutcomes(); return; }
      if (cancelCountdown(true)) { e.preventDefault(); return; }
      if (document.body.classList.contains("rail-open")) document.body.classList.remove("rail-open");
      return;
    }
    if (modalState.open || !$("incoming").hidden) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target.matches("input, textarea, select")) return;
    var k = e.key.length === 1 ? e.key.toLowerCase() : e.key;

    if (wrapMode === "dnc") { if (k === "Enter") { e.preventDefault(); submitOutcome("DNC", null); } return; }

    if (e.code === "Space" || e.key === " ") {
      if (e.target.closest("button, a, summary")) return;       // let a focused control do its thing
      e.preventDefault();
      if (state === "READY") dial(); else if (state === "LIVE" || state === "DIALING") hangup();
      return;
    }
    if (state === "LIVE" && /^[0-9*#]$/.test(e.key)) { sendTone(e.key); return; }

    if (state === "WRAP") {
      if (wrapMode === "cb") {
        var opts = $("cb-grid").querySelectorAll("[data-when]");
        if (/^[1-4]$/.test(k) && opts[+k - 1]) { e.preventDefault(); submitOutcome("CALLBACK", opts[+k - 1].getAttribute("data-when")); }
        return;
      }
      if (/^[0-9]$/.test(k)) {
        var idx = k === "0" ? 9 : +k - 1;
        if (cfg.outcomes[idx]) { e.preventDefault(); pickOutcome(cfg.outcomes[idx].key); }
        return;
      }
      if (k === "Enter" && suggested && !e.target.closest("button, a")) { e.preventDefault(); pickOutcome(suggested); return; }
    }

    if (k === "m" && state === "LIVE") setMuted(!muted);
    else if (k === "k" && state === "LIVE") toggleKeypad();
    else if (k === "v" && state === "LIVE") vmDrop();
    else if (k === "s" && state === "READY") skip();
    else if (k === "n" && cur) { e.preventDefault(); $("notes").focus(); }
    else if (k === "/") { e.preventDefault(); selectTab("queue"); document.body.classList.add("rail-open"); $("q").focus(); }
    else if (k === "d") { e.preventDefault(); manualModal(); }
    else if (k === "c" && cur) copyNumber();
    else if (k === "o" && cur) { scriptTab = "objections"; renderScript(); }
    else if (k === "z") undo();
    else if (k === "?") shortcutsModal();
    else if (k === "p") {
      if (!session.on) startSession(); else if (session.paused) resume(); else toggleMenu("pause-menu", "b-pause");
    }
  });

  function copyNumber() {
    if (!cur) return;
    var p = cur.phone;
    (navigator.clipboard ? navigator.clipboard.writeText(p) : Promise.reject()).then(
      function () { toast("success", "Copied <b>" + esc(fmtPhone(p)) + "</b>", { ms: 1800 }); },
      function () { toast("warn", "Couldn't reach the clipboard — the number is " + esc(p)); });
  }

  /* =============================================================== wiring */

  function hydrateIcons() {
    var nodes = document.querySelectorAll("[data-ic]");
    for (var i = 0; i < nodes.length; i++) nodes[i].outerHTML = icon(nodes[i].getAttribute("data-ic"), nodes[i].getAttribute("data-cls") || "");
    $("b-rail").innerHTML = icon("list");
    $("b-keys").innerHTML = icon("keyboard");
    $("b-manual").innerHTML = icon("keypad");
    $("b-copy").innerHTML = icon("copy", "sm");
    $("brand-mark").innerHTML = icon("bolt", "sm");
    $("keys").innerHTML = keysHTML("data-tone");
  }

  function wire() {
    $("b-dial").addEventListener("click", dial);
    $("b-hangup").addEventListener("click", hangup);
    $("b-mute").addEventListener("click", function () { setMuted(!muted); });
    $("b-keypad").addEventListener("click", function () { toggleKeypad(); });
    $("b-vmdrop").addEventListener("click", vmDrop);
    $("b-skip").addEventListener("click", skip);
    $("b-copy").addEventListener("click", copyNumber);
    $("b-accept").addEventListener("click", acceptIncoming);
    $("b-decline").addEventListener("click", declineIncoming);
    $("b-manual").addEventListener("click", manualModal);
    $("b-keys").addEventListener("click", shortcutsModal);
    $("b-rail").addEventListener("click", function () { document.body.classList.toggle("rail-open"); });
    $("k-cb-btn").addEventListener("click", function () { selectTab("callbacks"); document.body.classList.add("rail-open"); });
    $("keys").addEventListener("click", function (e) { var b = e.target.closest("[data-tone]"); if (b) sendTone(b.getAttribute("data-tone")); });
    $("countdown").addEventListener("click", function () { cancelCountdown(true); });

    $("b-session").addEventListener("click", function () { if (session.paused) resume(); else startSession(); });
    $("b-pause").addEventListener("click", function (e) { e.stopPropagation(); toggleMenu("pause-menu", "b-pause"); });
    $("pause-menu").addEventListener("click", function (e) {
      var b = e.target.closest("button"); if (!b) return;
      if (b.hasAttribute("data-end")) endSession(); else requestPause(b.getAttribute("data-pause"));
    });
    $("b-agent").addEventListener("click", function (e) { e.stopPropagation(); toggleMenu("agent-menu", "b-agent"); });
    $("agent-menu").addEventListener("click", function (e) {
      var b = e.target.closest("[data-act]");
      closeMenus();
      if (!b) return;
      var act = b.getAttribute("data-act");
      if (act === "switch") {
        if (state === "DIALING" || state === "LIVE" || state === "WRAP") toast("warn", "Finish this call before switching agent.");
        else agentPicker(false);
      } else if (act === "load") pickList();
      else if (act === "log") logModal();
      else if (act === "keys") shortcutsModal();
      else if (act === "settings") settingsModal();
    });
    document.addEventListener("click", function (e) {
      if (!e.target.closest(".menu-wrap")) closeMenus();
      if (!$("keypad").hidden && !e.target.closest("#keypad, #b-keypad")) toggleKeypad(false);
    });

    $("e-acts").addEventListener("click", function (e) {
      var b = e.target.closest("[data-act]"); if (!b) return;
      var act = b.getAttribute("data-act");
      if (act === "resume") resume(); else if (act === "load") pickList();
      else if (act === "manual") manualModal(); else if (act === "refresh") nextLead();
    });
    $("f-list").addEventListener("change", function () { if (this.files[0]) uploadList(this.files[0]); this.value = ""; });

    $("outcomes").addEventListener("click", function (e) { var b = e.target.closest("[data-k]"); if (b) pickOutcome(b.getAttribute("data-k")); });
    $("cb-grid").addEventListener("click", function (e) { var b = e.target.closest("[data-when]"); if (b) submitOutcome("CALLBACK", b.getAttribute("data-when")); });
    $("cb-back").addEventListener("click", backToOutcomes);
    $("cb-custom").addEventListener("input", function () {
      var v = this.value && new Date(this.value);
      if (!v || !cur) { $("cb-hint").textContent = "Your local time."; return; }
      var after = !inHours(v, cur.tz_offset, cfg.windows.callback);
      $("cb-hint").textContent = "= " + leadDay(v, cur.tz_offset) + " " + leadClock(v, cur.tz_offset) + " their time" + (after ? " · after hours, rings at 8am" : "");
    });
    $("cb-custom-go").addEventListener("click", function () {
      var v = $("cb-custom").value;
      if (!v || new Date(v) <= new Date()) { toast("warn", "Pick a callback time in the future."); return; }
      submitOutcome("CALLBACK", toServer(new Date(v)));
    });
    $("dnc-yes").addEventListener("click", function () { submitOutcome("DNC", null); });
    $("dnc-no").addEventListener("click", backToOutcomes);

    var saveDraft = debounce(function () {
      if (!cur) return;
      var v = $("notes").value;
      if (v) store.set(draftKey(cur.phone), v); else store.del(draftKey(cur.phone));
      $("notes-saved").textContent = v ? "Draft saved" : "";
    }, 350);
    $("notes").addEventListener("input", saveDraft);

    $("script-tabs").addEventListener("click", function (e) {
      var b = e.target.closest("[data-s]"); if (b) { scriptTab = b.getAttribute("data-s"); renderScript(); }
    });

    var tabs = document.querySelectorAll(".tab");
    for (var i = 0; i < tabs.length; i++) tabs[i].addEventListener("click", function () { selectTab(this.getAttribute("data-tab")); });
    $("q").addEventListener("input", debounce(refreshQueue, 220));

    $("rail").addEventListener("click", function (e) {
      var b;
      if ((b = e.target.closest("[data-open]"))) openLead(b.getAttribute("data-open"));
      else if ((b = e.target.closest("[data-callback]"))) openLead(b.getAttribute("data-callback"), "/api/manual");
      else if ((b = e.target.closest("[data-resched]"))) rescheduleModal(b.getAttribute("data-resched"));
      else if ((b = e.target.closest("[data-uncb]"))) {
        api("/api/reschedule", { phone: b.getAttribute("data-uncb"), callback_at: null, agent: AGENT }).then(function (d) {
          if (d.error) { toast("error", esc(d.error)); return; }
          data.callbacks = d.callbacks || []; renderCallbacks(); renderStats(d.stats); refreshQueue();
          toast("info", "Callback removed — the lead is back in the normal queue.");
        });
      } else if ((b = e.target.closest("[data-vm]"))) {
        var sid = b.getAttribute("data-vm"), a = $("vm-audio");
        a.hidden = false; a.src = "/api/voicemail/" + sid + ".mp3"; a.play().catch(function () {});
        var heard = heardSet(); if (heard.indexOf(sid) < 0) { heard.push(sid); store.set("pd_heard", JSON.stringify(heard.slice(-200))); }
        renderInbox();
      }
    });

    $("modal").addEventListener("click", function (e) {
      if (e.target === $("modal") || e.target.closest("[data-close]")) closeModal();
    });

    window.addEventListener("beforeunload", function (e) {
      if (state === "LIVE" || state === "DIALING") { e.preventDefault(); e.returnValue = ""; return; }
      if (cur && state === "READY" && !OFFLINE) navigator.sendBeacon("/api/release", JSON.stringify({ phone: cur.phone }));
    });
    matchMedia("(prefers-color-scheme: light)").addEventListener("change", applyTheme);
  }

  function pickList() {
    if (state === "LIVE" || state === "DIALING") { toast("warn", "Finish the call before loading a new list."); return; }
    if (OFFLINE) { toast("warn", "Preview mode has no server to prep a list."); return; }
    $("f-list").click();
  }

  /* ================================================================= boot */

  function boot() {
    hydrateIcons();
    wire();
    setState("IDLE");
    renderEmpty("connecting", "");

    fetch("/api/config").then(function (r) { if (!r.ok) throw new Error(); return r.json(); }).then(function (c) {
      if (!c || !c.outcomes) throw new Error();
      cfg = c;
    }).catch(function () {
      OFFLINE = true; cfg = FALLBACK;
      banner("warn", "<b>Preview mode.</b> No server found — these are sample leads and nothing is saved.");
    }).then(function () {
      $("cid").textContent = cfg.caller_id || "—";
      renderPauseMenu();
      if (!AGENT || !AGENT_NAME) {
        var known = (cfg.agents || []).filter(function (a) { return a.id === AGENT; })[0];
        if (AGENT && known) { AGENT_NAME = known.name; store.set("pd_agent_name", AGENT_NAME); }
        else return agentPicker(true);
      }
    }).then(function () {
      renderAgent();
      say("Signed in as " + esc(AGENT_NAME) + " (" + esc(AGENT) + ")");
      if (OFFLINE || !cfg.live) simMode();
      if (!OFFLINE && cfg.live) {
        api(withAgent("/api/token")).then(function (d) { if (d && d.token) attachTwilio(d.token); else simMode(); }).catch(simMode);
      }
      nextLead();
      refreshCallbacks(); refreshCalls(); refreshInbox();
      setInterval(function () {
        if (document.hidden) return;
        api(withAgent("/api/stats")).then(renderStats).catch(function () {});
        refreshCallbacks();
      }, 60000);
      setInterval(function () { if (!document.hidden) refreshInbox(); }, 300000);
    });
  }

  boot();
})();
