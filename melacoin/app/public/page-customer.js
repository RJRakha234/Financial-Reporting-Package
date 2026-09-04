"use strict";
(function () {
const M = window.Mela;
const { api, rupees, mela, melaToWei, when, escapeHtml, showMessage, clearMessage, onSubmit } = M;

M.mountHeader("customer");

const dialog = document.getElementById("convert-dialog");
const convertNotice = document.getElementById("convert-notice");
let activeVendor = null;
let latestQuote = null;

async function refresh() {
  const [summary, wallet, history] = await Promise.all([
    api("GET", "/api/customer/summary"),
    api("GET", "/api/wallet"),
    api("GET", "/api/customer/history"),
  ]);
  renderHeadline(summary);
  renderPoints(summary);
  renderWallet(wallet);
  renderHistory(history);
  return summary;
}

function renderHeadline(summary) {
  document.getElementById("headline").innerHTML = `
    <div class="stat">
      <div class="label">Points value</div>
      <div class="value">${summary.display.points_value}</div>
      <div class="note">${summary.total_points.toLocaleString("en-IN")} points across ${summary.vendors.length} shop(s)</div>
    </div>
    <div class="stat">
      <div class="label">MelaCoin</div>
      <div class="value">${summary.display.mela} MELA</div>
      <div class="note">worth ${summary.display.mela_value} at ${summary.display.mela_price} each</div>
    </div>
    <div class="stat">
      <div class="label">Everything together</div>
      <div class="value">${rupees(summary.total_points_value_paise + summary.mela_value_paise)}</div>
      <div class="note">points plus tokens</div>
    </div>`;
}

function renderPoints(summary) {
  const body = document.querySelector("#points-table tbody");
  if (!summary.vendors.length) {
    body.innerHTML = '<tr><td colspan="5" class="muted">No points yet. Shop at a listed store and they appear here.</td></tr>';
    return;
  }
  body.innerHTML = summary.vendors
    .map((entry) => {
      const canConvert = entry.vendor.allow_mela_conversion && entry.points >= entry.vendor.min_redeem_points;
      const reason = !entry.vendor.allow_mela_conversion
        ? "This shop has not enabled conversion"
        : `Needs ${entry.vendor.min_redeem_points} points`;
      return `<tr>
        <td><strong>${escapeHtml(entry.vendor.name)}</strong><br><small class="muted">${escapeHtml(entry.vendor.redeem_rate_label)}</small></td>
        <td class="num">${entry.points.toLocaleString("en-IN")}</td>
        <td class="num">${rupees(entry.value_paise)}</td>
        <td>${entry.next_expiry ? when(entry.next_expiry) : '<span class="muted">never</span>'}</td>
        <td class="num">${
          canConvert
            ? `<button class="small" data-convert="${entry.vendor.id}">Convert to MELA</button>`
            : `<span class="muted" title="${escapeHtml(reason)}">—</span>`
        }</td>
      </tr>`;
    })
    .join("");

  body.querySelectorAll("[data-convert]").forEach((button) => {
    button.addEventListener("click", () => {
      const entry = summary.vendors.find((v) => v.vendor.id === button.dataset.convert);
      openConvert(entry);
    });
  });

  document.getElementById("points-note").textContent =
    summary.vendors.length > 1 ? "Balances are kept separately per shop" : "";
}

function renderWallet(wallet) {
  const box = document.getElementById("wallet-box");
  const pending = wallet.withdrawals.filter((w) => w.status === "signed");
  box.innerHTML = `
    <div class="stat">
      <div class="label">Balance in the app</div>
      <div class="value">${wallet.display.mela} MELA</div>
      <div class="note">${wallet.display.value} · 1 MELA = ${wallet.display.price}</div>
    </div>
    ${
      wallet.chain_mode === "mock"
        ? '<p class="muted" style="margin-top:.7rem">The blockchain is switched off in this setup, so withdrawals are recorded but no on-chain transfer happens yet.</p>'
        : ""
    }
    ${pending.length ? `<p class="muted" style="margin-top:.7rem">${pending.length} withdrawal(s) waiting to be claimed on-chain.</p>` : ""}`;

  const addressField = document.querySelector('[name="to_address"]');
  if (wallet.wallet_address && !addressField.value) addressField.value = wallet.wallet_address;
}

function renderHistory(history) {
  const rows = [];
  for (const event of history.point_events) {
    const label = {
      EARN: "Earned points", REDEEM: "Used points on a bill",
      EXPIRE: "Points expired", CONVERT: "Converted to MelaCoin", ADJUST: "Adjustment",
    }[event.kind] || event.kind;
    rows.push({
      at: event.created_at, shop: event.vendor_name, what: label, points: event.points_delta,
    });
  }
  const body = document.querySelector("#history-table tbody");
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="4" class="muted">Nothing yet.</td></tr>';
    return;
  }
  body.innerHTML = rows
    .slice(0, 25)
    .map(
      (row) => `<tr>
        <td>${when(row.at)}</td>
        <td>${escapeHtml(row.shop)}</td>
        <td>${escapeHtml(row.what)}</td>
        <td class="num" style="color:${row.points >= 0 ? "var(--accent)" : "var(--muted)"}">
          ${row.points > 0 ? "+" : ""}${row.points.toLocaleString("en-IN")}
        </td>
      </tr>`
    )
    .join("");
}

// ---------------------------------------------------------------- convert

function openConvert(entry) {
  activeVendor = entry;
  latestQuote = null;
  clearMessage(convertNotice);
  document.getElementById("convert-shop").textContent =
    `${entry.vendor.name} · you have ${entry.points} points worth ${rupees(entry.value_paise)} here`;
  const input = document.querySelector('#convert-form [name="points"]');
  input.max = entry.points;
  input.value = entry.points;
  document.getElementById("convert-submit").disabled = true;
  dialog.showModal();
  previewConversion();
}

document.getElementById("convert-cancel").addEventListener("click", () => dialog.close());
document.querySelector('#convert-form [name="points"]').addEventListener("input", debounce(previewConversion, 250));

async function previewConversion() {
  const points = Number(document.querySelector('#convert-form [name="points"]').value);
  const preview = document.getElementById("convert-preview");
  const submit = document.getElementById("convert-submit");
  if (!activeVendor || !Number.isInteger(points) || points <= 0) {
    preview.textContent = "Enter an amount to see what you would get.";
    submit.disabled = true;
    return;
  }
  try {
    const quote = await api("POST", "/api/wallet/convert/quote", {
      vendor_id: activeVendor.vendor.id,
      points,
    });
    latestQuote = quote;
    preview.className = "notice";
    preview.innerHTML =
      `${points} points are worth <strong>${quote.display.gross}</strong> here.<br>` +
      `Platform fee (${quote.fee_bps / 100}%): −${quote.display.fee}<br>` +
      `You receive <strong>${quote.display.mela} MELA</strong> (${quote.display.net} at ${quote.display.price} per MELA).`;
    submit.disabled = false;
  } catch (error) {
    latestQuote = null;
    preview.className = "notice error";
    preview.textContent = error.message;
    submit.disabled = true;
  }
}

document.getElementById("convert-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!latestQuote) return;
  const submit = document.getElementById("convert-submit");
  submit.disabled = true;
  try {
    // Send back the exact amount we were quoted; the server refuses if the price moved.
    await api("POST", "/api/wallet/convert", {
      vendor_id: activeVendor.vendor.id,
      points: latestQuote.points,
      expect_mela_wei: latestQuote.mela_wei,
    });
    dialog.close();
    await refresh();
  } catch (error) {
    showMessage(convertNotice, error.message);
  } finally {
    submit.disabled = false;
  }
});

// --------------------------------------------------------------- withdraw

const walletNotice = document.getElementById("wallet-notice");
onSubmit(document.getElementById("withdraw-form"), walletNotice, async (form) => {
  const payload = {};
  if (form.to_address) payload.to_address = form.to_address;
  if (form.amount && form.amount.trim()) payload.mela_wei = melaToWei(form.amount).toString();

  const result = await api("POST", "/api/wallet/withdraw", payload);
  showMessage(
    walletNotice,
    `Withdrawal of ${mela(result.wei)} MELA recorded for ${result.to}. ${result.instructions}`,
    "success"
  );
  await refresh();
});

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

refresh().catch((error) => showMessage(document.getElementById("wallet-notice"), error.message));

})();
