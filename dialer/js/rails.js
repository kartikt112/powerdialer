/* Left rail: queue, callbacks, today's calls, inbox. Each tab owns a refresh
   and a render; clicks are delegated from the rail root. */
import { $, icon, esc, fmtPhone, fmtClock, parseUTC, toServer, leadClock, leadDay, rel, myClock, debounce, store } from "./util.js";
import { S, on, emit, actions, outcome } from "./state.js";
import { api, withAgent } from "./api.js";
import { toast, openModal, closeModal, closeX } from "./ui.js";
import { cbGridHTML, localInputMin } from "./wrap.js";

let activeTab = "queue";
export const currentTab = () => activeTab;

export function selectTab(name) {
  activeTab = name;
  document.querySelectorAll(".tab").forEach((t) => {
    const mine = t.getAttribute("data-tab") === name;
    t.setAttribute("aria-selected", mine ? "true" : "false");
    $("p-" + t.getAttribute("data-tab")).hidden = !mine;
  });
  (REFRESH[name] || (() => {}))();
}

const note = (title, text) => '<div class="list-note"><b>' + title + "</b>" + text + "</div>";

/* ---------------------------------------------------------------- queue -- */

export function refreshQueue() {
  const q = $("q").value.trim();
  return api("/api/leads?q=" + encodeURIComponent(q)).then((d) => {
    S.data.queue = d.leads || [];
    renderQueue(q);
  }).catch(() => {});
}

function renderQueue(q) {
  const now = new Date();
  $("l-queue").innerHTML = S.data.queue.map((l) => {
    const name = ((l.first || "") + " " + (l.last || "")).trim();
    let tag = "";
    if (q) {
      if (l.status === "DNC") tag = '<span class="pill danger">DNC</span>';
      else if (l.status === "DONE" || l.status === "EXHAUSTED") tag = '<span class="pill">' + esc(outcome(l.last_disposition).label) + "</span>";
      else if (l.status === "OUT") tag = '<span class="pill info">Open' + (l.checked_out_by ? " · " + esc(l.checked_out_by) : "") + "</span>";
      else if (l.callback_at) tag = '<span class="pill warn">Callback</span>';
    }
    const tries = !l.attempts ? "" : l.status === "NEW" ? " · try " + (l.attempts + 1)
      : " · " + l.attempts + (l.attempts === 1 ? " call" : " calls");
    return '<button class="row' + (l.in_window ? "" : " closed") + '" data-open="' + esc(l.phone) + '"' +
      (l.status === "DNC" ? " disabled" : "") + ">" +
      '<span class="rank' + (l.rank >= 80 ? " hot" : "") + '">' + esc(l.rank || "-") + "</span>" +
      '<span class="main"><span class="t1">' + esc(l.company || fmtPhone(l.phone)) + '</span><span class="t2">' +
      esc(name || fmtPhone(l.phone)) + tries + "</span></span>" +
      '<span class="meta">' + (tag || '<span class="num">' + leadClock(now, l.tz_offset) + "</span>" + esc(l.state || "") +
      (l.in_window ? "" : " · closed")) + "</span></button>";
  }).join("") || (q ? note("No match", "Nothing in any list matches &ldquo;" + esc(q) + "&rdquo;.")
                    : note("Queue is clear", "Load a list from the agent menu, or wait. Retries and callbacks pull back in as they come due."));
}

/* ------------------------------------------------------------ callbacks -- */

export function refreshCallbacks() {
  return api("/api/callbacks").then((d) => { S.data.callbacks = d.callbacks || []; renderCallbacks(); }).catch(() => {});
}

export function renderCallbacks() {
  let late = 0;
  $("l-callbacks").innerHTML = S.data.callbacks.map((c) => {
    const at = parseUTC(c.callback_at), overdue = at <= new Date();
    if (overdue) late++;
    const name = ((c.first || "") + " " + (c.last || "")).trim();
    return '<div class="row card"><span class="dot">' + icon("calendar") + "</span>" +
      '<span class="main"><span class="t1">' + esc(c.company || fmtPhone(c.phone)) + '</span><span class="t2">' + esc(name || fmtPhone(c.phone)) + "</span></span>" +
      '<span class="meta"><span class="num' + (overdue ? " late" : "") + '">' + (overdue ? "due " : "") + rel(at) + "</span>" +
      leadDay(at, c.tz_offset) + " " + leadClock(at, c.tz_offset) + " theirs</span>" +
      (c.note ? '<span class="quote">&ldquo;' + esc(c.note) + "&rdquo;</span>" : "") +
      '<span class="row-actions"><button class="btn sm primary" data-open="' + esc(c.phone) + '">' + icon("phone", "sm") + "Call now</button>" +
      '<button class="btn sm" data-resched="' + esc(c.phone) + '">Reschedule</button>' +
      '<button class="btn sm quiet" data-uncb="' + esc(c.phone) + '" title="Drop the callback; the lead goes back to the normal queue">Remove</button></span></div>';
  }).join("") || note("No callbacks scheduled", "Pick Callback in wrap-up and it shows here with the note you left.");
  $("n-cb").textContent = S.data.callbacks.length || "";
  $("n-cb").className = "count" + (late ? " hot" : "");
}

