/* Toasts, the one modal, menus, the banner. */
import { $, esc } from "./util.js";

export function toast(kind, html, opts) {
  opts = opts || {};
  const el = document.createElement("div");
  el.className = "toast " + (kind === "error" ? "error" : "");
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.innerHTML = '<span class="msg">' + html + "</span>";
  (opts.actions || []).forEach((a) => {
    const b = document.createElement("button");
    b.className = "btn sm";
    b.innerHTML = a.html;
    b.addEventListener("click", () => { a.run(); if (!a.keep) close(); });
    el.appendChild(b);
  });
  const x = document.createElement("button");
  x.className = "btn sm"; x.textContent = "Close"; x.addEventListener("click", close);
  el.appendChild(x);
  $("toasts").appendChild(el);
  const timer = setTimeout(close, opts.ms || (kind === "error" ? 7000 : 4000));
  function close() { clearTimeout(timer); if (el.parentNode) el.remove(); if (opts.onClose) opts.onClose(); }
  return { close };
}

export function banner(kind, html) {
  $("banner").hidden = !html;
  $("banner").className = "banner" + (kind === "danger" ? " danger" : "");
  $("banner-text").innerHTML = html || "";
}

export const modal = { open: false, opener: null };
export const closeX = () => '<button class="btn quiet sm" data-close>Close</button>';
export function openModal(html, opts) {
  opts = opts || {};
  modal.open = true; modal.opener = document.activeElement;
  $("modal-box").className = "dialog" + (opts.wide ? " wide" : "") + (opts.xwide ? " xwide" : "");
  $("modal-box").innerHTML = html;
  $("modal-box").onkeydown = null;
  $("modal").hidden = false;
  const f = $("modal-box").querySelector("[autofocus], input, select, textarea, button:not([data-close])");
  if (f) f.focus();
}
export function closeModal() {
  if (!modal.open) return;
  $("modal").hidden = true; $("modal-box").innerHTML = "";
  modal.open = false;
  if (modal.opener && modal.opener.focus) modal.opener.focus();
}

const MENUS = [["main-menu", "b-menu"], ["pause-menu", "b-pause"]];
export function closeMenus() { MENUS.forEach(([m, b]) => { $(m).hidden = true; $(b).setAttribute("aria-expanded", "false"); }); }
export const menuOpen = () => MENUS.some(([m]) => !$(m).hidden);
export function toggleMenu(menu, btn) {
  const open = $(menu).hidden;
  closeMenus();
  $(menu).hidden = !open;
  $(btn).setAttribute("aria-expanded", String(open));
}

export function copyText(text, okHtml) {
  const done = () => toast("ok", okHtml || "Copied", { ms: 1600 });
  if (navigator.clipboard) return navigator.clipboard.writeText(text).then(done, () => toast("error", "Could not copy: " + esc(text)));
  toast("error", "Could not copy: " + esc(text));
  return Promise.resolve();
}
