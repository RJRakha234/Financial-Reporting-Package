"use strict";
/**
 * loyalty.service.js - earning, redeeming and expiring loyalty points.
 *
 * The mental model, in one paragraph:
 *
 * Points are a promise a shop makes to one customer. They are NOT money and they
 * are NOT the token - they only mean something at the shop that issued them. Every
 * time a customer earns, we create a "lot": a dated parcel of points with its own
 * expiry date. When they spend points we drain the lot that expires soonest, which
 * is the friendliest order for the customer. A balance is always the sum of the
 * live lots, so we can always show exactly where a number came from.
 */
const db = require("../db");
const { RuleError } = require("../errors");
const money = require("../money");
const settings = require("./settings.service");
const tokenService = require("./token.service");

// ------------------------------------------------------------------ expiry

/**
 * Retires any lot whose expiry has passed and records an EXPIRE event for each.
 * Called before every balance read, so a customer never sees points they cannot use.
 */
function expireDuePoints({ customerId = null, vendorId = null } = {}) {
  const at = db.now();
  const filters = ["points_remaining > 0", "expires_at IS NOT NULL", "expires_at <= ?"];
  const args = [at];
  if (customerId) { filters.push("customer_id = ?"); args.push(customerId); }
  if (vendorId) { filters.push("vendor_id = ?"); args.push(vendorId); }

  const due = db.get().prepare(`SELECT * FROM point_lots WHERE ${filters.join(" AND ")}`).all(...args);
  if (due.length === 0) return 0;

  let expired = 0;
  db.transaction((database) => {
    const clearLot = database.prepare("UPDATE point_lots SET points_remaining = 0 WHERE id = ?");
    const logEvent = database.prepare(
      `INSERT INTO point_events (id, vendor_id, customer_id, kind, points_delta, lot_id, ref_type, ref_id, note, created_at)
       VALUES (?, ?, ?, 'EXPIRE', ?, ?, 'point_lot', ?, ?, ?)`
    );
    for (const lot of due) {
      clearLot.run(lot.id);
      logEvent.run(
        db.newId("evt"), lot.vendor_id, lot.customer_id, -lot.points_remaining,
        lot.id, lot.id, `expired on ${lot.expires_at}`, at
      );
      expired += lot.points_remaining;
    }
  });
  return expired;
}

// ---------------------------------------------------------------- balances

function pointsBalance(customerId, vendorId) {
  expireDuePoints({ customerId, vendorId });
  const row = db
    .get()
    .prepare(
      `SELECT COALESCE(SUM(points_remaining), 0) AS balance FROM point_lots
       WHERE customer_id = ? AND vendor_id = ? AND points_remaining > 0
         AND (expires_at IS NULL OR expires_at > ?)`
    )
    .get(customerId, vendorId, db.now());
  return row.balance;
}

/** Live lots, soonest expiry first - the order in which they will be spent. */
function activeLots(customerId, vendorId) {
  return db
    .get()
    .prepare(
      `SELECT * FROM point_lots
       WHERE customer_id = ? AND vendor_id = ? AND points_remaining > 0
         AND (expires_at IS NULL OR expires_at > ?)
       ORDER BY (expires_at IS NULL), expires_at ASC, earned_at ASC`
    )
    .all(customerId, vendorId, db.now());
}

/** Every shop where this customer holds points, with what those points are worth there. */
function balancesByVendor(customerId) {
  expireDuePoints({ customerId });
  const rows = db
    .get()
    .prepare(
      `SELECT v.*, COALESCE(SUM(l.points_remaining), 0) AS balance,
              MIN(CASE WHEN l.points_remaining > 0 THEN l.expires_at END) AS next_expiry
       FROM vendors v
       JOIN point_lots l ON l.vendor_id = v.id AND l.customer_id = ?
            AND l.points_remaining > 0 AND (l.expires_at IS NULL OR l.expires_at > ?)
       GROUP BY v.id
       HAVING balance > 0
       ORDER BY balance DESC`
    )
    .all(customerId, db.now());

  return rows.map((vendor) => ({
    vendor: publicVendor(vendor),
    points: vendor.balance,
    value_paise: money.paiseForPoints(vendor.balance, vendor.redeem_milli_paise_per_point),
    next_expiry: vendor.next_expiry,
  }));
}

