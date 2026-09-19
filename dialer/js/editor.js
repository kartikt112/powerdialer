/* Script editor. Every step of every version, both follow-up emails and the
   objections rule, editable from the cockpit. Saves go to the server, which
   keeps them in DATA_DIR/scripts.json laid over config.yaml, so wording tuned
   on the phones survives a redeploy and "Reset" always brings the shipped
   text back. Objection cards are edited in their own panel (o). */
import { $, esc } from "./util.js";
import { S, emit } from "./state.js";
import { api } from "./api.js";
import { openModal, closeX, toast } from "./ui.js";
import { renderTpl, renderPlain, leadVars } from "./script.js";

const FLOWS = [["call", "Call"], ["voicemail", "Voicemail"], ["inbound", "They called back"]];
const EMAILS = [["booked_confirm", "After a booked call"], ["no_book_intrigue", "After resonated, no book"]];
const TOKENS = ["dm_first", "company", "process", "oem", "agent", "pain", "email", "callback_number", "calendly", "booked_when"];
const SAMPLE = { first: "Dale", last: "Harlan", co: "Harlan Precision Machining", title: "Owner", city: "Dayton", state: "OH",
                 process: "machining", oem: "Honda", email: "dale@harlanprecision.example", dm_name: "Dale Harlan",
                 caller_id_spoken: "989, 375, 1429" };

let version = "", sel = { kind: "step", id: "" }, lastField = null, creating = false, deleting = false;

const tree = () => (S.cfg.scripts.tree || {})[version] || { order: {}, steps: {} };
const edited = () => S.cfg.scripts_edited || { steps: {}, emails: [], custom_versions: [] };
const paras = (text) => String(text || "").split(/\n\s*\n/).map((x) => x.replace(/\s*\n\s*/g, " ").trim()).filter(Boolean);
const lines = (text) => String(text || "").split("\n").map((x) => x.trim()).filter(Boolean);

function isEdited(item) {
  const e = edited();
  if (item.kind === "step") return ((e.steps || {})[version] || []).indexOf(item.id) >= 0;
  if (item.kind === "email") return (e.emails || []).indexOf(item.id) >= 0;
  return !!e.rule;
}

function navHTML() {
  const t = tree();
  const item = (kind, id, label) => '<button class="ed-item' + (sel.kind === kind && sel.id === id ? " on" : "") + '" data-kind="' + kind + '" data-id="' + esc(id) + '">' +
    "<span>" + esc(label) + "</span>" + (isEdited({ kind, id }) ? '<i title="Edited here"></i>' : "") + "</button>";
  return FLOWS.map((f) => '<div class="ed-group">' + f[1] + "</div>" + (t.order[f[0]] || []).map((id) => { const st = t.steps[id] || {};
      const cond = { dm_name: "name" }[st.when || st.unless] || (st.when || st.unless);
      return item("step", id, (st.title || id) + (st.when ? ", " + cond + " known" : st.unless ? ", " + cond + " unknown" : "") + (st.attempts ? ", try " + st.attempts.join(" and ") : "")); }).join("")).join("") +
    '<div class="ed-group">Follow-up emails</div>' + EMAILS.map((m) => item("email", m[0], m[1])).join("") +
    '<div class="ed-group">Objections</div>' + item("rule", "rule", "The rule on top of the panel");
}

const area = (id, label, value, rows, hint) => '<label class="lbl" for="' + id + '">' + label + (hint ? ' <span class="hint">' + hint + "</span>" : "") +
  '</label><textarea class="field" id="' + id + '" rows="' + rows + '">' + esc(value) + "</textarea>";

function formHTML() {
  if (sel.kind === "email") {
    const m = (S.cfg.scripts.emails || {})[sel.id] || {};
    return '<label class="lbl" for="ed-subject">Subject <span class="hint">their first name only keeps it personal</span></label><input class="field" id="ed-subject" value="' + esc(m.subject || "") + '">' +
      area("ed-body", "Body", (m.body || "").trim(), 14);
  }
  if (sel.kind === "rule") return area("ed-rule", "Shown at the top of the objections panel", S.cfg.scripts.objection_rule || "", 5) +
    '<p class="hint" style="margin-top:10px">The objection cards themselves are edited where you use them: press <kbd>o</kbd>, then Edit on a card.</p>';
  const s = tree().steps[sel.id] || {};
  let h = '<label class="lbl" for="ed-title">Step name</label><input class="field" id="ed-title" maxlength="60" value="' + esc(s.title || "") + '">' +
    area("ed-say", "What you say", (s.say || []).join("\n\n"), Math.min(14, 3 + (s.say || []).join(" ").length / 70 | 0), "blank line between lines. The first is the main one, the rest are alternatives.") +
    area("ed-cue", "Stage direction", s.cue || "", 2, "shown in grey italics");
  if (s.prompts) h += area("ed-prompts", "Questions to pick from", s.prompts.join("\n\n"), 6, "blank line between questions");
  if (s.contracts) h += area("ed-contracts", "Mini-contracts", s.contracts.join("\n\n"), 5, "one tick box each");
  if (s.chips) h += area("ed-chips", "Tap-to-reveal answers", s.chips.map((c) => c.q + " => " + c.a).join("\n"), 6, "one per line: their question => your answer");
  if (s.rules) h += area("ed-rules", "Reminders", s.rules.join("\n"), 4, "one per line");
  return h;
}

