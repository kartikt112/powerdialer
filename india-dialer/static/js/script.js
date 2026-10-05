/* The script column: the call as a tree walked with the arrow keys, in
   English with the Hinglish line under it when the toggle is on (h).
   Objections open on o. Every word comes from config.yaml (scripts), laid
   over by edits made in the cockpit, picked by the session's script version. */
import { $, esc, firstName, prettyPhone, istInput, fromIstInput, istWhen, store } from "./util.js";
import { S, on, emit, actions, outcome } from "./state.js";
import { toast } from "./ui.js";

const FLOWS = [["call", "Call"], ["callback", "Callback"], ["follow_up", "Follow-up"]];
let flow = "call", idx = 0, objOpen = false, objQuery = "";
let hinglish = store.get("in_hinglish", "0") === "1";

/* ---- tokens -------------------------------------------------------------------- */

function nextWorkday11() {
  const d = new Date(Date.now() + 330 * 60000);
  do { d.setUTCDate(d.getUTCDate() + 1); } while (d.getUTCDay() === 0);
  d.setUTCHours(11, 0, 0, 0);
  return new Date(d.getTime() - 330 * 60000);
}

/* How the company name is said out loud: no brackets, no Pvt Ltd. */
export function spoken(name) {
  const n = String(name || "").replace(/\s*\([^)]*\)\s*/g, " ")
    .replace(/[,.]?\s+(private|pvt\.?)\s+(limited|ltd\.?)\s*$/i, "").replace(/[,.]?\s+(limited|ltd\.?|llp|inc\.?)\s*$/i, "")
    .replace(/\s+/g, " ").trim();
  return n || String(name || "");
}