/** The vendor fields that are safe to show a customer. Never leaks the API key. */
function publicVendor(vendor) {
  return {
    id: vendor.id,
    name: vendor.name,
    slug: vendor.slug,
    category: vendor.category,
    city: vendor.city,
    earn_milli_points_per_rupee: vendor.earn_milli_points_per_rupee,
    redeem_milli_paise_per_point: vendor.redeem_milli_paise_per_point,
    min_redeem_points: vendor.min_redeem_points,
    max_redeem_bps: vendor.max_redeem_bps,
    points_expiry_days: vendor.points_expiry_days,
    earn_on_net: !!vendor.earn_on_net,
    allow_mela_conversion: !!vendor.allow_mela_conversion,
    mela_conversion_fee_bps: vendor.mela_conversion_fee_bps,
    accepts_mela: !!vendor.accepts_mela,
    conversion_budget_paise: vendor.conversion_budget_paise,
    net_outflow_tolerance_bps: vendor.net_outflow_tolerance_bps,
    active: !!vendor.active,
    earn_rate_label: money.describeEarnRate(vendor.earn_milli_points_per_rupee),
    redeem_rate_label: money.describeRedeemRate(vendor.redeem_milli_paise_per_point),
  };
}

// ------------------------------------------------------------------- quote

/**
 * Works out a bill without changing anything. The POS screen calls this on every
 * keystroke, and `recordPurchase` re-runs exactly the same maths before committing,
 * so what the cashier sees and what gets saved can never drift apart.
 */
function quotePurchase({ vendor, customerId, grossPaise, pointsToRedeem = 0, melaWeiToSpend = 0n }) {
  if (!vendor.active) throw new RuleError(`${vendor.name} is not accepting transactions right now`);
  if (!Number.isInteger(grossPaise) || grossPaise <= 0) throw new RuleError("Bill amount must be more than zero");

  const points = Number(pointsToRedeem) || 0;
  if (!Number.isInteger(points) || points < 0) throw new RuleError("Points to redeem must be a whole number");
  const melaWei = BigInt(melaWeiToSpend || 0);
  if (melaWei < 0n) throw new RuleError("MELA amount cannot be negative");

  const pointsAvailable = pointsBalance(customerId, vendor.id);
  const capPaise = money.maxRedeemablePaise(grossPaise, vendor.max_redeem_bps);
  const maxPointsByCap = money.maxPointsForPaiseCap(capPaise, vendor.redeem_milli_paise_per_point);
  const maxRedeemablePoints = Math.min(pointsAvailable, maxPointsByCap);

  if (points > pointsAvailable) {
    throw new RuleError(`Only ${pointsAvailable} point(s) available at ${vendor.name}`);
  }
  if (points > 0 && points < vendor.min_redeem_points) {
    throw new RuleError(`${vendor.name} requires at least ${vendor.min_redeem_points} points to redeem`);
  }
  if (points > maxPointsByCap) {
    throw new RuleError(
      `Points can pay for at most ${money.formatPaise(capPaise)} of this bill ` +
        `(${vendor.max_redeem_bps / 100}%), which is ${maxPointsByCap} point(s)`
    );
  }

  const pointsDiscountPaise = money.paiseForPoints(points, vendor.redeem_milli_paise_per_point);
  const pricePaise = settings.melaPricePaise();

  let melaDiscountPaise = 0;
  if (melaWei > 0n) {
    if (!vendor.accepts_mela) throw new RuleError(`${vendor.name} does not accept MELA yet`);
    const held = tokenService.balanceWei(customerId);
    if (melaWei > held) throw new RuleError("Not enough MELA in your wallet");
    melaDiscountPaise = money.paiseForMelaWei(melaWei, pricePaise);
    if (melaDiscountPaise > grossPaise - pointsDiscountPaise) {
      throw new RuleError("That is more MELA than this bill needs");
    }
  }

  const netPaise = grossPaise - pointsDiscountPaise - melaDiscountPaise;
  // Earning on the cash actually paid stops a loop where points earn more points.
  const earnBase = vendor.earn_on_net ? netPaise : grossPaise;
  const pointsEarned = money.pointsForSpend(earnBase, vendor.earn_milli_points_per_rupee);

  return {
    vendor: publicVendor(vendor),
    gross_paise: grossPaise,
    points_available: pointsAvailable,
    max_redeemable_points: maxRedeemablePoints,
    max_redeemable_paise: capPaise,
    points_redeemed: points,
    points_discount_paise: pointsDiscountPaise,
    mela_wei_paid: melaWei.toString(),
    mela_discount_paise: melaDiscountPaise,
    mela_price_paise: pricePaise,
    net_paise: netPaise,
    points_earned: pointsEarned,
    points_after: pointsAvailable - points + pointsEarned,
    earn_base_paise: earnBase,
    display: {
      gross: money.formatPaise(grossPaise),
      points_discount: money.formatPaise(pointsDiscountPaise),
      mela_discount: money.formatPaise(melaDiscountPaise),
      net: money.formatPaise(netPaise),
    },
  };
}