function read() {
  const v = (id) => ($(id) ? $(id).value : null);
  if (sel.kind === "email") return { op: "email", kind: sel.id, subject: v("ed-subject"), body: v("ed-body") };
  if (sel.kind === "rule") return { op: "rule", text: v("ed-rule") };
  const fields = { title: v("ed-title"), say: paras(v("ed-say")), cue: (v("ed-cue") || "").trim() };
  if ($("ed-prompts")) fields.prompts = paras(v("ed-prompts"));
  if ($("ed-contracts")) fields.contracts = paras(v("ed-contracts"));
  if ($("ed-rules")) fields.rules = lines(v("ed-rules"));
  if ($("ed-chips")) fields.chips = lines(v("ed-chips")).map((l) => { const i = l.indexOf("=>"); return i < 0 ? { q: l, a: "" } : { q: l.slice(0, i).trim(), a: l.slice(i + 2).trim() }; });
  return { op: "step", version, id: sel.id, fields };
}

function preview() {
  const vars = leadVars(S.cur || SAMPLE, S.cur ? null : { pain: "three days a package", booked_for_local: "2026-09-24T14:00" });
  const d = read();
  let h;
  if (d.op === "email") h = "<b>" + esc(renderPlain(d.subject, vars)) + "</b><br><br>" + esc(renderPlain(d.body, vars)).replace(/\n/g, "<br>");
  else if (d.op === "rule") h = esc(d.text);
  else h = d.fields.say.map((l, i) => '<p class="' + (i ? "say alt" : "say") + '">' + renderTpl(l, vars) + "</p>").join("") +
    (d.fields.cue ? '<p class="cue">' + renderTpl(d.fields.cue, vars) + "</p>" : "");
  $("ed-preview").innerHTML = h || '<span class="hint">Nothing to say yet.</span>';
  $("ed-preview-for").textContent = S.cur ? "with " + (S.cur.co || "this lead") : "with a sample lead";
}

