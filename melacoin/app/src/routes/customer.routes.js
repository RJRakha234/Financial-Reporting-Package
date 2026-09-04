"use strict";
const db = require("../db");
const v = require("../validate");
const money = require("../money");
const loyalty = require("../services/loyalty.service");
const tokenService = require("../services/token.service");
const settings = require("../services/settings.service");

function register(router) {
  /** Everything the customer's home screen needs, in one call. */
  router.get("/api/customer/summary", async (ctx) => {
    const user = ctx.requireRole("customer");
    const byVendor = loyalty.balancesByVendor(user.id);
    const pointsValuePaise = byVendor.reduce((total, entry) => total + entry.value_paise, 0);
    const melaWei = tokenService.balanceWei(user.id);
    const pricePaise = settings.melaPricePaise();

    return {
      body: {
        vendors: byVendor,
        total_points: byVendor.reduce((total, entry) => total + entry.points, 0),
        total_points_value_paise: pointsValuePaise,
        mela_wei: melaWei.toString(),
        mela_value_paise: money.paiseForMelaWei(melaWei, pricePaise),
        mela_price_paise: pricePaise,
        wallet_address: user.wallet_address,
        display: {
          points_value: money.formatPaise(pointsValuePaise),
          mela: money.formatMela(melaWei),
          mela_value: money.formatPaise(money.paiseForMelaWei(melaWei, pricePaise)),
          mela_price: money.formatPaise(pricePaise),
        },
      },
    };
  });

  /** The point lots at one shop, in the order they will be spent. */
  router.get("/api/customer/points/:vendorId", async (ctx) => {
    const user = ctx.requireRole("customer");
    const vendor = db.get().prepare("SELECT * FROM vendors WHERE id = ?").get(ctx.params.vendorId);
    if (!vendor) throw require("../http").notFound("Shop not found");

    const lots = loyalty.activeLots(user.id, vendor.id);
    return {
      body: {
        vendor: loyalty.publicVendor(vendor),
        balance: lots.reduce((total, lot) => total + lot.points_remaining, 0),
        lots: lots.map((lot) => ({
          ...lot,
          value_paise: money.paiseForPoints(lot.points_remaining, vendor.redeem_milli_paise_per_point),
        })),
      },
    };
  });

  /** One merged timeline of purchases, point movements and conversions. */
  router.get("/api/customer/history", async (ctx) => {
    const user = ctx.requireRole("customer");
    const purchases = db
      .get()
      .prepare(
        `SELECT p.*, v.name AS vendor_name FROM purchases p
         JOIN vendors v ON v.id = p.vendor_id
         WHERE p.customer_id = ? ORDER BY p.created_at DESC LIMIT 50`
      )
      .all(user.id);
    const events = db
      .get()
      .prepare(
        `SELECT e.*, v.name AS vendor_name FROM point_events e
         JOIN vendors v ON v.id = e.vendor_id
         WHERE e.customer_id = ? ORDER BY e.created_at DESC LIMIT 100`
      )
      .all(user.id);

    return {
      body: {
        purchases: purchases.map((p) => ({
          ...p,
          display: { gross: money.formatPaise(p.gross_paise), net: money.formatPaise(p.net_paise) },
        })),
        point_events: events,
        conversions: require("../services/conversion.service").historyFor(user.id),
        mela_ledger: tokenService.history(user.id),
      },
    };
  });

  /** Save the customer's own blockchain wallet address, used for withdrawals. */
  router.patch("/api/customer/profile", async (ctx) => {
    const user = ctx.requireRole("customer");
    const address = v.optionalString(ctx.body, "wallet_address", { pattern: /^0x[0-9a-fA-F]{40}$/, max: 42 });
    const phone = v.optionalString(ctx.body, "phone", { max: 20 });

    db.get()
      .prepare("UPDATE users SET wallet_address = COALESCE(?, wallet_address), phone = COALESCE(?, phone) WHERE id = ?")
      .run(address, phone, user.id);

    const updated = db.get().prepare("SELECT * FROM users WHERE id = ?").get(user.id);
    return { body: { user: require("./auth.routes").publicUser(updated) } };
  });
}

module.exports = { register };
