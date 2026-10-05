/* Script editor: every step of every version, the objection cards, the rule
   and the three follow-up templates, editable from the cockpit at any time.
   Saves go to data/scripts.json, laid over config.yaml, so "Reset" always
   brings the shipped text back. Dashes are refused; claims the offer can't
   back (an engineer checking, security, certificates) get a warning. */
import { $, esc } from "./util.js";
import { S, emit, actions } from "./state.js";
import { api } from "./api.js";
import { openModal, closeX, toast } from "./ui.js";
import { renderTpl, renderPlain, leadVars } from "./script.js";

const FLOWS = [["call", "Call"], ["callback", "Callback"], ["follow_up", "Sample follow-up"]];
const TOKENS = ["dm_first", "company", "agent", "whatsapp", "email", "demo_when", "website", "city"];
const SAMPLE = { company: "Shree Precision Components", dm_name: "Rahul Deshmukh", city: "Pune", whatsapp: "+919820012345", email: "rahul@example.in" };

let version = "", sel = { kind: "step", id: "" }, lastField = null, creating = false, deleting = false;

const scripts = () => S.cfg.scripts || {};
const tree = () => (scripts().tree || {})[version] || { order: {}, steps: {} };
const edited = () => S.cfg.scripts_edited || {};
const paras = (text) => String(text || "").split(/\n\s*\n/).map((x) => x.replace(/\s*\n\s*/g, " ").trim()).filter(Boolean);
const lines = (text) => String(text || "").split("\n").map((x) => x.trim()).filter(Boolean);

function isEdited(item) {
  const e = edited();
  if (item.kind === "step") return (((e.steps || {})[version]) || []).indexOf(item.id) >= 0;
  if (item.kind === "template") return (e.templates || []).indexOf(item.id) >= 0;
  if (item.kind === "obj") return (e.objections || []).indexOf(item.id) >= 0;
  if (item.kind === "rule") return !!e.rule;
  return false;
}

function navHTML() {
  const t = tree(), seen = {};
  const item = (kind, id, label) => '<button class="ed-item' + (sel.kind === kind && sel.id === id ? " on" : "") + '" data-kind="' + kind + '" data-id="' + esc(id) + '">' +
    "<span>" + esc(label) + "</span>" + (isEdited({ kind, id }) ? '<span class="muted small">edited</span>' : "") + "</button>";
  let h = FLOWS.map((f) => {
    const ids = (t.order[f[0]] || []).filter((id) => !seen[id] && (seen[id] = true));
    if (!ids.length) return "";
    return '<p class="ed-group">' + f[1] + "</p>" + ids.map((id) => { const st = t.steps[id] || {};
      return item("step", id, (st.title || id) + (st.when ? ", name known" : st.unless ? ", no name" : "")); }).join("");
  }).join("");
  h += '<p class="ed-group">Objections</p>' + item("rule", "rule", "The rule on top") +
    (scripts().objections || []).map((o) => item("obj", o.key, o.title)).join("") + item("obj", "__new", "Add an objection");
  h += '<p class="ed-group">Follow-up templates</p>' + Object.keys(scripts().templates || {}).map((k) => item("template", k, scripts().templates[k].label || k)).join("");
  return h;
}

const area = (id, label, value, rows, hint) => '<label class="lbl" for="' + id + '">' + label + (hint ? ' <span class="muted">' + hint + "</span>" : "") +
  '</label><textarea class="field" id="' + id + '" rows="' + rows + '">' + esc(value) + "</textarea>";
const rowsFor = (text) => Math.min(12, 3 + (String(text).length / 70 | 0));

function formHTML() {
  if (sel.kind === "template") {
    const m = (scripts().templates || {})[sel.id] || {};
    return area("ed-wa", "WhatsApp text", m.whatsapp || "", rowsFor(m.whatsapp || "")) +
      '<label class="lbl" for="ed-subject">Email subject <span class="muted">their first name only</span></label><input class="field" id="ed-subject" value="' + esc(m.subject || "") + '">' +
      area("ed-email", "Email body", m.email || "", rowsFor(m.email || ""));
  }
  if (sel.kind === "rule") return area("ed-rule", "Shown at the top of the objections panel", scripts().objection_rule || "", 4);
  if (sel.kind === "obj") {
    const o = (scripts().objections || []).find((x) => x.key === sel.id) || {};
    return (sel.id === "__new" ? '<label class="lbl" for="ed-title">What they say</label><input class="field" id="ed-title" maxlength="80" placeholder="We use a consultant">' : "") +
      area("ed-anchor", "1. Anchor", o.anchor || "", 2, "agree, lower the guard") + area("ed-disrupt", "2. Pattern disrupt", o.disrupt || "", 2) +
      area("ed-question", "3. Question", o.question || "", 2, "hand it back to them");
  }
  const s = tree().steps[sel.id] || {};
  let h = '<label class="lbl" for="ed-title">Step name</label><input class="field" id="ed-title" maxlength="60" value="' + esc(s.title || "") + '">' +
    area("ed-say", "What you say, English", (s.say || []).join("\n\n"), rowsFor((s.say || []).join(" ")), "blank line between lines") +
    area("ed-say-hi", "Hinglish", (s.say_hi || []).join("\n\n"), rowsFor((s.say_hi || []).join(" ")), "same order as the English") +
    area("ed-cue", "Stage direction", s.cue || "", 2);
  if (s.chips) h += area("ed-chips", "Questions", s.chips.map((c) => c.q + (c.q_hi ? " => " + c.q_hi : "")).join("\n"), 5, "one per line: English => Hinglish");
  if (s.rules) h += area("ed-rules", "Reminders", s.rules.join("\n"), 3, "one per line");
  return h;
}

