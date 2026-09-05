"use strict";
/**
 * The two things that happen at a real till: signing somebody up with nothing but
 * a phone number, and undoing a bill that was typed wrong.
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { freshDb, makeUser, makeVendor, db } = require("./helpers");

const counter = require("../src/services/counter.service");
const loyalty = require("../src/services/loyalty.service");
const conversion = require("../src/services/conversion.service");
const settlement = require("../src/services/settlement.service");
const tokenService = require("../src/services/token.service");
const settings = require("../src/services/settings.service");
const money = require("../src/money");

let vendor, n;
const key = () => `ctr-${++n}`;

test.beforeEach(() => {
  freshDb();
  n = 0;
  ({ vendor } = makeVendor());
  settings.setMelaPricePaise(100);
});

// ------------------------------------------------------------------ enrolment

test("a shop signs someone up with only a phone number", () => {
  const { customer, created } = counter.enrol({ phone: "9800000001", name: "Snigdha" });
  assert.equal(created, true);
  assert.equal(customer.phone, "9800000001");
  assert.equal(customer.name, "Snigdha");
  assert.equal(customer.claimed, false, "no password yet");
  assert.equal(customer.email, null, "the placeholder address is never shown");
});

test("a name is optional - the counter often will not have one", () => {
  const { customer } = counter.enrol({ phone: "9800000002" });
  assert.match(customer.name, /0002/, "falls back to something the shop can recognise");
});

test("however the shopkeeper types the number, it is one person", () => {
  const first = counter.enrol({ phone: "9800000003", name: "Ravi" });
  for (const typed of ["+91 98000 00003", "098000-00003", " 9800000003 ", "+919800000003"]) {
    const again = counter.enrol({ phone: typed });
    assert.equal(again.created, false, `"${typed}" should find the existing customer`);
    assert.equal(again.customer.id, first.customer.id);
  }
  assert.equal(db.get().prepare("SELECT COUNT(*) AS n FROM users WHERE role='customer'").get().n, 1);
});

test("a number that cannot be a mobile is refused", () => {
  for (const bad of ["12345", "1234567890", "98000000011", "abcdefghij", ""]) {
    assert.throws(() => counter.enrol({ phone: bad }), /10-digit mobile number/, `"${bad}"`);
  }
});

test("enrolling twice is safe, and fills in a name we did not have", () => {
  counter.enrol({ phone: "9800000004" });
  const second = counter.enrol({ phone: "9800000004", name: "Meera" });
  assert.equal(second.created, false);
  assert.equal(second.customer.name, "Meera");
});

test("a counter-enrolled customer earns points immediately", () => {
  const { customer } = counter.enrol({ phone: "9800000005" });
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 10000, idempotencyKey: key() });
  assert.equal(loyalty.pointsBalance(customer.id, vendor.id), 250);
});

// -------------------------------------------------------------------- claiming

test("an unclaimed account cannot be logged into", () => {
  const { customer } = counter.enrol({ phone: "9800000006" });
  const row = db.get().prepare("SELECT * FROM users WHERE id = ?").get(customer.id);
  const auth = require("../src/auth");
  for (const guess of ["", "unclaimed", "password", "melacoin123"]) {
    assert.equal(auth.verifyPassword(guess, row.password_hash), false);
  }
});

test("the customer takes ownership by registering with the same number, and keeps their points", () => {
  const { customer } = counter.enrol({ phone: "9800000007", name: "Snigdha" });
  loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 40000, idempotencyKey: key() });
  assert.equal(loyalty.pointsBalance(customer.id, vendor.id), 1000);

  const claimed = counter.claim({
    phone: "+91 98000 00007", name: "Snigdha Rao",
    email: "snigdha@example.test", password: "my-own-password",
  });

  assert.equal(claimed.id, customer.id, "same account, not a new one");
  assert.equal(claimed.email, "snigdha@example.test");
  assert.equal(loyalty.pointsBalance(customer.id, vendor.id), 1000, "points survive the claim");
  assert.equal(require("../src/auth").verifyPassword("my-own-password", claimed.password_hash), true);
});

test("an already-claimed account cannot be claimed again", () => {
  counter.enrol({ phone: "9800000008" });
  counter.claim({ phone: "9800000008", name: "A", email: "a@example.test", password: "password-one" });
  assert.throws(
    () => counter.claim({ phone: "9800000008", name: "B", email: "b@example.test", password: "password-two" }),
    /already exists. Sign in instead/
  );
});

// --------------------------------------------------------------------- voiding

function sale(customerId, gross, extra = {}) {
  return loyalty.recordPurchase({ vendor, customerId, grossPaise: gross, idempotencyKey: key(), ...extra }).purchase;
}

test("a mistyped bill can be undone: points given are withdrawn", () => {
  const { customer } = counter.enrol({ phone: "9800000009" });
  const typo = sale(customer.id, 200000); // Rs 2000 instead of Rs 200
  assert.equal(loyalty.pointsBalance(customer.id, vendor.id), 5000);

  const result = counter.voidPurchase({ vendor, purchaseId: typo.id, reason: "typed 2000 not 200" });
  assert.equal(result.points_balance, 0);
  assert.ok(result.purchase.voided_at);
  assert.equal(result.purchase.void_reason, "typed 2000 not 200");
});

test("undoing a bill gives back the points it spent, to the same lots", () => {
  const { customer } = counter.enrol({ phone: "9800000010" });
  sale(customer.id, 400000);                       // earns 10,000 points
  const before = loyalty.pointsBalance(customer.id, vendor.id);

  const redeemed = sale(customer.id, 100000, { pointsToRedeem: 2000 });
  assert.equal(redeemed.points_discount_paise, 20000);
  const afterRedeem = loyalty.pointsBalance(customer.id, vendor.id);
  assert.equal(afterRedeem, before - 2000 + redeemed.points_earned);

  counter.voidPurchase({ vendor, purchaseId: redeemed.id });
  assert.equal(
    loyalty.pointsBalance(customer.id, vendor.id), before,
    "back to exactly where we were before the bill"
  );
});

test("a bill cannot be undone twice", () => {
  const { customer } = counter.enrol({ phone: "9800000011" });
  const p = sale(customer.id, 10000);
  counter.voidPurchase({ vendor, purchaseId: p.id });
  assert.throws(() => counter.voidPurchase({ vendor, purchaseId: p.id }), /already been cancelled/);
});

test("one shop cannot undo another shop's bill", () => {
  const { customer } = counter.enrol({ phone: "9800000012" });
  const p = sale(customer.id, 10000);
  const { vendor: other } = makeVendor({ name: "Someone Else" });
  assert.throws(() => counter.voidPurchase({ vendor: other, purchaseId: p.id }), /belongs to another shop/);
});

test("a bill is not undoable once the points it gave have been spent", () => {
  const { customer } = counter.enrol({ phone: "9800000013" });
  const first = sale(customer.id, 100000);          // gives 2,500 points
  sale(customer.id, 50000, { pointsToRedeem: 2500 }); // and they get spent

  assert.throws(
    () => counter.voidPurchase({ vendor, purchaseId: first.id }),
    /already spent the points it gave them/
  );
});

test("a bill is not undoable after the window closes", () => {
  const { customer } = counter.enrol({ phone: "9800000014" });
  const p = sale(customer.id, 10000);
  const old = new Date(Date.now() - 3 * 3600_000).toISOString();
  db.get().prepare("UPDATE purchases SET created_at = ? WHERE id = ?").run(old, p.id);

  assert.throws(() => counter.voidPurchase({ vendor, purchaseId: p.id }), /only be cancelled within/);
});

test("undoing a bill paid with MelaCoin returns the tokens and unwinds the settlement", () => {
  const source = makeUser();
  const { vendor: issuer } = makeVendor({ name: "Issuer" });
  loyalty.recordPurchase({ vendor: issuer, customerId: source.id, grossPaise: 400000, idempotencyKey: key() });
  conversion.convert({ vendor: issuer, customerId: source.id, points: 10000 });
  const walletBefore = tokenService.balanceWei(source.id);

  const paid = sale(source.id, 50000, { melaWeiToSpend: 100n * money.WEI_PER_MELA });
  assert.equal(paid.mela_discount_paise, 10000);
  assert.equal(settlement.balancePaise(vendor.id), -10000, "platform owes the shop");

  counter.voidPurchase({ vendor, purchaseId: paid.id });
  assert.equal(tokenService.balanceWei(source.id), walletBefore, "MelaCoin returned in full");
  assert.equal(settlement.balancePaise(vendor.id), 0, "and the shop is no longer owed for it");
});

test("a cancelled bill stops counting towards the shop's takings", () => {
  const { customer } = counter.enrol({ phone: "9800000015" });
  sale(customer.id, 50000);
  const typo = sale(customer.id, 500000);

  const balanceSvc = require("../src/services/balance.service");
  assert.equal(balanceSvc.netPosition(vendor.id).own_sales_paise, 550000);

  counter.voidPurchase({ vendor, purchaseId: typo.id });
  assert.equal(balanceSvc.netPosition(vendor.id).own_sales_paise, 50000);
  assert.equal(settlement.platformStats(100).purchase_volume_paise, 50000);
});

test("a cancelled bill's reference cannot be silently reused", () => {
  const { customer } = counter.enrol({ phone: "9800000016" });
  const p = loyalty.recordPurchase({
    vendor, customerId: customer.id, grossPaise: 10000, idempotencyKey: "BILL-42",
  }).purchase;
  counter.voidPurchase({ vendor, purchaseId: p.id });

  assert.throws(
    () => loyalty.recordPurchase({ vendor, customerId: customer.id, grossPaise: 10000, idempotencyKey: "BILL-42" }),
    /has been cancelled. Use a new reference/
  );
});

test("only bills inside the window are offered for cancelling", () => {
  const { customer } = counter.enrol({ phone: "9800000017" });
  const recent = sale(customer.id, 10000);
  const stale = sale(customer.id, 20000);
  db.get().prepare("UPDATE purchases SET created_at = ? WHERE id = ?")
    .run(new Date(Date.now() - 5 * 3600_000).toISOString(), stale.id);

  const offered = counter.voidableBills(vendor.id);
  assert.deepEqual(offered.map((b) => b.id), [recent.id]);
});

test("a real name the customer chose is never overwritten by a shop's guess", () => {
  const { customer } = counter.enrol({ phone: "9800000018", name: "Snigdha Rao" });
  const again = counter.enrol({ phone: "9800000018", name: "Chai Guy" });
  assert.equal(again.customer.name, "Snigdha Rao");
  assert.equal(again.customer.id, customer.id);
});

test("the longest shape a person writes still fits and normalises", () => {
  // "+91 98000 00019" is 15 characters. The counter field must not clip it.
  const long = "+91 98000 00019";
  assert.ok(long.length <= 20, "the input's maxlength must allow this");
  assert.equal(counter.normalisePhone(long), "9800000019");
});
