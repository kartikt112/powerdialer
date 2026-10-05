/* Campaigns: pick which one you're calling, load lists into one, edit or
   delete them, the daily target with its pace, and deleting leads.

   The chosen campaign rides on every request (api.js sends X-Campaign), so
   the queue, callbacks, bookings, calls and numbers all narrow to it. */
import { $, esc, fmtNum, fmtPhone, parseUTC, myClock, store } from "./util.js";
import { S, on, emit, actions, busy } from "./state.js";
import { api, campaignHeaders } from "./api.js";
import { toast, say, openModal, closeModal, closeX, closeMenus, toggleMenu } from "./ui.js";

const current = () => S.campaigns.find((c) => c.id === S.campaign) || null;

/* ---------------------------------------------------------------- switcher -- */

export function refreshCampaigns() {
  return api("/api/campaigns").then((d) => {
    S.campaigns = d.campaigns || [];
    if (S.campaign && !S.campaigns.some((c) => c.id === S.campaign && c.status === "active")) setCampaign(null, true);
    renderSwitcher();
    emit("campaigns-loaded");
    return S.campaigns;
  }).catch(() => {});
}

function renderSwitcher() {
  const c = current();
  $("b-campaign").innerHTML = '<span class="muted">Campaign</span> ' + esc(c ? c.name : "All campaigns");
  const active = S.campaigns.filter((x) => x.status === "active");
  const row = (id, name, sub) => '<button role="menuitem" data-camp="' + id + '"' + ((S.campaign || 0) === id ? ' aria-current="true"' : "") + ">" +
    '<span class="m1">' + esc(name) + '</span><span class="m2">' + sub + "</span></button>";
  $("camp-menu").innerHTML =
    active.map((x) => row(x.id, x.name, fmtNum(x.open) + " to call · " + fmtNum(x.dials_today) + " dials today")).join("") +
    row(0, "All campaigns", "every lead, every list") +
    '<hr><button role="menuitem" data-camp-act="load">Load a list…</button>' +
    '<button role="menuitem" data-camp-act="new">New campaign…</button>' +
    '<button role="menuitem" data-camp-act="manage">Manage campaigns…</button>';
}

export function setCampaign(id, quiet) {
  if (busy()) { toast("warn", "Finish this call before switching campaigns."); return; }
  const prev = S.campaign;
  S.campaign = id || null;
  store.set("pd_campaign", S.campaign ? String(S.campaign) : "");
  renderSwitcher();
  if (quiet || prev === S.campaign) return;
  const c = current();
  say("Campaign: " + (c ? esc(c.name) : "all campaigns"));
  if (S.cur && S.state === "READY") api("/api/release", { phone: S.cur.phone, agent: S.agent });
  emit("campaign", S.campaign);
}

/* ------------------------------------------------------------- load a list -- */

