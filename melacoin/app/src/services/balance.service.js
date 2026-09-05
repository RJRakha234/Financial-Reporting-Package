"use strict";
/**
 * balance.service.js - stops any shop from quietly funding the whole network.
 *
 * THE PROBLEM THIS SOLVES
 *
 * A shop discharges its loyalty promise in one of two ways, and they are not equal:
 *
 *   Customer redeems at the shop   -> the shop gives a discount and gets a return
 *                                     visit. It paid for footfall and received it.
 *   Customer converts to MelaCoin  -> the shop pays the same rupees in cash, and the
 *                                     footfall lands on a DIFFERENT shop.
 *
 * The second one is strictly worse for the issuing shop, and it gets worse still:
 * in an ordinary loyalty scheme a large share of points are never redeemed at all,
 * and that unclaimed value is silent profit. Conversion destroys that "breakage" -
 * it turns a promise that might have cost nothing into a guaranteed cash outflow.
 *
 * So a shop with a generous earn rate and no MelaCoin acceptance becomes a one-way
 * valve: money leaves every month and never comes back. It will not notice for a
 * quarter, and then it will leave and tell the whole street.
 *
 * THE RULE
 *
 * Over a rolling window we measure each shop's net give-and-take with the network:
 *
 *     net outflow = (value of its points converted away) - (value of MelaCoin it took)
 *
 * and cap that at a share of the shop's OWN sales, with a floor so a new or tiny
 * shop is not frozen out on day one:
 *
 *     cap = max(floor, tolerance_bps x own sales in window)
 *
 * Two deliberate choices in that formula:
 *
 *  - It is the NET, not the gross. A shop that both issues and accepts in rough
 *    balance is never constrained, which is exactly the behaviour we want to reward.
 *  - Accepting MelaCoin RESTORES headroom. The incentive to take part is arithmetic,
 *    not a lecture.
 *
 * And one thing this deliberately does NOT count: settlement payments. A shop paying
 * its invoice does not mean it stopped being a net donor. This cap is about economic
 * fairness between shops, not about how much the shop currently owes you - that is
 * credit risk, and it is a different number in settlement.service.js.
 */
const db = require("../db");
const money = require("../money");
const config = require("../config");
const { RuleError } = require("../errors");

function windowStart(days = config.settlementWindowDays) {
  return new Date(Date.now() - days * 86400_000).toISOString();
}

/**
 * A shop's give-and-take with the rest of the network over the rolling window.
 * Every figure is in paise.
 */
function netPosition(vendorId, days = config.settlementWindowDays) {
  const vendor = db.get().prepare("SELECT * FROM vendors WHERE id = ?").get(vendorId);
  if (!vendor) throw new RuleError("Shop not found");

  const since = windowStart(days);
  const database = db.get();

  const sales = database
    .prepare("SELECT COALESCE(SUM(gross_paise), 0) AS total FROM purchases\n       WHERE vendor_id = ? AND created_at >= ? AND voided_at IS NULL")
    .get(vendorId, since).total;

  // Positive rows are conversions away from this shop; negative rows are MelaCoin it took in.
  const outflow = database
    .prepare(
      `SELECT COALESCE(SUM(amount_paise), 0) AS total FROM vendor_settlements
       WHERE vendor_id = ? AND created_at >= ? AND kind = 'CONVERSION_DEBIT'`
    )
    .get(vendorId, since).total;

  const inflow = -database
    .prepare(
      `SELECT COALESCE(SUM(amount_paise), 0) AS total FROM vendor_settlements
       WHERE vendor_id = ? AND created_at >= ? AND kind = 'MELA_ACCEPTANCE_CREDIT'`
    )
    .get(vendorId, since).total;

  const net = outflow - inflow;
  const capFromSales = money.applyBps(sales, vendor.net_outflow_tolerance_bps);
  let cap = Math.max(capFromSales, vendor.net_outflow_floor_paise);
  // The shop's own budget, if it set one, can only tighten the platform's cap.
  if (vendor.conversion_budget_paise > 0) cap = Math.min(cap, vendor.conversion_budget_paise);

  const headroom = Math.max(0, cap - net);
  const usedBps = cap === 0 ? 10000 : Math.min(10000, Math.max(0, Math.floor((net * 10000) / cap)));

  return {
    vendor_id: vendorId,
    vendor_name: vendor.name,
    window_days: days,
    since,
    own_sales_paise: sales,
    outflow_paise: outflow,
    inflow_paise: inflow,
    net_outflow_paise: net,
    cap_paise: cap,
    cap_from_sales_paise: capFromSales,
    floor_paise: vendor.net_outflow_floor_paise,
    budget_paise: vendor.conversion_budget_paise,
    headroom_paise: headroom,
    used_bps: usedBps,
    /** A shop letting points out but refusing MelaCoin is a one-way valve. */
    one_way_valve: !!vendor.allow_mela_conversion && !vendor.accepts_mela,
    status: headroom === 0 ? "blocked" : usedBps >= 8000 ? "near_limit" : "healthy",
    display: {
      own_sales: money.formatPaise(sales),
      outflow: money.formatPaise(outflow),
      inflow: money.formatPaise(inflow),
      net_outflow: money.formatPaise(net),
      cap: money.formatPaise(cap),
      headroom: money.formatPaise(headroom),
    },
  };
}

