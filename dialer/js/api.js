/* Thin fetch wrapper. Every route answers JSON, errors included. */
import { S } from "./state.js";

export function api(path, body) {
  const opts = body !== undefined
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : undefined;
  return fetch(path, opts).then((r) => r.json().catch(() => ({ error: "Server returned " + r.status })));
}
export function withAgent(path) {
  return path + (path.indexOf("?") < 0 ? "?" : "&") + "agent=" + encodeURIComponent(S.agent);
}
