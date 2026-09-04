"use strict";
/**
 * pos.routes.js - the endpoints a shop's billing counter calls.
 *
 * These are authenticated with the shop's API key (header: x-api-key), not with a
 * login. A till, a Zoho/Petpooja plugin, or a shopkeeper's phone app talks to these.
 */
const db = require("../db");
const v = require("../validate");
const money = require("../money");
const { badRequest, notFound } = require("../http");
const loyalty = require("../services/loyalty.service");
const settings = require("../services/settings.service");
const tokenService = require("../services/token.service");

/** Finds the customer by email or phone - whatever the cashier typed. */
function findCustomer(body) {
  const identifier = v.requiredString(body, "customer", { max: 254 });
  const query = identifier.includes("@")
    ? ["SELECT * FROM users WHERE email = ? AND role = 'customer'", identifier.toLowerCase()]
    : ["SELECT * FROM users WHERE phone = ? AND role = 'customer'", identifier];
  const customer = db.get().prepare(query[0]).get(query[1]);
  if (!customer) throw notFound(`No customer found for "${identifier}"`);
  return customer;
}

function register(router) {
  /** Look up a customer's standing at this shop before ringing up a bill. */
  router.get("/api/pos/customer", async (ctx) => {
    const vendor = ctx.requireApiKey();
    const identifier = (ctx.query.get("customer") || "").trim();
    if (!identifier) throw badRequest('Add ?customer=<email or phone>');
    const customer = findCustomer({ customer: identifier });
    const points = loyalty.pointsBalance(customer.id, vendor.id);

    return {
      body: {
        customer: { id: customer.id, name: customer.name, email: customer.email, phone: customer.phone },
        points,
        points_value_paise: money.paiseForPoints(points, vendor.redeem_milli_paise_per_point),
        mela_wei: tokenService.balanceWei(customer.id).toString(),
        mela_price_paise: settings.melaPricePaise(),
      },
    };
  });

  /**
   * Price a bill. Changes nothing - call it as often as you like while the cashier
   * is still typing, then send the identical numbers to /purchase.
   */
  router.post("/api/pos/quote", async (ctx) => {
    const vendor = ctx.requireApiKey();
    const customer = findCustomer(ctx.body);
    return {
      body: loyalty.quotePurchase({
        vendor,
        customerId: customer.id,
        grossPaise: v.requiredInt(ctx.body, "gross_paise", { min: 1 }),
        pointsToRedeem: v.optionalInt(ctx.body, "points_to_redeem") || 0,
        melaWeiToSpend: v.optionalBigInt(ctx.body, "mela_wei_to_spend"),
      }),
    };
  });

  /**
   * Commit the bill. Send an `idempotency_key` that is unique per real-world sale
   * (your bill number works well). Sending it twice returns the first receipt
   * instead of charging the customer twice.
   */
  router.post("/api/pos/purchase", async (ctx) => {
    const vendor = ctx.requireApiKey();
    const customer = findCustomer(ctx.body);

    const result = loyalty.recordPurchase({
      vendor,
      customerId: customer.id,
      grossPaise: v.requiredInt(ctx.body, "gross_paise", { min: 1 }),
      pointsToRedeem: v.optionalInt(ctx.body, "points_to_redeem") || 0,
      melaWeiToSpend: v.optionalBigInt(ctx.body, "mela_wei_to_spend"),
      idempotencyKey: v.requiredString(ctx.body, "idempotency_key", { max: 100 }),
      billRef: v.optionalString(ctx.body, "bill_ref", { max: 100 }),
    });

    const balanceAfter = loyalty.pointsBalance(customer.id, vendor.id);
    return {
      status: result.replayed ? 200 : 201,
      body: {
        replayed: result.replayed,
        receipt: {
          ...result.purchase,
          display: {
            gross: money.formatPaise(result.purchase.gross_paise),
            points_discount: money.formatPaise(result.purchase.points_discount_paise),
            mela_discount: money.formatPaise(result.purchase.mela_discount_paise),
            net: money.formatPaise(result.purchase.net_paise),
          },
        },
        customer: { id: customer.id, name: customer.name, email: customer.email },
        points_balance: balanceAfter,
        points_balance_value_paise: money.paiseForPoints(balanceAfter, vendor.redeem_milli_paise_per_point),
      },
    };
  });
}

module.exports = { register };
