/* Session dialogs: the start form (target + script version) and the
   end-of-session card with that session's own funnel. */
import { $, esc, icon, fmtPct, fmtHMS } from "./util.js";
import { S, actions, campaignScript } from "./state.js";
import { openModal, closeModal, closeX } from "./ui.js";

export function sessionStartModal() {
  return new Promise((resolve) => {
    const d = S.cfg.session_defaults || {}, versions = S.cfg.script_versions || ["v1"];
    const lastVersion = localStorage.getItem("pd_script_version");
    const fromCampaign = campaignScript();
    const chosen = versions.indexOf(fromCampaign) >= 0 ? fromCampaign
      : versions.indexOf(lastVersion) >= 0 ? lastVersion : (S.cfg.default_script_version || versions[0]);
    openModal(
      '<div class="dh"><div><h2>Start a power session</h2><p class="sub">Leads dial themselves after each wrap-up. ' +
      "The session ends at whichever target comes first, then shows you its funnel.</p></div>" + closeX() + "</div>" +
      '<div class="formrow"><div><label class="lbl" for="ss-dials">Target dials</label><input class="field num" id="ss-dials" type="number" min="1" max="400" value="' + (d.target_dials || 100) + '"></div>' +
      '<div><label class="lbl" for="ss-mins">Or minutes</label><input class="field num" id="ss-mins" type="number" min="5" max="480" value="' + (d.target_minutes || 90) + '"></div></div>' +
      '<div><label class="lbl">Script version <span class="muted" style="font-size:12px">· stats split by version, so change one thing at a time</span></label>' +
      '<div class="seg" id="ss-version">' + versions.map((v) => '<button data-v="' + esc(v) + '" aria-selected="' + (v === chosen) + '">' + esc(v) + "</button>").join("") + "</div></div>" +
      '<div class="acts"><button class="btn" data-close>Cancel</button><button class="btn primary" id="ss-go" autofocus>' + icon("play") + 'Start <kbd class="onfill">enter</kbd></button></div>');
    let version = chosen, done = false;
    $("ss-version").addEventListener("click", (e) => {
      const b = e.target.closest("[data-v]"); if (!b) return;
      version = b.getAttribute("data-v");
      $("ss-version").querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", x === b ? "true" : "false"));
    });
    const go = () => {
      done = true;
      localStorage.setItem("pd_script_version", version);
      const out = { script_version: version, target_dials: Math.max(1, +$("ss-dials").value || 100), target_minutes: Math.max(1, +$("ss-mins").value || 90) };
      closeModal();
      resolve(out);
    };
    $("ss-go").addEventListener("click", go);
    ["ss-dials", "ss-mins"].forEach((id) => $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") go(); }));
    $("ss-go").focus();
    const watch = setInterval(() => { if ($("modal").hidden) { clearInterval(watch); if (!done) resolve(null); } }, 200);
  });
}

export function sessionEndCard(data, why) {
  const f = data.funnel || {}, r = f.rates || {}, s = data.session || {}, top = (data.objections || [])[0];
  const line = (label, n, rate) => "<tr><td>" + label + "</td><td>" + n + (rate != null ? '<span class="t">' + rate + "</span>" : "") + "</td></tr>";
  openModal(
    '<div class="dh"><div><h2>Session done</h2><p class="sub">' + esc(why || "") + " Script " + esc(s.script_version || "") + " · " +
    fmtHMS(s.active_seconds || 0) + " active.</p></div>" + closeX() + "</div>" +
    '<table class="stat-table"><tbody>' +
    line("Dials", f.dials || 0) + line("Pickups", f.pickups || 0, fmtPct(r.pickup)) +
    line("DMs pitched", f.pitched || 0, "PR " + fmtPct(r.pr)) + line("Resonations", f.resonated || 0, "RR " + fmtPct(r.rr)) +
    line("Offered", f.offered || 0) + line("Booked", f.booked || 0, "ABR " + fmtPct(r.abr)) +
    line("Effective conversations", f.conversations || 0) + "</tbody></table>" +
    (top ? '<div class="callout" style="border-color:var(--accent)"><div>Most common objection: <b>' + esc(top.label) + "</b> (" + top.count +
      "). Add a rebuttal?<span class=\"by\">One better answer to the objection you hear most moves the whole funnel.</span></div></div>" : "") +
    '<div class="acts">' + (top ? '<button class="btn" id="se-obj" data-key="' + esc(top.key) + '">Open objections</button>' : "") +
    '<button class="btn primary" data-close>Done</button></div>');
  const b = $("se-obj");
  if (b) b.addEventListener("click", () => { closeModal(); actions.openObjections(b.getAttribute("data-key")); });
}
