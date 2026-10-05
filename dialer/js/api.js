/* Thin fetch wrapper. Every route answers JSON, errors included. Every call
   says which campaign the cockpit is working (X-Campaign); none means all. */
import { S } from "./state.js";

export function campaignHeaders(extra) {
  return Object.assign(S.campaign ? { "X-Campaign": String(S.campaign) } : {}, extra || {});
}

export function api(path, body) {
  const opts = body !== undefined
    ? { method: "POST", headers: campaignHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body) }
    : { headers: campaignHeaders() };
  return fetch(path, opts).then((r) => r.json().catch(() => ({ error: "Server returned " + r.status })));
}
export function withAgent(path) {
  return path + (path.indexOf("?") < 0 ? "?" : "&") + "agent=" + encodeURIComponent(S.agent);
}
/* Download links can't send headers, so they carry the campaign in the URL. */
export function withCampaign(path) {
  return S.campaign ? path + (path.indexOf("?") < 0 ? "?" : "&") + "campaign=" + S.campaign : path;
}
