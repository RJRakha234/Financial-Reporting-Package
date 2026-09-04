"use strict";
/** End-to-end tests through the real HTTP server, exactly as a browser or till would call it. */
const test = require("node:test");
const assert = require("node:assert/strict");
const { freshDb } = require("./helpers");
const { createApp } = require("../src/index");
const money = require("../src/money");

let server, baseUrl;

test.before(async () => {
  freshDb();
  server = createApp();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  baseUrl = `http://127.0.0.1:${server.address().port}`;
});

test.after(() => server.close());

async function call(method, path, { body, token, apiKey } = {}) {
  const headers = { "content-type": "application/json" };
  if (token) headers.authorization = `Bearer ${token}`;
  if (apiKey) headers["x-api-key"] = apiKey;
  const response = await fetch(`${baseUrl}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: response.status, body: await response.json() };
}

// State shared across the ordered scenario below.
const state = {};

test("health check reports mock chain mode", async () => {
  const { status, body } = await call("GET", "/api/health");
  assert.equal(status, 200);
  assert.equal(body.ok, true);
  assert.equal(body.chain_mode, "mock");
});

test("a shop owner signs up and receives a POS key exactly once", async () => {
  const { status, body } = await call("POST", "/api/auth/register", {
    body: {
      name: "Ramesh Iyer", email: "Ramesh@Shop.test", password: "chai-and-samosa",
      role: "vendor", shop_name: "Chai Corner", city: "Bengaluru", category: "cafe",
    },
  });

  assert.equal(status, 201);
  assert.equal(body.user.email, "ramesh@shop.test", "email should be normalised to lowercase");
  assert.ok(body.api_key.startsWith("mela_sk_"));
  assert.equal(body.vendor.slug, "chai-corner");
  state.vendorToken = body.token;
  state.apiKey = body.api_key;
  state.vendorId = body.vendor.id;

  // The key is never returned again.
  const me = await call("GET", "/api/auth/me", { token: state.vendorToken });
  assert.equal(me.body.vendor.id, state.vendorId);
  assert.equal(me.body.api_key, undefined);
});

test("duplicate signups are refused", async () => {
  const { status, body } = await call("POST", "/api/auth/register", {
    body: { name: "Someone Else", email: "ramesh@shop.test", password: "another-password", role: "vendor", shop_name: "Copycat" },
  });
  assert.equal(status, 409);
  assert.match(body.error, /already exists/);
});

test("a customer signs up and starts with nothing", async () => {
  const { status, body } = await call("POST", "/api/auth/register", {
    body: { name: "Anita Sharma", email: "anita@example.test", password: "loyalty-points", phone: "9800000001" },
  });
  assert.equal(status, 201);
  state.customerToken = body.token;

  const summary = await call("GET", "/api/customer/summary", { token: state.customerToken });
  assert.equal(summary.body.total_points, 0);
  assert.equal(summary.body.mela_wei, "0");
});

test("the shop sets its own earn and redeem rates", async () => {
  const { status, body } = await call("PATCH", "/api/vendor/settings", {
    token: state.vendorToken,
    body: {
      earn_milli_points_per_rupee: 2000,   // 2 points per rupee
      redeem_milli_paise_per_point: 5000,  // 1 point = 5 paise
      min_redeem_points: 50,
      max_redeem_bps: 5000,
    },
  });
  assert.equal(status, 200);
  assert.equal(body.vendor.earn_rate_label, "2 point(s) per ₹1 spent");
  assert.equal(body.vendor.redeem_rate_label, "1 point = ₹0.05");
});

test("a rate outside the safe range is rejected", async () => {
  const { status, body } = await call("PATCH", "/api/vendor/settings", {
    token: state.vendorToken,
    body: { earn_milli_points_per_rupee: 999999999 },
  });
  assert.equal(status, 400);
  assert.match(body.error, /must be between/);
});

test("the till rings up a sale and the customer earns points", async () => {
  const quote = await call("POST", "/api/pos/quote", {
    apiKey: state.apiKey,
    body: { customer: "anita@example.test", gross_paise: 50000 },
  });
  assert.equal(quote.status, 200);
  assert.equal(quote.body.points_earned, 1000); // Rs 500 x 2 points
  assert.equal(quote.body.display.net, "₹500.00");

  const sale = await call("POST", "/api/pos/purchase", {
    apiKey: state.apiKey,
    body: { customer: "anita@example.test", gross_paise: 50000, idempotency_key: "INV-1", bill_ref: "INV-1" },
  });
  assert.equal(sale.status, 201);
  assert.equal(sale.body.points_balance, 1000);
  assert.equal(sale.body.points_balance_value_paise, 5000); // 1000 pts x 5 paise = Rs 50
});

test("a repeated bill number returns the original receipt", async () => {
  const repeat = await call("POST", "/api/pos/purchase", {
    apiKey: state.apiKey,
    body: { customer: "anita@example.test", gross_paise: 50000, idempotency_key: "INV-1" },
  });
  assert.equal(repeat.status, 200);
  assert.equal(repeat.body.replayed, true);
  assert.equal(repeat.body.points_balance, 1000, "balance must not double");
});

test("the till cannot work without a valid key", async () => {
  const missing = await call("POST", "/api/pos/quote", {
    body: { customer: "anita@example.test", gross_paise: 1000 },
  });
  assert.equal(missing.status, 401);

  const wrong = await call("POST", "/api/pos/quote", {
    apiKey: "mela_sk_totally-made-up-key",
    body: { customer: "anita@example.test", gross_paise: 1000 },
  });
  assert.equal(wrong.status, 401);
});

test("the customer redeems points on the next visit", async () => {
  const sale = await call("POST", "/api/pos/purchase", {
    apiKey: state.apiKey,
    body: { customer: "9800000001", gross_paise: 20000, points_to_redeem: 500, idempotency_key: "INV-2" },
  });
  assert.equal(sale.status, 201);
  assert.equal(sale.body.receipt.points_discount_paise, 2500); // 500 x 5 paise = Rs 25
  assert.equal(sale.body.receipt.net_paise, 17500);
  assert.equal(sale.body.receipt.points_earned, 350); // 2 x Rs 175 actually paid
  assert.equal(sale.body.points_balance, 1000 - 500 + 350);
});

test("redeeming below the shop's minimum is refused with a clear message", async () => {
  const { status, body } = await call("POST", "/api/pos/purchase", {
    apiKey: state.apiKey,
    body: { customer: "anita@example.test", gross_paise: 20000, points_to_redeem: 10, idempotency_key: "INV-3" },
  });
  assert.equal(status, 400);
  assert.match(body.error, /at least 50 points/);
});

test("the customer converts shop points into MelaCoin", async () => {
  const summary = await call("GET", "/api/customer/summary", { token: state.customerToken });
  const points = summary.body.vendors[0].points;
  assert.equal(points, 850);

  const quote = await call("POST", "/api/wallet/convert/quote", {
    token: state.customerToken,
    body: { vendor_id: state.vendorId, points },
  });
  assert.equal(quote.status, 200);
  assert.equal(quote.body.gross_paise, 4250); // 850 x 5 paise = Rs 42.50
  assert.equal(quote.body.fee_paise, 85);     // 2% platform spread
  assert.equal(quote.body.net_paise, 4165);

  const done = await call("POST", "/api/wallet/convert", {
    token: state.customerToken,
    body: { vendor_id: state.vendorId, points, expect_mela_wei: quote.body.mela_wei },
  });
  assert.equal(done.status, 201);

  const wallet = await call("GET", "/api/wallet", { token: state.customerToken });
  assert.equal(wallet.body.mela_wei, quote.body.mela_wei);
  assert.equal(wallet.body.mela_value_paise, 4165); // Rs 41.65 of MELA at Rs 1 each

  const after = await call("GET", "/api/customer/summary", { token: state.customerToken });
  assert.equal(after.body.total_points, 0, "converted points are gone");
});

test("a stale quote is refused rather than silently repriced", async () => {
  await call("POST", "/api/pos/purchase", {
    apiKey: state.apiKey,
    body: { customer: "anita@example.test", gross_paise: 100000, idempotency_key: "INV-4" },
  });
  const { status, body } = await call("POST", "/api/wallet/convert", {
    token: state.customerToken,
    body: { vendor_id: state.vendorId, points: 100, expect_mela_wei: "999999999999999999999" },
  });
  assert.equal(status, 400);
  assert.match(body.error, /price changed/);
});

test("the shop sees what it now owes the platform", async () => {
  const { body } = await call("GET", "/api/vendor/settlement", { token: state.vendorToken });
  assert.equal(body.balance_paise, 4250); // the full rupee value of the converted points
  assert.match(body.explanation, /payable to the platform/);
});

test("a customer cannot reach vendor or admin endpoints", async () => {
  const vendorArea = await call("GET", "/api/vendor/me", { token: state.customerToken });
  assert.equal(vendorArea.status, 403);

  const adminArea = await call("GET", "/api/admin/stats", { token: state.customerToken });
  assert.equal(adminArea.status, 403);

  const anonymous = await call("GET", "/api/customer/summary");
  assert.equal(anonymous.status, 401);
});

test("a withdrawal debits the app wallet and records the request", async () => {
  const before = await call("GET", "/api/wallet", { token: state.customerToken });
  const address = "0x1111111111111111111111111111111111111111";

  const { status, body } = await call("POST", "/api/wallet/withdraw", {
    token: state.customerToken,
    body: { to_address: address, mela_wei: before.body.mela_wei },
  });
  assert.equal(status, 201);
  assert.equal(body.mode, "mock");
  assert.equal(body.to, address);
  assert.match(body.instructions, /mock mode/);

  const after = await call("GET", "/api/wallet", { token: state.customerToken });
  assert.equal(after.body.mela_wei, "0");
  assert.equal(after.body.withdrawals.length, 1);
});

test("a bad wallet address is rejected", async () => {
  const { status, body } = await call("POST", "/api/wallet/withdraw", {
    token: state.customerToken,
    body: { to_address: "not-an-address", mela_wei: "1" },
  });
  assert.equal(status, 400);
  assert.match(body.error, /not in the expected format/);
});

test("logging out invalidates the token immediately", async () => {
  const login = await call("POST", "/api/auth/login", {
    body: { email: "anita@example.test", password: "loyalty-points" },
  });
  assert.equal(login.status, 200);

  await call("POST", "/api/auth/logout", { token: login.body.token });
  const after = await call("GET", "/api/customer/summary", { token: login.body.token });
  assert.equal(after.status, 401);
});

test("a wrong password never says whether the account exists", async () => {
  const wrongPassword = await call("POST", "/api/auth/login", {
    body: { email: "anita@example.test", password: "wrong-password" },
  });
  const noSuchUser = await call("POST", "/api/auth/login", {
    body: { email: "nobody@example.test", password: "wrong-password" },
  });
  assert.equal(wrongPassword.status, 401);
  assert.equal(noSuchUser.status, 401);
  assert.equal(wrongPassword.body.error, noSuchUser.body.error);
});

test("unknown endpoints and wrong methods give useful errors", async () => {
  const missing = await call("GET", "/api/does-not-exist");
  assert.equal(missing.status, 404);

  const wrongMethod = await call("GET", "/api/auth/login");
  assert.equal(wrongMethod.status, 405);
});

test("malformed JSON is rejected without crashing the server", async () => {
  const response = await fetch(`${baseUrl}/api/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{not json",
  });
  assert.equal(response.status, 400);
  const health = await call("GET", "/api/health");
  assert.equal(health.status, 200, "the server should still be alive");
});

