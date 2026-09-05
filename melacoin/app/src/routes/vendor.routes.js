"use strict";
/** Endpoints for a shop owner logged into the vendor dashboard. */
const db = require("../db");
const v = require("../validate");
const money = require("../money");
const auth = require("../auth");
const { badRequest } = require("../http");
const loyalty = require("../services/loyalty.service");
const settlement = require("../services/settlement.service");
const balance = require("../services/balance.service");

/**
 * Every rate a vendor may change, with the limits that stop a typo becoming a
 * disaster. A shop that fat-fingers "1000 points per rupee" would hand out its
 * whole margin before anyone noticed, so the ceiling is enforced here.
 */
const EDITABLE = {
  name: { kind: "string", max: 80 },
  category: { kind: "string", max: 40 },
  city: { kind: "string", max: 60 },
  earn_milli_points_per_rupee: { kind: "int", min: 0, max: 100000 },   // <= 100 points per rupee
  redeem_milli_paise_per_point: { kind: "int", min: 1, max: 1000000 }, // <= Rs 10 per point
  min_redeem_points: { kind: "int", min: 0, max: 1000000 },
  max_redeem_bps: { kind: "int", min: 0, max: 10000 },
  points_expiry_days: { kind: "int", min: 0, max: 3650 },
  mela_conversion_fee_bps: { kind: "int", min: 0, max: 5000 },         // <= 50% spread
  // The shop's own ceiling on how much of its point value may leave as MelaCoin
  // in a window. 0 means "no ceiling of my own" - the platform's cap still applies.
  conversion_budget_paise: { kind: "int", min: 0, max: 100000000 },
  earn_on_net: { kind: "bool" },
  allow_mela_conversion: { kind: "bool" },
  accepts_mela: { kind: "bool" },
  active: { kind: "bool" },
};