function rescheduleModal(phone) {
  const c = S.data.callbacks.find((x) => x.phone === phone);
  if (!c) return;
  const cur = parseUTC(c.callback_at);
  openModal(
    '<div class="dh"><div><h2>Reschedule callback</h2><p class="sub">' + esc(c.company || fmtPhone(c.phone)) + " · currently " +
    leadDay(cur, c.tz_offset) + " " + leadClock(cur, c.tz_offset) + " their time</p></div>" + closeX() + "</div>" +
    '<div class="cb-grid" id="rs-grid">' + cbGridHTML(c.tz_offset) + "</div>" +
    '<div class="cb-custom"><input class="field" type="datetime-local" id="rs-custom" min="' + localInputMin() + '" aria-label="Custom time, your local time">' +
    '<button class="btn" id="rs-go">Set</button><span class="hint">Your local time.</span></div><p class="err" id="rs-err"></p>');
  const save = (when) => api("/api/reschedule", { phone, callback_at: when, agent: S.agent }).then((d) => {
    if (d.error) { $("rs-err").textContent = d.error; return; }
    S.data.callbacks = d.callbacks || []; renderCallbacks(); actions.renderStats(d.stats); closeModal();
    const w = parseUTC(when);
    toast("success", "Callback moved to <b>" + leadDay(w, c.tz_offset) + " " + leadClock(w, c.tz_offset) + "</b> their time.");
  });
  $("rs-grid").addEventListener("click", (e) => { const b = e.target.closest("[data-when]"); if (b) save(b.getAttribute("data-when")); });
  $("rs-go").addEventListener("click", () => {
    const v = $("rs-custom").value;
    if (!v || new Date(v) <= new Date()) { $("rs-err").textContent = "Pick a time in the future."; return; }
    save(toServer(new Date(v)));
  });
}

/* -------------------------------------------------------- today's calls -- */

const TONE_COLOR = { good: "var(--good)", info: "var(--accent)", danger: "var(--danger)", warn: "var(--warn)", plain: "var(--text-3)" };

export function refreshCalls() {
  return api(withAgent("/api/calls")).then((d) => { S.data.calls = d.calls || []; renderCalls(); }).catch(() => {});
}

function renderCalls() {
  const counts = {}, order = [];
  S.data.calls.forEach((c) => { if (!counts[c.disposition]) { counts[c.disposition] = 0; order.push(c.disposition); } counts[c.disposition]++; });
  order.sort((a, b) => counts[b] - counts[a]);
  const shades = [75, 50, 35, 25].map((p) => "color-mix(in srgb, var(--text-3) " + p + "%, transparent)");
  let plainN = 0;
  const colorOf = {};
  order.forEach((k) => {
    const tone = outcome(k).tone || "plain";
    colorOf[k] = tone === "plain" ? shades[Math.min(plainN++, shades.length - 1)] : TONE_COLOR[tone];
  });
  $("breakdown").hidden = !S.data.calls.length;
  $("breakdown").innerHTML = '<div class="bar" role="img" aria-label="Outcome breakdown">' + order.map((k) =>
    '<i style="flex:' + counts[k] + ";background:" + colorOf[k] + '" title="' + esc(outcome(k).label) + ": " + counts[k] + '"></i>').join("") +
    '</div><div class="legend">' + order.map((k) =>
    '<span><i style="background:' + colorOf[k] + '"></i>' + esc(outcome(k).label) + " <b>" + counts[k] + "</b></span>").join("") + "</div>";

  $("l-calls").innerHTML = S.data.calls.map((c) => {
    const o = outcome(c.disposition), at = parseUTC(c.at);
    const name = ((c.first || "") + " " + (c.last || "")).trim();
    if (c.booked) return bookingCard(c, true);
    return '<button class="row" data-open="' + esc(c.phone) + '" title="Open this lead">' +
      '<span class="dot" style="color:' + (TONE_COLOR[o.tone] || "var(--text-3)") + '">' + icon(o.connect ? "phone" : "missed") + "</span>" +
      '<span class="main"><span class="t1">' + esc(c.company || fmtPhone(c.phone)) + '</span><span class="t2">' + esc(o.label) +
      (name ? " · " + esc(name) : "") + "</span></span>" +
      '<span class="meta"><span class="num">' + (at ? myClock(at) : "") + "</span>" + (c.duration ? fmtClock(c.duration) : "") + "</span>" +
      (c.notes ? '<span class="quote">' + esc(c.notes) + "</span>" : "") + "</button>";
  }).join("") || note("No calls yet today", "Every outcome you save lands here with its notes, and survives a reload.");
}