function read() {
  const v = (id) => ($(id) ? $(id).value : null);
  if (sel.kind === "template") return { op: "template", kind: sel.id, whatsapp: v("ed-wa"), subject: v("ed-subject"), email: v("ed-email") };
  if (sel.kind === "rule") return { op: "rule", text: v("ed-rule") };
  if (sel.kind === "obj") {
    const o = (scripts().objections || []).find((x) => x.key === sel.id) || {};
    return { op: "objection", key: sel.id === "__new" ? "" : sel.id, title: sel.id === "__new" ? v("ed-title") : o.title,
             anchor: v("ed-anchor"), disrupt: v("ed-disrupt"), question: v("ed-question") };
  }
  const fields = { title: v("ed-title"), say: paras(v("ed-say")), say_hi: paras(v("ed-say-hi")), cue: (v("ed-cue") || "").trim() };
  if ($("ed-rules")) fields.rules = lines(v("ed-rules"));
  if ($("ed-chips")) fields.chips = lines(v("ed-chips")).map((l) => { const i = l.indexOf("=>"); return i < 0 ? { q: l } : { q: l.slice(0, i).trim(), q_hi: l.slice(i + 2).trim() }; });
  return { op: "step", version, id: sel.id, fields };
}

function preview() {
  const lead = S.cur || SAMPLE, vars = leadVars(lead, S.cur ? null : { dm_name: SAMPLE.dm_name, whatsapp: SAMPLE.whatsapp });
  const d = read();
  let h;
  const para = (t) => esc(renderPlain(t, vars)).replace(/\n/g, "<br>");
  if (d.op === "template") h = '<p class="lbl">WhatsApp</p><p>' + para(d.whatsapp) + '</p><p class="lbl" style="margin-top:12px">Email: ' + esc(renderPlain(d.subject, vars)) + "</p><p>" + para(d.email) + "</p>";
  else if (d.op === "rule") h = esc(d.text);
  else if (d.op === "objection") h = "<ol>" + [d.anchor, d.disrupt, d.question].filter(Boolean).map((x) => "<li>" + renderTpl(x, vars) + "</li>").join("") + "</ol>";
  else h = d.fields.say.map((l, i) => '<p class="say' + (i ? " alt" : "") + '">' + renderTpl(l, vars) + "</p>" +
      (d.fields.say_hi[i] ? '<p class="say hi under">' + renderTpl(d.fields.say_hi[i], vars) + "</p>" : "")).join("") +
    (d.fields.cue ? '<p class="cue">' + renderTpl(d.fields.cue, vars) + "</p>" : "");
  $("ed-preview").innerHTML = h || '<span class="muted">Nothing yet.</span>';
  $("ed-preview-for").textContent = S.cur ? "with " + S.cur.company : "with a sample lead";
}