export function loadListModal(presetFile) {
  closeMenus();
  if (S.state === "LIVE" || S.state === "DIALING") { toast("warn", "Finish the call before loading a list."); return; }
  let file = presetFile || null;
  const active = S.campaigns.filter((x) => x.status === "active");
  const into = current() ? String(current().id) : "new";
  openModal('<div class="dh"><div><h2>Load a list</h2><p class="sub">Excel or CSV. It is checked, scored and de-duplicated, then goes into a campaign you call on its own. Numbers already called keep their history; numbers you deleted stay deleted.</p></div>' + closeX() + "</div>" +
    '<label class="drop" id="ll-drop"><input type="file" id="ll-file" accept=".xlsx,.xls,.csv"><b id="ll-name">' + (file ? esc(file.name) : "Choose a file") + '</b><span class="muted">or drop it here</span></label>' +
    '<div class="formrow2" style="margin-top:14px"><div><label class="lbl" for="ll-into">Put it into</label><select class="field" id="ll-into">' +
    '<option value="new"' + (into === "new" ? " selected" : "") + ">A new campaign</option>" +
    active.map((x) => '<option value="' + x.id + '"' + (into === String(x.id) ? " selected" : "") + ">" + esc(x.name) + " (" + fmtNum(x.leads) + " leads)</option>").join("") +
    '</select></div><div id="ll-name-wrap"><label class="lbl" for="ll-cname">New campaign name</label><input class="field" id="ll-cname" maxlength="80" placeholder="Named after the file"></div></div>' +
    '<p class="err" id="ll-err" role="alert"></p><div class="acts"><button class="btn primary" id="ll-go"' + (file ? "" : " disabled") + ">Load it</button></div>");
  const sync = () => { $("ll-name-wrap").hidden = $("ll-into").value !== "new"; };
  sync();
  $("ll-into").addEventListener("change", sync);
  const take = (f) => {
    if (!f) return;
    if (!/\.(xlsx|xls|csv)$/i.test(f.name)) { $("ll-err").textContent = "That needs to be a .xlsx or .csv file."; return; }
    file = f; $("ll-name").textContent = f.name; $("ll-err").textContent = ""; $("ll-go").disabled = false;
    if (!$("ll-cname").value) $("ll-cname").placeholder = f.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ");
  };
  $("ll-file").addEventListener("change", (e) => take(e.target.files[0]));
  const drop = $("ll-drop");
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); take(e.dataTransfer.files[0]); });
  $("ll-go").addEventListener("click", () => {
    if (!file) return;
    const target = $("ll-into").value;
    const name = target === "new" ? ($("ll-cname").value.trim() || $("ll-cname").placeholder || file.name) : "";
    upload(file, target === "new" ? null : Number(target), name);
    closeModal();
  });
}

function upload(file, campaignId, newName) {
  const t = toast("info", "Prepping <b>" + esc(file.name) + "</b>: checking, scoring and de-duplicating.", { ms: 300000 });
  const headers = { "X-Filename": file.name };
  if (newName) headers["X-Campaign-Name"] = encodeURIComponent(newName);
  else if (campaignId) headers["X-Campaign"] = String(campaignId);
  fetch("/api/upload", { method: "POST", headers, body: file })
    .then((r) => r.json())
    .then((d) => {
      t.close();
      if (!d.ok) {
        toast("error", "<b>List prep failed:</b> " + esc(d.error || "unknown error") +
          (d.log ? "<br><small>" + esc(String(d.log).split("\n").pop()) + "</small>" : ""), { ms: 15000 });
        return;
      }
      refreshCampaigns().then(() => {
        setCampaign(d.campaign_id, true);
        const c = current();
        toast("success", "<b>" + fmtNum(d.added) + "</b> new leads, " + fmtNum(d.refreshed) + " already known, into <b>" +
          esc(c ? c.name : "the campaign") + "</b>. Now calling this campaign.", { ms: 8000 });
        say("List loaded into " + esc(c ? c.name : "campaign") + ": " + d.added + " new");
        emit("campaign", S.campaign);
      });
    })
    .catch((e) => { t.close(); toast("error", "Upload failed: " + esc(e.message)); });
}

/* ------------------------------------------------------------------ manage -- */

function scriptOptions(selected) {
  return '<option value="">Default script</option>' + (S.cfg.script_versions || []).map((v) =>
    "<option" + (v === selected ? " selected" : "") + ">" + esc(v) + "</option>").join("");
}

