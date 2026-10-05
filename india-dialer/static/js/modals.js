/* Dialogs: keys, session start and end, add a lead, settings, phone page. */
import { $, esc, fmtClock, store } from "./util.js";
import { S, emit, actions } from "./state.js";
import { api } from "./api.js";
import { toast, openModal, closeModal, closeX, closeMenus } from "./ui.js";
import { qrSvg } from "./qr.js";

export const pauseReasons = () => (S.cfg && S.cfg.pause_reasons) || ["Break", "Lunch", "Research", "Other"];

const KEYS = [
  ["space", "Dial, then end the call"], ["c", "They picked up"], ["n", "No answer, log it"], ["t", "Try the next number"],
  ["1 to 9, 0", "Pick the outcome"], ["enter", "Save the suggested outcome"], ["z", "Undo the last save"], ["s", "Skip this lead for now"],
  ["o", "Objections"], ["left, right", "Script step back, forward"], ["h", "Hinglish on or off"],
  ["w", "WhatsApp message"], ["e", "Email"], ["b", "Book a demo"], ["p", "Start, pause or resume"],
  ["/", "Search the queue"], ["esc", "Close, or hold the countdown"], ["?", "This list"]
];

export function helpModal() {
  openModal('<div class="dh"><h2>Keys</h2>' + closeX() + '</div><div class="keys-table">' +
    KEYS.map((k) => "<div><span>" + esc(k[1]) + "</span><kbd>" + esc(k[0]) + "</kbd></div>").join("") + "</div>" +
    '<p class="sub" style="margin-top:14px">The laptop never places the call. Dial opens it on your phone through the tel: link, or scan the code with the phone camera.</p>', { wide: true });
}

export function sessionModal() {
  const versions = S.cfg.script_versions || ["v1"];
  const last = store.get("in_session", "");
  let prev = {};
  try { prev = JSON.parse(last) || {}; } catch (e) {}
  openModal('<div class="dh"><h2>Start a session</h2>' + closeX() + "</div>" +
    '<p class="sub">Power mode: the next lead opens and dials after a short countdown. Press esc to hold it, p to pause.</p>' +
    '<div class="grid3"><div><label class="lbl" for="s-dials">Dials</label><input class="field" id="s-dials" type="number" min="1" max="400" value="' + esc(prev.d || 60) + '"></div>' +
    '<div><label class="lbl" for="s-mins">Minutes</label><input class="field" id="s-mins" type="number" min="10" max="480" value="' + esc(prev.m || 90) + '"></div>' +
    '<div><label class="lbl" for="s-script">Script</label><select class="field" id="s-script">' +
    versions.map((v) => '<option' + (v === (prev.s || S.session.script) ? " selected" : "") + ">" + esc(v) + "</option>").join("") + "</select></div></div>" +
    '<div class="acts"><button class="btn primary" id="s-go">Start</button><button class="btn quiet" data-close>Cancel</button></div>');
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (done) return; done = true; resolve(v); };
    $("s-go").addEventListener("click", () => {
      const opts = { target_dials: Math.max(1, +$("s-dials").value || 60), target_minutes: Math.max(10, +$("s-mins").value || 90), script_version: $("s-script").value };
      store.set("in_session", JSON.stringify({ d: opts.target_dials, m: opts.target_minutes, s: opts.script_version }));
      finish(opts); closeModal();
    });
    $("modal-box").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); $("s-go").click(); } };
    const obs = new MutationObserver(() => { if ($("modal").hidden) { obs.disconnect(); finish(null); } });
    obs.observe($("modal"), { attributes: true });
  });
}

