"use strict";
(function () {
const M = window.Mela;
const { api, rupees, when, escapeHtml, showMessage, clearMessage, onSubmit, rupeesToPaise } = M;

M.mountHeader("vendor");

let vendor = null;

/**
 * The form talks in the units a shopkeeper thinks in (points per rupee, rupees per
 * point, percentages). The API stores whole numbers only. These two functions are
 * the translation layer, and they are the only place the conversion happens.
 */
const toForm = (v) => ({
  earn_points_per_rupee: v.earn_milli_points_per_rupee / 1000,
  redeem_rupees_per_point: v.redeem_milli_paise_per_point / 100000,
  min_redeem_points: v.min_redeem_points,
  max_redeem_percent: v.max_redeem_bps / 100,
  points_expiry_days: v.points_expiry_days,
  mela_conversion_fee_percent: v.mela_conversion_fee_bps / 100,
  conversion_budget_rupees: Math.round((v.conversion_budget_paise || 0) / 100),
});

const fromForm = (form) => ({
  earn_milli_points_per_rupee: Math.round(Number(form.earn_points_per_rupee) * 1000),
  redeem_milli_paise_per_point: Math.round(Number(form.redeem_rupees_per_point) * 100000),
  min_redeem_points: Math.round(Number(form.min_redeem_points)),
  max_redeem_bps: Math.round(Number(form.max_redeem_percent) * 100),
  points_expiry_days: Math.round(Number(form.points_expiry_days)),
  mela_conversion_fee_bps: Math.round(Number(form.mela_conversion_fee_percent) * 100),
  conversion_budget_paise: Math.round(Number(form.conversion_budget_rupees) * 100),
  earn_on_net: !!form.earn_on_net,
  allow_mela_conversion: !!form.allow_mela_conversion,
  accepts_mela: !!form.accepts_mela,
  active: !!form.active,
});

async function refresh() {
  const [me, customers, settlement, sales, balance] = await Promise.all([
    api("GET", "/api/vendor/me"),
    api("GET", "/api/vendor/customers"),
    api("GET", "/api/vendor/settlement"),
    api("GET", "/api/vendor/purchases"),
    api("GET", "/api/vendor/balance"),
  ]);
  vendor = me.vendor;
  renderHeadline(me);
  renderBalance(balance);
  fillSettings(me.vendor);
  renderCustomers(customers.customers);
  renderSettlement(settlement);
  renderSales(sales.purchases);
  document.getElementById("key-prefix").textContent = me.api_key_prefix ? `${me.api_key_prefix}…` : "No key yet";
}

function renderHeadline(me) {
  const s = me.stats;
  document.getElementById("headline").innerHTML = `
    <div class="stat">
      <div class="label">Sales through MelaCoin</div>
      <div class="value">${s.display.gross}</div>
      <div class="note">${s.purchases} bill(s) · ${s.customers} customer(s)</div>
    </div>
    <div class="stat">
      <div class="label">Points you still owe</div>
      <div class="value">${s.display.outstanding_liability}</div>
      <div class="note">${s.outstanding_points.toLocaleString("en-IN")} live points</div>
    </div>
    <div class="stat">
      <div class="label">Platform account</div>
      <div class="value">${s.display.settlement_balance}</div>
      <div class="note">${s.display.settlement_direction}</div>
    </div>`;
}

/** The shop's give-and-take with the rest of the network. */
function renderBalance(b) {
  const tone = b.status === "blocked" ? "var(--danger)" : b.status === "near_limit" ? "var(--warn)" : "var(--accent)";
  const used = Math.min(100, Math.max(0, b.used_bps / 100));
  const label = { blocked: "Conversion paused", near_limit: "Close to the limit", healthy: "Healthy" }[b.status];

  document.getElementById("balance-box").innerHTML = `
    <div class="stat">
      <div class="label">Net value that has left your shop</div>
      <div class="value" style="color:${tone}">${b.display.net_outflow}</div>
      <div class="note">of ${b.display.cap} allowed over ${b.window_days} days · ${escapeHtml(label)}</div>
    </div>
    <div style="height:8px;border-radius:5px;background:var(--surface-2);border:1px solid var(--border);
                overflow:hidden;margin:.7rem 0">
      <div style="height:100%;width:${used}%;background:${tone}"></div>
    </div>
    <table style="margin-top:.4rem">
      <tbody>
        <tr><td>Your customers converted away</td><td class="num">${b.display.outflow}</td></tr>
        <tr><td>MelaCoin you accepted</td><td class="num">${b.display.inflow}</td></tr>
        <tr><td>Still allowed to leave</td><td class="num"><strong>${b.display.headroom}</strong></td></tr>
        <tr><td>Your sales in this window</td><td class="num">${b.display.own_sales}</td></tr>
      </tbody>
    </table>
    <p class="muted" style="margin-top:.7rem;font-size:.87rem">${escapeHtml(b.explanation)}</p>
    ${
      b.one_way_valve
        ? `<div class="notice error" style="margin-top:.7rem">You let points convert out but do not accept
             MelaCoin. Value only flows away from you. Turning on <strong>Accept MelaCoin as payment</strong>
             brings customers — and your allowance — back.</div>`
        : ""
    }`;
}

function fillSettings(v) {
  const form = document.getElementById("settings-form");
  const values = toForm(v);
  for (const [field, value] of Object.entries(values)) form.elements[field].value = value;
  form.elements.earn_on_net.checked = !!v.earn_on_net;
  form.elements.allow_mela_conversion.checked = !!v.allow_mela_conversion;
  form.elements.accepts_mela.checked = !!v.accepts_mela;
  form.elements.active.checked = !!v.active;
  updatePreview();
}

/** Shows the effect of the current settings on a sample ₹500 bill, live. */
function updatePreview() {
  const form = document.getElementById("settings-form");
  const earn = Number(form.elements.earn_points_per_rupee.value) || 0;
  const perPoint = Number(form.elements.redeem_rupees_per_point.value) || 0;
  const capPercent = Number(form.elements.max_redeem_percent.value) || 0;

  const sample = 500;
  const points = Math.floor(sample * earn);
  const worth = points * perPoint;
  const costPercent = sample > 0 ? ((worth / sample) * 100).toFixed(2) : "0";
  const capRupees = (sample * capPercent) / 100;

  const preview = document.getElementById("rate-preview");
  preview.className = Number(costPercent) > 10 ? "notice error" : "notice";
  preview.innerHTML =
    `On a <strong>₹500</strong> bill a customer earns <strong>${points.toLocaleString("en-IN")} points</strong>, ` +
    `worth <strong>₹${worth.toFixed(2)}</strong> back at your shop.<br>` +
    `That is a discount of <strong>${costPercent}%</strong> on every sale — treat it as a marketing cost.<br>` +
    `Points could pay at most <strong>₹${capRupees.toFixed(2)}</strong> of this bill.` +
    (Number(costPercent) > 10 ? "<br><strong>That is high.</strong> Most shops sit between 1% and 5%." : "");
}

document.getElementById("settings-form").addEventListener("input", updatePreview);

const settingsNotice = document.getElementById("settings-notice");
onSubmit(document.getElementById("settings-form"), settingsNotice, async (form) => {
  const result = await api("PATCH", "/api/vendor/settings", fromForm(form));
  vendor = result.vendor;
  showMessage(settingsNotice, "Saved. New rules apply to the next sale.", "success");
  await refresh();
});

// -------------------------------------------------------------------- POS

const posNotice = document.getElementById("pos-notice");
const savedKey = sessionStorage.getItem("melacoin.apikey");
if (savedKey) document.querySelector('#pos-form [name="api_key"]').value = savedKey;

function posPayload(form) {
  return {
    customer: form.customer,
    gross_paise: rupeesToPaise(form.amount),
    points_to_redeem: Number(form.points) || 0,
  };
}

document.getElementById("pos-quote").addEventListener("click", async () => {
  const form = Object.fromEntries(new FormData(document.getElementById("pos-form")));
  clearMessage(posNotice);
  try {
    sessionStorage.setItem("melacoin.apikey", form.api_key);
    const quote = await api("POST", "/api/pos/quote", posPayload(form), { "x-api-key": form.api_key });
    showMessage(
      posNotice,
      `Bill ${quote.display.gross} − points ${quote.display.points_discount} = customer pays ${quote.display.net}. ` +
        `They will earn ${quote.points_earned} points. They can use up to ${quote.max_redeemable_points} points on this bill.`,
      "success"
    );
  } catch (error) {
    showMessage(posNotice, error.message);
  }
});

onSubmit(document.getElementById("pos-form"), posNotice, async (form) => {
  sessionStorage.setItem("melacoin.apikey", form.api_key);
  const result = await api(
    "POST",
    "/api/pos/purchase",
    // A real till would use its bill number here. This demo makes one up per sale.
    { ...posPayload(form), idempotency_key: `ui-${Date.now()}-${Math.random().toString(36).slice(2, 8)}` },
    { "x-api-key": form.api_key }
  );
  showMessage(
    posNotice,
    `Done. ${escapeHtml(result.customer.name)} paid ${result.receipt.display.net}, earned ` +
      `${result.receipt.points_earned} points, and now has ${result.points_balance} points ` +
      `(${rupees(result.points_balance_value_paise)}) with you.`,
    "success"
  );
  document.querySelector('#pos-form [name="points"]').value = 0;
  await refresh();
});

// --------------------------------------------------------------- till key

document.getElementById("rotate-key").addEventListener("click", async () => {
  const keyNotice = document.getElementById("key-notice");
  if (!confirm("Your current till key will stop working immediately. Continue?")) return;
  try {
    const result = await api("POST", "/api/vendor/api-key");
    sessionStorage.setItem("melacoin.apikey", result.api_key);
    document.querySelector('#pos-form [name="api_key"]').value = result.api_key;
    keyNotice.className = "notice key success";
    keyNotice.classList.remove("hidden");
    keyNotice.textContent = `${result.api_key} — ${result.note}`;
    await refresh();
  } catch (error) {
    showMessage(keyNotice, error.message);
  }
});

// ---------------------------------------------------------------- tables

function renderCustomers(customers) {
  const body = document.querySelector("#customers-table tbody");
  body.innerHTML = customers.length
    ? customers
        .map(
          (c) => `<tr>
            <td><strong>${escapeHtml(c.name)}</strong><br><small class="muted">${escapeHtml(c.email)}</small></td>
            <td class="num">${c.visits}</td>
            <td class="num">${c.points.toLocaleString("en-IN")}</td>
            <td class="num">${rupees(c.points_value_paise)}</td>
          </tr>`
        )
        .join("")
    : '<tr><td colspan="4" class="muted">No customers yet.</td></tr>';
}

function renderSettlement(settlement) {
  document.getElementById("settlement-explanation").textContent = settlement.explanation;
  const body = document.querySelector("#settlement-table tbody");
  const labels = {
    CONVERSION_DEBIT: "Customer converted points to MelaCoin",
    MELA_ACCEPTANCE_CREDIT: "You accepted MelaCoin",
    PAYMENT: "Payment settled",
    ADJUST: "Adjustment",
  };
  body.innerHTML = settlement.entries.length
    ? settlement.entries
        .map(
          (entry) => `<tr>
            <td>${when(entry.created_at)}</td>
            <td>${escapeHtml(labels[entry.kind] || entry.kind)}</td>
            <td class="num">${entry.display_amount}</td>
          </tr>`
        )
        .join("")
    : '<tr><td colspan="3" class="muted">Nothing outstanding.</td></tr>';
}

function renderSales(purchases) {
  const body = document.querySelector("#sales-table tbody");
  body.innerHTML = purchases.length
    ? purchases
        .map(
          (p) => `<tr>
            <td>${when(p.created_at)}</td>
            <td>${escapeHtml(p.customer_name)}</td>
            <td class="num">${p.display.gross}</td>
            <td class="num">${p.points_redeemed || "—"}</td>
            <td class="num">${p.display.net}</td>
            <td class="num">+${p.points_earned}</td>
          </tr>`
        )
        .join("")
    : '<tr><td colspan="6" class="muted">No sales yet. Try one above.</td></tr>';
}

refresh().catch((error) => showMessage(settingsNotice, error.message));

})();
