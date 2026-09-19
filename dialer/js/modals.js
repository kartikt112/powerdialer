/* Dialogs: agent picker, dial a number, shortcuts, activity log, settings,
   list upload. */
import { $, esc, icon, keysHTML, store } from "./util.js";
import { S, actions, busy } from "./state.js";
import { toast, say, openModal, closeModal, closeX, getLog } from "./ui.js";

export function applyTheme() {
  let t = S.settings.theme;
  if (t === "system") t = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", t);
}

export function agentPicker(firstRun) {
  return new Promise((resolve) => {
    const seats = S.cfg.agents || [];
    openModal(
      '<div class="dh"><div><h2>Who\'s dialing?</h2><p class="sub">Your name goes into the scripts and onto every call you log. ' +
      "The seat is your line: inbound calls ring the seat set on the carrier.</p></div>" + (firstRun ? "" : closeX()) + "</div>" +
      (seats.length ? '<div class="seat-grid">' + seats.map((a) =>
        '<button class="btn seat" data-seat="' + esc(a.id) + '" data-name="' + esc(a.name || a.id) + '"><span>' +
        esc(a.name || a.id) + "</span><small>" + esc(a.id) + "</small></button>").join("") +
        '</div><span class="eyebrow">Or someone else</span>' : "") +
      '<div class="formrow"><div><label class="lbl" for="ap-name">Your first name</label><input class="field" id="ap-name" maxlength="24" autocomplete="given-name" value="' + esc(S.agentName) + '"></div>' +
      '<div><label class="lbl" for="ap-seat">Seat</label><input class="field num" id="ap-seat" maxlength="24" placeholder="' +
      esc((seats[0] && seats[0].id) || "agent1") + '" value="' + esc(S.agent || (seats[0] && seats[0].id) || "agent1") + '"></div></div>' +
      '<p class="err" id="ap-err"></p><div class="acts"><button class="btn primary" id="ap-go">Start dialing</button></div>',
      { locked: firstRun });
    function choose(id, name) {
      id = String(id || "").replace(/[^A-Za-z0-9_-]/g, "").slice(0, 24);
      name = String(name || "").trim().slice(0, 24);
      if (!id) { $("ap-err").textContent = "Seat can only use letters, numbers, dashes and underscores."; return; }
      if (!name) { $("ap-err").textContent = "Add your first name. The scripts use it."; return; }
      const changedSeat = S.agent && id !== S.agent;
      store.set("dialer_agent", id); store.set("pd_agent_name", name);
      S.agent = id; S.agentName = name;
      closeModal(true);
      if (changedSeat) { location.reload(); return; }
      resolve();
    }
    $("modal-box").addEventListener("click", (e) => {
      const s = e.target.closest("[data-seat]");
      if (s) choose(s.getAttribute("data-seat"), s.getAttribute("data-name"));
    });
    $("ap-go").addEventListener("click", () => choose($("ap-seat").value, $("ap-name").value));
    ["ap-name", "ap-seat"].forEach((id) => $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") $("ap-go").click(); }));
  });
}

export function manualModal() {
  if (busy()) { toast("warn", "Finish this call first."); return; }
  openModal(
    '<div class="dh"><div><h2>Dial a number</h2><p class="sub">US and Canada only. It is checked against the do-not-call list and the daily cap, ' +
    "then opens as a lead so the call is logged like any other.</p></div>" + closeX() + "</div>" +
    '<div><label class="lbl" for="md-num">Phone number</label><input class="field num" id="md-num" type="tel" inputmode="tel" autocomplete="off" placeholder="(415) 555-0134" style="height:44px;font-size:18px"></div>' +
    '<div class="keys" id="md-keys">' + keysHTML("data-d")
      .replace(/<button class="key" type="button" data-d="\*">\*<\/button>/, "<span></span>")
      .replace(/<button class="key" type="button" data-d="#">#<\/button>/,
               '<button class="key" type="button" data-d="back" aria-label="Delete last digit">' + icon("undo") + "</button>") + "</div>" +
    '<p class="err" id="md-err"></p><div class="acts"><button class="btn" data-close>Cancel</button><button class="btn primary" id="md-go">Open lead</button></div>');
  const inp = $("md-num");
  $("md-keys").addEventListener("click", (e) => {
    const b = e.target.closest("[data-d]");
    if (!b) return;
    const d = b.getAttribute("data-d");
    inp.value = d === "back" ? inp.value.slice(0, -1) : inp.value + d;
    inp.focus();
  });
  function go() {
    const digits = inp.value.replace(/\D/g, "").replace(/^1(?=\d{10}$)/, "");
    if (digits.length !== 10) { $("md-err").textContent = "Enter a 10-digit US or Canadian number."; return; }
    $("md-go").disabled = true;
    actions.openLead("+1" + digits, "/api/manual").then((ok) => { if (ok) closeModal(); else $("md-go").disabled = false; });
  }
  $("md-go").addEventListener("click", go);
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
}

export const SHORTCUTS = [
  ["Start, pause or resume the session", "p"], ["Dial, or hang up", "space"], ["Hold the auto-dial", "esc"], ["Skip lead", "s"],
  ["Mute", "m"], ["Keypad", "k"], ["Drop voicemail", "v"], ["Touch-tones on a live call", "0-9 * #"],
  ["Jump to notes", "n"], ["Pick an outcome in wrap-up", "1-9 0"], ["Accept the suggested outcome", "enter"], ["Undo last outcome", "z"],
  ["Full stats and CSV", "t"], ["Search leads", "/"], ["Dial a number", "d"], ["Copy the number", "c"], ["Objections", "o"]
];
export function shortcutsModal() {
  openModal('<div class="dh"><h2>Keyboard shortcuts</h2>' + closeX() + '</div><div class="keys-table">' +
    SHORTCUTS.map((r) => "<div><span>" + r[0] + "</span><kbd>" + r[1] + "</kbd></div>").join("") + "</div>", { wide: true });
}

export function logModal() {
  openModal('<div class="dh"><div><h2>Activity log</h2><p class="sub">This browser session only. Saved calls live in the Calls tab.</p></div>' + closeX() +
    '</div><div class="log">' + (getLog().map((e) => "<div><time>" + esc(e.t) + "</time><span>" + e.h + "</span></div>").join("") ||
    '<p class="muted">Nothing yet.</p>') + "</div>", { wide: true });
}

export function settingsModal() {
  const st = S.settings;
  const delays = [0, 2, 3, 5, 8, 12], curDelay = actions.autodialDelay();
  if (delays.indexOf(curDelay) < 0) delays.push(curDelay);
  delays.sort((a, b) => a - b);
  openModal(
    '<div class="dh"><h2>Settings</h2>' + closeX() + "</div><div>" +
    '<div class="setting"><div class="what"><b>Theme</b><span>System follows your OS.</span></div><div class="seg" id="st-theme">' +
      ["system", "dark", "light"].map((t) => '<button data-v="' + t + '" aria-selected="' + (st.theme === t) + '">' + t[0].toUpperCase() + t.slice(1) + "</button>").join("") + "</div></div>" +
    '<div class="setting"><div class="what"><b>Auto-dial delay</b><span>Breathing room between wrap-up and the next dial in a session.</span></div>' +
      '<select class="field" id="st-delay">' + delays.map((d) => '<option value="' + d + '"' + (d === curDelay ? " selected" : "") + ">" + (d ? d + " seconds" : "Instant") + "</option>").join("") + "</select></div>" +
    '<div class="setting"><div class="what"><b>Microphone</b><span id="st-mic-h">Used for calls from this browser.</span></div><select class="field" id="st-mic"><option value="">System default</option></select></div>' +
    '<div class="setting"><div class="what"><b>Speaker</b><span id="st-spk-h">Where the other side plays.</span></div><select class="field" id="st-spk"><option value="">System default</option></select></div>' +
    '<div class="setting"><div class="what"><b>Desktop alert on inbound</b><span>Only when this tab is in the background.</span></div>' +
      '<button class="btn" id="st-notify" aria-pressed="' + st.notify + '">' + (st.notify ? "On" : "Off") + "</button></div></div>");

  $("st-theme").addEventListener("click", (e) => {
    const b = e.target.closest("[data-v]"); if (!b) return;
    st.theme = b.getAttribute("data-v"); store.set("pd_theme", st.theme); applyTheme();
    $("st-theme").querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", x === b ? "true" : "false"));
  });
  $("st-delay").addEventListener("change", function () { st.delay = this.value; store.set("pd_delay", this.value); });
  $("st-notify").addEventListener("click", function () {
    const set = (v) => { st.notify = v; store.set("pd_notify", v ? "1" : "0"); this.textContent = v ? "On" : "Off"; this.setAttribute("aria-pressed", v); };
    if (st.notify) return set(false);
    if (!window.Notification) { toast("warn", "This browser does not support desktop notifications."); return; }
    Notification.requestPermission().then((p) => {
      if (p === "granted") set(true); else toast("warn", "Notifications are blocked for this site. Allow them in the browser's site settings.");
    });
  });

  function fill(sel, kind, chosen) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    navigator.mediaDevices.enumerateDevices().then((list) => {
      list.filter((d) => d.kind === kind && d.deviceId && d.deviceId !== "default").forEach((d, i) => {
        const o = document.createElement("option");
        o.value = d.deviceId; o.textContent = d.label || (kind === "audioinput" ? "Microphone " : "Speaker ") + (i + 1);
        if (d.deviceId === chosen) o.selected = true;
        sel.appendChild(o);
      });
    }).catch(() => {});
  }
  fill($("st-mic"), "audioinput", st.mic);
  fill($("st-spk"), "audiooutput", st.speaker);
  if (!S.live) {
    $("st-mic").disabled = $("st-spk").disabled = true;
    $("st-mic-h").textContent = $("st-spk-h").textContent = "Available once a carrier is connected.";
  } else if (!actions.carrier().audio.outputSupported) {
    $("st-spk").disabled = true; $("st-spk-h").textContent = "This browser cannot switch speakers. Use the OS setting.";
  }
  $("st-mic").addEventListener("change", function () { st.mic = this.value; store.set("pd_mic", this.value); actions.applyAudio(); });
  $("st-spk").addEventListener("change", function () { st.speaker = this.value; store.set("pd_speaker", this.value); actions.applyAudio(); });
}

export function pickList() {
  if (S.state === "LIVE" || S.state === "DIALING") { toast("warn", "Finish the call before loading a new list."); return; }
  $("f-list").click();
}

export function uploadList(file) {
  const t = toast("info", "Prepping <b>" + esc(file.name) + "</b>: validating, scoring and de-duping.", { ms: 300000 });
  say("Uploading <b>" + esc(file.name) + "</b>");
  fetch("/api/upload", { method: "POST", headers: { "X-Filename": file.name }, body: file })
    .then((r) => r.json())
    .then((d) => {
      t.close();
      if (d.ok) {
        toast("success", "List loaded: <b>" + d.added + "</b> new leads, " + d.refreshed + " already known.");
        say("List prepped: " + d.added + " new, " + d.refreshed + " known");
        actions.renderStats(d.stats);
        if (S.state === "IDLE" && !S.session.paused) actions.nextLead(); else actions.refreshQueue();
      } else {
        toast("error", "<b>List prep failed:</b> " + esc(d.error || "unknown error") +
          (d.log ? "<br><small>" + esc(String(d.log).split("\n").pop()) + "</small>" : ""), { ms: 15000 });
      }
    })
    .catch((e) => { t.close(); toast("error", "Upload failed: " + esc(e.message)); });
}
