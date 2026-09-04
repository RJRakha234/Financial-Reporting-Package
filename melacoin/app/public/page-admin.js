"use strict";
(function () {
const M = window.Mela;
const { api, rupees, when, escapeHtml, showMessage, onSubmit } = M;

M.mountHeader("admin");

async function refresh() {
  const [stats, settlements, audit] = await Promise.all([
    api("GET", "/api/admin/stats"),
    api("GET", "/api/admin/settlements"),
    api("GET", "/api/admin/audit"),
  ]);
  renderBacking(stats);
  renderHeadline(stats);
  renderSettlements(settlements.vendors);
  renderAudit(audit.entries);
  return stats;
}

function renderBacking(stats) {
  const percent = (stats.backing_ratio_bps / 100).toFixed(2);
  const healthy = stats.backing_ratio_bps >= 10000;
  document.getElementById("backing").innerHTML = `
    <div class="grid three">
      <div class="stat">
        <div class="label">Backing ratio</div>
        <div class="value" style="color:${healthy ? "var(--accent)" : "var(--danger)"}">${percent}%</div>
        <div class="note">${healthy ? "Every token is covered" : "You have issued more than you collected"}</div>
      </div>
      <div class="stat">
        <div class="label">MelaCoin in customer hands</div>
        <div class="value">${stats.display.mela_outstanding}</div>
        <div class="note">worth ${stats.display.mela_outstanding_value}</div>
      </div>
      <div class="stat">
        <div class="label">Rupees behind it</div>
        <div class="value">${stats.display.treasury_reserve}</div>
        <div class="note">collected from shops, minus MELA paid out to shops</div>
      </div>
    </div>`;
}

function renderHeadline(stats) {
  document.getElementById("headline").innerHTML = `
    <div class="stat">
      <div class="label">Network volume</div>
      <div class="value">${stats.display.purchase_volume}</div>
      <div class="note">${stats.purchases} bill(s) · ${stats.vendors} shop(s) · ${stats.customers} customer(s)</div>
    </div>
    <div class="stat">
      <div class="label">Platform revenue</div>
      <div class="value">${stats.display.platform_fee}</div>
      <div class="note">conversion fees from ${stats.conversions} conversion(s)</div>
    </div>
    <div class="stat">
      <div class="label">Live points</div>
      <div class="value">${stats.live_points.toLocaleString("en-IN")}</div>
      <div class="note">unspent across all shops</div>
    </div>`;
}

function renderSettlements(vendors) {
  const body = document.querySelector("#settlements-table tbody");
  if (!vendors.length) {
    body.innerHTML = '<tr><td colspan="3" class="muted">Everyone is settled.</td></tr>';
    return;
  }
  body.innerHTML = vendors
    .map(
      (v) => `<tr>
        <td>${escapeHtml(v.name)}</td>
        <td class="num" style="color:${v.balance_paise > 0 ? "var(--accent)" : "var(--warn)"}">${v.display_balance}</td>
        <td class="num">${
          v.balance_paise > 0
            ? `<button class="small secondary" data-settle="${escapeHtml(v.id)}" data-amount="${v.balance_paise}">Mark paid</button>`
            : ""
        }</td>
      </tr>`
    )
    .join("");

  body.querySelectorAll("[data-settle]").forEach((button) => {
    button.addEventListener("click", async () => {
      const notice = document.getElementById("settlement-notice");
      const amount = Number(button.dataset.amount);
      if (!confirm(`Record a payment of ${rupees(amount)} from this shop?`)) return;
      try {
        await api("POST", "/api/admin/settlements/payment", {
          vendor_id: button.dataset.settle,
          amount_paise: amount,
          note: "Settled from admin console",
        });
        showMessage(notice, "Payment recorded.", "success");
        await refresh();
      } catch (error) {
        showMessage(notice, error.message);
      }
    });
  });
}

function renderAudit(entries) {
  const body = document.querySelector("#audit-table tbody");
  body.innerHTML = entries.length
    ? entries
        .slice(0, 30)
        .map(
          (entry) => `<tr>
            <td>${when(entry.created_at)}</td>
            <td class="mono">${escapeHtml(entry.action)}</td>
            <td class="muted">${escapeHtml((entry.meta || "").slice(0, 120))}</td>
          </tr>`
        )
        .join("")
    : '<tr><td colspan="3" class="muted">Nothing logged yet.</td></tr>';
}

const priceNotice = document.getElementById("price-notice");
onSubmit(document.getElementById("price-form"), priceNotice, async (form) => {
  const result = await api("POST", "/api/admin/mela-price", {
    mela_price_paise: Math.round(Number(form.price) * 100),
  });
  showMessage(priceNotice, `Price updated to ${result.display}.`, "success");
  await refresh();
});

refresh().catch((error) => showMessage(priceNotice, error.message));

// Pre-fill the price box with what the price actually is right now.
api("GET", "/api/public/token")
  .then((token) => {
    document.querySelector('#price-form [name="price"]').value = (token.price_paise / 100).toFixed(2);
  })
  .catch(() => {
    /* the dashboard still works without this */
  });

})();