export function newCampaignModal() {
  closeMenus();
  openModal('<div class="dh"><h2>New campaign</h2>' + closeX() + "</div>" +
    '<div class="formrow2"><div><label class="lbl" for="nc-name">Name</label><input class="field" id="nc-name" maxlength="80" placeholder="Ohio stampers, October"></div>' +
    '<div><label class="lbl" for="nc-target">Dials a day</label><input class="field" id="nc-target" type="number" min="0" max="2000" placeholder="' + (S.cfg.daily_target || 150) + '"></div></div>' +
    '<div><label class="lbl" for="nc-script">Script</label><select class="field" id="nc-script">' + scriptOptions("") + "</select></div>" +
    '<p class="err" id="nc-err"></p><div class="acts"><button class="btn primary" id="nc-go">Create</button></div>');
  $("nc-go").addEventListener("click", () => {
    const name = $("nc-name").value.trim();
    if (!name) { $("nc-err").textContent = "Give it a name."; return; }
    api("/api/campaigns/new", { name, script_version: $("nc-script").value, daily_target: Number($("nc-target").value) || 0 }).then((d) => {
      if (d.error) { $("nc-err").textContent = d.error; return; }
      S.campaigns = d.campaigns; closeModal(); setCampaign(d.id);
      toast("success", "Campaign <b>" + esc(name) + "</b> created. Load a list into it from the Campaign menu.");
    });
  });
}

export function manageModal(editId) {
  closeMenus();
  refreshCampaigns().then(() => {
    const list = S.campaigns;
    const rows = list.map((c) => {
      if (c.id === editId) {
        return '<div class="camp edit" data-id="' + c.id + '"><div class="formrow3">' +
          '<div><label class="lbl">Name</label><input class="field" data-f="name" maxlength="80" value="' + esc(c.name) + '"></div>' +
          '<div><label class="lbl">Dials a day</label><input class="field" data-f="daily_target" type="number" min="0" max="2000" value="' + (c.daily_target || "") + '" placeholder="' + (S.cfg.daily_target || 150) + '"></div>' +
          '<div><label class="lbl">Script</label><select class="field" data-f="script_version">' + scriptOptions(c.script_version) + "</select></div></div>" +
          '<div class="acts"><button class="btn primary sm" data-save="' + c.id + '">Save</button><button class="btn quiet sm" data-cancel>Cancel</button></div></div>';
      }
      return '<div class="camp' + (c.status === "archived" ? " archived" : "") + '" data-id="' + c.id + '">' +
        '<div class="c1"><b>' + esc(c.name) + "</b>" + (c.status === "archived" ? ' <span class="pill">archived</span>' : "") +
        (c.id === S.campaign ? ' <span class="pill good">calling now</span>' : "") + "</div>" +
        '<div class="c2">' + fmtNum(c.leads) + " leads · " + fmtNum(c.open) + " to call · " + fmtNum(c.untouched) + " not called yet · " +
        fmtNum(c.dials_today) + " dials today · " + fmtNum(c.dials_all) + " all time · " + fmtNum(c.booked) + " booked · " +
        (c.daily_target ? c.daily_target + " a day" : "default target") + " · script " + esc(c.script_version || "default") + "</div>" +
        (c.files.length ? '<div class="c2 muted">Lists: ' + c.files.map(esc).join(", ") + "</div>" : "") +
        '<div class="acts">' + (c.status === "active" && c.id !== S.campaign ? '<button class="btn sm" data-use="' + c.id + '">Call this</button>' : "") +
        '<button class="btn quiet sm" data-edit="' + c.id + '">Edit</button>' +
        '<button class="btn quiet sm" data-archive="' + c.id + '" data-to="' + (c.status === "archived" ? "active" : "archived") + '">' + (c.status === "archived" ? "Bring back" : "Archive") + "</button>" +
        '<button class="btn quiet sm danger" data-del="' + c.id + '">Delete…</button></div></div>';
    }).join("") || '<p class="muted">No campaigns yet. Load a list to start one.</p>';
    openModal('<div class="dh"><div><h2>Campaigns</h2><p class="sub">Each campaign is its own queue, script, target and numbers. A number in two campaigns is never called twice: its tries and do-not-call are shared.</p></div>' + closeX() + "</div>" +
      '<div class="camps" id="camps">' + rows + "</div>" +
      '<div class="acts"><button class="btn" id="mc-new">New campaign</button><button class="btn" id="mc-load">Load a list</button></div>', { wide: true });
    $("mc-new").addEventListener("click", newCampaignModal);
    $("mc-load").addEventListener("click", () => loadListModal());
    $("camps").addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      if (b.dataset.use) { setCampaign(Number(b.dataset.use)); manageModal(); }
      else if (b.dataset.edit) manageModal(Number(b.dataset.edit));
      else if (b.hasAttribute("data-cancel")) manageModal();
      else if (b.dataset.save) {
        const box = b.closest(".camp"), f = {};
        box.querySelectorAll("[data-f]").forEach((el) => { f[el.dataset.f] = el.value; });
        api("/api/campaigns/update", Object.assign({ id: Number(b.dataset.save) }, f)).then((d) => {
          if (d.error) { toast("error", esc(d.error)); return; }
          S.campaigns = d.campaigns; renderSwitcher(); emit("campaign-edited"); manageModal();
        });
      } else if (b.dataset.archive) {
        api("/api/campaigns/update", { id: Number(b.dataset.archive), status: b.dataset.to }).then((d) => {
          S.campaigns = d.campaigns;
          if (b.dataset.to === "archived" && Number(b.dataset.archive) === S.campaign) setCampaign(null);
          renderSwitcher(); manageModal();
        });
      } else if (b.dataset.del) confirmDelete(Number(b.dataset.del));
    });
  });
}

