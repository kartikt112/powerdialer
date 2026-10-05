/* The left rail: Queue, Callbacks, Samples, Follow-ups, Calls, Stats, plus
   the History timeline on the right. Everything is text rows. */
import { $, esc, parseUTC, istWhen, istClock, rel, fmtClock, prettyPhone, debounce, store } from "./util.js";
import { S, on, emit, actions, outcome } from "./state.js";
import { api, withAgent } from "./api.js";

let tab = store.get("in_tab", "queue");
const renderers = {};
export function registerTab(name, fn) { renderers[name] = fn; }

export function selectTab(name) {
  tab = name;
  store.set("in_tab", name);
  document.querySelectorAll(".railtabs [data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.getAttribute("data-tab") === name)));
  document.querySelectorAll(".rail .panel").forEach((p) => { p.hidden = p.id !== "p-" + name; });
  refresh(name);
}
export const currentTab = () => tab;

const skel = (n) => '<div class="skel"></div>'.repeat(n || 6);
function loading(id) { if (!$(id).children.length) $(id).innerHTML = skel(); }

/* ---- queue ---------------------------------------------------------------- */

function queueRow(l, wait) {
  const bits = ["Tier " + esc(l.tier)];
  if (wait) bits.push(esc(wait));
  if (l.city) bits.push(esc(l.city));
  if (l.attempts) bits.push("try " + (l.attempts + 1));
  if (l.last_outcome) bits.push(esc(outcome(l.last_outcome).label.toLowerCase()));
  if (!wait && l.next_action_type === "callback" && l.next_action_at) bits.push("call back " + esc(istWhen(parseUTC(l.next_action_at))));
  return '<button class="row' + (S.cur && S.cur.id === l.id ? " on" : "") + '" data-lead="' + l.id + '">' +
    '<span class="t1">' + esc(l.company) + '</span><span class="t2">' + bits.join(" · ") + "</span></button>";
}

function renderQueue() {
  const q = $("q").value.trim();
  loading("l-queue");
  api(withAgent("/api/queue" + (q ? "?q=" + encodeURIComponent(q) : ""))).then((d) => {
    const leads = d.leads || [];
    if (!q) { S.data.queueCount = d.total != null ? d.total : leads.length; $("n-queue").textContent = S.data.queueCount || ""; if (d.window) actions.renderClock(d.window); }
    if (q) {
      $("l-queue").innerHTML = leads.length ? leads.map(queueRow).join("") : '<p class="note">Nothing matches “' + esc(q) + "”.</p>";
      return;
    }
    const lists = d.lists || { total: 0, lists: [] }, later = d.later || [], w = d.window || {};
    S.data.listTotal = lists.total;
    if (d.window) actions.renderClock(d.window);
    if (!lists.total) {
      $("l-queue").innerHTML = '<p class="note"><b>No leads yet.</b>Import a CSV or Excel list to start calling.</p>' +
        '<div class="acts" style="padding:0 8px"><button class="btn sm primary" data-import>Import a list</button></div>';
      return;
    }
    let h = '<p class="lists-line">' + lists.total + " leads from " + lists.lists.length + (lists.lists.length === 1 ? " list" : " lists") +
      ' <button class="btn quiet sm" data-import>Import another</button></p>';
    let group = "";
    leads.forEach((l) => {
      if (l.group !== group) { group = l.group; h += '<p class="group-h">' + esc(group) + "</p>"; }
      h += queueRow(l);
    });
    if (later.length) {
      const why = !leads.length && w.tier && w.tier !== "power" && w.tier !== "soft"
        ? esc(w.label) + (w.next_open ? ", cold calls open " + esc(w.next_open) : "") + ". Open any lead to call it by hand."
        : "Waiting for a time, a retry or a hold.";
      h += '<p class="group-h">Later<span>' + (d.later_total || later.length) + "</span></p>" +
        '<p class="note later-note">' + why + "</p>" + later.map((l) => queueRow(l, l.wait)).join("");
    }
    if (!leads.length && !later.length) h += '<p class="note">Every lead on your lists has been worked to the end.</p>';
    $("l-queue").innerHTML = h;
  });
}
registerTab("queue", renderQueue);

/* ---- callbacks -------------------------------------------------------------- */

function renderCallbacks() {
  loading("l-callbacks");
  api("/api/callbacks").then((d) => {
    const list = d.callbacks || [];
    S.data.callbacks = list;
    const now = Date.now();
    const due = list.filter((c) => parseUTC(c.next_action_at) <= now).length;
    $("n-cb").textContent = list.length || "";
    $("n-cb").classList.toggle("due", due > 0);
    emit("callbacks", list);
    $("l-callbacks").innerHTML = list.length ? list.map((c) => {
      const at = parseUTC(c.next_action_at), late = at < now;
      return '<button class="row' + (S.cur && S.cur.id === c.id ? " on" : "") + '" data-lead="' + c.id + '">' +
        '<span class="t1">' + esc(c.company) + '</span><span class="r' + (late ? " late" : "") + '"><b>' + esc(istWhen(at)) + "</b>" + (late ? " · due" : "") + "</span>" +
        '<span class="t2">' + esc([c.dm_name, c.city].filter(Boolean).join(" · ")) + "</span></button>";
    }).join("") : '<p class="note">No callbacks booked. Pick <b>Callback</b> in the wrap-up to book one.</p>';
  });
}
registerTab("callbacks", renderCallbacks);

/* ---- calls today ------------------------------------------------------------ */

function renderCalls() {
  loading("l-calls");
  api("/api/calls?range=today").then((d) => {
    const list = d.calls || [];
    $("n-calls").textContent = list.length || "";
    $("l-calls").innerHTML = list.length ? list.map((c) => {
      const o = outcome(c.outcome);
      return '<button class="row" data-lead="' + c.lead_id + '"><span class="t1">' + esc(c.company) + "</span>" +
        '<span class="r">' + esc(istClock(parseUTC(c.dialed_at))) + "</span>" +
        '<span class="t2">' + esc(o.label) + (c.continued ? ", tried the next number" : "") + (c.talk_seconds ? " · " + fmtClock(c.talk_seconds) : "") +
        (c.number ? " · " + esc(prettyPhone(c.number, c.number_kind)) : "") + "</span></button>";
    }).join("") : '<p class="note">No calls yet today.</p>';
  });
}
registerTab("calls", renderCalls);

/* ---- history (right column) -------------------------------------------------- */

function renderHistory(l) {
  const ev = [];
  if (l) {
    (l.history || []).forEach((h) => ev.push({ at: parseUTC(h.dialed_at), t: outcome(h.outcome).label + (h.continued ? ", tried the next number" : ""),
      p: [h.talk_seconds ? "talked " + fmtClock(h.talk_seconds) : "", h.number ? prettyPhone(h.number, h.number_kind) : "", h.notes || ""].filter(Boolean).join(" · ") }));
    (l.followups || []).forEach((f) => ev.push({ at: parseUTC(f.at), t: (f.channel === "whatsapp" ? "WhatsApp" : "Email") + " opened", p: (f.template || "").replace(/_/g, " ") }));
    ev.sort((a, b) => b.at - a.at);
  }
  $("h-count").textContent = ev.length ? ev.length : "";
  $("timeline").innerHTML = ev.length ? ev.map((e) => '<div class="ev"><b>' + esc(e.t) + '</b> <span class="when">' + esc(istWhen(e.at)) + " · " + esc(rel(e.at)) + "</span>" +
    (e.p ? "<p>" + esc(e.p) + "</p>" : "") + "</div>").join("") : '<p class="note">' + (l ? "First contact." : "") + "</p>";
}
on("lead", renderHistory);

/* ---- wiring -------------------------------------------------------------------- */

export function refresh(name) { const fn = renderers[name || tab]; if (fn) fn(); }

export function refreshAll(quiet) {
  renderCallbacks();
  if (tab !== "callbacks") refresh(tab);
  if (tab !== "queue") api(withAgent("/api/queue")).then((d) => { S.data.queueCount = d.total != null ? d.total : (d.leads || []).length; $("n-queue").textContent = S.data.queueCount || ""; });
  emit("refresh", quiet);
}

export function wireRails() {
  document.querySelector(".railtabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-tab]"); if (b) selectTab(b.getAttribute("data-tab"));
  });
  $("rail").addEventListener("click", (e) => {
    if (e.target.closest("button:not(.row), a")) return;
    const r = e.target.closest("[data-lead]"); if (r) actions.openLead(+r.getAttribute("data-lead"));
  });
  $("q").addEventListener("input", debounce(renderQueue, 200));
  $("l-queue").addEventListener("click", (e) => { if (e.target.closest("[data-import]")) actions.importList(); });
  on("saved", () => refreshAll());
  on("lead", () => { if (tab === "queue" || tab === "callbacks") document.querySelectorAll(".rail .row[data-lead]").forEach((r) => r.classList.toggle("on", !!S.cur && +r.getAttribute("data-lead") === S.cur.id)); });
  if (!renderers[tab]) tab = "queue";
  selectTab(tab);
}
