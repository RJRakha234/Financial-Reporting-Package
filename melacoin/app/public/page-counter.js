"use strict";
(function () {
const M = window.Mela;
const { api, rupees, when, escapeHtml, showMessage, clearMessage, rupeesToPaise } = M;

/**
 * The counter screen. One job at a time, one big button per screen, and never
 * more than four taps from "who is this" to "done". It is used with a thumb, by
 * someone who is also making chai.
 */

const KEY_STORE = "melacoin.counter.key";
let apiKey = null;
let vendor = null;      // the shop, learned from the first successful call
let customer = null;    // who is at the counter right now
let quote = null;       // the priced bill, refreshed as they type
let usePoints = false;
let lastBill = null;

const el = (id) => document.getElementById(id);
const screen = () => el("screen");
const notice = () => el("notice");

const withKey = (extra = {}) => ({ "x-api-key": apiKey, ...extra });

// ------------------------------------------------------------------ till key

function askForKey(message) {
  screen().innerHTML = `
    <div class="step">
      <h2>Connect this counter</h2>
      <p class="hint">Paste the till key from your dashboard. You only do this once on this phone.</p>
      <input class="big" id="key-input" placeholder="mela_sk_…" autocomplete="off" spellcheck="false"
             style="font-size:1rem;text-align:left">
      <button class="big" type="button" id="key-save">Connect</button>
    </div>`;
  if (message) showMessage(notice(), message);
  el("key-save").addEventListener("click", async () => {
    const value = el("key-input").value.trim();
    if (!value) return;
    apiKey = value;
    try {
      localStorage.setItem(KEY_STORE, value);
    } catch { /* private mode still works for this session */ }
    clearMessage(notice());
    await start();
  });
}

el("change-key").addEventListener("click", () => {
  try { localStorage.removeItem(KEY_STORE); } catch { /* ignore */ }
  apiKey = null;
  askForKey();
});

// -------------------------------------------------------------- screen 1: who

function screenPhone() {
  customer = null; quote = null; usePoints = false;
  clearMessage(notice());
  screen().innerHTML = `
    <div class="step">
      <h2>Customer's phone number</h2>
      <p class="hint">Ten digits. Ask for it while you make the chai.</p>
      <!-- Generous length: people type "+91 98000 00001" with spaces, and silently
           truncating that produces an invalid number and a baffling error. -->
      <input class="big" id="phone" inputmode="tel" autocomplete="off"
             maxlength="20" placeholder="9800000001">
      <button class="big" type="button" id="find">Find</button>
    </div>`;

  const phone = el("phone");
  phone.focus();
  el("find").addEventListener("click", lookup);
  phone.addEventListener("keydown", (e) => { if (e.key === "Enter") lookup(); });
  renderRecent();
}

async function lookup() {
  const typed = el("phone").value.trim();
  if (!typed) return;
  clearMessage(notice());
  try {
    const found = await api("GET", `/api/pos/customer?customer=${encodeURIComponent(typed)}`, undefined, withKey());
    customer = { ...found.customer, points: found.points, points_value_paise: found.points_value_paise };
    screenCustomer();
  } catch (error) {
    // Not found is the normal case for a new regular, not an error.
    if (/No customer found/i.test(error.message)) screenNew(typed);
    else showMessage(notice(), error.message);
  }
}

// ---------------------------------------------------------- screen 1b: new

function screenNew(typed) {
  screen().innerHTML = `
    <div class="step">
      <h2>New customer</h2>
      <p class="hint">${escapeHtml(typed)} is not signed up yet. Add them — it takes one tap.</p>
      <input class="big" id="new-name" placeholder="Name (optional)" style="font-size:1.05rem">
      <button class="big" type="button" id="add">Add ${escapeHtml(typed)}</button>
      <button class="big secondary" type="button" id="back">Back</button>
    </div>`;

  el("back").addEventListener("click", screenPhone);
  el("add").addEventListener("click", async () => {
    try {
      const result = await api("POST", "/api/pos/enroll",
        { phone: typed, name: el("new-name").value.trim() || undefined }, withKey());
      customer = { ...result.customer, points: result.points, points_value_paise: result.points_value_paise };
      showMessage(notice(), result.message, "success");
      screenCustomer();
    } catch (error) {
      showMessage(notice(), error.message);
    }
  });
}

// ------------------------------------------------------- screen 2: the bill

function screenCustomer() {
  const minimum = vendor ? vendor.min_redeem_points : 0;
  const canRedeem = customer.points >= minimum && customer.points_value_paise > 0;
  const toGo = Math.max(0, minimum - customer.points);
  const pct = minimum > 0 ? Math.min(100, (customer.points / minimum) * 100) : 100;

  screen().innerHTML = `
    <div class="step">
      <div class="who">
        <span class="nm">${escapeHtml(customer.name)}</span>
        <span class="ph">${escapeHtml(customer.phone || "")}</span>
      </div>
      <div class="pts">
        <span class="n">${customer.points.toLocaleString("en-IN")}</span>
        <span class="u">points · worth ${rupees(customer.points_value_paise)} here</span>
      </div>

      ${canRedeem ? `
        <div class="ready">
          <div class="big-txt">FREE CHAI READY</div>
          <div class="sub-txt">${rupees(customer.points_value_paise)} of points to spend</div>
        </div>` : `
        <div class="progress">
          <div class="track"><div class="fill" style="width:${pct}%"></div></div>
          <div class="cap">${toGo.toLocaleString("en-IN")} more points until a free one</div>
        </div>`}
    </div>

    <div class="step">
      <h2>Bill amount</h2>
      <p class="hint">What they are paying, in rupees.</p>
      <input class="big" id="amount" inputmode="decimal" placeholder="20">
      <div id="quote-box"></div>
      <button class="big" type="button" id="charge" disabled>Enter an amount</button>
      <button class="big secondary" type="button" id="cancel">Different customer</button>
    </div>`;

  el("cancel").addEventListener("click", screenPhone);
  el("amount").focus();
  el("amount").addEventListener("input", debounce(refreshQuote, 260));
  el("amount").addEventListener("keydown", (e) => { if (e.key === "Enter") charge(); });
  el("charge").addEventListener("click", charge);
}

async function refreshQuote() {
  const rupeeValue = Number(el("amount").value);
  const box = el("quote-box");
  const button = el("charge");

  if (!rupeeValue || rupeeValue <= 0) {
    quote = null; box.innerHTML = "";
    button.disabled = true; button.textContent = "Enter an amount";
    return;
  }

  try {
    quote = await api("POST", "/api/pos/quote", {
      customer: customer.phone || customer.id,
      gross_paise: rupeesToPaise(rupeeValue),
      points_to_redeem: 0,
    }, withKey());
  } catch (error) {
    quote = null; box.innerHTML = "";
    button.disabled = true; button.textContent = "Enter an amount";
    showMessage(notice(), error.message);
    return;
  }

  clearMessage(notice());
  const maxPoints = quote.max_redeemable_points;
  const discount = maxPoints > 0 ? Math.floor(maxPoints * vendor.redeem_milli_paise_per_point / 1000) : 0;

  box.innerHTML = maxPoints >= vendor.min_redeem_points && discount > 0
    ? `<label class="toggle">
         <input type="checkbox" id="use-points" ${usePoints ? "checked" : ""}>
         <span class="t-txt">Use ${maxPoints.toLocaleString("en-IN")} points
           <small>Takes ${rupees(discount)} off this bill</small></span>
       </label>`
    : "";

  const box2 = el("use-points");
  if (box2) {
    box2.addEventListener("change", () => { usePoints = box2.checked; updateButton(); });
  } else {
    usePoints = false;
  }
  updateButton();
}

function updateButton() {
  const button = el("charge");
  if (!quote) { button.disabled = true; return; }
  const maxPoints = quote.max_redeemable_points;
  const discount = usePoints ? Math.floor(maxPoints * vendor.redeem_milli_paise_per_point / 1000) : 0;
  const toPay = quote.gross_paise - discount;
  button.disabled = false;
  button.textContent = `Take ${rupees(toPay)}`;
}

async function charge() {
  const button = el("charge");
  if (!quote || button.disabled) return;
  button.disabled = true;

  try {
    const result = await api("POST", "/api/pos/purchase", {
      customer: customer.phone || customer.id,
      gross_paise: quote.gross_paise,
      points_to_redeem: usePoints ? quote.max_redeemable_points : 0,
      // The counter, not the shopkeeper, invents the reference. Re-tapping the
      // button on a bad signal must never ring the sale up twice.
      idempotency_key: `ctr-${customer.id}-${quote.gross_paise}-${Date.now()}`,
    }, withKey());
    lastBill = result;
    screenDone(result);
  } catch (error) {
    button.disabled = false;
    showMessage(notice(), error.message);
  }
}

// ------------------------------------------------------- screen 3: finished

function screenDone(result) {
  const receipt = result.receipt;
  const minimum = vendor.min_redeem_points;
  const toGo = Math.max(0, minimum - result.points_balance);

  screen().innerHTML = `
    <div class="step" style="text-align:center">
      <div class="done-mark">✓</div>
      <div style="font-size:1.5rem;font-weight:700">${receipt.display.net}</div>
      <p class="hint" style="margin-top:.2rem">taken from ${escapeHtml(result.customer.name)}</p>
    </div>

    <div class="step">
      <div class="receipt-line"><span>Bill</span><span class="v">${receipt.display.gross}</span></div>
      ${receipt.points_redeemed > 0
        ? `<div class="receipt-line"><span>Points used</span><span class="v">−${receipt.display.points_discount}</span></div>` : ""}
      <div class="receipt-line"><span>Points earned</span><span class="v">+${receipt.points_earned}</span></div>
      <div class="receipt-line"><span>Their balance</span><span class="v">${result.points_balance.toLocaleString("en-IN")}</span></div>
      <p class="hint" style="margin-top:.7rem;margin-bottom:0">
        ${toGo > 0
          ? `${toGo.toLocaleString("en-IN")} more points until their next free one.`
          : `They have enough for a free one next time — tell them.`}
      </p>
      <button class="big" type="button" id="next">Next customer</button>
      <button class="big secondary" type="button" id="undo">Undo this bill</button>
    </div>`;

  el("next").addEventListener("click", () => { screenPhone(); });
  el("undo").addEventListener("click", () => undo(receipt.id, el("undo")));
  renderRecent();
}

// ------------------------------------------------------------------- undoing

async function undo(purchaseId, button) {
  if (!confirm("Cancel this bill? The points go back to how they were.")) return;
  if (button) button.disabled = true;
  try {
    const result = await api("POST", "/api/pos/void", { purchase_id: purchaseId }, withKey());
    showMessage(notice(), result.message, "success");
    screenPhone();
  } catch (error) {
    if (button) button.disabled = false;
    showMessage(notice(), error.message);
  }
}

/** The handful of bills that could still be cancelled, so a typo is always reachable. */
async function renderRecent() {
  const host = el("recent");
  try {
    const { bills } = await api("GET", "/api/pos/voidable", undefined, withKey());
    if (!bills.length) { host.innerHTML = ""; return; }
    host.innerHTML = `<h3>Recent — can still be cancelled</h3>` + bills.slice(0, 4).map((bill) => `
      <div class="bill">
        <div class="b-who">${escapeHtml(bill.customer_name)}<small>${when(bill.created_at)}</small></div>
        <div class="b-amt">${bill.display.net}</div>
        <button class="secondary" type="button" data-undo="${escapeHtml(bill.id)}">Undo</button>
      </div>`).join("");

    host.querySelectorAll("[data-undo]").forEach((button) => {
      button.addEventListener("click", () => undo(button.dataset.undo, button));
    });
  } catch {
    host.innerHTML = "";
  }
}

// --------------------------------------------------------------------- boot

async function start() {
  try {
    // One call proves the key and tells the counter which shop it is sitting on.
    const me = await api("GET", "/api/pos/me", undefined, withKey());
    vendor = me.vendor;
    el("shop-name").innerHTML =
      `${escapeHtml(vendor.name)}<small>${escapeHtml(vendor.earn_rate_label)}</small>`;
    screenPhone();
  } catch (error) {
    apiKey = null;
    askForKey(
      /Invalid or missing/i.test(error.message)
        ? "That key was not recognised. Copy it again from your dashboard."
        : error.message
    );
  }
}

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

try { apiKey = localStorage.getItem(KEY_STORE); } catch { apiKey = null; }
if (apiKey) start(); else askForKey();
})();