/* ------------------------------------------- booked calls + follow-through -- */

export function statusPill(b) {
  if (b.sale) return '<span class="pill good">Sale' + (b.sale_amount ? " · $" + Number(b.sale_amount).toLocaleString("en-US") : "") + "</span>";
  if (b.show_status === "SHOWED") return '<span class="pill good">Showed</span>';
  if (b.show_status === "NO_SHOW") return '<span class="pill danger">No-show</span>';
  if (b.show_status === "RESCHEDULED") return '<span class="pill warn">Rescheduled</span>';
  return "";
}

export function bookingCard(b, compact) {
  const when = parseUTC(b.booked_for), name = b.dm_name || ((b.first || "") + " " + (b.last || "")).trim();
  const past = when && when <= new Date();
  return '<div class="row card"><span class="dot" style="color:var(--good)">' + icon("calendar") + "</span>" +
    '<span class="main"><span class="t1">' + esc(b.company || fmtPhone(b.phone)) + '</span><span class="t2">' + esc(name || fmtPhone(b.phone)) + "</span></span>" +
    '<span class="meta"><span class="num' + (past && !b.show_status ? " late" : "") + '">' + (when ? rel(when) : "") + "</span>" +
    (when ? leadDay(when, b.tz_offset) + " " + leadClock(when, b.tz_offset) + " theirs" : "") + "</span>" +
    (b.pain && !compact ? '<span class="quote">&ldquo;' + esc(b.pain) + "&rdquo;</span>" : "") +
    '<span class="row-actions">' + statusPill(b) +
    '<button class="btn sm' + (past && !b.show_status ? " primary" : "") + '" data-follow="' + b.id + '">' + (b.show_status || b.sale ? "Edit result" : "Mark show / sale") + "</button>" +
    '<button class="btn sm quiet" data-open="' + esc(b.phone) + '">Open lead</button></span></div>';
}

export function followModal(id) {
  const b = (S.data.bookings || []).concat(S.data.calls).find((x) => String(x.id) === String(id));
  if (!b) return;
  const when = parseUTC(b.booked_for);
  let status = b.show_status || "", sale = !!b.sale;
  openModal(
    '<div class="dh"><div><h2>What happened on the booked call?</h2><p class="sub">' + esc(b.company || fmtPhone(b.phone)) +
    (when ? " · " + leadDay(when, b.tz_offset) + " " + leadClock(when, b.tz_offset) + " their time" : "") +
    ". It is credited to the day you dialed and booked it, " + esc((b.at || "").slice(0, 10)) + ".</p></div>" + closeX() + "</div>" +
    '<div class="seg" id="ft-status">' + [["SHOWED", "Showed"], ["NO_SHOW", "No-show"], ["RESCHEDULED", "Rescheduled"], ["", "Not yet"]].map((x) =>
      '<button data-v="' + x[0] + '" aria-selected="' + (status === x[0]) + '">' + x[1] + "</button>").join("") + "</div>" +
    '<label class="check"><input type="checkbox" id="ft-sale"' + (sale ? " checked" : "") + "><span>It became a sale</span></label>" +
    '<div id="ft-amount-row"' + (sale ? "" : " hidden") + '><label class="lbl" for="ft-amount">Sale amount, USD</label><input class="field num" id="ft-amount" type="number" min="0" step="1" value="' + (b.sale_amount || "") + '"></div>' +
    '<p class="err" id="ft-err"></p><div class="acts"><button class="btn" data-close>Cancel</button><button class="btn primary" id="ft-save">Save</button></div>');
  $("ft-status").addEventListener("click", (e) => {
    const x = e.target.closest("[data-v]"); if (!x) return;
    status = x.getAttribute("data-v");
    $("ft-status").querySelectorAll("button").forEach((y) => y.setAttribute("aria-selected", y === x ? "true" : "false"));
  });
  $("ft-sale").addEventListener("change", function () {
    sale = this.checked; $("ft-amount-row").hidden = !sale;
    if (sale) { status = "SHOWED"; $("ft-status").querySelectorAll("button").forEach((y) => y.setAttribute("aria-selected", y.getAttribute("data-v") === "SHOWED" ? "true" : "false")); $("ft-amount").focus(); }
  });
  $("ft-save").addEventListener("click", () => {
    api("/api/followthrough", { id: b.id, show_status: status, sale, sale_amount: Number($("ft-amount").value || 0), agent: S.agent }).then((d) => {
      if (d.error) { $("ft-err").textContent = d.error; return; }
      S.data.bookings = d.bookings || [];
      actions.renderStats(d.stats);
      closeModal(); refreshCalls(); emit("bookings");
      toast("success", "Saved. The funnel is updated.");
    });
  });
}

