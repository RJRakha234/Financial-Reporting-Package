"use strict";
/**
 * conversion.service.js - turning shop points into MelaCoin.
 *
 * This is the single most important function in the product, because it is the
 * moment a promise made by one shop becomes a token that works everywhere. Three
 * things happen at once, and all three must happen or none of them:
 *
 *   1. The customer's points at that shop are destroyed.
 *   2. The customer is credited MELA in their app wallet.
 *   3. The shop is billed. Its debt to the customer just moved onto the platform's
 *      books, so the shop owes the platform the rupee value of those points.
 *
 * Step 3 is the one every beginner forgets. Skip it and you are quietly funding
 * every shop's marketing budget out of your own pocket. See docs/04-money-math.md.
 */
const db = require("../db");
const { RuleError } = require("../errors");
const money = require("../money");
const settings = require("./settings.service");
const tokenService = require("./token.service");
const loyalty = require("./loyalty.service");
const balance = require("./balance.service");

/** Shows what a conversion would produce, without doing it. */
function quoteConversion({ vendor, customerId, points }) {
  if (!vendor.active) throw new RuleError(`${vendor.name} is not active`);
  if (!vendor.allow_mela_conversion) {
    throw new RuleError(`${vendor.name} has not enabled MelaCoin conversion`);
  }
  const amount = Number(points);
  if (!Number.isInteger(amount) || amount <= 0) throw new RuleError("Enter a whole number of points");

  const available = loyalty.pointsBalance(customerId, vendor.id);
  if (amount > available) throw new RuleError(`Only ${available} point(s) available at ${vendor.name}`);
  if (amount < vendor.min_redeem_points) {
    throw new RuleError(`${vendor.name} requires at least ${vendor.min_redeem_points} points to convert`);
  }

  const pricePaise = settings.melaPricePaise();
  const grossPaise = money.paiseForPoints(amount, vendor.redeem_milli_paise_per_point);
  const feePaise = money.applyBps(grossPaise, vendor.mela_conversion_fee_bps);
  const netPaise = grossPaise - feePaise;
  const melaWei = money.melaWeiForPaise(netPaise, pricePaise);

  if (melaWei === 0n) {
    throw new RuleError("That is too few points to make even a fraction of a MELA");
  }

  // The shop must not be bled dry funding footfall for the rest of the network.
  // Throws with the exact number of points that WOULD be allowed right now.
  const position = balance.assertConversionAllowed(vendor, grossPaise);

  return {
    vendor: loyalty.publicVendor(vendor),
    points: amount,
    gross_paise: grossPaise,
    fee_bps: vendor.mela_conversion_fee_bps,
    fee_paise: feePaise,
    net_paise: netPaise,
    mela_price_paise: pricePaise,
    mela_wei: melaWei.toString(),
    points_after: available - amount,
    vendor_headroom_paise: position.headroom_paise - grossPaise,
    vendor_headroom_used_bps: position.used_bps,
    display: {
      gross: money.formatPaise(grossPaise),
      fee: money.formatPaise(feePaise),
      net: money.formatPaise(netPaise),
      mela: money.formatMela(melaWei),
      price: money.formatPaise(pricePaise),
    },
  };
}

/** Performs the conversion. All three steps commit together or not at all. */
function convert({ vendor, customerId, points }) {
  const quote = quoteConversion({ vendor, customerId, points });
  const conversionId = db.newId("cnv");
  const at = db.now();

  db.transaction((database) => {
    // 1. Destroy the points.
    loyalty.consumeLots(database, {
      customerId, vendorId: vendor.id, points: quote.points,
      kind: "CONVERT", refType: "conversion", refId: conversionId,
      note: `converted to ${quote.display.mela} MELA`, at,
    });

    database
      .prepare(
        `INSERT INTO conversions (id, customer_id, vendor_id, points, gross_paise, fee_paise,
                                  net_paise, mela_price_paise, mela_wei, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
      )
      .run(conversionId, customerId, vendor.id, quote.points, quote.gross_paise, quote.fee_paise,
           quote.net_paise, quote.mela_price_paise, quote.mela_wei, at);

    // 2. Credit MELA to the customer's app wallet.
    tokenService.post({
      customerId, kind: "CONVERT_IN", weiDelta: quote.mela_wei,
      refType: "conversion", refId: conversionId,
      note: `${quote.points} points from ${vendor.name}`,
    });

    // 3. Bill the shop for the liability it just handed over.
    loyalty.recordSettlement(database, {
      vendorId: vendor.id, kind: "CONVERSION_DEBIT", amountPaise: quote.gross_paise,
      refType: "conversion", refId: conversionId,
      note: `${quote.points} points converted to MELA`, at,
    });
  });

  return { id: conversionId, ...quote };
}

function historyFor(customerId, limit = 50) {
  return db
    .get()
    .prepare(
      `SELECT c.*, v.name AS vendor_name, v.slug AS vendor_slug
       FROM conversions c JOIN vendors v ON v.id = c.vendor_id
       WHERE c.customer_id = ? ORDER BY c.created_at DESC LIMIT ?`
    )
    .all(customerId, limit);
}

module.exports = { quoteConversion, convert, historyFor };