test("the public token endpoint states the facts", async () => {
  const { body } = await call("GET", "/api/public/token");
  assert.equal(body.symbol, "MELA");
  assert.equal(body.decimals, 18);
  assert.equal(body.price_display, "₹1.00");
});

test("the vendor payload carries every setting the dashboard edits", async () => {
  // The dashboard fills its form from this object and posts the whole form back.
  // A field missing here silently resets to "off" the next time the shop saves,
  // so every editable setting must survive the round trip.
  const { EDITABLE } = require("../src/routes/vendor.routes");
  const { body } = await call("GET", "/api/vendor/me", { token: state.vendorToken });

  for (const field of Object.keys(EDITABLE)) {
    if (field === "name" || field === "category" || field === "city") continue;
    assert.ok(field in body.vendor, `/api/vendor/me must return "${field}"`);
  }

  // And a save that leaves a checkbox alone must not flip it.
  await call("PATCH", "/api/vendor/settings", {
    token: state.vendorToken,
    body: { earn_on_net: body.vendor.earn_on_net, allow_mela_conversion: body.vendor.allow_mela_conversion },
  });
  const after = await call("GET", "/api/vendor/me", { token: state.vendorToken });
  assert.equal(after.body.vendor.earn_on_net, body.vendor.earn_on_net);
  assert.equal(after.body.vendor.allow_mela_conversion, body.vendor.allow_mela_conversion);
});