function confirmDelete(id) {
  const c = S.campaigns.find((x) => x.id === id); if (!c) return;
  openModal('<div class="dh"><h2>Delete ' + esc(c.name) + "?</h2>" + closeX() + "</div>" +
    "<p>The campaign goes, and so do its <b>" + fmtNum(c.leads) + "</b> leads, except ones that are also in another campaign. " +
    "Calls already made stay in your history and your numbers. Do-not-call entries are kept.</p>" +
    '<p class="sub">To keep it but stop calling it, archive it instead.</p>' +
    '<div class="acts"><button class="btn danger solid" id="cd-yes">Delete the campaign</button><button class="btn" id="cd-no">Cancel</button></div>');
  $("cd-no").addEventListener("click", () => manageModal());
  $("cd-yes").addEventListener("click", () => {
    api("/api/campaigns/delete", { id }).then((d) => {
      S.campaigns = d.campaigns;
      if (id === S.campaign) setCampaign(null);
      renderSwitcher();
      toast("success", "Deleted <b>" + esc(c.name) + "</b> and " + fmtNum(d.deleted_leads) + " of its leads.");
      emit("campaign", S.campaign);
      manageModal();
    });
  });
}

/* ------------------------------------------------------------- delete leads -- */

/* phones: array of E.164 numbers. Offers undo. */
export function deleteLeads(phones, label) {
  if (!phones.length) return;
  const removeOnly = !!S.campaign;
  const n = phones.length, what = label || (n === 1 ? "this lead" : n + " leads");
  openModal('<div class="dh"><h2>Remove ' + esc(what) + "?</h2>" + closeX() + "</div>" +
    "<p>" + (removeOnly
      ? "<b>Remove from this campaign</b> takes " + (n === 1 ? "it" : "them") + " out of <b>" + esc((current() || {}).name || "") + "</b> only. " +
        "<b>Delete everywhere</b> takes " + (n === 1 ? "it" : "them") + " off every campaign and list."
      : "Deleted leads come off every campaign, queue and list.") +
    " Calls already made stay in your history and numbers, and loading the same list again won't bring them back.</p>" +
    '<div class="acts">' + (removeOnly ? '<button class="btn" id="dl-remove">Remove from this campaign</button>' : "") +
    '<button class="btn danger solid" id="dl-delete">Delete everywhere</button><button class="btn quiet" data-close>Cancel</button></div>');
  const done = (route, verb) => api(route, { phones }).then((d) => {
    closeModal();
    if (d.error) { toast("error", esc(d.error)); return; }
    emit("leads-removed", phones);
    toast("success", verb + " " + esc(what) + ".", {
      ms: 9000, actions: route === "/api/leads/delete"
        ? [{ html: "Undo", run: () => api("/api/leads/restore", { phones }).then(() => { emit("leads-removed", []); toast("success", "Brought back."); }) }]
        : []
    });
  });
  if (removeOnly) $("dl-remove").addEventListener("click", () => done("/api/leads/remove", "Removed"));
  $("dl-delete").addEventListener("click", () => done("/api/leads/delete", "Deleted"));
}

