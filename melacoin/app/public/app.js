"use strict";
/**
 * app.js - shared helpers for every page: talking to the API, remembering the
 * logged-in user, and formatting rupees and points the same way everywhere.
 */

// Everything lives inside this function so the only name this file adds to the page
// is `window.Mela`. Without it, a page script doing `const { api } = window.Mela`
// collides with the `api` declared here and the whole page's JavaScript dies.
(function () {

const STORAGE_KEY = "melacoin.session";

const session = {
  get() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    } catch {
      return null;
    }
  },
  set(value) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  },
  clear() {
    localStorage.removeItem(STORAGE_KEY);
    sessionStorage.removeItem("melacoin.apikey");
  },
  token() {
    return this.get()?.token || null;
  },
};

/** Calls the API and throws an Error carrying the server's message on failure. */
async function api(method, path, body, extraHeaders = {}) {
  const headers = { "content-type": "application/json", ...extraHeaders };
  const token = session.token();
  if (token) headers.authorization = `Bearer ${token}`;

  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  let payload = {};
  try {
    payload = await response.json();
  } catch {
    /* an empty body is fine */
  }

  if (!response.ok) {
    if (response.status === 401 && session.token()) {
      session.clear();
      location.href = "/";
    }
    throw new Error(payload.error || `Request failed (${response.status})`);
  }
  return payload;
}

// ------------------------------------------------------------- formatting

const rupees = (paise) => {
  const negative = paise < 0;
  const absolute = Math.abs(paise);
  const whole = Math.floor(absolute / 100).toLocaleString("en-IN");
  return `${negative ? "-" : ""}₹${whole}.${String(absolute % 100).padStart(2, "0")}`;
};

/** wei (a string of up to 18 zeros) -> a short readable MELA amount. */
const mela = (wei, decimals = 4) => {
  const amount = BigInt(wei || 0);
  const ONE = 10n ** 18n;
  const whole = (amount / ONE).toString();
  const fraction = (amount % ONE).toString().padStart(18, "0").slice(0, decimals).replace(/0+$/, "");
  return fraction ? `${whole}.${fraction}` : whole;
};

const melaToWei = (amount) => {
  const [whole, fraction = ""] = String(amount).trim().split(".");
  return BigInt(whole || 0) * 10n ** 18n + BigInt((fraction + "000000000000000000").slice(0, 18));
};

const rupeesToPaise = (value) => Math.round(Number(value) * 100);
const when = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "—");
const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ------------------------------------------------------------------- ui

function showMessage(element, message, kind = "error") {
  if (!element) return;
  element.className = `notice ${kind}`;
  element.textContent = message;
  element.classList.remove("hidden");
}

function clearMessage(element) {
  if (element) element.classList.add("hidden");
}

/** Wraps a submit handler so the button disables and errors always surface. */
function onSubmit(form, noticeElement, handler) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"], button:not([type])');
    if (button) button.disabled = true;
    clearMessage(noticeElement);
    try {
      await handler(Object.fromEntries(new FormData(form)));
    } catch (error) {
      showMessage(noticeElement, error.message);
    } finally {
      if (button) button.disabled = false;
    }
  });
}

/** Renders the shared header and kicks the user out if they are on the wrong page. */
function mountHeader(currentRole) {
  const current = session.get();
  const nav = document.getElementById("nav");
  if (!nav) return current;

  if (!current) {
    if (currentRole) {
      location.href = "/";
      return null;
    }
    return null;
  }
  if (currentRole && current.user.role !== currentRole) {
    location.href = homeFor(current.user.role);
    return null;
  }

  nav.innerHTML = `
    <span class="badge">${escapeHtml(current.user.name)} · ${escapeHtml(current.user.role)}</span>
    <a href="${homeFor(current.user.role)}"><button class="secondary small" type="button">My dashboard</button></a>
    <button class="secondary small" type="button" id="logout">Sign out</button>`;

  nav.querySelector("#logout").addEventListener("click", async () => {
    try {
      await api("POST", "/api/auth/logout");
    } catch {
      /* signing out locally is what matters */
    }
    session.clear();
    location.href = "/";
  });
  return current;
}

const homeFor = (role) =>
  role === "vendor" ? "/vendor.html" : role === "admin" ? "/admin.html" : "/customer.html";

window.Mela = {
  api, session, rupees, mela, melaToWei, rupeesToPaise, when,
  escapeHtml, showMessage, clearMessage, onSubmit, mountHeader, homeFor,
};

})();
