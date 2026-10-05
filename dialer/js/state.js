/* One shared, mutable state object plus a tiny event bus. Modules read and
   write S directly and announce changes with emit(); nobody imports app.js,
   so there are no circular imports. Cross-module commands go through
   `actions`, which app.js fills in at boot. */
import { store } from "./util.js";

export const S = {
  cfg: null,
  state: "IDLE",            // IDLE READY DIALING LIVE WRAP
  cur: null,                // checked-out lead payload
  picked: false,            // chosen by hand: never auto-dialled
  inbound: false,
  stats: null,
  callSec: 0, ringSec: 0, wrapSec: 0,
  live: false,              // a real carrier is attached
  muted: false,
  agent: store.get("dialer_agent", ""),
  agentName: store.get("pd_agent_name", ""),
  session: { on: false, paused: null, pausePending: null, activeSec: 0, pauseSec: 0 },
  call: {},                 // per-call capture (pain, objections, booking...)
  settings: {
    theme: store.get("pd_theme", "system"),
    delay: store.get("pd_delay", ""),
    mic: store.get("pd_mic", ""),
    speaker: store.get("pd_speaker", ""),
    notify: store.get("pd_notify", "0") === "1"
  },
  campaign: Number(store.get("pd_campaign", "")) || null,   // the campaign being called; null = all
  campaigns: [],
  data: { queue: [], callbacks: [], calls: [], missed: [], voicemails: [] }
};

const listeners = {};
export function on(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); }
export function emit(evt, payload) { (listeners[evt] || []).forEach((fn) => fn(payload)); }

export const actions = {};  // openLead, nextLead, dial, ... registered by app.js

export function outcome(key) {
  const list = (S.cfg && S.cfg.outcomes) || [];
  for (const o of list) if (o.key === key) return o;
  return { key, label: String(key || "").replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase()),
           tone: "plain", kind: "final" };
}
/* The script the current campaign is set to, if any. */
export function campaignScript() {
  const c = (S.campaigns || []).find((x) => x.id === S.campaign);
  return (c && c.script_version) || "";
}
export function busy() { return S.state === "DIALING" || S.state === "LIVE" || S.state === "WRAP"; }