function render(status, ok) {
  const versions = S.cfg.script_versions || Object.keys(scripts().tree || {});
  const custom = (edited().custom_versions || []).indexOf(version) >= 0;
  openModal(
    '<div class="dh"><div><h2>Scripts and templates</h2><p class="sub">Change any line, any time, even mid-session. It shows on the next step you open.</p></div>' + closeX() + "</div>" +
    '<div class="ed-versions"><span class="tabs-text" id="ed-version">' + versions.map((v) => '<button data-v="' + esc(v) + '" aria-selected="' + (v === version) + '">' + esc(v) + "</button>").join("") + "</span>" +
    (creating
      ? '<span class="ed-inline"><input class="field" id="ed-newname" maxlength="12" placeholder="v' + (versions.length + 1) + '" aria-label="Name for the new version"><button class="btn sm" id="ed-create">Create from ' + esc(version) + '</button><button class="btn sm quiet" id="ed-cancel">Cancel</button></span>'
      : '<button class="btn sm quiet" id="ed-newv">New version from ' + esc(version) + "</button>") +
    (custom ? '<button class="btn sm quiet" id="ed-delv">' + (deleting ? "Really delete " + esc(version) + "?" : "Delete " + esc(version)) + "</button>" : "") + "</div>" +
    '<div class="ed-body"><nav class="ed-nav" id="ed-nav">' + navHTML() + "</nav>" +
    '<div class="ed-form"><div id="ed-fields">' + formHTML() + "</div>" +
    '<div class="ed-tokens"><span class="muted small">Insert</span>' + TOKENS.map((t) => '<button class="btn quiet sm" data-token="{' + t + '}">' + t + "</button>").join("") + "</div>" +
    '<p class="lbl">Preview <span class="muted" id="ed-preview-for"></span></p><div class="ed-preview" id="ed-preview"></div>' +
    '<p class="' + (ok ? "ok" : "err") + '" id="ed-err">' + esc(status || "") + "</p>" +
    '<div class="acts">' + '<button class="btn primary" id="ed-save">Save</button>' +
    (isEdited(sel) ? '<button class="btn quiet" id="ed-reset">Reset to the shipped text</button>' : "") + "</div></div></div>", { xwide: true });
  preview();

  $("ed-version").addEventListener("click", (e) => { const b = e.target.closest("[data-v]"); if (b) { version = b.getAttribute("data-v"); pickFirst(); render(); } });
  $("ed-nav").addEventListener("click", (e) => { const b = e.target.closest(".ed-item"); if (b) { sel = { kind: b.getAttribute("data-kind"), id: b.getAttribute("data-id") }; render(); } });
  $("ed-fields").addEventListener("input", preview);
  $("ed-fields").addEventListener("focusin", (e) => { if (e.target.matches("textarea, input")) lastField = e.target; });
  $("modal-box").querySelector(".ed-tokens").addEventListener("click", (e) => {
    const b = e.target.closest("[data-token]"), f = lastField && document.body.contains(lastField) ? lastField : $("ed-say") || $("ed-wa") || $("ed-anchor") || $("ed-rule");
    if (!b || !f) return;
    const t = b.getAttribute("data-token"), a = f.selectionStart || 0, z = f.selectionEnd || 0;
    f.value = f.value.slice(0, a) + t + f.value.slice(z); f.focus(); f.selectionStart = f.selectionEnd = a + t.length; preview();
  });
  $("ed-save").addEventListener("click", () => send(read(), "Saved."));
  if ($("ed-reset")) $("ed-reset").addEventListener("click", () => send(
    sel.kind === "template" ? { op: "reset_template", kind: sel.id } : sel.kind === "rule" ? { op: "rule", text: "" }
      : sel.kind === "obj" ? { op: "reset_objection", key: sel.id } : { op: "reset_step", version, id: sel.id }, "Back to the shipped text."));
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
  api("/api/scripts", body).then((d) => {
    if (d.error) { $("ed-err").className = "err"; $("ed-err").textContent = d.error; return; }
    S.cfg.scripts = d.scripts; S.cfg.script_versions = d.script_versions; S.cfg.scripts_edited = d.scripts_edited;
    if (switchTo) { version = d.script_versions.indexOf(switchTo.toLowerCase()) >= 0 ? switchTo.toLowerCase() : d.script_versions[0]; pickFirst(); }
    if (body.op === "objection" && sel.id === "__new") {
      const made = (d.scripts.objections || []).find((o) => o.title === body.title);
      if (made) sel = { kind: "obj", id: made.key };
    }
    emit("scripts");
    render(okText + ((d.warnings || []).length ? " Check it doesn't promise " + d.warnings.join(" or ") + " the offer can't back." : ""), !(d.warnings || []).length);
  });
}

function pickFirst() {
  const t = tree();
  if (sel.kind === "step" && !t.steps[sel.id]) sel = { kind: "step", id: (t.order.call || Object.keys(t.steps))[0] };
}

export function openScriptEditor(target) {
  const versions = S.cfg.script_versions || Object.keys(scripts().tree || {});
  if (!versions.length) { toast("error", "No scripts are configured."); return; }
  const want = S.session.script || S.cfg.default_script_version;
  version = versions.indexOf(want) >= 0 ? want : versions.indexOf(version) >= 0 ? version : versions[0];
  if (target && target.indexOf("obj:") === 0) sel = { kind: "obj", id: target.slice(4) };
  else sel = { kind: "step", id: target || sel.id };
  creating = deleting = false;
  pickFirst();
  render();
}

export function wireEditor() { actions.editScripts = openScriptEditor; }
