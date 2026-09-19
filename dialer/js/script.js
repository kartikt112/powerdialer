/* Script rail: templated scripts and the lead's call history. */
import { $, icon, esc, fmtClock, parseUTC, leadDay, myClock, firstName } from "./util.js";
import { S, on, outcome } from "./state.js";

let scriptTab = "opener";
export function setScriptTab(t) { scriptTab = t; renderScript(); }

export function leadVars(l) {
  const dm = l.dm_name || ((l.first || "") + " " + (l.last || "")).trim();
  return {
    first: l.first || "there", last: l.last || "", company: l.co || "your company", title: l.title || "",
    agent: S.agentName || "me", city: l.city || "", state: l.state || "",
    process: l.process || "", oem: l.oem || "", dm_first: firstName(dm) || "there",
    email: (S.call && S.call.email) || l.email || "", pain: (S.call && S.call.pain) || l.pain || "",
    callback_number: l.caller_id_spoken || ""
  };
}

/* {token}, {?token}shown when set{/token}, {!token}shown when empty{/token}, **bold** */
export function renderTpl(tpl, vars) {
  let s = String(tpl || "").replace(/\{([?!])(\w+)\}([\s\S]*?)\{\/\2\}/g, (_, op, k, body) =>
    ((op === "?") === !!vars[k]) ? body : "");
  s = esc(s).replace(/\{(\w+)\}/g, (m, k) =>
    Object.prototype.hasOwnProperty.call(vars, k) ? '<span class="tok">' + esc(vars[k]) + "</span>" : m);
  return s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
}
export function renderPlain(tpl, vars) {
  return String(tpl || "").replace(/\{([?!])(\w+)\}([\s\S]*?)\{\/\2\}/g, (_, op, k, body) =>
    ((op === "?") === !!vars[k]) ? body : "").replace(/\{(\w+)\}/g, (m, k) =>
    Object.prototype.hasOwnProperty.call(vars, k) ? vars[k] : m).replace(/\*\*/g, "");
}

export function renderScript() {
  $("script-tabs").querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-selected", b.getAttribute("data-s") === scriptTab ? "true" : "false"));
  const box = $("script");
  if (!S.cur) { box.innerHTML = '<p class="muted">The script fills in with the lead\'s name and company once one is on screen.</p>'; return; }
  const v = leadVars(S.cur), sc = S.cfg.scripts || {};
  if (scriptTab === "objections") {
    box.innerHTML = (sc.objections || []).map((o) =>
      '<details class="obj"><summary>' + esc(o.q) + icon("chevron", "sm") + "</summary><p>" + renderTpl(o.a, v) + "</p></details>").join("") ||
      '<p class="muted">No objections configured. Add them under dialer.scripts.objections in config.yaml.</p>';
    return;
  }
  let html = "";
  if (scriptTab === "opener") {
    if (S.cfg.recording) html += '<div class="must">' + icon("alert") + "<span>Say first: &ldquo;" + esc(S.cfg.disclosure) + "&rdquo;</span></div>";
    if (S.inbound) html += '<p style="margin-bottom:10px"><b>Return call.</b> They saw your missed call. Pick up where the voicemail left off.</p>';
  }
  html += "<p>" + renderTpl(sc[scriptTab], v) + "</p>";
  if (scriptTab === "voicemail") html += '<p class="tip">Or press <kbd>v</kbd> on a live call to drop the recorded message and move on.</p>';
  box.innerHTML = html;
}

export function renderTimeline() {
  const cur = S.cur, h = (cur && cur.history) || [];
  $("h-count").textContent = h.length ? h.length + (h.length === 1 ? " call" : " calls") : "";
  if (!cur) { $("timeline").innerHTML = ""; return; }
  $("timeline").innerHTML = h.map((e) => {
    const o = outcome(e.disposition), at = parseUTC(e.at);
    return '<div class="ev"><i class="node ' + esc(o.tone) + '"></i><div>' +
      '<div class="l1">' + esc(o.label) + "<span>" + (at ? leadDay(at, cur.tz_offset) + " · " + myClock(at) : "") +
      (e.duration ? " · " + fmtClock(e.duration) : "") + (e.agent ? " · " + esc(e.agent) : "") + "</span></div>" +
      (e.notes ? '<div class="l2">' + esc(e.notes) + "</div>" : "") + "</div></div>";
  }).join("") || '<p class="muted">First time anyone has called this lead.</p>';
}

export function wireScript() {
  $("script-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-s]");
    if (b) setScriptTab(b.getAttribute("data-s"));
  });
  on("lead", () => { scriptTab = "opener"; renderScript(); renderTimeline(); });
  on("call-ended", (reason) => { if (reason === "voicemail") setScriptTab("voicemail"); });
}