/* ------------------------------------------------------------- target strip -- */

let targetTimer = null;
export function refreshTarget() {
  clearTimeout(targetTimer);
  targetTimer = setTimeout(() => api("/api/target").then(renderTarget).catch(() => {}), 150);
}

function renderTarget(t) {
  if (!t || t.error) return;
  const pct = t.target ? Math.min(100, Math.round((t.done / t.target) * 100)) : 0;
  const clock = (s) => (s ? myClock(parseUTC(s)) : "");
  let pace;
  if (t.verdict === "hit") pace = '<b class="good">Target hit.</b> ' + (t.done - t.target > 0 ? fmtNum(t.done - t.target) + " over." : "");
  else if (t.verdict === "no_pace") pace = "Pace shows after a few dials.";
  else if (t.verdict === "on_track") pace = "At <b>" + t.per_hour + "</b> an hour you reach " + fmtNum(t.target) + " by <b>" + clock(t.eta) + "</b>" +
    (t.calling_ends ? ", before calling hours end at " + clock(t.calling_ends) : "") + ".";
  else pace = '<b class="warn">Short at this pace.</b> At ' + t.per_hour + " an hour you reach about <b>" + fmtNum(t.projected_by_end || t.done) + "</b> by " +
    clock(t.calling_ends) + " when calling hours end. " + fmtNum(Math.max(0, Math.ceil((t.left) / Math.max(1, (parseUTC(t.calling_ends) - Date.now()) / 3600000)))) + " an hour would get there.";
  if (t.verdict !== "hit" && !t.calling_ends) pace += " No calling window opens again today.";
  const rest = t.untouched ? " · " + fmtNum(t.untouched) + " not called yet" + (t.days_to_first_touch ? ", about " + t.days_to_first_touch + " calling day" + (t.days_to_first_touch === 1 ? "" : "s") + " at this target" : "") : "";
  $("target-line").innerHTML =
    '<span class="tg-n"><b>' + fmtNum(t.done) + '</b> of ' + fmtNum(t.target) + ' dials today</span>' +
    '<span class="tg-bar" role="progressbar" aria-valuemin="0" aria-valuemax="' + t.target + '" aria-valuenow="' + t.done + '"><i style="width:' + pct + '%"></i></span>' +
    '<span class="tg-pace">' + pace + '<span class="muted">' + rest + "</span></span>" +
    '<button class="btn quiet sm" id="tg-edit">Change target</button>';
}

function editTarget() {
  const c = current();
  if (!c) {
    toast("info", "The target belongs to a campaign. Pick one from the Campaign menu, or change the default (dialer.daily_target) in config.yaml.");
    return;
  }
  manageModal(c.id);
}

/* ------------------------------------------------------------------- wiring -- */

export function wireCampaigns() {
  $("b-campaign").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("camp-menu", "b-campaign"); });
  $("camp-menu").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    closeMenus();
    if (b.dataset.camp !== undefined) setCampaign(Number(b.dataset.camp) || null);
    else if (b.dataset.campAct === "load") loadListModal();
    else if (b.dataset.campAct === "new") newCampaignModal();
    else if (b.dataset.campAct === "manage") manageModal();
  });
  $("target-line").addEventListener("click", (e) => { if (e.target.closest("#tg-edit")) editTarget(); });
  on("saved", () => { refreshTarget(); refreshCampaigns(); });
  on("campaign", refreshTarget);
  on("campaign-edited", refreshTarget);
  actions.loadList = loadListModal;
  actions.deleteLeads = deleteLeads;
  setInterval(() => { if (!document.hidden) refreshTarget(); }, 60000);
  refreshCampaigns();
  refreshTarget();
}