export function sessionEndCard(id, why, script, started) {
  const ss = S.session;
  const perHour = ss.activeSec > 60 ? Math.round(ss.dials / (ss.activeSec / 3600)) : ss.dials;
  const body = (extra) => '<div class="dh"><h2>Session done</h2>' + closeX() + '</div><p class="sub">' + esc(why) + "</p>" +
    '<div class="keys-table"><div><span>Dials</span><b class="num">' + ss.dials + "</b></div><div><span>Time on calls</span><b class=\"num\">" + fmtClock(ss.activeSec) + "</b></div>" +
    '<div><span>Dials an hour</span><b class="num">' + perHour + "</b></div><div><span>Script</span><b>" + esc(script || "") + "</b></div>" + (extra || "") + "</div>";
  openModal(body());
  api("/api/stats?range=session&session=" + id).then((d) => {
    if (!d || d.error || !d.session || !$("modal-box").querySelector(".keys-table")) return;
    const s = d.session;
    $("modal-box").innerHTML = body('<div><span>Talk time, average</span><b class="num">' + fmtClock(s.avg_talk || 0) + "</b></div>" +
      '<div><span>Positive conversations</span><b class="num">' + (s.positive || 0) + " of " + (s.positive_target || 10) + "</b></div>");
  }).catch(() => {});
}

/* ---- add a lead ------------------------------------------------------------- */

function addLeadModal(prefill) {
  closeMenus();
  const p = prefill || {};
  openModal('<div class="dh"><h2>' + (p.referral ? "Add the referral" : "Add a lead") + "</h2>" + closeX() + "</div>" +
    (p.referral ? '<p class="sub">Referred by ' + esc(S.cur ? S.cur.company : "") + ". It goes into the queue as a new lead.</p>" : "") +
    '<div class="grid2"><div><label class="lbl" for="a-co">Company</label><input class="field" id="a-co" maxlength="120"></div>' +
    '<div><label class="lbl" for="a-ph">Phone</label><input class="field" id="a-ph" type="tel" maxlength="40"></div></div>' +
    '<div class="grid3"><div><label class="lbl" for="a-name">Person</label><input class="field" id="a-name" maxlength="60"></div>' +
    '<div><label class="lbl" for="a-city">City</label><input class="field" id="a-city" maxlength="60"></div>' +
    '<div><label class="lbl" for="a-tier">Tier</label><select class="field" id="a-tier"><option>A</option><option selected>B</option><option>C</option></select></div></div>' +
    '<div><label class="lbl" for="a-why">Why call them</label><input class="field" id="a-why" maxlength="200"></div>' +
    '<p class="err" id="a-err"></p><div class="acts"><button class="btn primary" id="a-go">Add</button><button class="btn quiet" data-close>Cancel</button></div>');
  $("a-go").addEventListener("click", () => {
    const body = { company: $("a-co").value.trim(), phone: $("a-ph").value.trim(), contact: $("a-name").value.trim(),
                   city: $("a-city").value.trim(), tier: $("a-tier").value, why: $("a-why").value.trim() };
    if (!body.company) { $("a-err").textContent = "Company is needed."; return; }
    const url = p.referral ? "/api/referral" : "/api/lead/add";
    if (p.referral) body.lead_id = S.cur && S.cur.id;
    api(url, body).then((d) => {
      if (d.error) { $("a-err").textContent = d.error; return; }
      closeModal();
      toast("ok", "Added <b>" + esc(body.company) + "</b> to the queue.");
      emit("saved", {});
    });
  });
}

/* ---- settings ---------------------------------------------------------------- */

function settingsModal() {
  closeMenus();
  const theme = store.get("in_theme", "system");
  openModal('<div class="dh"><h2>Settings</h2>' + closeX() + "</div>" +
    '<div class="grid2"><div><label class="lbl" for="st-agent">Your name, stamped on calls</label><input class="field" id="st-agent" value="' + esc(S.agent) + '" maxlength="24"></div>' +
    '<div><label class="lbl" for="st-theme">Theme</label><select class="field" id="st-theme">' +
    ["system", "light", "dark"].map((t) => "<option" + (t === theme ? " selected" : "") + ">" + t + "</option>").join("") + "</select></div></div>" +
    '<label class="check"><input type="checkbox" id="st-tel"' + (actions.settings.openTel ? " checked" : "") + '><span>Dial opens the tel: link (FaceTime on a Mac, Phone Link on Windows). Off: QR code only.</span></label>' +
    '<p class="sub">Number types come from ' + (S.cfg.phone_type_source === "phonenumbers" ? "Google phone number data." : "a simple rule. Install phonenumbers for exact mobile or landline.") + "</p>" +
    '<div class="acts"><button class="btn primary" id="st-save">Save</button></div>');
  $("st-save").addEventListener("click", () => {
    const agent = $("st-agent").value.trim().toLowerCase().replace(/[^a-z0-9_-]/g, "") || "pawan";
    store.set("in_agent", agent); S.agent = agent;
    const t = $("st-theme").value; store.set("in_theme", t);
    document.documentElement.setAttribute("data-theme", t === "system" ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : t);
    actions.settings.openTel = $("st-tel").checked; store.set("in_open_tel", actions.settings.openTel ? "1" : "0");
    closeModal();
  });
}

