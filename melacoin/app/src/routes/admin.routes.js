"use strict";
/** Platform operator endpoints: the price, the treasury, and who owes whom. */
const db = require("../db");
const v = require("../validate");
const money = require("../money");
const settings = require("../services/settings.service");
const settlement = require("../services/settlement.service");
const loyalty = require("../services/loyalty.service");
const balance = require("../services/balance.service");

function register(router) {
  router.get("/api/admin/stats", async (ctx) => {
    ctx.requireRole("admin");
    return { body: settlement.platformStats(settings.melaPricePaise()) };
  });

  /**
   * Sets the MELA price used for conversions and MELA payments.
   *
   * Before listing, this number is your promise and you should move it slowly and
   * publicly. After listing, feed it from the exchange price instead of typing it,
   * or your app and the market will disagree and arbitrageurs will drain you.
   */
  router.post("/api/admin/mela-price", async (ctx) => {
    const user = ctx.requireRole("admin");
    const paise = v.requiredInt(ctx.body, "mela_price_paise", { min: 1, max: 100_000_00 });
    const previous = settings.melaPricePaise();
    settings.setMelaPricePaise(paise);
    db.get()
      .prepare("INSERT INTO audit_log (id, actor_user_id, action, entity, meta, created_at) VALUES (?,?,?,?,?,?)")
      .run(db.newId("aud"), user.id, "admin.price.set", "settings",
           JSON.stringify({ from: previous, to: paise }), db.now());

    return { body: { mela_price_paise: paise, previous_paise: previous, display: money.formatPaise(paise) } };
  });

  router.get("/api/admin/settlements", async (ctx) => {
    ctx.requireRole("admin");
    return { body: { vendors: settlement.outstandingByVendor() } };
  });

  /** Records that a shop paid the platform (or the platform paid the shop). */
  router.post("/api/admin/settlements/payment", async (ctx) => {
    const user = ctx.requireRole("admin");
    const vendorId = v.requiredString(ctx.body, "vendor_id");
    const amountPaise = v.requiredInt(ctx.body, "amount_paise", { min: 1 });
    const balance = settlement.recordPayment({
      vendorId,
      amountPaise,
      note: v.optionalString(ctx.body, "note", { max: 200 }),
    });
    db.get()
      .prepare("INSERT INTO audit_log (id, actor_user_id, action, entity, entity_id, meta, created_at) VALUES (?,?,?,?,?,?,?)")
      .run(db.newId("aud"), user.id, "admin.settlement.payment", "vendor", vendorId,
           JSON.stringify({ amountPaise }), db.now());

    return { body: { vendor_id: vendorId, balance_paise: balance, display_balance: money.formatPaise(balance) } };
  });

  /** Runs the expiry sweep by hand. It also runs automatically on every balance read. */
  router.post("/api/admin/expire-points", async (ctx) => {
    ctx.requireRole("admin");
    return { body: { points_expired: loyalty.expireDuePoints() } };
  });

  /**
   * Which shops are funding the network and which are riding it.
   * Anything with status "blocked" or "near_limit", or a one-way valve, is a shop
   * about to churn. This is a morning check, not a monthly report.
   */
  router.get("/api/admin/balances", async (ctx) => {
    ctx.requireRole("admin");
    const positions = balance.allPositions();
    return {
      body: {
        positions,
        at_risk: positions.filter((p) => p.status !== "healthy" || p.one_way_valve).length,
        one_way_valves: positions.filter((p) => p.one_way_valve).map((p) => p.vendor_name),
      },
    };
  });

  router.get("/api/admin/audit", async (ctx) => {
    ctx.requireRole("admin");
    return {
      body: {
        entries: db.get().prepare("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 200").all(),
      },
    };
  });
}

module.exports = { register };
