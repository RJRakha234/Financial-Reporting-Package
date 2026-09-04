"use strict";
/**
 * money.js - every number in MelaCoin, in one place.
 *
 * THE ONE RULE: never store money or points as a decimal (2.5, 19.99). Floating
 * point maths silently loses fractions of a paisa, and a loyalty system that loses
 * fractions is a loyalty system that gets sued. Everything here is a whole number.
 *
 * Units used across the whole codebase:
 *   paise                    money. 100 paise = 1 rupee. Always an integer.
 *   points                   loyalty points. Always a whole integer.
 *   milliPointsPerRupee      vendor earn rate x 1000.  2.5 points per Rs 1 -> 2500
 *   milliPaisePerPoint       vendor redeem rate x 1000. 1 point = 10 paise -> 10000
 *   bps ("basis points")     percentages x 100.  5% -> 500, 100% -> 10000
 *   wei                      MELA token amount x 10^18 (how ERC-20 tokens count)
 *
 * Rounding is always DOWN for what we give out and UP for what we ask for, so a
 * rounding error can never create value out of thin air.
 */

const WEI_PER_MELA = 10n ** 18n;
const BPS_DENOMINATOR = 10000;

/** Throws unless `value` is a whole number that is not negative. */
function assertWholeNonNegative(value, label) {
  if (!Number.isInteger(value) || value < 0) {
    throw new TypeError(`${label} must be a whole number >= 0, received ${value}`);
  }
}

/**
 * Points a customer earns for spending `paise` at a vendor.
 * Rounded DOWN - a customer never earns a fraction of a point.
 *
 *   Rs 100 (10000 paise) at 2.5 points/rupee (2500) -> 250 points
 */
function pointsForSpend(paise, milliPointsPerRupee) {
  assertWholeNonNegative(paise, "paise");
  assertWholeNonNegative(milliPointsPerRupee, "milliPointsPerRupee");
  // paise / 100 = rupees, milliPointsPerRupee / 1000 = points per rupee
  return Math.floor((paise * milliPointsPerRupee) / 100000);
}

/**
 * Rupee value (in paise) of `points` at a vendor's redeem rate.
 * Rounded DOWN - the discount never exceeds what the points are worth.
 *
 *   250 points at 10 paise/point (10000) -> 2500 paise = Rs 25
 */
function paiseForPoints(points, milliPaisePerPoint) {
  assertWholeNonNegative(points, "points");
  assertWholeNonNegative(milliPaisePerPoint, "milliPaisePerPoint");
  return Math.floor((points * milliPaisePerPoint) / 1000);
}

/**
 * How many points are needed to cover `paise`.
 * Rounded UP - we must never hand out a bigger discount than the points paid for.
 */
function pointsForPaise(paise, milliPaisePerPoint) {
  assertWholeNonNegative(paise, "paise");
  if (!Number.isInteger(milliPaisePerPoint) || milliPaisePerPoint <= 0) {
    throw new TypeError(`milliPaisePerPoint must be a whole number > 0, received ${milliPaisePerPoint}`);
  }
  return Math.ceil((paise * 1000) / milliPaisePerPoint);
}

/**
 * The most points that can be applied without the discount exceeding `paise`.
 * Rounded DOWN, so the resulting discount is always within the cap.
 */
function maxPointsForPaiseCap(paise, milliPaisePerPoint) {
  assertWholeNonNegative(paise, "paise");
  if (!Number.isInteger(milliPaisePerPoint) || milliPaisePerPoint <= 0) {
    throw new TypeError(`milliPaisePerPoint must be a whole number > 0, received ${milliPaisePerPoint}`);
  }
  return Math.floor((paise * 1000) / milliPaisePerPoint);
}

/** A percentage of an amount, in basis points. 500 bps of Rs 100 = Rs 5. Rounds DOWN. */
function applyBps(paise, bps) {
  assertWholeNonNegative(paise, "paise");
  assertWholeNonNegative(bps, "bps");
  return Math.floor((paise * bps) / BPS_DENOMINATOR);
}

/**
 * The most a customer may pay with points on this bill.
 * The lower of (a) the vendor's percentage cap and (b) the bill itself.
 */
function maxRedeemablePaise(grossPaise, maxRedeemBps) {
  return Math.min(grossPaise, applyBps(grossPaise, maxRedeemBps));
}

/** Convert rupees (paise) into MELA, in wei. Rounds DOWN. `pricePaise` = price of 1 MELA. */
function melaWeiForPaise(paise, pricePaise) {
  assertWholeNonNegative(paise, "paise");
  if (!Number.isInteger(pricePaise) || pricePaise <= 0) {
    throw new TypeError(`pricePaise must be a whole number > 0, received ${pricePaise}`);
  }
  return (BigInt(paise) * WEI_PER_MELA) / BigInt(pricePaise);
}

/** Convert MELA (wei) into rupees (paise). Rounds DOWN. */
function paiseForMelaWei(wei, pricePaise) {
  const amount = BigInt(wei);
  if (amount < 0n) throw new TypeError("wei must be >= 0");
  if (!Number.isInteger(pricePaise) || pricePaise <= 0) {
    throw new TypeError(`pricePaise must be a whole number > 0, received ${pricePaise}`);
  }
  return Number((amount * BigInt(pricePaise)) / WEI_PER_MELA);
}

/** "123456" -> "Rs 1,234.56" using the Indian digit grouping (1,23,456). */
function formatPaise(paise) {
  const negative = paise < 0;
  const absolute = Math.abs(paise);
  const rupees = Math.floor(absolute / 100);
  const remainder = String(absolute % 100).padStart(2, "0");
  return `${negative ? "-" : ""}₹${groupIndian(rupees)}.${remainder}`;
}

/** 1234567 -> "12,34,567" (lakh/crore grouping). */
function groupIndian(wholeNumber) {
  const digits = String(wholeNumber);
  if (digits.length <= 3) return digits;
  const lastThree = digits.slice(-3);
  const rest = digits.slice(0, -3);
  return `${rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",")},${lastThree}`;
}

/** wei -> a readable MELA string, trimmed to `decimals` places. */
function formatMela(wei, decimals = 4) {
  const amount = BigInt(wei);
  const negative = amount < 0n;
  const absolute = negative ? -amount : amount;
  const whole = absolute / WEI_PER_MELA;
  const fraction = (absolute % WEI_PER_MELA).toString().padStart(18, "0").slice(0, decimals).replace(/0+$/, "");
  return `${negative ? "-" : ""}${groupIndian(Number(whole))}${fraction ? `.${fraction}` : ""}`;
}

/** Human-friendly rate strings for the UI, derived from the stored integers. */
function describeEarnRate(milliPointsPerRupee) {
  return `${milliPointsPerRupee / 1000} point(s) per ₹1 spent`;
}

function describeRedeemRate(milliPaisePerPoint) {
  return `1 point = ${formatPaise(milliPaisePerPoint / 1000)}`;
}

module.exports = {
  WEI_PER_MELA,
  BPS_DENOMINATOR,
  pointsForSpend,
  paiseForPoints,
  pointsForPaise,
  maxPointsForPaiseCap,
  applyBps,
  maxRedeemablePaise,
  melaWeiForPaise,
  paiseForMelaWei,
  formatPaise,
  formatMela,
  groupIndian,
  describeEarnRate,
  describeRedeemRate,
};