/** Rupees of point value that may still be converted away from this shop right now. */
function conversionHeadroom(vendorId) {
  return netPosition(vendorId).headroom_paise;
}

/**
 * Throws unless `grossPaise` of this shop's points may be converted right now.
 * The message tells the customer exactly how many points they CAN convert, because
 * "not allowed" with no number is the most infuriating error a wallet can give.
 */
function assertConversionAllowed(vendor, grossPaise) {
  const position = netPosition(vendor.id);
  if (grossPaise <= position.headroom_paise) return position;

  const allowedPoints = money.maxPointsForPaiseCap(
    position.headroom_paise,
    vendor.redeem_milli_paise_per_point
  );

  if (allowedPoints <= 0) {
    throw new RuleError(
      `${vendor.name} has reached its MelaCoin conversion limit for this period. ` +
        `Your points are safe and can still be spent at ${vendor.name} as usual, ` +
        `and conversion reopens as the limit resets.`
    );
  }
  throw new RuleError(
    `${vendor.name} can only release ${money.formatPaise(position.headroom_paise)} ` +
      `more to MelaCoin this period, which is ${allowedPoints} point(s). ` +
      `Convert ${allowedPoints} or fewer, or spend the rest at ${vendor.name}.`
  );
}

/**
 * The shops that have paid into the network more than they have taken out.
 *
 * This is the fix that costs nobody anything: when a customer is holding MelaCoin and
 * asking "where do I spend this?", put these shops at the top of the list. Footfall
 * flows back to whoever funded it, the imbalance closes on its own, and no customer
 * ever sees a refusal.
 *
 * It is also the same ranking machinery that later sells promoted placement.
 */
function shopsNeedingFootfall(limit = 20, days = config.settlementWindowDays) {
  const since = windowStart(days);
  const rows = db
    .get()
    .prepare(
      `SELECT v.id, v.name, v.slug, v.category, v.city,
              COALESCE(SUM(CASE WHEN s.kind = 'CONVERSION_DEBIT' THEN s.amount_paise ELSE 0 END), 0) AS outflow,
              COALESCE(-SUM(CASE WHEN s.kind = 'MELA_ACCEPTANCE_CREDIT' THEN s.amount_paise ELSE 0 END), 0) AS inflow
       FROM vendors v
       LEFT JOIN vendor_settlements s ON s.vendor_id = v.id AND s.created_at >= ?
       WHERE v.active = 1 AND v.accepts_mela = 1
       GROUP BY v.id`
    )
    .all(since);

  return rows
    .map((row) => ({
      id: row.id,
      name: row.name,
      slug: row.slug,
      category: row.category,
      city: row.city,
      net_outflow_paise: row.outflow - row.inflow,
      display_net_outflow: money.formatPaise(row.outflow - row.inflow),
    }))
    .filter((row) => row.net_outflow_paise > 0)
    .sort((a, b) => b.net_outflow_paise - a.net_outflow_paise)
    .slice(0, limit);
}

/** Every shop's position, worst first - the platform's morning health check. */
function allPositions(days = config.settlementWindowDays) {
  return db
    .get()
    .prepare("SELECT id FROM vendors WHERE active = 1")
    .all()
    .map((row) => netPosition(row.id, days))
    .sort((a, b) => b.used_bps - a.used_bps);
}

module.exports = {
  netPosition,
  conversionHeadroom,
  assertConversionAllowed,
  shopsNeedingFootfall,
  allPositions,
};
