/* Toasts, the activity log, the one shared modal, menus, the banner. */
import { $, icon, esc } from "./util.js";

let LOG = [];
try { LOG = JSON.parse(sessionStorage.getItem("pd_log") || "[]"); } catch (e) {}
export function say(html) {
  LOG.unshift({ t: new Date().toTimeString().slice(0, 5), h: html });
  LOG.length = Math.min(LOG.length, 120);
  try { sessionStorage.setItem("pd_log", JSON.stringify(LOG)); } catch (e) {}
}
export const getLog = () => LOG;

export function toast(kind, html, opts) {
  opts = opts || {};
  const icons = { error: "alert", warn: "alert", success: "check", info: "info" };
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.innerHTML = icon(icons[kind] || "info") + '<span class="msg">' + html + "</span>";
  (opts.actions || (opts.action ? [opts.action] : [])).forEach((a) => {
    const b = document.createElement("button");
    b.className = "btn sm";
    b.innerHTML = a.html;
    b.addEventListener("click", () => { a.run(); if (!a.keep) close(); });
    el.appendChild(b);
  });
  const x = document.createElement("button");
  x.className = "btn icon sm quiet"; x.setAttribute("aria-label", "Dismiss"); x.innerHTML = icon("x", "sm");
  x.addEventListener("click", close);
  el.appendChild(x);
  $("toasts").appendChild(el);
  const timer = setTimeout(close, opts.ms || (kind === "error" ? 7000 : 4200));
  function close() {
    clearTimeout(timer);
    if (!el.parentNode) return;
    el.classList.add("out");
    setTimeout(() => { if (el.parentNode) el.remove(); }, 180);
    if (opts.onClose) opts.onClose();
  }
  if (kind === "error" || kind === "warn") say(html);
  return { close };
}

export function banner(kind, html) {
  $("banner").hidden = !html;
  $("banner").className = "banner" + (kind === "danger" ? " danger" : "");
  $("banner-text").innerHTML = html || "";
}

/* ---- modal ------------------------------------------------------------ */
export const modal = { open: false, locked: false, opener: null };
export const closeX = () => '<button class="btn icon sm quiet" data-close aria-label="Close">' + icon("x") + "</button>";
export function openModal(html, opts) {
  opts = opts || {};
  modal.open = true; modal.locked = !!opts.locked; modal.opener = document.activeElement;
  $("modal-box").className = "dialog" + (opts.wide ? " wide" : "") + (opts.xwide ? " xwide" : "");
  $("modal-box").innerHTML = html;
  $("modal").hidden = false;
  const f = $("modal-box").querySelector("[autofocus], input, select, button:not([data-close])");
  if (f) f.focus();
}
export function closeModal(force) {
  if (!modal.open || (modal.locked && !force)) return;
  $("modal").hidden = true; $("modal-box").innerHTML = "";
  const o = modal.opener;
  modal.open = false; modal.locked = false; modal.opener = null;
  if (o && o.focus) o.focus();
}

/* ---- menus ------------------------------------------------------------ */
const MENUS = [["agent-menu", "b-agent"], ["pause-menu", "b-pause"], ["camp-menu", "b-campaign"]];
export function closeMenus() {
  MENUS.forEach(([m, b]) => { $(m).hidden = true; $(b).setAttribute("aria-expanded", "false"); });
}
export const menuOpen = () => MENUS.some(([m]) => !$(m).hidden);
export function toggleMenu(menuId, btnId) {
  const open = $(menuId).hidden;
  closeMenus();
  $(menuId).hidden = !open;
  $(btnId).setAttribute("aria-expanded", open ? "true" : "false");
  if (open) { const f = $(menuId).querySelector("button, a"); if (f) f.focus(); }
}

export function copyText(text, okHtml) {
  return (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(
    () => { toast("success", okHtml || "Copied", { ms: 1800 }); return true; },
    () => { toast("warn", "Could not reach the clipboard. Text: " + esc(text.slice(0, 80))); return false; });
}