function render(status) {
  const versions = S.cfg.script_versions || Object.keys(S.cfg.scripts.tree || {});
  const custom = (edited().custom_versions || []).indexOf(version) >= 0;
  openModal(
    '<div class="dh"><div><h2>Scripts</h2><p class="sub">Change any line, any time, even mid-session. Saved edits apply to the next step you open.</p></div>' + closeX() + "</div>" +
    '<div class="ed-versions"><div class="seg" id="ed-version">' + versions.map((v) => '<button data-v="' + esc(v) + '" aria-selected="' + (v === version) + '">' + esc(v) + "</button>").join("") + "</div>" +
    (creating
      ? '<span class="ed-inline"><input class="field" id="ed-newname" maxlength="12" placeholder="v' + (versions.length + 1) + '" aria-label="Name for the new version"><button class="btn sm" id="ed-create">Create from ' + esc(version) + '</button><button class="btn sm quiet" id="ed-cancel">Cancel</button></span>'
      : '<button class="btn sm quiet" id="ed-newv">New version from ' + esc(version) + "</button>") +
    (custom ? '<button class="btn sm quiet danger" id="ed-delv">' + (deleting ? "Really delete " + esc(version) + "?" : "Delete " + esc(version)) + "</button>" : "") + "</div>" +
    '<div class="ed-body"><nav class="ed-nav" id="ed-nav">' + navHTML() + "</nav>" +
    '<div class="ed-form"><div id="ed-fields">' + formHTML() + "</div>" +
    '<div class="ed-tokens"><span class="hint">Insert</span>' + TOKENS.map((t) => '<button class="ed-token" data-token="{' + t + '}">' + t + "</button>").join("") +
    '<button class="ed-token" data-token="{?oem} ... {/oem}" title="Shown only when the lead has that value">if set</button></div>' +
    '<div class="ed-preview-h"><span class="lbl" style="margin:0">Preview <span class="hint" id="ed-preview-for"></span></span></div><div class="ed-preview step" id="ed-preview"></div>' +
    '<p class="err" id="ed-err">' + esc(status || "") + "</p>" +
    '<div class="acts">' + (isEdited(sel) ? '<button class="btn quiet" id="ed-reset">Reset to the shipped text</button>' : "") +
    '<button class="btn primary" id="ed-save">Save</button></div></div></div>', { xwide: true });
  preview();

  $("ed-version").addEventListener("click", (e) => { const b = e.target.closest("[data-v]"); if (b) { version = b.getAttribute("data-v"); pickFirst(); render(); } });
  $("ed-nav").addEventListener("click", (e) => { const b = e.target.closest(".ed-item"); if (b) { sel = { kind: b.getAttribute("data-kind"), id: b.getAttribute("data-id") }; render(); } });
  $("ed-fields").addEventListener("input", preview);
  $("ed-fields").addEventListener("focusin", (e) => { if (e.target.matches("textarea, input")) lastField = e.target; });
  $("modal-box").querySelector(".ed-tokens").addEventListener("click", (e) => {
    const b = e.target.closest("[data-token]"), f = lastField && document.body.contains(lastField) ? lastField : $("ed-say") || $("ed-body") || $("ed-rule");
    if (!b || !f) return;
    const t = b.getAttribute("data-token"), a = f.selectionStart || 0, z = f.selectionEnd || 0;
    f.value = f.value.slice(0, a) + t + f.value.slice(z); f.focus(); f.selectionStart = f.selectionEnd = a + t.length; preview();
  });
  $("ed-save").addEventListener("click", () => send(read(), "Saved."));
  if ($("ed-reset")) $("ed-reset").addEventListener("click", () => send(sel.kind === "email" ? { op: "reset_email", kind: sel.id } : sel.kind === "rule" ? { op: "rule", text: "" } : { op: "reset_step", version, id: sel.id }, "Back to the shipped text."));
  if ($("ed-newv")) $("ed-newv").addEventListener("click", () => { creating = true; render(); $("ed-newname").focus(); });
  if ($("ed-create")) {
    const create = () => { const name = ($("ed-newname").value || $("ed-newname").placeholder).trim(); creating = false;
      send({ op: "version", name, extends: version }, "Version created. Change one step, then run a session on it to compare.", name); };
    $("ed-create").addEventListener("click", create);
    $("ed-newname").addEventListener("keydown", (e) => { if (e.key === "Enter") create(); });
    $("ed-cancel").addEventListener("click", () => { creating = false; render(); });
  }
  if ($("ed-delv")) $("ed-delv").addEventListener("click", () => {
    if (!deleting) { deleting = true; render(); return; }
    deleting = false;
    send({ op: "delete_version", name: version }, "Version deleted. Calls already logged keep their label.", (S.cfg.script_versions || [])[0]);
  });
}

function send(body, okText, switchTo) {
  api("/api/scripts", Object.assign({ agent: S.agent }, body)).then((d) => {
    if (d.error) { $("ed-err").textContent = d.error; return; }
    S.cfg.scripts = d.scripts; S.cfg.script_versions = d.script_versions; S.cfg.scripts_edited = d.scripts_edited;
    if (switchTo) { version = d.script_versions.indexOf(switchTo.toLowerCase()) >= 0 ? switchTo.toLowerCase() : d.script_versions[0]; pickFirst(); }
    emit("scripts");
    render(okText + ((d.warnings || []).length ? " Check: " + d.warnings.join(", ") + "." : ""));
    $("ed-err").className = "err ok";
  });
}

function pickFirst() {
  const t = tree();
  if (sel.kind === "step" && !t.steps[sel.id]) sel = { kind: "step", id: (t.order.call || Object.keys(t.steps))[0] };
}

export function openScriptEditor(stepId, atVersion) {
  const versions = S.cfg.script_versions || Object.keys((S.cfg.scripts || {}).tree || {});
  if (!versions.length) { toast("warn", "No scripts are configured."); return; }
  version = versions.indexOf(atVersion) >= 0 ? atVersion : (versions.indexOf(version) >= 0 ? version : versions[0]);
  sel = { kind: "step", id: stepId || sel.id };
  creating = deleting = false;
  pickFirst();
  render();
}
