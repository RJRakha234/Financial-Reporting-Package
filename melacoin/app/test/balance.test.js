"use strict";
/**
 * Tests for the net-outflow cap - the rule that stops one shop quietly funding
 * footfall for every other shop on the street.
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { freshDb, makeUser, makeVendor, db } = require("./helpers");

const loyalty = require("../src/services/loyalty.service");
const conversion = require("../src/services/conversion.service");
const settlement = require("../src/services/settlement.service");
const balance = require("../src/services/balance.service");
const settings = require("../src/services/settings.service");
const money = require("../src/money");

let customer, counter;
const key = () => `bal-${++counter}`;

/**
 * A shop issuing points FASTER than its tolerance allows to leave: a 2% loyalty
 * cost against a 1% outflow tolerance. That gap is the only thing that ever makes
 * the cap bite, and it is exactly the real-world danger case - a shop being more
 * generous than the value it is willing to let walk out of the door.
 *
 * Set the tolerance at or above your loyalty cost and conversion never blocks;
 * set it below and you are deliberately rationing. There is a test for both.
 */
const capped = (extra = {}) =>
  makeVendor({
    earn_milli_points_per_rupee: 2000,   // 2 points per rupee
    redeem_milli_paise_per_point: 1000,  // 1 point = 1 paisa  -> 2% loyalty cost
    min_redeem_points: 0,
    net_outflow_tolerance_bps: 100,      // may leak 1% of its own sales
    net_outflow_floor_paise: 0,          // no floor, so the maths is pure
    conversion_budget_paise: 0,
    ...extra,
  }).vendor;

test.beforeEach(() => {
  freshDb();
  counter = 0;
  customer = makeUser();
  settings.setMelaPricePaise(100);
});

// ------------------------------------------------------------------ the cap

test("the cap is a share of the shop's own sales", () => {
  const shop = capped();
  // Rs 10,000 of sales -> 1% -> Rs 100 may leave as MelaCoin
  loyalty.recordPurchase({ vendor: shop, customerId: customer.id, grossPaise: 1000000, idempotencyKey: key() });

  const position = balance.netPosition(shop.id);
  assert.equal(position.own_sales_paise, 1000000);
  assert.equal(position.cap_paise, 10000);
  assert.equal(position.headroom_paise, 10000);
  assert.equal(position.status, "healthy");
});

test("a shop that only ever pays out is stopped at its limit", () => {
  const shop = capped();
  loyalty.recordPurchase({ vendor: shop, customerId: customer.id, grossPaise: 1000000, idempotencyKey: key() });
  // Rs 10,000 of sales issued 20,000 points (Rs 200) but only allows Rs 100 out.
  assert.equal(loyalty.pointsBalance(customer.id, shop.id), 20000);
  assert.equal(balance.netPosition(shop.id).cap_paise, 10000);

  // Exactly at the cap is allowed.
  conversion.convert({ vendor: shop, customerId: customer.id, points: 10000 });
  assert.equal(balance.netPosition(shop.id).headroom_paise, 0);
  assert.equal(balance.netPosition(shop.id).status, "blocked");

  // The customer still holds 10,000 points, and none of them may leave.
  assert.equal(loyalty.pointsBalance(customer.id, shop.id), 10000);
  assert.throws(
    () => conversion.convert({ vendor: shop, customerId: customer.id, points: 1000 }),
    /reached its MelaCoin conversion limit/
  );
});

test("the refusal tells the customer exactly how many points they CAN convert", () => {
  const shop = capped();
  loyalty.recordPurchase({ vendor: shop, customerId: customer.id, grossPaise: 1000000, idempotencyKey: key() });

  // She holds 20,000 points but only Rs 100 (10,000 points) may leave.
  assert.throws(
    () => conversion.convert({ vendor: shop, customerId: customer.id, points: 20000 }),
    (error) => {
      // Rs 100 of headroom is 10,000 points at 1 paisa each - both must appear.
      assert.match(error.message, /can only release ₹100\.00/);
      const allowed = Number(error.message.match(/which is (\d+) point/)[1]);
      assert.equal(allowed, 10000);
      // The number it names must actually work, or the message is a lie.
      conversion.convert({ vendor: shop, customerId: customer.id, points: allowed });
      return true;
    }
  );
});

