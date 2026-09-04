"use strict";
/**
 * settlement.service.js - the running account between each shop and the platform.
 *
 * Sign convention, which you should read twice:
 *   POSITIVE = the shop owes the platform.
 *   NEGATIVE = the platform owes the shop.
 *
 * A shop goes positive when its customers convert points into MELA (the shop's
 * obligation moved to us). It goes negative when it accepts MELA as payment (it
 * gave away goods and we hold the tokens). At the end of each cycle you settle the
 * net figure - usually one small bank transfer in one direction.
 */
const db = require("../db");
const { RuleError } = require("../errors");
const money = require("../money");
const tokenService = require("./token.service");

function balancePaise(vendorId) {
  const row = db
    .get()
    .prepare("SELECT COALESCE(SUM(amount_paise), 0) AS balance FROM vendor_settlements WHERE vendor_id = ?")
    .get(vendorId);
  return row.balance;
}

function ledger(vendorId, limit = 100) {
  return db
    .get()
    .prepare("SELECT * FROM vendor_settlements WHERE vendor_id = ? ORDER BY created_at DESC, id DESC LIMIT ?")
    .all(vendorId, limit);
}

/** Records money actually moving between a shop and the platform. */
function recordPayment({ vendorId, amountPaise, note }) {
  if (!Number.isInteger(amountPaise) || amountPaise === 0) {
    throw new RuleError("Payment amount must be a whole number of paise, and not zero");
  }
  db.transaction((database) => {
    database
      .prepare(
        `INSERT INTO vendor_settlements (id, vendor_id, kind, amount_paise, ref_type, note, created_at)
         VALUES (?, ?, 'PAYMENT', ?, 'manual', ?, ?)`
      )
      .run(db.newId("stl"), vendorId, -amountPaise, note || null, db.now());
  });
  return balancePaise(vendorId);
}

/**
 * The numbers you should look at every single morning.
 *
 * `backing_ratio_bps` is the one that decides whether the business is honest:
 * it is the rupees actually collected from shops divided by the rupee value of all
 * the MELA you have issued. Below 10000 (100%) you have issued tokens you cannot
 * cover, which is exactly how loyalty schemes and token projects die.
 */
function platformStats(melaPricePaise) {
  const database = db.get();
  const one = (sql, ...args) => database.prepare(sql).get(...args);

  const conversions = one(
    `SELECT COUNT(*) AS count, COALESCE(SUM(points),0) AS points,
            COALESCE(SUM(gross_paise),0) AS gross, COALESCE(SUM(fee_paise),0) AS fee,
            COALESCE(SUM(net_paise),0) AS net FROM conversions`
  );
  const purchases = one(
    `SELECT COUNT(*) AS count, COALESCE(SUM(gross_paise),0) AS gross,
            COALESCE(SUM(points_discount_paise),0) AS point_discounts,
            COALESCE(SUM(mela_discount_paise),0) AS mela_accepted FROM purchases`
  );
  const livePoints = one(
    `SELECT COALESCE(SUM(points_remaining),0) AS points FROM point_lots
     WHERE points_remaining > 0 AND (expires_at IS NULL OR expires_at > ?)`,
    db.now()
  );
  const vendorCount = one("SELECT COUNT(*) AS count FROM vendors WHERE active = 1").count;
  const customerCount = one("SELECT COUNT(*) AS count FROM users WHERE role = 'customer'").count;

  const custodialWei = tokenService.totalCustodialWei();
  const outstandingValuePaise = money.paiseForMelaWei(custodialWei, melaPricePaise);
  const collectedPaise = conversions.net; // rupees behind the tokens we issued
  const owedToVendorsPaise = purchases.mela_accepted; // paid out when shops took MELA
  const reservePaise = collectedPaise - owedToVendorsPaise;

  return {
    vendors: vendorCount,
    customers: customerCount,
    purchases: purchases.count,
    purchase_volume_paise: purchases.gross,
    point_discounts_paise: purchases.point_discounts,
    live_points: livePoints.points,
    conversions: conversions.count,
    points_converted: conversions.points,
    conversion_gross_paise: conversions.gross,
    platform_fee_paise: conversions.fee,
    mela_outstanding_wei: custodialWei.toString(),
    mela_outstanding_value_paise: outstandingValuePaise,
    mela_accepted_by_vendors_paise: owedToVendorsPaise,
    treasury_reserve_paise: reservePaise,
    backing_ratio_bps:
      outstandingValuePaise === 0 ? 10000 : Math.floor((reservePaise * 10000) / outstandingValuePaise),
    display: {
      purchase_volume: money.formatPaise(purchases.gross),
      platform_fee: money.formatPaise(conversions.fee),
      treasury_reserve: money.formatPaise(reservePaise),
      mela_outstanding: money.formatMela(custodialWei),
      mela_outstanding_value: money.formatPaise(outstandingValuePaise),
    },
  };
}

/** Every shop with a non-zero balance, so you know who to invoice. */
function outstandingByVendor() {
  return db
    .get()
    .prepare(
      `SELECT v.id, v.name, v.slug, COALESCE(SUM(s.amount_paise), 0) AS balance_paise
       FROM vendors v LEFT JOIN vendor_settlements s ON s.vendor_id = v.id
       GROUP BY v.id HAVING balance_paise != 0 ORDER BY balance_paise DESC`
    )
    .all()
    .map((row) => ({ ...row, display_balance: money.formatPaise(row.balance_paise) }));
}

module.exports = { balancePaise, ledger, recordPayment, platformStats, outstandingByVendor };
