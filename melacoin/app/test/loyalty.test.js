"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { freshDb, makeUser, makeVendor, db } = require("./helpers");

const loyalty = require("../src/services/loyalty.service");
const conversion = require("../src/services/conversion.service");
const settlement = require("../src/services/settlement.service");
const tokenService = require("../src/services/token.service");
const settings = require("../src/services/settings.service");
const money = require("../src/money");

let vendor, customer, counter;
const key = () => `test-${++counter}`;

test.beforeEach(() => {
  freshDb();
  counter = 0;
  ({ vendor } = makeVendor());
  customer = makeUser();
  settings.setMelaPricePaise(100); // 1 MELA = Rs 1
});

// ------------------------------------------------------------------ earning

test("a Rs 100 bill at 2.5 points per rupee earns 250 points", () => {
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 10000, idempotencyKey: key() });
  assert.equal(loyalty.pointsBalance(customer.id, vendor.id), 250);
});

test("each vendor's own rate is applied, not a global one", () => {
  const { vendor: stingy } = makeVendor({ earn_milli_points_per_rupee: 100 }); // 0.1 pts/rupee
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 10000, idempotencyKey: key() });
  loyalty.recordPurchase({ vendor: stingy, customerId: customer.id, grossPaise: 10000, idempotencyKey: key() });

  assert.equal(loyalty.pointsBalance(customer.id, vendor.id), 250);
  assert.equal(loyalty.pointsBalance(customer.id, stingy.id), 10);
  // Points at one shop are invisible at the other. That is the whole point of points.
  assert.equal(loyalty.balancesByVendor(customer.id).length, 2);
});

test("the same idempotency key never charges or credits twice", () => {
  const first = loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 10000, idempotencyKey: "bill-1" });
  const second = loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 10000, idempotencyKey: "bill-1" });

  assert.equal(second.replayed, true);
  assert.equal(second.purchase.id, first.purchase.id);
  assert.equal(loyalty.pointsBalance(customer.id, vendor.id), 250);
  assert.equal(db.get().prepare("SELECT COUNT(*) AS n FROM purchases").get().n, 1);
});

// ---------------------------------------------------------------- redeeming

test("redeeming points reduces the bill at the vendor's own rate", () => {
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 100000, idempotencyKey: key() });
  assert.equal(loyalty.pointsBalance(customer.id, vendor.id), 2500);

  const { purchase } = loyalty.recordPurchase({
    vendor, customerId: customer.id, grossPaise: 20000, pointsToRedeem: 1000, idempotencyKey: key(),
  });

  assert.equal(purchase.points_discount_paise, 10000); // 1000 pts x 10 paise = Rs 100
  assert.equal(purchase.net_paise, 10000);             // Rs 200 bill - Rs 100
  assert.equal(purchase.points_earned, 250);           // earned on the Rs 100 actually paid
  assert.equal(loyalty.pointsBalance(customer.id, vendor.id), 2500 - 1000 + 250);
});

test("the vendor's percentage cap is enforced", () => {
  const { vendor: capped } = makeVendor({ max_redeem_bps: 1000 }); // points may pay only 10%
  loyalty.recordPurchase({ vendor: capped, customerId: customer.id, grossPaise: 1000000, idempotencyKey: key() });

  const quote = loyalty.quotePurchase({ vendor: capped, customerId: customer.id, grossPaise: 100000 });
  assert.equal(quote.max_redeemable_paise, 10000);  // 10% of Rs 1000
  assert.equal(quote.max_redeemable_points, 1000);  // Rs 100 of discount at 10 paise a point

  assert.throws(
    () => loyalty.recordPurchase({
      vendor: capped, customerId: customer.id, grossPaise: 100000, pointsToRedeem: 1001, idempotencyKey: key(),
    }),
    /at most/
  );

  // One point under the cap is fine, and lands exactly on the ceiling.
  const { purchase } = loyalty.recordPurchase({
    vendor: capped, customerId: customer.id, grossPaise: 100000, pointsToRedeem: 1000, idempotencyKey: key(),
  });
  assert.equal(purchase.points_discount_paise, 10000);
});

test("a minimum redemption threshold is enforced", () => {
  const { vendor: strict } = makeVendor({ min_redeem_points: 500 });
  loyalty.recordPurchase({ vendor: strict, customerId: customer.id, grossPaise: 100000, idempotencyKey: key() });

  assert.throws(
    () => loyalty.recordPurchase({
      vendor: strict, customerId: customer.id, grossPaise: 50000, pointsToRedeem: 100, idempotencyKey: key(),
    }),
    /at least 500 points/
  );
});

