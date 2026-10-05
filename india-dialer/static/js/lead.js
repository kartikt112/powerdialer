/* The lead card: who to call, the number in large type with a QR code for the
   phone camera, every other number to try next, and what is known so far. */
import { $, esc, prettyPhone, parseUTC, istWhen, rel } from "./util.js";
import { S, on, emit, actions, outcome } from "./state.js";
import { api } from "./api.js";
import { toast } from "./ui.js";
import { qrSvg } from "./qr.js";

const KIND = { mobile: "mobile", landline: "landline, usually reception", tollfree: "toll-free, a call centre" };
const ORIGIN = { dm: "the DM's own number", added: "added by you", referral: "from a referral", list: "" };

const stageName = (k) => (((S.cfg.pipeline || {}).stages || []).find((s) => s.key === k) || { label: k }).label.toLowerCase();

export function activePhone() {
  const p = S.cur && S.cur.phones;
  return p && p.length ? p[Math.min(S.phoneIdx, p.length - 1)] : null;
}
export function firstDialable(lead) {
  const i = (lead.phones || []).findIndex((p) => !p.blocked);
  return i < 0 ? 0 : i;
}
export function nextDialable() {
  const phones = (S.cur && S.cur.phones) || [];
  for (let k = 1; k <= phones.length; k++) {
    const i = (S.phoneIdx + k) % phones.length;
    if (!phones[i].blocked && i !== S.phoneIdx) return i;
  }
  return -1;
}

export function renderLead() {
  const l = S.cur;
  $("lead").hidden = !l;
  $("empty").hidden = !!l;
  if (!l) return;

  $("hold").hidden = !l.held;
  if (l.held) { $("hold-text").textContent = l.hold_reason; $("hold-clear").checked = false; }

  const meta = ["<b>Tier " + esc(l.tier) + "</b>", esc(l.segment)];
  if (l.city || l.state) meta.push(esc([l.city, l.state].filter(Boolean).join(", ")));
  if (l.est_drawings_month) meta.push("about " + esc(l.est_drawings_month) + " drawings a month");
  meta.push(l.attempts ? "try " + l.attempt_no + " of " + l.max_attempts : "first call");
  if (l.last_outcome) meta.push("last: " + esc(outcome(l.last_outcome).label.toLowerCase()));
  if (l.group) meta.push('<span class="flag">' + esc(l.group) + "</span>");
  const card = (l.samples || []).find((c) => ["won", "lost"].indexOf(c.stage) < 0);
  if (card) meta.push('<span class="flag">' + (card.kind === "demo" ? "Demo" : "Sample") + ": " + esc(stageName(card.stage)) + "</span>");
  if (l.referred_by_company) meta.push("referred by " + esc(l.referred_by_company));
  const w = l.window || {};
  if (w.day === "sunday" || w.day === "holiday") meta.push('<span class="flag">' + esc(w.day_note) + ": only callbacks they asked for</span>");
  else if (w.day === "saturday") meta.push('<span class="flag">' + esc(w.day_note) + "</span>");
  else if (w.legal && w.tier !== "power" && w.tier !== "soft") meta.push('<span class="flag">' + esc(w.label) + ", hand-picked</span>");
  if (l.flags && !l.held) meta.push('<span class="flag">Check: ' + esc(l.flags) + "</span>");
  $("c-meta").innerHTML = meta.join(" · ");
  $("c-company").textContent = l.company;
  const who = l.dm_name ? "<b>" + esc(l.dm_name) + "</b>" + (l.dm_title ? ", " + esc(l.dm_title) : "") : "";
  $("c-ask").innerHTML = "Ask for " + (who || "<b>" + esc(l.ask_for || "the quality head") + "</b>") +
    (who && l.ask_for && l.ask_for.indexOf(l.dm_name) < 0 ? '<span class="muted"> (list says: ' + esc(l.ask_for) + ")</span>" : "");
  $("c-why").textContent = l.why || "";

  renderNumber();
  renderNumbers();

  const q = encodeURIComponent;
  const links = [];
  if (l.website && /^https?:/.test(l.website)) links.push(["Website", l.website]);
  links.push(["Google: quality head", "https://www.google.com/search?q=" + q('"' + l.company + '" quality head')]);
  links.push(["LinkedIn", "https://www.linkedin.com/search/results/people/?keywords=" + q(l.company + " quality")]);
  links.push(["IndiaMART", "https://dir.indiamart.com/search.mp?ss=" + q(l.company)]);
  $("c-links").innerHTML = links.map((x) => '<a href="' + esc(x[1]) + '" target="_blank" rel="noopener noreferrer">' + esc(x[0]) + "</a>").join("") +
    (l.is_dnc ? "" : '<span class="links-fu"><button class="btn quiet sm" data-fu-here="whatsapp">WhatsApp <kbd>w</kbd></button><button class="btn quiet sm" data-fu-here="email">Email <kbd>e</kbd></button></span>');

  const last = (l.history || []).find((h) => h.notes || h.pain);
  $("c-last").hidden = !last && !l.pain && !l.notes;
  const bits = [];
  if (l.pain) bits.push("“" + esc(l.pain) + "”<span class=\"by\">their pain, in their words</span>");
  if (last && last.notes) bits.push("“" + esc(last.notes) + "”<span class=\"by\">" + esc(outcome(last.outcome).label) + ", " + istWhen(parseUTC(last.dialed_at)) + "</span>");
  if (l.notes) bits.push(esc(l.notes) + '<span class="by">lead notes</span>');
  $("c-last").innerHTML = bits.join("<br>");

  $("notes").value = "";
  syncDiscovery();
  emit("lead", l);
}

