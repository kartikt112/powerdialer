import { S } from "./state.js";

export function api(path, body) {
  const opts = body !== undefined
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.assign({ agent: S.agent }, body)) }
    : undefined;
  return fetch(path, opts).then((r) => r.json().catch(() => ({ error: "The server answered " + r.status })));
}
export const withAgent = (path) => path + (path.indexOf("?") < 0 ? "?" : "&") + "agent=" + encodeURIComponent(S.agent);