test("you cannot spend points you do not have", () => {
  assert.throws(
    () => loyalty.recordPurchase({
      vendor, customerId: customer.id, grossPaise: 10000, pointsToRedeem: 5000, idempotencyKey: key(),
    }),
    /Only 0 point/
  );
  assert.equal(db.get().prepare("SELECT COUNT(*) AS n FROM purchases").get().n, 0);
});

test("earn_on_net = 0 earns on the full bill instead of the cash paid", () => {
  const { vendor: gross } = makeVendor({ earn_on_net: 0 });
  loyalty.recordPurchase({ vendor: gross, customerId: customer.id, grossPaise: 100000, idempotencyKey: key() });

  const { purchase } = loyalty.recordPurchase({
    vendor: gross, customerId: customer.id, grossPaise: 20000, pointsToRedeem: 1000, idempotencyKey: key(),
  });
  assert.equal(purchase.points_earned, 500); // 2.5 x Rs 200, not x Rs 100
});

// ------------------------------------------------------------------ expiry

test("points expire and the soonest-to-expire lot is spent first", () => {
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 10000, idempotencyKey: key() });
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 10000, idempotencyKey: key() });

  const lots = loyalty.activeLots(customer.id, vendor.id);
  assert.equal(lots.length, 2);

  // Make the second lot expire sooner than the first.
  const soon = new Date(Date.now() + 60_000).toISOString();
  db.get().prepare("UPDATE point_lots SET expires_at = ? WHERE id = ?").run(soon, lots[1].id);

  loyalty.recordPurchase({
    vendor, customerId: customer.id, grossPaise: 20000, pointsToRedeem: 100, idempotencyKey: key(),
  });

  const after = db.get().prepare("SELECT * FROM point_lots WHERE id = ?").get(lots[1].id);
  assert.equal(after.points_remaining, 150, "the lot expiring soonest should be drained first");
});

test("expired points vanish from the balance and leave an audit trail", () => {
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 10000, idempotencyKey: key() });
  const yesterday = new Date(Date.now() - 86_400_000).toISOString();
  db.get().prepare("UPDATE point_lots SET expires_at = ? WHERE customer_id = ?").run(yesterday, customer.id);

  assert.equal(loyalty.pointsBalance(customer.id, vendor.id), 0);
  const expiryEvents = db.get().prepare("SELECT * FROM point_events WHERE kind = 'EXPIRE'").all();
  assert.equal(expiryEvents.length, 1);
  assert.equal(expiryEvents[0].points_delta, -250);
});

test("points that never expire are kept forever", () => {
  const { vendor: forever } = makeVendor({ points_expiry_days: 0 });
  loyalty.recordPurchase({ vendor: forever, customerId: customer.id, grossPaise: 10000, idempotencyKey: key() });
  const lot = db.get().prepare("SELECT * FROM point_lots WHERE vendor_id = ?").get(forever.id);
  assert.equal(lot.expires_at, null);
  assert.equal(loyalty.pointsBalance(customer.id, forever.id), 250);
});

// -------------------------------------------------------------- conversion

test("converting points to MELA burns points, credits tokens and bills the shop", () => {
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 400000, idempotencyKey: key() });
  const points = loyalty.pointsBalance(customer.id, vendor.id);
  assert.equal(points, 10000); // Rs 4000 x 2.5

  const result = conversion.convert({ vendor, customerId: customer.id, points });

  // Rs 1000 of points, 2% fee -> Rs 980 -> 980 MELA at Rs 1 each
  assert.equal(result.gross_paise, 100000);
  assert.equal(result.fee_paise, 2000);
  assert.equal(result.net_paise, 98000);
  assert.equal(result.mela_wei, (980n * money.WEI_PER_MELA).toString());

  assert.equal(loyalty.pointsBalance(customer.id, vendor.id), 0, "points must be destroyed");
  assert.equal(tokenService.balanceWei(customer.id), 980n * money.WEI_PER_MELA);
  // The shop owes the platform the full rupee value of the points it no longer owes the customer.
  assert.equal(settlement.balancePaise(vendor.id), 100000);
});

test("a shop can switch conversion off", () => {
  const { vendor: closed } = makeVendor({ allow_mela_conversion: 0 });
  loyalty.recordPurchase({ vendor: closed, customerId: customer.id, grossPaise: 400000, idempotencyKey: key() });
  assert.throws(
    () => conversion.convert({ vendor: closed, customerId: customer.id, points: 1000 }),
    /has not enabled MelaCoin conversion/
  );
});

