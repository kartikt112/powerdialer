/* Small helpers: DOM, escaping, IST time, Indian number formats, storage. */

export const $ = (id) => document.getElementById(id);

export function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
export const pad = (n) => (n < 10 ? "0" : "") + n;
export function fmtClock(s) { s = Math.max(0, Math.floor(s)); return (s >= 3600 ? Math.floor(s / 3600) + ":" : "") + pad(Math.floor(s / 60) % 60) + ":" + pad(s % 60); }
export const fmtNum = (n) => Number(n || 0).toLocaleString("en-IN");
export const fmtINR = (n) => "₹" + Number(n || 0).toLocaleString("en-IN");
export function fmtPct(x) {
  if (x == null || !isFinite(x)) return "-";
  const v = x * 100;
  return (v >= 10 || v === 0 ? v.toFixed(0) : v.toFixed(1)) + "%";
}

/* Server stamps are UTC 'YYYY-MM-DD HH:MM:SS'. */
export const parseUTC = (s) => (s ? new Date(String(s).replace(" ", "T") + "Z") : null);
export const toServer = (d) => d.toISOString().slice(0, 19).replace("T", " ");

/* IST is a fixed +5:30. Read an instant as IST with the UTC getters. */
const IST_MS = 330 * 60000;
export const istShift = (d) => new Date(d.getTime() + IST_MS);
export function istClock(d) {
  const x = istShift(d), h = x.getUTCHours();
  return ((h % 12) || 12) + ":" + pad(x.getUTCMinutes()) + (h < 12 ? " am" : " pm");
}
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function istDay(d) {
  const x = istShift(d), n = istShift(new Date());
  const day = (v) => Math.floor(Date.UTC(v.getUTCFullYear(), v.getUTCMonth(), v.getUTCDate()) / 86400000);
  const diff = day(x) - day(n);
  if (diff === 0) return "Today";
  if (diff === 1) return "Tomorrow";
  if (diff === -1) return "Yesterday";
  return DOW[x.getUTCDay()] + " " + x.getUTCDate() + " " + MON[x.getUTCMonth()];
}
/* "from today's call", "from the call on Mon 28 Sep" */
export function fromCall(d) {
  if (!d) return "";
  const day = istDay(d);
  return day === "Today" ? "from today's call" : day === "Yesterday" ? "from yesterday's call" : "from the call on " + day;
}
export const istWhen = (d) => (d ? istDay(d) + " " + istClock(d) : "");
/* 'YYYY-MM-DDTHH:MM' for a datetime-local input, on the IST clock. */
export function istInput(d) {
  const x = istShift(d);
  return x.getUTCFullYear() + "-" + pad(x.getUTCMonth() + 1) + "-" + pad(x.getUTCDate()) + "T" + pad(x.getUTCHours()) + ":" + pad(x.getUTCMinutes());
}
export function fromIstInput(v) {
  const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/.exec(v || "");
  return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) - IST_MS) : null;
}
export function rel(d) {
  const mins = Math.round((d.getTime() - Date.now()) / 60000), a = Math.abs(mins);
  if (a < 1) return "now";
  const txt = a < 60 ? a + " min" : a < 1440 ? Math.floor(a / 60) + " h" + (a % 60 && a < 600 ? " " + (a % 60) + " min" : "") : Math.floor(a / 1440) + " d";
  return mins < 0 ? txt + " ago" : "in " + txt;
}

export function prettyPhone(e164, kind) {
  let d = String(e164 || "").replace(/\D/g, "");
  if (d.length === 12 && d.startsWith("91")) d = d.slice(2);
  if (d.length === 13 && d.startsWith("91")) d = d.slice(2);
  if (/^18(00|60)/.test(d)) return d.slice(0, 4) + " " + d.slice(4, 7) + " " + d.slice(7);
  if (d.length !== 10) return e164 || "";
  if (kind === "mobile" || /^9/.test(d)) return d.slice(0, 5) + " " + d.slice(5);
  const two = ["11", "20", "22", "33", "40", "44", "79", "80"].indexOf(d.slice(0, 2)) >= 0;
  const std = two ? 2 : /^[1-7]/.test(d) ? 3 : 4, local = d.slice(std);
  return "0" + d.slice(0, std) + " " + (local.length === 8 ? local.slice(0, 4) + " " + local.slice(4) : local.slice(0, 3) + " " + local.slice(3));
}

export const firstName = (full) => {
  const words = String(full || "").replace(/^(mr|mrs|ms|dr|shri)\.?\s+/i, "").trim().split(/\s+/);
  return words[0] || "";
};
export function debounce(fn, ms) { let t; return function (...a) { clearTimeout(t); t = setTimeout(() => fn.apply(this, a), ms); }; }
export const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
  del(k) { try { localStorage.removeItem(k); } catch (e) {} }
};
