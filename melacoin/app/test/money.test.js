"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const m = require("../src/money");

test("pointsForSpend: the headline example from the docs", () => {
  // Rs 100 spent, vendor gives 2.5 points per rupee
  assert.equal(m.pointsForSpend(10000, 2500), 250);
  // 1 point per rupee
  assert.equal(m.pointsForSpend(10000, 1000), 100);
  // half a point per rupee
  assert.equal(m.pointsForSpend(10000, 500), 50);
});

test("pointsForSpend: rounds down, never up", () => {
  // Rs 9.99 at 1 point/rupee -> 9 points, not 10
  assert.equal(m.pointsForSpend(999, 1000), 9);
  assert.equal(m.pointsForSpend(1, 1000), 0);
  assert.equal(m.pointsForSpend(0, 2500), 0);
});

test("pointsForSpend: rejects nonsense input rather than guessing", () => {
  assert.throws(() => m.pointsForSpend(-100, 1000), TypeError);
  assert.throws(() => m.pointsForSpend(10.5, 1000), TypeError);
  assert.throws(() => m.pointsForSpend(100, -1), TypeError);
});

test("paiseForPoints: value of points when redeeming", () => {
  assert.equal(m.paiseForPoints(250, 10000), 2500); // 250 pts x Rs 0.10 = Rs 25
  assert.equal(m.paiseForPoints(1, 10000), 10);
  assert.equal(m.paiseForPoints(1, 500), 0); // worth half a paisa -> rounds to zero
});

test("pointsForPaise: rounds UP so a discount is always fully paid for", () => {
  assert.equal(m.pointsForPaise(2500, 10000), 250);
  assert.equal(m.pointsForPaise(2501, 10000), 251); // one extra point for one extra paisa
  assert.equal(m.pointsForPaise(1, 10000), 1);
  assert.throws(() => m.pointsForPaise(100, 0), TypeError);
});

test("round trip: converting points to money and back never gains value", () => {
  for (const rate of [10000, 7500, 333, 1]) {
    for (const points of [1, 7, 99, 1234, 98765]) {
      const paise = m.paiseForPoints(points, rate);
      const needed = m.pointsForPaise(paise, rate);
      assert.ok(needed <= points, `${points} pts @ ${rate} became ${needed} pts`);
    }
  }
});

test("applyBps and maxRedeemablePaise", () => {
  assert.equal(m.applyBps(10000, 500), 500); // 5% of Rs 100 = Rs 5
  assert.equal(m.applyBps(10000, 10000), 10000); // 100%
  assert.equal(m.applyBps(333, 500), 16); // rounds down
  assert.equal(m.maxRedeemablePaise(10000, 5000), 5000); // 50% cap
  assert.equal(m.maxRedeemablePaise(10000, 20000), 10000); // cap can never exceed the bill
});

test("melaWeiForPaise: rupees into tokens", () => {
  // MELA priced at Rs 1.00 (100 paise): Rs 25 -> 25 MELA
  assert.equal(m.melaWeiForPaise(2500, 100), 25n * m.WEI_PER_MELA);
  // MELA priced at Rs 2.00: Rs 25 -> 12.5 MELA
  assert.equal(m.melaWeiForPaise(2500, 200), 125n * m.WEI_PER_MELA / 10n);
  assert.equal(m.melaWeiForPaise(0, 100), 0n);
  assert.throws(() => m.melaWeiForPaise(100, 0), TypeError);
});

test("paiseForMelaWei: tokens back into rupees, rounding down", () => {
  assert.equal(m.paiseForMelaWei(25n * m.WEI_PER_MELA, 100), 2500);
  assert.equal(m.paiseForMelaWei(1n, 100), 0); // a speck of a token is worth nothing
});

test("money conversion never creates rupees out of thin air", () => {
  for (const price of [1, 37, 100, 250, 99999]) {
    for (const paise of [1, 99, 12345, 1000000]) {
      const wei = m.melaWeiForPaise(paise, price);
      assert.ok(m.paiseForMelaWei(wei, price) <= paise);
    }
  }
});

test("formatPaise uses Indian grouping", () => {
  assert.equal(m.formatPaise(0), "₹0.00");
  assert.equal(m.formatPaise(5), "₹0.05");
  assert.equal(m.formatPaise(123456), "₹1,234.56");
  assert.equal(m.formatPaise(1234567890), "₹1,23,45,678.90");
  assert.equal(m.formatPaise(-2500), "-₹25.00");
});

test("formatMela trims trailing zeros", () => {
  assert.equal(m.formatMela(25n * m.WEI_PER_MELA), "25");
  assert.equal(m.formatMela(125n * m.WEI_PER_MELA / 10n), "12.5");
  assert.equal(m.formatMela(0n), "0");
});

test("maxPointsForPaiseCap never lets the discount exceed the cap", () => {
  assert.equal(m.maxPointsForPaiseCap(3000, 10000), 300); // Rs 30 cap, 10p/point -> 300 points
  assert.equal(m.maxPointsForPaiseCap(2999, 10000), 299); // rounds down
  for (const rate of [10000, 7777, 3, 1]) {
    for (const cap of [1, 250, 99999]) {
      const points = m.maxPointsForPaiseCap(cap, rate);
      assert.ok(m.paiseForPoints(points, rate) <= cap);
    }
  }
});