test("blocking conversion never touches the customer's points in the shop", () => {
  const shop = capped();
  loyalty.recordPurchase({ vendor: shop, customerId: customer.id, grossPaise: 1000000, idempotencyKey: key() });
  conversion.convert({ vendor: shop, customerId: customer.id, points: 10000 }); // uses the whole cap

  const held = loyalty.pointsBalance(customer.id, shop.id);
  assert.equal(held, 10000, "she still holds the points the cap would not release");

  // Conversion is closed...
  assert.throws(() => conversion.convert({ vendor: shop, customerId: customer.id, points: held }));

  // ...but spending them in the shop that issued them still works. This is the
  // whole point: the shop is protected, the customer loses nothing.
  const { purchase } = loyalty.recordPurchase({
    vendor: shop, customerId: customer.id, grossPaise: 100000, pointsToRedeem: 1000, idempotencyKey: key(),
  });
  assert.equal(purchase.points_discount_paise, 1000); // 1,000 points x 1 paisa = Rs 10
});

// --------------------------------------------------------------- reciprocity

test("accepting MelaCoin restores the shop's headroom", () => {
  const shop = capped();
  loyalty.recordPurchase({ vendor: shop, customerId: customer.id, grossPaise: 1000000, idempotencyKey: key() });
  conversion.convert({ vendor: shop, customerId: customer.id, points: 10000 });
  assert.equal(balance.netPosition(shop.id).headroom_paise, 0);

  // The customer spends Rs 40 of that MelaCoin back at the same shop.
  loyalty.recordPurchase({
    vendor: shop, customerId: customer.id, grossPaise: 100000,
    melaWeiToSpend: 40n * money.WEI_PER_MELA, idempotencyKey: key(),
  });

  const after = balance.netPosition(shop.id);
  assert.equal(after.inflow_paise, 4000);
  assert.ok(after.headroom_paise > 0, "taking MelaCoin in must reopen conversion");
  assert.equal(after.status, "healthy");
});

test("a shop in balance is never constrained, however much flows through it", () => {
  const shop = capped({ net_outflow_tolerance_bps: 1 }); // an almost absurd 0.01% cap
  loyalty.recordPurchase({ vendor: shop, customerId: customer.id, grossPaise: 1000000, idempotencyKey: key() });

  // Give the customer MelaCoin from somewhere else entirely.
  const other = capped({ net_outflow_tolerance_bps: 10000, net_outflow_floor_paise: 100000000 });
  loyalty.recordPurchase({ vendor: other, customerId: customer.id, grossPaise: 5000000, idempotencyKey: key() });
  conversion.convert({ vendor: other, customerId: customer.id, points: 50000 });

  // The strict shop takes Rs 400 of MelaCoin in...
  loyalty.recordPurchase({
    vendor: shop, customerId: customer.id, grossPaise: 100000,
    melaWeiToSpend: 400n * money.WEI_PER_MELA, idempotencyKey: key(),
  });

  // ...so it can now let Rs 400-ish of its own points out without complaint.
  const position = balance.netPosition(shop.id);
  assert.ok(position.headroom_paise >= 40000);
  assert.doesNotThrow(() => conversion.convert({ vendor: shop, customerId: customer.id, points: 10000 }));
  assert.ok(balance.netPosition(shop.id).net_outflow_paise < 0, "still a net receiver");
});

// ------------------------------------------------------------------- limits

test("the floor keeps a brand-new shop usable on day one", () => {
  const shop = capped({ net_outflow_floor_paise: 50000 }); // Rs 500 floor
  // Barely any sales, so the percentage cap would be almost nothing.
  loyalty.recordPurchase({ vendor: shop, customerId: customer.id, grossPaise: 20000, idempotencyKey: key() });

  const position = balance.netPosition(shop.id);
  assert.equal(position.cap_from_sales_paise, 200); // 1% of Rs 200
  assert.equal(position.cap_paise, 50000, "the floor must win");
  assert.doesNotThrow(() => conversion.convert({ vendor: shop, customerId: customer.id, points: 200 }));
});

test("a shop's own budget can tighten the cap but never loosen it", () => {
  const tight = capped({ conversion_budget_paise: 5000 }); // Rs 50 self-imposed
  loyalty.recordPurchase({ vendor: tight, customerId: customer.id, grossPaise: 1000000, idempotencyKey: key() });
  assert.equal(balance.netPosition(tight.id).cap_paise, 5000, "budget below the platform cap wins");

  const greedy = capped({ conversion_budget_paise: 99999999 }); // asking for more
  loyalty.recordPurchase({ vendor: greedy, customerId: customer.id, grossPaise: 1000000, idempotencyKey: key() });
  assert.equal(balance.netPosition(greedy.id).cap_paise, 10000, "platform cap still binds");
});

