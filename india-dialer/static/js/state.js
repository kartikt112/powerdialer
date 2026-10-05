/* One shared state object and a tiny event bus. Modules talk through
   `actions` (registered by app.js) so there are no circular imports. */
import { store } from "./util.js";

export const S = {
  cfg: null,
  agent: store.get("in_agent", "pawan"),
  state: "idle",          // idle ready ringing connected wrap
  cur: null,              // the lead on screen (server payload)
  phoneIdx: 0,            // which of cur.phones is being dialled
  call: {},               // what this attempt has captured so far
  dialedAt: null, connectedAt: null, endedAt: null,
  picked: false,          // opened by hand: never auto-dialled
  session: { on: false, paused: null, pausePending: null, id: null, activeSec: 0, pauseSec: 0, dials: 0, target: 100, minutes: 90, script: "" },
  stats: null,
  data: {}
};

const listeners = {};
export function on(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); }
export function emit(evt, payload) { (listeners[evt] || []).forEach((fn) => fn(payload)); }
export const actions = {};

export function outcome(key) {
  return ((S.cfg && S.cfg.outcomes) || []).find((o) => o.key === key) ||
    { key, label: String(key || "").replace(/_/g, " ").toLowerCase(), kind: "final" };
}
export const onCall = () => S.state === "ringing" || S.state === "connected";
export const busy = () => onCall() || S.state === "wrap";