function phoneModal() {
  closeMenus();
  const lan = S.cfg.lan || {};
  openModal('<div class="dh"><h2>Phone page</h2>' + closeX() + "</div>" + (lan.on
    ? '<p class="sub">Scan this with your phone on the same Wi-Fi. It shows the number to dial and a dial button, and follows the laptop.</p><div class="qr-wrap" style="margin:10px 0">' +
      qrSvg(lan.url, { ecl: "M", border: 3, label: "Phone page link" }) + '</div><p class="mono" style="word-break:break-all">' + esc(lan.url) + "</p>"
    : '<p class="sub">The phone page is off. It only listens on this laptop unless you start the server with the LAN flag:</p><pre class="mono">python3 india-dialer/serve.py --lan</pre>' +
      '<p class="sub">You do not need it: the QR code next to every number already dials from the phone camera.</p>'));
}

/* ---- import a list ------------------------------------------------------------ */

const FIELD_NAME = { company: "company", phone: "phone", tier: "tier", city: "city", state: "state", location: "location",
  type: "type", ask_for: "ask for", first_name: "first name", last_name: "last name", dm_title: "title", why: "why",
  est_drawings_month: "drawings a month", website: "website", flags: "flags", email: "email", whatsapp: "WhatsApp",
  phone_source: "phone source", source: "source", language_pref: "language" };

function listBlock(title, items, fmt) {
  if (!items.length) return "";
  return '<details class="obj"><summary>' + esc(title) + ' <span class="muted">' + items.length + "</span></summary>" +
    '<ul class="imp-list">' + items.slice(0, 200).map((x) => "<li>" + fmt(x) + "</li>").join("") + "</ul></details>";
}