export function leadVars(l, call) {
  const c = call || S.call || {};
  const dm = c.dm_name || (l && l.dm_name) || "";
  const wa = c.whatsapp || (l && l.whatsapp) || "";
  const demo = c.demo_at ? fromIstInput(c.demo_at) : null;
  return {
    dm_name: dm, dm_first: firstName(dm) || "sir", company: spoken(l && l.company) || "your company",
    agent: (S.cfg.brand && S.cfg.brand.caller) || "Pawan", city: (l && l.city) || "",
    whatsapp: wa ? prettyPhone(wa, "mobile") : "", email: c.email || (l && l.email) || "",
    demo_when: demo ? istWhen(demo).replace("Tomorrow", "tomorrow").replace("Today", "today") : "tomorrow at 11",
    website: ((S.cfg.brand && S.cfg.brand.website) || "").replace(/^https?:\/\//, "")
  };
}

function expand(tpl, vars) {
  return String(tpl || "").replace(/\{([?!])(\w+)\}([\s\S]*?)\{\/\2\}/g, (_, op, k, body) => ((op === "?") === !!vars[k]) ? body : "");
}
/* {token}, {?token}shown when set{/token}, {!token}shown when empty{/token}, **bold** */
export function renderTpl(tpl, vars) {
  return esc(expand(tpl, vars)).replace(/\{(\w+)\}/g, (m, k) =>
    Object.prototype.hasOwnProperty.call(vars, k) ? '<span class="tok">' + esc(vars[k]) + "</span>" : m)
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
}
export function renderPlain(tpl, vars) {
  return expand(tpl, vars).replace(/\{(\w+)\}/g, (m, k) => Object.prototype.hasOwnProperty.call(vars, k) ? vars[k] : m).replace(/\*\*/g, "");
}

/* ---- the tree ------------------------------------------------------------------------ */

function version() {
  const tree = (S.cfg.scripts && S.cfg.scripts.tree) || {};
  const want = S.session.script || S.cfg.default_script_version;
  return tree[want] ? want : Object.keys(tree)[0];
}
function tree() { return ((S.cfg.scripts && S.cfg.scripts.tree) || {})[version()] || { order: {}, steps: {} }; }

function visible(step, vars) {
  if (step.when && !vars[step.when]) return false;
  if (step.unless && vars[step.unless]) return false;
  return true;
}
function steps() {
  if (!S.cur) return [];
  const t = tree(), vars = leadVars(S.cur);
  return (t.order[flow] || []).map((id) => Object.assign({ id }, t.steps[id] || {})).filter((s) => visible(s, vars));
}
export const currentVersion = () => version();
export function currentStepId() { const l = steps(); return l.length ? l[Math.max(0, Math.min(l.length - 1, idx))].id : ""; }

export function jumpTo(target) {
  const list = steps();
  if (typeof target === "string") {
    let i = list.findIndex((s) => s.id === target);
    if (i < 0) {
      const t = tree(), home = Object.keys(t.order).find((f) => t.order[f].indexOf(target) >= 0);
      if (!home) return;
      flow = home;
      i = Math.max(0, steps().findIndex((s) => s.id === target));
    }
    idx = i;
  } else idx = Math.max(0, Math.min(list.length - 1, idx + target));
  objOpen = false;
  renderScript();
}
function setFlow(f) { flow = f; idx = 0; objOpen = false; renderScript(); }

/* ---- one step --------------------------------------------------------------------------- */

const CAPTURE = {
  dm_name: ["Decision maker", 'maxlength="60"'], dm_mobile: ["Their direct mobile", 'type="tel" maxlength="20"'],
  whatsapp: ["WhatsApp number", 'type="tel" maxlength="20"'], email: ["Email", 'type="email" maxlength="80"'],
  demo_at: ["Demo, IST", 'type="datetime-local"'], actual_drawings_month: ["Drawings a month", 'maxlength="20"'],
  software_used: ["Software", 'maxlength="120"'], pain: ["In their words", 'maxlength="300"']
};
function input(key, cls) {
  const def = CAPTURE[key] || [key, ""];
  let v = S.call[key] || "";
  if (key === "whatsapp" && v && /^\+?91/.test(v)) v = prettyPhone(v, "mobile");
  return '<div class="' + (cls || "") + '"><label class="lbl" for="sc-' + key + '">' + esc(def[0]) + "</label>" +
    '<input class="field" id="sc-' + key + '" data-bind="' + key + '" value="' + esc(v) + '" ' + def[1] + ' autocomplete="off"></div>';
}

function lines(s, vars) {
  const en = s.say || [], hi = s.say_hi || [];
  let h = "";
  const n = Math.max(en.length, hinglish ? hi.length : 0);
  for (let i = 0; i < n; i++) {
    if (en[i]) h += '<p class="say' + (i ? " alt" : "") + '">' + renderTpl(en[i], vars) + "</p>";
    if (hinglish && hi[i]) h += '<p class="say hi' + (en[i] ? " under" : "") + '" lang="hi-Latn">' + renderTpl(hi[i], vars) + "</p>";
  }
  return h;
}

function stepHTML(s, vars) {
  let h = '<h3 class="step-t">' + esc(s.title || s.id) + "</h3>" + lines(s, vars);
  if (s.cue) h += '<p class="cue">' + renderTpl(s.cue, vars) + "</p>";
  if ((s.chips || []).length) {
    const asked = S.call.asked || [];
    h += '<div class="chips-q">' + s.chips.map((c, i) => {
      const used = asked.indexOf(s.id + ":" + i) >= 0;
      return '<button class="' + (used ? "used" : "") + '" data-chip="' + i + '">' + renderTpl(c.q, vars) +
        (hinglish && c.q_hi ? '<span class="q-hi">' + renderTpl(c.q_hi, vars) + "</span>" : "") + "</button>" +
        (used && c.field ? input(c.field, "chip-field") : "");
    }).join("") + "</div>";
  }
  if ((s.capture || []).length) {
    h += '<div class="capture grid2">' + s.capture.map((k) => input(k)).join("") + "</div>";
    if (s.capture.indexOf("demo_at") >= 0) {
      const picks = [["Tomorrow 11:00", nextWorkday11()], ["Tomorrow 15:00", new Date(nextWorkday11().getTime() + 4 * 3600000)]];
      h += '<div class="acts">' + picks.map((p) => '<button class="btn sm" data-demo="' + istInput(p[1]) + '">' + esc(p[0]) + "</button>").join("") + "</div>";
    }
  }
  if ((s.branches || []).length) h += '<div class="branches">' + s.branches.map((b, i) =>
    '<button class="btn sm" data-branch="' + i + '">' + esc(b.label) + (b.outcome ? '<span class="muted"> · saves ' + esc(outcome(b.outcome).label.toLowerCase()) + "</span>" : "") + "</button>").join("") + "</div>";
  if ((s.rules || []).length) h += '<ul class="rules">' + s.rules.map((r) => "<li>" + renderTpl(r, vars) + "</li>").join("") + "</ul>";
  return h;
}

export function renderScript() {
  $("hinglish").checked = hinglish;
  $("flow-tabs").innerHTML = FLOWS.filter((f) => (tree().order[f[0]] || []).length).map((f) =>
    '<button role="tab" data-f="' + f[0] + '" aria-selected="' + (f[0] === flow) + '">' + f[1] + "</button>").join("");
  $("script-ver").textContent = S.cur ? version() + ((S.cfg.scripts_edited || {}).steps && ((S.cfg.scripts_edited.steps[version()] || []).length) ? ", edited" : "") : "";
  $("b-obj").setAttribute("aria-pressed", String(objOpen));
  if (objOpen) { renderObjections(); return; }
  $("objpanel").hidden = true; $("treebox").hidden = false;
  if (!S.cur) {
    $("crumbs").innerHTML = ""; $("step-next").innerHTML = ""; $("step-nav").hidden = true;
    $("step").innerHTML = '<p class="note">The script fills in with the name and company once a lead is on screen.</p>';
    return;
  }
  const list = steps(), vars = leadVars(S.cur);
  $("step-nav").hidden = list.length < 2;
  if (!list.length) { $("crumbs").innerHTML = ""; $("step-next").innerHTML = ""; $("step").innerHTML = '<p class="note">No steps in this flow.</p>'; return; }
  idx = Math.max(0, Math.min(list.length - 1, idx));
  $("crumbs").innerHTML = list.map((s, i) => '<button class="crumb' + (i === idx ? " on" : "") + '" data-go="' + esc(s.id) + '">' + esc(s.title || s.id) + "</button>").join("");
  $("step").innerHTML = stepHTML(list[idx], vars);
  $("step-next").innerHTML = list.slice(idx + 1, idx + 3).map((s) =>
    '<button class="upnext" data-go="' + esc(s.id) + '"><b>' + esc(s.title || s.id) + "</b><span>" +
    esc(renderPlain(((hinglish && s.say_hi) || s.say || [""])[0], vars)) + "</span></button>").join("");
  $("b-prev").disabled = idx === 0; $("b-next").disabled = idx >= list.length - 1;
}

function takeBranch(i) {
  const b = (steps()[idx].branches || [])[i];
  if (!b) return;
  if (b.outcome) {
    S.call.suggest = b.outcome;
    toast("ok", "After you hang up, <kbd>enter</kbd> saves <b>" + esc(outcome(b.outcome).label) + "</b>.");
  }
  if (b.to) jumpTo(b.to);
}

/* ---- objections --------------------------------------------------------------------------- */

const objections = () => (S.cfg.scripts && S.cfg.scripts.objections) || [];

export function openObjections() { objOpen = true; objQuery = ""; $("obj-q").value = ""; renderScript(); $("obj-q").focus(); }
export function closeObjections() { if (!objOpen) return false; objOpen = false; renderScript(); return true; }
export const objectionsOpen = () => objOpen;

function renderObjections() {
  $("treebox").hidden = true; $("objpanel").hidden = false;
  $("obj-rule").textContent = (S.cfg.scripts && S.cfg.scripts.objection_rule) || "";
  const vars = leadVars(S.cur || {});
  const q = objQuery.toLowerCase();
  const list = objections().filter((o) => !q || [o.title, o.anchor, o.disrupt, o.question].join(" ").toLowerCase().indexOf(q) >= 0);
  const heard = (S.call && S.call.objections) || [];
  $("obj-list").innerHTML = list.map((o) => {
    const on = o.tag && heard.indexOf(o.tag) >= 0;
    return '<details class="obj"' + (q || list.length <= 2 ? " open" : "") + '><summary>' + esc(o.title) + (on ? '<span class="muted"> · heard</span>' : "") + "</summary>" +
      "<ol><li>" + renderTpl(o.anchor, vars) + "</li><li>" + renderTpl(o.disrupt, vars) + "</li><li>" + renderTpl(o.question, vars) + "</li></ol>" +
      '<div class="obj-acts">' + (o.tag && S.cur ? '<button class="btn sm' + (on ? " primary" : "") + '" data-heard="' + esc(o.tag) + '">' + (on ? "Heard it, logged" : "Heard it") + "</button>" : "") +
      (o.referral && S.cur ? '<button class="btn sm" data-referral>Add the referral</button>' : "") +
      '<button class="btn quiet sm" data-oedit="' + esc(o.key) + '">Edit</button></div></details>';
  }).join("") || '<p class="note">Nothing matches. Add a card from Edit.</p>';
}

/* ---- keys and wiring ------------------------------------------------------------------------- */

function toggleHinglish() {
  hinglish = !hinglish;
  store.set("in_hinglish", hinglish ? "1" : "0");
  renderScript();
}

/* Keys outside inputs. Returns true when used. */
export function scriptKey(e, k) {
  if (k === "h") { toggleHinglish(); return true; }
  if (!S.cur) return false;
  if (k === "o") { e.preventDefault(); if (objOpen) closeObjections(); else openObjections(); return true; }
  if (objOpen) return false;
  if (k === "ArrowRight") { e.preventDefault(); jumpTo(1); return true; }
  if (k === "ArrowLeft") { e.preventDefault(); jumpTo(-1); return true; }
  return false;
}

function flowFor(l) {
  if (!l) return "call";
  if (l.group === "Callback due" || l.next_action_type === "callback") return "callback";
  if (l.status === "PIPELINE") return "follow_up";
  return "call";
}

export function wireScript() {
  $("flow-tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-f]"); if (b) setFlow(b.getAttribute("data-f")); });
  $("b-next").addEventListener("click", () => jumpTo(1));
  $("b-prev").addEventListener("click", () => jumpTo(-1));
  $("b-obj").addEventListener("click", () => (objOpen ? closeObjections() : openObjections()));
  $("b-edit").addEventListener("click", () => actions.editScripts && actions.editScripts(currentStepId()));
  $("hinglish").addEventListener("change", toggleHinglish);
  $("obj-close").addEventListener("click", () => closeObjections());
  $("obj-q").addEventListener("input", function () { objQuery = this.value; if (objOpen) renderObjections(); });
  $("obj-q").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); closeObjections(); } });

  $("treebox").addEventListener("click", (e) => {
    let b;
    if ((b = e.target.closest("[data-go]"))) jumpTo(b.getAttribute("data-go"));
    else if ((b = e.target.closest("[data-branch]"))) takeBranch(+b.getAttribute("data-branch"));
    else if ((b = e.target.closest("[data-chip]"))) {
      const key = currentStepId() + ":" + b.getAttribute("data-chip");
      S.call.asked = S.call.asked || [];
      const i = S.call.asked.indexOf(key);
      if (i >= 0) S.call.asked.splice(i, 1); else S.call.asked.push(key);
      renderScript();
      const again = $("step").querySelector('[data-chip="' + b.getAttribute("data-chip") + '"]');
      const f = again && again.nextElementSibling && again.nextElementSibling.querySelector("input");
      if (i < 0 && f) f.focus();
    } else if ((b = e.target.closest("[data-demo]"))) {
      S.call.demo_at = b.getAttribute("data-demo"); emit("call"); renderScript();
    }
  });
  $("treebox").addEventListener("input", (e) => {
    const bind = e.target.getAttribute && e.target.getAttribute("data-bind");
    if (bind) { S.call[bind] = e.target.value.trim(); emit("call"); }
  });
  $("treebox").addEventListener("change", (e) => { if (e.target.getAttribute("data-bind") === "demo_at") renderScript(); });
  $("treebox").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches("input")) { e.preventDefault(); e.target.blur(); } });
  on("call", () => {
    $("treebox").querySelectorAll("[data-bind]").forEach((el) => { if (document.activeElement !== el && el.type !== "datetime-local") el.value = S.call[el.getAttribute("data-bind")] || ""; });
  });

  $("obj-list").addEventListener("click", (e) => {
    let b;
    if ((b = e.target.closest("[data-heard]"))) {
      e.preventDefault();
      const tag = b.getAttribute("data-heard"), list = S.call.objections, i = list.indexOf(tag);
      if (i >= 0) list.splice(i, 1); else list.push(tag);
      renderObjections();
    } else if (e.target.closest("[data-referral]")) { e.preventDefault(); actions.referral(); }
    else if ((b = e.target.closest("[data-oedit]"))) { e.preventDefault(); actions.editScripts && actions.editScripts("obj:" + b.getAttribute("data-oedit")); }
  });

  on("lead", (l) => { flow = flowFor(l); idx = 0; objOpen = false; renderScript(); });
  on("session", renderScript);
  on("scripts", renderScript);
  renderScript();
}