export function renderNumber() {
  const l = S.cur, p = activePhone();
  const hasNumber = !!p;
  $("c-number").textContent = hasNumber ? prettyPhone(p.e164, p.kind) : "No number";
  $("c-number").disabled = !hasNumber;
  $("c-number-meta").innerHTML = hasNumber
    ? "<b>" + esc(KIND[p.kind] || p.kind) + "</b>" + (ORIGIN[p.origin] ? " · " + esc(ORIGIN[p.origin]) : "") +
      (p.blocked ? ' · <span class="flag">' + esc(p.blocked) + "</span>" : "") +
      (l.phones.length > 1 ? " · number " + (S.phoneIdx + 1) + " of " + l.phones.length : "")
    : "Find one with the research links, then add it below.";
  const blocked = l.dial_block || !hasNumber || (p && p.blocked);
  $("b-dial").href = hasNumber && !blocked ? "tel:" + p.dial : "#";
  $("b-dial").classList.toggle("disabled", !!blocked);
  $("b-dial").title = blocked ? (l.dial_block || (p && p.blocked) || "No number") : "Opens the call on your phone";
  $("qr-wrap").innerHTML = hasNumber && !blocked
    ? qrSvg("tel:" + p.dial, { ecl: "M", border: 3, label: "Scan to dial " + prettyPhone(p.e164, p.kind) }) + "<p>Scan with your phone to dial</p>"
    : "";
  $("dial-hint").textContent = l.dial_block || "";
  emit("number");
}

export function renderNumbers() {
  const l = S.cur;
  const rows = (l.phones || []).map((p, i) =>
    '<div class="num-row' + (i === S.phoneIdx ? " active" : "") + (p.blocked ? " blocked" : "") + '">' +
    '<span class="pos">' + (i + 1) + "</span>" +
    '<span><button class="btn quiet sm v" data-phone="' + i + '"' + (p.blocked ? " disabled" : "") + ">" + esc(prettyPhone(p.e164, p.kind)) + "</button>" +
    ' <span class="k">' + esc(p.kind) + (ORIGIN[p.origin] ? ", " + esc(ORIGIN[p.origin]) : "") + "</span></span>" +
    '<span class="why-blocked">' + esc(p.blocked || (p.last_result ? "last: " + outcome(p.last_result).label.toLowerCase() : "")) + "</span></div>").join("");
  $("c-numbers").innerHTML = (l.phones.length > 1 || !l.phones.length ? rows : "") +
    '<div class="add"><input class="field" id="add-number" type="tel" placeholder="Add a number" autocomplete="off"><button class="btn sm" id="b-add-number">Add</button></div>';
}

export function syncDiscovery() {
  document.querySelectorAll("#discovery [data-call]").forEach((el) => {
    if (document.activeElement !== el) el.value = (S.call && S.call[el.getAttribute("data-call")]) || "";
  });
}

export function freshCall(l) {
  return { objections: [], other: "", offered: false, dm_name: l.dm_name || "", dm_mobile: l.dm_mobile || "",
           whatsapp: l.whatsapp || "", email: l.email || "", language_pref: l.language_pref || "",
           actual_drawings_month: l.actual_drawings_month || "", current_method: l.current_method || "",
           software_used: l.software_used || "", pain: l.pain || "", demo_at: "", callback_at: "", asked: [] };
}

export function wireLead() {
  $("c-number").addEventListener("click", () => {
    const p = activePhone();
    if (p) navigator.clipboard && navigator.clipboard.writeText(p.dial).then(() => toast("ok", "Copied " + esc(prettyPhone(p.e164, p.kind)), { ms: 1500 }));
  });
  $("c-numbers").addEventListener("click", (e) => {
    const b = e.target.closest("[data-phone]");
    if (b && S.state === "ready") { S.phoneIdx = +b.getAttribute("data-phone"); renderNumber(); renderNumbers(); }
    if (e.target.id === "b-add-number") addNumber();
  });
  $("c-numbers").addEventListener("keydown", (e) => { if (e.target.id === "add-number" && e.key === "Enter") addNumber(); });
  $("hold-clear").addEventListener("change", function () {
    if (!this.checked) return;
    api("/api/lead/clear-hold", { lead_id: S.cur.id }).then((d) => {
      if (d.error) { toast("error", esc(d.error)); return; }
      S.cur = d.lead; renderLead();
      toast("ok", "Cleared. You can dial " + esc(S.cur.company) + ".");
    });
  });
  on("call", syncDiscovery);
  $("discovery").addEventListener("input", (e) => {
    const k = e.target.getAttribute("data-call");
    if (k) { S.call[k] = e.target.value.trim(); emit("call"); }
  });
  const langs = ["", "English", "Hindi", "Marathi", "Tamil", "Gujarati", "Kannada", "Telugu", "Other"];
  $("d-lang").innerHTML = langs.map((x) => '<option value="' + x + '">' + (x || "Not known") + "</option>").join("");
}

function addNumber() {
  const v = $("add-number").value.trim();
  if (!v) return;
  api("/api/lead/phone", { lead_id: S.cur.id, number: v }).then((d) => {
    if (d.error) { toast("error", esc(d.error)); return; }
    const keep = S.cur.phones.length;
    S.cur = d.lead;
    if (!keep) S.phoneIdx = firstDialable(S.cur);
    renderLead();
    actions.setState(S.state);
  });
}

export const leadAge = (l) => (l.last_called_at ? rel(parseUTC(l.last_called_at)) : "");