/* ---------------------------------------------------------------- inbox -- */

const heardSet = () => { try { return JSON.parse(store.get("pd_heard", "[]")); } catch (e) { return []; } };

export function refreshInbox() {
  return Promise.all([api("/api/missed"), api("/api/voicemails")]).then((r) => {
    S.data.missed = r[0].missed || []; S.data.voicemails = r[1].voicemails || [];
    renderInbox();
  }).catch(() => {});
}

function renderInbox() {
  const heard = heardSet();
  let unheard = 0, html = "";
  if (S.data.missed.length) {
    html += '<div class="group-h"><span class="eyebrow">Missed calls</span></div>' + S.data.missed.map((m) =>
      '<div class="row card"><span class="dot" style="color:var(--danger)">' + icon("missed") + "</span>" +
      '<span class="main"><span class="t1">' + esc(m.company || fmtPhone(m.phone)) + '</span><span class="t2">' +
      esc(m.name || (m.company ? fmtPhone(m.phone) : "Not in any list")) + "</span></span>" +
      '<span class="meta"><span class="num">' + rel(parseUTC(m.at)) + "</span></span>" +
      '<span class="row-actions">' + (m.dnc ? '<span class="pill danger">On the do-not-call list</span>'
        : '<button class="btn sm primary" data-callback="' + esc(m.phone) + '">' + icon("phone", "sm") + "Call back</button>") + "</span></div>").join("");
  }
  if (S.data.voicemails.length) {
    html += '<div class="group-h"><span class="eyebrow">Voicemails</span></div>' + S.data.voicemails.map((v) => {
      const isNew = heard.indexOf(v.sid) < 0;
      if (isNew) unheard++;
      return '<div class="row card' + (isNew ? " unheard" : "") + '"><span class="dot">' + icon("voicemail") + "</span>" +
        '<span class="main"><span class="t1">' + esc(v.company || fmtPhone(v.from)) + '</span><span class="t2">' +
        (v.company ? esc(fmtPhone(v.from)) + " · " : "") + v.duration + "s" + (isNew ? " · new" : "") + "</span></span>" +
        '<span class="meta"></span><span class="row-actions"><button class="btn sm" data-vm="' + esc(v.sid) + '">' + icon("play", "sm") + "Play</button>" +
        (v.phone ? '<button class="btn sm primary" data-callback="' + esc(v.phone) + '">' + icon("phone", "sm") + "Call back</button>" : "") + "</span></div>";
    }).join("");
  }
  $("l-inbox").innerHTML = html || note("Inbox is empty", S.live ? "Missed calls and voicemails from people ringing you back show up here."
    : "Missed calls and voicemails appear here once a carrier is connected.");
  $("n-inbox").textContent = (S.data.missed.length + unheard) || "";
}

/* --------------------------------------------------------------- wiring -- */

const REFRESH = { queue: refreshQueue, callbacks: refreshCallbacks, calls: refreshCalls, inbox: refreshInbox };

export function wireRails() {
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => selectTab(t.getAttribute("data-tab"))));
  $("q").addEventListener("input", debounce(refreshQueue, 220));

  $("rail").addEventListener("click", (e) => {
    let b;
    if ((b = e.target.closest("[data-follow]"))) followModal(b.getAttribute("data-follow"));
    else if ((b = e.target.closest("[data-open]"))) actions.openLead(b.getAttribute("data-open"));
    else if ((b = e.target.closest("[data-callback]"))) actions.openLead(b.getAttribute("data-callback"), "/api/manual", { returning: true });
    else if ((b = e.target.closest("[data-resched]"))) rescheduleModal(b.getAttribute("data-resched"));
    else if ((b = e.target.closest("[data-uncb]"))) {
      api("/api/reschedule", { phone: b.getAttribute("data-uncb"), callback_at: null, agent: S.agent }).then((d) => {
        if (d.error) { toast("error", esc(d.error)); return; }
        S.data.callbacks = d.callbacks || []; renderCallbacks(); actions.renderStats(d.stats); refreshQueue();
        toast("info", "Callback removed. The lead is back in the normal queue.");
      });
    } else if ((b = e.target.closest("[data-vm]"))) {
      const sid = b.getAttribute("data-vm"), a = $("vm-audio");
      a.hidden = false; a.src = "/api/voicemail/" + sid + ".mp3"; a.play().catch(() => {});
      const heard = heardSet();
      if (heard.indexOf(sid) < 0) { heard.push(sid); store.set("pd_heard", JSON.stringify(heard.slice(-200))); }
      renderInbox();
    }
  });

  on("saved", () => { refreshCalls(); refreshCallbacks(); });
  on("minute", () => { if (activeTab === "callbacks") renderCallbacks(); });
}