function register(router) {
  router.get("/api/vendor/me", async (ctx) => {
    const { vendor } = ctx.requireVendor();
    const stats = db
      .get()
      .prepare(
        `SELECT COUNT(*) AS purchases, COALESCE(SUM(gross_paise),0) AS gross,
                COALESCE(SUM(points_earned),0) AS points_issued,
                COALESCE(SUM(points_redeemed),0) AS points_redeemed,
                COALESCE(SUM(points_discount_paise),0) AS discounts,
                COUNT(DISTINCT customer_id) AS customers
         FROM purchases WHERE vendor_id = ? AND voided_at IS NULL`
      )
      .get(vendor.id);

    const outstanding = db
      .get()
      .prepare(
        `SELECT COALESCE(SUM(points_remaining),0) AS points FROM point_lots
         WHERE vendor_id = ? AND points_remaining > 0 AND (expires_at IS NULL OR expires_at > ?)`
      )
      .get(vendor.id, db.now()).points;

    const liabilityPaise = money.paiseForPoints(outstanding, vendor.redeem_milli_paise_per_point);
    const balancePaise = settlement.balancePaise(vendor.id);

    return {
      body: {
        vendor: loyalty.publicVendor(vendor),
        api_key_prefix: vendor.api_key_prefix,
        stats: {
          ...stats,
          outstanding_points: outstanding,
          // What it would cost if every live point were redeemed tomorrow. Shops
          // should watch this the way they watch a gift-card balance.
          outstanding_liability_paise: liabilityPaise,
          settlement_balance_paise: balancePaise,
          display: {
            gross: money.formatPaise(stats.gross),
            discounts: money.formatPaise(stats.discounts),
            outstanding_liability: money.formatPaise(liabilityPaise),
            settlement_balance: money.formatPaise(balancePaise),
            settlement_direction:
              balancePaise > 0 ? "You owe the platform" : balancePaise < 0 ? "The platform owes you" : "Settled",
          },
        },
      },
    };
  });

  router.patch("/api/vendor/settings", async (ctx) => {
    const { vendor } = ctx.requireVendor();
    const updates = [];
    const values = [];

    for (const [field, rule] of Object.entries(EDITABLE)) {
      if (!(field in ctx.body)) continue;
      let value;
      if (rule.kind === "string") value = v.requiredString(ctx.body, field, { max: rule.max });
      else if (rule.kind === "int") value = v.requiredInt(ctx.body, field, { min: rule.min, max: rule.max });
      else value = v.optionalBool(ctx.body, field) ? 1 : 0;
      updates.push(`${field} = ?`);
      values.push(value);
    }
    if (updates.length === 0) throw badRequest("Nothing to update");

    db.get().prepare(`UPDATE vendors SET ${updates.join(", ")} WHERE id = ?`).run(...values, vendor.id);
    db.get()
      .prepare("INSERT INTO audit_log (id, actor_user_id, action, entity, entity_id, meta, created_at) VALUES (?,?,?,?,?,?,?)")
      .run(db.newId("aud"), ctx.user.id, "vendor.settings.update", "vendor", vendor.id, JSON.stringify(ctx.body), db.now());

    const updated = db.get().prepare("SELECT * FROM vendors WHERE id = ?").get(vendor.id);
    return { body: { vendor: loyalty.publicVendor(updated) } };
  });

  /** Issues a brand-new POS key and immediately invalidates the old one. */
  router.post("/api/vendor/api-key", async (ctx) => {
    const { vendor } = ctx.requireVendor();
    const generated = auth.generateApiKey();
    db.get()
      .prepare("UPDATE vendors SET api_key_hash = ?, api_key_prefix = ? WHERE id = ?")
      .run(generated.hash, generated.prefix, vendor.id);
    db.get()
      .prepare("INSERT INTO audit_log (id, actor_user_id, action, entity, entity_id, meta, created_at) VALUES (?,?,?,?,?,?,?)")
      .run(db.newId("aud"), ctx.user.id, "vendor.apikey.rotate", "vendor", vendor.id, null, db.now());

    return { body: { api_key: generated.key, note: "Copy this now. It is not shown again." } };
  });

  /** Am I funding the rest of the network? The number a shop owner should watch. */
  router.get("/api/vendor/balance", async (ctx) => {
    const { vendor } = ctx.requireVendor();
    const position = balance.netPosition(vendor.id);
    return {
      body: {
        ...position,
        explanation:
          position.net_outflow_paise <= 0
            ? "You have taken in at least as much MelaCoin as your customers have converted away. You are a net beneficiary of the network."
            : position.status === "blocked"
              ? "Your customers have converted away more than your limit allows, so conversion of your points is paused. Points can still be redeemed in your shop as normal. Accepting MelaCoin restores headroom immediately."
              : "Your customers have converted more of your points away than you have taken back in MelaCoin. Accepting MelaCoin brings this back towards zero.",
      },
    };
  });

  router.get("/api/vendor/purchases", async (ctx) => {
    const { vendor } = ctx.requireVendor();
    const rows = db
      .get()
      .prepare(
        `SELECT p.*, u.name AS customer_name, u.email AS customer_email
         FROM purchases p JOIN users u ON u.id = p.customer_id
         WHERE p.vendor_id = ? ORDER BY p.created_at DESC LIMIT 100`
      )
      .all(vendor.id);
    return {
      body: {
        purchases: rows.map((row) => ({
          ...row,
          display: { gross: money.formatPaise(row.gross_paise), net: money.formatPaise(row.net_paise) },
        })),
      },
    };
  });

  router.get("/api/vendor/customers", async (ctx) => {
    const { vendor } = ctx.requireVendor();
    const rows = db
      .get()
      .prepare(
        `SELECT u.id, u.name, u.email,
                COALESCE(SUM(CASE WHEN l.points_remaining > 0 AND (l.expires_at IS NULL OR l.expires_at > ?)
                                  THEN l.points_remaining ELSE 0 END), 0) AS points,
                (SELECT COUNT(*) FROM purchases p WHERE p.vendor_id = l.vendor_id AND p.customer_id = u.id
                        AND p.voided_at IS NULL) AS visits
         FROM point_lots l JOIN users u ON u.id = l.customer_id
         WHERE l.vendor_id = ? GROUP BY u.id ORDER BY points DESC LIMIT 200`
      )
      .all(db.now(), vendor.id);
    return {
      body: {
        customers: rows.map((row) => ({
          ...row,
          points_value_paise: money.paiseForPoints(row.points, vendor.redeem_milli_paise_per_point),
        })),
      },
    };
  });

  router.get("/api/vendor/settlement", async (ctx) => {
    const { vendor } = ctx.requireVendor();
    const balance = settlement.balancePaise(vendor.id);
    return {
      body: {
        balance_paise: balance,
        display_balance: money.formatPaise(balance),
        explanation:
          balance > 0
            ? "Customers converted your points into MelaCoin. That liability moved to the platform, so this amount is payable to the platform."
            : balance < 0
              ? "You accepted MelaCoin as payment. The platform owes you this amount in rupees."
              : "Nothing outstanding in either direction.",
        entries: settlement.ledger(vendor.id).map((entry) => ({
          ...entry,
          display_amount: money.formatPaise(entry.amount_paise),
        })),
      },
    };
  });
}

module.exports = { register, EDITABLE };