function importModal() {
  closeMenus();
  let file = null, content = "";
  const shell = (body) => openModal('<div class="dh"><h2>Import a list</h2>' + closeX() + "</div>" + body, { wide: true });
  const pick = (err) => {
    shell('<p class="sub">A CSV or Excel file with one company per row. Columns are matched by name: company, phone (several phone columns are fine), ' +
      "tier, city, state, type, ask for, why, drawings a month, website, flags, email. IndiaMART and LinkedIn exports work too. " +
      "Nothing is imported until you've seen the preview. Importing again is safe: companies already called keep their history.</p>" +
      '<label class="imp-drop" id="imp-drop"><input type="file" id="imp-file" accept=".csv,.xlsx,.xlsm,text/csv"><b>Choose a file</b><span class="muted">or drop it here</span></label>' +
      '<p class="err" id="imp-err">' + esc(err || "") + "</p>");
    const take = (f) => {
      if (!f) return;
      file = f;
      $("imp-err").textContent = "";
      $("imp-drop").innerHTML = "<b>" + esc(f.name) + '</b><span class="muted">reading...</span>';
      const r = new FileReader();
      r.onload = () => { content = String(r.result).split(",")[1] || ""; preview(); };
      r.onerror = () => pick("That file could not be read.");
      r.readAsDataURL(f);
    };
    $("imp-file").addEventListener("change", (e) => take(e.target.files[0]));
    const drop = $("imp-drop");
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); take(e.dataTransfer.files[0]); });
  };
  const preview = () => {
    api("/api/import/preview", { filename: file.name, content }).then((d) => {
      if (d.error) { pick(d.error); return; }
      const k = d.kinds || {}, t = d.tiers || {};
      const ignored = d.columns.filter((c) => !c.field).map((c) => c.header);
      const used = d.columns.filter((c) => c.field).map((c) => esc(c.header) + (c.field !== c.header.toLowerCase() ? ' <span class="muted">as ' + esc(FIELD_NAME[c.field] || c.field) + "</span>" : ""));
      shell('<p class="sub">' + esc(d.filename) + ": " + d.rows + " rows, " + d.leads + " companies" + (d.merged.length ? ", " + d.merged.length + " duplicate rows merged" : "") + ".</p>" +
        '<div class="keys-table">' +
        "<div><span>New to the list</span><b class=\"num\">" + d.new + "</b></div><div><span>Already on it, will be updated</span><b class=\"num\">" + d.already + "</b></div>" +
        "<div><span>Tier A, B, C</span><b class=\"num\">" + (t.A || 0) + " / " + (t.B || 0) + " / " + (t.C || 0) + "</b></div>" +
        "<div><span>Numbers: mobile, landline</span><b class=\"num\">" + (k.mobile || 0) + " / " + (k.landline || 0) + (k.tollfree ? " / " + k.tollfree + " toll-free" : "") + "</b></div>" +
        "</div>" +
        (d.no_phone_column ? '<p class="err">No phone column found, so every company will need a number before it can be called.</p>' : "") +
        (d.dnc.length ? '<p class="err">' + d.dnc.length + " on your do-not-call list stay blocked: " + esc(d.dnc.join(", ")) + "</p>" : "") +
        '<div class="imp-lists">' +
        listBlock("Held for the defence or aerospace check", d.held, (x) => "<b>" + esc(x.company) + "</b> " + esc(x.reason)) +
        listBlock("More than one number", d.multi, (x) => "<b>" + esc(x.company) + "</b> " + esc(x.numbers.join(", "))) +
        listBlock("International numbers, not dialled", d.intl, (x) => "<b>" + esc(x.company) + "</b> " + esc(x.numbers.join(", "))) +
        listBlock("No number yet", d.no_number, (x) => esc(x)) +
        listBlock("Notes", d.notes, (x) => esc(x)) +
        listBlock("Columns used", used, (x) => x) +
        listBlock("Columns ignored", ignored, (x) => esc(x)) + "</div>" +
        '<p class="err" id="imp-err"></p><div class="acts"><button class="btn primary" id="imp-go">Import ' + d.leads + " companies</button>" +
        '<button class="btn quiet" id="imp-back">Choose another file</button></div>');
      $("imp-back").addEventListener("click", () => pick());
      $("imp-go").addEventListener("click", () => {
        $("imp-go").disabled = true; $("imp-go").textContent = "Importing...";
        api("/api/import", { filename: file.name, content }).then((r) => {
          if (r.error) { $("imp-err").textContent = r.error; $("imp-go").disabled = false; $("imp-go").textContent = "Try again"; return; }
          const x = r.result;
          closeModal();
          toast("ok", "<b>Imported " + esc(file.name) + ".</b> " + x.added + " added, " + x.refreshed + " updated" +
            (x.kept_worked ? ", " + x.kept_worked + " already called left as they were" : "") + ". " + x.dialable + " ready to call.", { ms: 9000 });
          emit("saved", {});
          if (!S.cur && actions.nextLead) actions.nextLead();
        });
      });
    });
  };
  pick();
}

export function wireModals() {
  $("main-menu").addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    const act = b.getAttribute("data-act");
    if (act === "lead") addLeadModal();
    else if (act === "import") importModal();
    else if (act === "settings") settingsModal();
    else if (act === "phone") phoneModal();
    else if (act === "scripts" && actions.editScripts) { closeMenus(); actions.editScripts(); }
  });
  actions.addLead = addLeadModal;
  actions.importList = importModal;
  actions.referral = () => addLeadModal({ referral: true });
}