test("conversion is all-or-nothing", () => {
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 400000, idempotencyKey: key() });
  assert.throws(
    () => conversion.convert({ vendor, customerId: customer.id, points: 999999 }),
    /Only 10000 point/
  );
  // Nothing partial was written.
  assert.equal(loyalty.pointsBalance(customer.id, vendor.id), 10000);
  assert.equal(tokenService.balanceWei(customer.id), 0n);
  assert.equal(settlement.balancePaise(vendor.id), 0);
  assert.equal(db.get().prepare("SELECT COUNT(*) AS n FROM conversions").get().n, 0);
});

test("a higher MELA price yields fewer tokens for the same points", () => {
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 400000, idempotencyKey: key() });
  settings.setMelaPricePaise(400); // 1 MELA now costs Rs 4
  const result = conversion.convert({ vendor, customerId: customer.id, points: 10000 });
  assert.equal(result.mela_wei, (245n * money.WEI_PER_MELA).toString()); // Rs 980 / Rs 4
});

// ------------------------------------------------------------ paying in MELA

test("a customer can pay a bill with MELA and the platform then owes that shop", () => {
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 400000, idempotencyKey: key() });
  conversion.convert({ vendor, customerId: customer.id, points: 10000 });

  const { vendor: otherShop } = makeVendor({ name: "Somewhere Else" });
  const melaToSpend = 100n * money.WEI_PER_MELA; // 100 MELA = Rs 100

  const { purchase } = loyalty.recordPurchase({
    vendor: otherShop, customerId: customer.id, grossPaise: 50000,
    melaWeiToSpend: melaToSpend, idempotencyKey: key(),
  });

  assert.equal(purchase.mela_discount_paise, 10000);
  assert.equal(purchase.net_paise, 40000);
  assert.equal(tokenService.balanceWei(customer.id), 880n * money.WEI_PER_MELA);
  // Negative balance = the platform owes this shop rupees for the tokens it accepted.
  assert.equal(settlement.balancePaise(otherShop.id), -10000);
});

test("a customer cannot spend MELA they do not have", () => {
  assert.throws(
    () => loyalty.recordPurchase({
      vendor, customerId: customer.id, grossPaise: 50000,
      melaWeiToSpend: 5n * money.WEI_PER_MELA, idempotencyKey: key(),
    }),
    /Not enough MELA/
  );
});

test("a shop that does not accept MELA refuses it", () => {
  const { vendor: cashOnly } = makeVendor({ accepts_mela: 0 });
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 400000, idempotencyKey: key() });
  conversion.convert({ vendor, customerId: customer.id, points: 10000 });

  assert.throws(
    () => loyalty.recordPurchase({
      vendor: cashOnly, customerId: customer.id, grossPaise: 50000,
      melaWeiToSpend: money.WEI_PER_MELA, idempotencyKey: key(),
    }),
    /does not accept MELA/
  );
});

test("MELA cannot pay more than the bill", () => {
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 400000, idempotencyKey: key() });
  conversion.convert({ vendor, customerId: customer.id, points: 10000 });

  assert.throws(
    () => loyalty.recordPurchase({
      vendor, customerId: customer.id, grossPaise: 5000,
      melaWeiToSpend: 500n * money.WEI_PER_MELA, idempotencyKey: key(),
    }),
    /more MELA than this bill needs/
  );
});

// -------------------------------------------------------------- accounting

test("the platform never issues MELA it has not collected rupees for", () => {
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 400000, idempotencyKey: key() });
  conversion.convert({ vendor, customerId: customer.id, points: 10000 });

  const stats = settlement.platformStats(settings.melaPricePaise());
  assert.equal(stats.mela_outstanding_value_paise, 98000); // Rs 980 of MELA in customer hands
  assert.equal(stats.treasury_reserve_paise, 98000);       // Rs 980 collectable from the shop
  assert.equal(stats.backing_ratio_bps, 10000);            // exactly 100% backed
  assert.equal(stats.platform_fee_paise, 2000);            // the platform's Rs 20 revenue
});

test("settling a shop's bill clears its balance", () => {
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 400000, idempotencyKey: key() });
  conversion.convert({ vendor, customerId: customer.id, points: 10000 });
  assert.equal(settlement.balancePaise(vendor.id), 100000);

  settlement.recordPayment({ vendorId: vendor.id, amountPaise: 100000, note: "UPI ref 123" });
  assert.equal(settlement.balancePaise(vendor.id), 0);
});

test("the MELA ledger can never go negative", () => {
  assert.throws(
    () => tokenService.post({ customerId: customer.id, kind: "SPEND", weiDelta: -1n }),
    /Not enough MELA/
  );
});