test("paying the settlement invoice does NOT buy more headroom", () => {
  const shop = capped();
  loyalty.recordPurchase({ vendor: shop, customerId: customer.id, grossPaise: 1000000, idempotencyKey: key() });
  conversion.convert({ vendor: shop, customerId: customer.id, points: 10000 });
  assert.equal(balance.netPosition(shop.id).headroom_paise, 0);

  // The shop settles its bill in full. Its DEBT is now zero...
  settlement.recordPayment({ vendorId: shop.id, amountPaise: 10000, note: "paid in full" });
  assert.equal(settlement.balancePaise(shop.id), 0);

  // ...but it is still just as much a net donor, so the cap has not moved.
  // Credit risk and economic fairness are different questions.
  assert.equal(balance.netPosition(shop.id).headroom_paise, 0);
});

// ------------------------------------------------------------------ routing

test("shops that funded the network are ranked first for MelaCoin spending", () => {
  const donorBig = capped({ name: "Big Donor", net_outflow_tolerance_bps: 10000, net_outflow_floor_paise: 100000000 });
  const donorSmall = capped({ name: "Small Donor", net_outflow_tolerance_bps: 10000, net_outflow_floor_paise: 100000000 });
  const neutral = capped({ name: "Neutral Shop" });

  loyalty.recordPurchase({ vendor: donorBig, customerId: customer.id, grossPaise: 5000000, idempotencyKey: key() });
  loyalty.recordPurchase({ vendor: donorSmall, customerId: customer.id, grossPaise: 1000000, idempotencyKey: key() });
  conversion.convert({ vendor: donorBig, customerId: customer.id, points: 50000 });   // Rs 500 out
  conversion.convert({ vendor: donorSmall, customerId: customer.id, points: 10000 }); // Rs 100 out

  const ranked = balance.shopsNeedingFootfall();
  assert.equal(ranked[0].name, "Big Donor");
  assert.equal(ranked[1].name, "Small Donor");
  assert.ok(!ranked.some((r) => r.name === "Neutral Shop"), "a shop with no deficit needs no help");
});

test("a shop that refuses MelaCoin is never suggested as a place to spend it", () => {
  const refuser = capped({ name: "Cash Only", accepts_mela: 0, net_outflow_tolerance_bps: 10000, net_outflow_floor_paise: 100000000 });
  loyalty.recordPurchase({ vendor: refuser, customerId: customer.id, grossPaise: 1000000, idempotencyKey: key() });
  conversion.convert({ vendor: refuser, customerId: customer.id, points: 10000 });

  assert.ok(balance.netPosition(refuser.id).net_outflow_paise > 0, "it is a donor");
  assert.ok(!balance.shopsNeedingFootfall().some((r) => r.name === "Cash Only"));
});

test("a one-way valve is flagged to the platform", () => {
  const valve = capped({ name: "Valve", allow_mela_conversion: 1, accepts_mela: 0 });
  const healthy = capped({ name: "Healthy", allow_mela_conversion: 1, accepts_mela: 1 });

  assert.equal(balance.netPosition(valve.id).one_way_valve, true);
  assert.equal(balance.netPosition(healthy.id).one_way_valve, false);
  assert.deepEqual(
    balance.allPositions().filter((p) => p.one_way_valve).map((p) => p.vendor_name),
    ["Valve"]
  );
});

test("the window is rolling, so old outflow stops counting", () => {
  const shop = capped();
  loyalty.recordPurchase({ vendor: shop, customerId: customer.id, grossPaise: 1000000, idempotencyKey: key() });
  conversion.convert({ vendor: shop, customerId: customer.id, points: 10000 });
  assert.equal(balance.netPosition(shop.id).headroom_paise, 0);

  // Backdate that conversion beyond the window.
  const old = new Date(Date.now() - 40 * 86400_000).toISOString();
  db.get().prepare("UPDATE vendor_settlements SET created_at = ? WHERE vendor_id = ?").run(old, shop.id);

  const after = balance.netPosition(shop.id);
  assert.equal(after.outflow_paise, 0, "outflow outside the window is forgotten");
  assert.ok(after.headroom_paise > 0, "the shop's allowance recovers over time");
});

test("a tolerance at or above the loyalty cost never blocks anybody", () => {
  // The rule of thumb a shop should be given: set your outflow tolerance to at
  // least what your loyalty programme already costs you, and conversion is never
  // rationed - every point you issue can leave, because your own sales fund it.
  const shop = capped({ net_outflow_tolerance_bps: 200 }); // 2% cost, 2% tolerance
  loyalty.recordPurchase({ vendor: shop, customerId: customer.id, grossPaise: 1000000, idempotencyKey: key() });

  const held = loyalty.pointsBalance(customer.id, shop.id);
  assert.equal(held, 20000);
  assert.doesNotThrow(() => conversion.convert({ vendor: shop, customerId: customer.id, points: held }));
  assert.equal(balance.netPosition(shop.id).headroom_paise, 0, "exactly used up, never exceeded");
});