// -------------------------------------------------------------- the write

/**
 * Commits a bill: takes the points, takes the MELA, gives the new points.
 *
 * `idempotencyKey` makes this safe to retry. A shaky counter connection that sends
 * the same sale twice must not charge a customer's points twice, so a repeat of the
 * same key returns the original receipt instead of creating a second one.
 */
function recordPurchase({ vendor, customerId, grossPaise, pointsToRedeem = 0, melaWeiToSpend = 0n, idempotencyKey, billRef = null }) {
  if (!idempotencyKey || typeof idempotencyKey !== "string") {
    throw new RuleError("idempotency_key is required so a retried sale is not counted twice");
  }

  const existing = db
    .get()
    .prepare("SELECT * FROM purchases WHERE vendor_id = ? AND idempotency_key = ?")
    .get(vendor.id, idempotencyKey);
  if (existing) {
    // A cancelled bill must never be reported as a successful sale just because
    // the same reference came round again - that would tell the counter the sale
    // went through when nothing happened.
    if (existing.voided_at) {
      throw new RuleError("That bill reference was used for a bill that has been cancelled. Use a new reference.");
    }
    return { purchase: existing, replayed: true };
  }

  const quote = quotePurchase({ vendor, customerId, grossPaise, pointsToRedeem, melaWeiToSpend });
  const purchaseId = db.newId("pur");
  const at = db.now();
  const melaWei = BigInt(melaWeiToSpend || 0);

  const purchase = db.transaction((database) => {
    database
      .prepare(
        `INSERT INTO purchases (id, vendor_id, customer_id, idempotency_key, gross_paise,
                                points_redeemed, points_discount_paise, mela_wei_paid, mela_discount_paise,
                                net_paise, points_earned, bill_ref, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
      )
      .run(
        purchaseId, vendor.id, customerId, idempotencyKey, quote.gross_paise,
        quote.points_redeemed, quote.points_discount_paise, melaWei.toString(), quote.mela_discount_paise,
        quote.net_paise, quote.points_earned, billRef, at
      );

    if (quote.points_redeemed > 0) {
      consumeLots(database, {
        customerId, vendorId: vendor.id, points: quote.points_redeemed,
        kind: "REDEEM", refType: "purchase", refId: purchaseId,
        note: `redeemed against bill ${billRef || purchaseId}`, at,
      });
    }

    if (melaWei > 0n) {
      // The customer's MELA leaves circulation for us and the shop gets rupees from
      // the platform treasury instead. See docs/04-money-math.md, "Who owes whom".
      tokenService.post({
        customerId, kind: "SPEND", weiDelta: -melaWei,
        refType: "purchase", refId: purchaseId, note: `spent at ${vendor.name}`,
      });
      recordSettlement(database, {
        vendorId: vendor.id, kind: "MELA_ACCEPTANCE_CREDIT", amountPaise: -quote.mela_discount_paise,
        refType: "purchase", refId: purchaseId,
        note: `MELA accepted for bill ${billRef || purchaseId}`, at,
      });
    }

    if (quote.points_earned > 0) {
      grantPoints(database, {
        customerId, vendor, points: quote.points_earned, purchaseId, at,
      });
    }

    return database.prepare("SELECT * FROM purchases WHERE id = ?").get(purchaseId);
  });

  return { purchase, quote, replayed: false };
}

/** Creates a new lot of points with the vendor's expiry rule applied. */
function grantPoints(database, { customerId, vendor, points, purchaseId = null, at, note = null }) {
  const lotId = db.newId("lot");
  const expiresAt =
    vendor.points_expiry_days > 0
      ? new Date(Date.parse(at) + vendor.points_expiry_days * 86400_000).toISOString()
      : null;

  database
    .prepare(
      `INSERT INTO point_lots (id, vendor_id, customer_id, purchase_id, points_earned, points_remaining, earned_at, expires_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
    )
    .run(lotId, vendor.id, customerId, purchaseId, points, points, at, expiresAt);

  database
    .prepare(
      `INSERT INTO point_events (id, vendor_id, customer_id, kind, points_delta, lot_id, ref_type, ref_id, note, created_at)
       VALUES (?, ?, ?, 'EARN', ?, ?, ?, ?, ?, ?)`
    )
    .run(db.newId("evt"), vendor.id, customerId, points, lotId,
         purchaseId ? "purchase" : "adjustment", purchaseId, note, at);

  return lotId;
}

/**
 * Spends `points` from the customer's lots, soonest-to-expire first, writing one
 * event per lot touched. Throws if there are not enough - the caller's transaction
 * then rolls back and nothing is half-spent.
 */
function consumeLots(database, { customerId, vendorId, points, kind, refType, refId, note, at }) {
  const lots = database
    .prepare(
      `SELECT * FROM point_lots
       WHERE customer_id = ? AND vendor_id = ? AND points_remaining > 0
         AND (expires_at IS NULL OR expires_at > ?)
       ORDER BY (expires_at IS NULL), expires_at ASC, earned_at ASC`
    )
    .all(customerId, vendorId, at);

  const drainLot = database.prepare("UPDATE point_lots SET points_remaining = points_remaining - ? WHERE id = ?");
  const logEvent = database.prepare(
    `INSERT INTO point_events (id, vendor_id, customer_id, kind, points_delta, lot_id, ref_type, ref_id, note, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  );

  let left = points;
  for (const lot of lots) {
    if (left === 0) break;
    const take = Math.min(left, lot.points_remaining);
    drainLot.run(take, lot.id);
    logEvent.run(db.newId("evt"), vendorId, customerId, kind, -take, lot.id, refType, refId, note, at);
    left -= take;
  }
  if (left > 0) throw new RuleError(`Not enough points: short by ${left}`);
}

/** Writes a line to the vendor's running account with the platform. */
function recordSettlement(database, { vendorId, kind, amountPaise, refType, refId, note, at }) {
  database
    .prepare(
      `INSERT INTO vendor_settlements (id, vendor_id, kind, amount_paise, ref_type, ref_id, note, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
    )
    .run(db.newId("stl"), vendorId, kind, amountPaise, refType || null, refId || null, note || null, at || db.now());
}

module.exports = {
  expireDuePoints,
  pointsBalance,
  activeLots,
  balancesByVendor,
  publicVendor,
  quotePurchase,
  recordPurchase,
  grantPoints,
  consumeLots,
  recordSettlement,
};
