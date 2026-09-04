"use strict";
(function () {
const { api, session, showMessage, onSubmit, mountHeader, homeFor, escapeHtml } = window.Mela;

// Already signed in? Go straight to the right dashboard.
const existing = session.get();
if (existing) location.href = homeFor(existing.user.role);
mountHeader(null);

// --- account type tabs -------------------------------------------------------
let signupRole = "customer";
document.querySelectorAll('[role="tab"]').forEach((tab) => {
  tab.addEventListener("click", () => {
    signupRole = tab.dataset.role;
    document.querySelectorAll('[role="tab"]').forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
    document.getElementById("customer-fields").classList.toggle("hidden", signupRole !== "customer");
    document.getElementById("vendor-fields").classList.toggle("hidden", signupRole !== "vendor");
    document.querySelector('[name="shop_name"]').required = signupRole === "vendor";
  });
});

// --- sign in -----------------------------------------------------------------
const loginNotice = document.getElementById("login-notice");
onSubmit(document.getElementById("login-form"), loginNotice, async (form) => {
  const result = await api("POST", "/api/auth/login", { email: form.email, password: form.password });
  session.set(result);
  location.href = homeFor(result.user.role);
});

// --- create account ----------------------------------------------------------
const registerNotice = document.getElementById("register-notice");
onSubmit(document.getElementById("register-form"), registerNotice, async (form) => {
  const payload = { name: form.name, email: form.email, password: form.password, role: signupRole };
  if (signupRole === "vendor") {
    payload.shop_name = form.shop_name;
    payload.city = form.city;
    payload.category = form.category;
  } else if (form.phone) {
    payload.phone = form.phone;
  }

  const result = await api("POST", "/api/auth/register", payload);
  session.set(result);

  // A shop's till key is shown exactly once, so make them copy it before moving on.
  if (result.api_key) {
    sessionStorage.setItem("melacoin.apikey", result.api_key);
    showMessage(
      registerNotice,
      `Shop created. Your till (POS) key is ${result.api_key} — copy it now, it is never shown again. Taking you to your dashboard…`,
      "success"
    );
    setTimeout(() => (location.href = homeFor(result.user.role)), 4000);
    return;
  }
  location.href = homeFor(result.user.role);
});

// --- shop directory ----------------------------------------------------------
api("GET", "/api/public/vendors")
  .then(({ vendors }) => {
    const body = document.querySelector("#vendor-table tbody");
    if (!vendors.length) {
      body.innerHTML = '<tr><td colspan="6" class="muted">No shops yet. Create a shop account, or run <span class="mono">npm run seed</span>.</td></tr>';
      return;
    }
    body.innerHTML = vendors
      .map(
        (v) => `<tr>
          <td><strong>${escapeHtml(v.name)}</strong><br><small class="muted">${escapeHtml(v.category)}</small></td>
          <td>${escapeHtml(v.city || "—")}</td>
          <td>${escapeHtml(v.earn_rate_label)}</td>
          <td>${escapeHtml(v.redeem_rate_label)}</td>
          <td>${v.allow_mela_conversion ? '<span class="badge good">Yes</span>' : '<span class="badge">No</span>'}</td>
          <td>${v.accepts_mela ? '<span class="badge good">Yes</span>' : '<span class="badge">No</span>'}</td>
        </tr>`
      )
      .join("");
  })
  .catch(() => {
    document.querySelector("#vendor-table tbody").innerHTML =
      '<tr><td colspan="6" class="muted">Could not load the shop list.</td></tr>';
  });

})();
