"use strict";
const db = require("../db");
const auth = require("../auth");
const v = require("../validate");
const { badRequest, unauthorized, conflict } = require("../http");
const loyalty = require("../services/loyalty.service");

/** Sensible starting rates for a new shop. The owner can change all of them later. */
const VENDOR_DEFAULTS = {
  earn_milli_points_per_rupee: 1000,    // 1 point per rupee
  redeem_milli_paise_per_point: 10000,  // 1 point = 10 paise, so 100 points = Rs 10
  min_redeem_points: 100,
  max_redeem_bps: 3000,                 // points may pay up to 30% of a bill
  points_expiry_days: 365,
  earn_on_net: 1,
  allow_mela_conversion: 1,
  mela_conversion_fee_bps: 200,
  accepts_mela: 1,
};

function publicUser(user) {
  return {
    id: user.id, name: user.name, email: user.email, phone: user.phone,
    role: user.role, wallet_address: user.wallet_address, created_at: user.created_at,
  };
}

function uniqueSlug(base) {
  let slug = base || "shop";
  let attempt = 1;
  const exists = () => db.get().prepare("SELECT 1 FROM vendors WHERE slug = ?").get(slug);
  while (exists()) slug = `${base}-${++attempt}`;
  return slug;
}

function register(router) {
  router.post("/api/auth/register", async (ctx) => {
    const name = v.requiredString(ctx.body, "name", { max: 80 });
    const email = v.email(ctx.body);
    const password = v.requiredString(ctx.body, "password", { min: 8, max: 200 });
    const phone = v.optionalString(ctx.body, "phone", { max: 20 });
    const role = v.optionalString(ctx.body, "role") || "customer";
    if (!["customer", "vendor"].includes(role)) {
      throw badRequest('"role" must be "customer" or "vendor"');
    }

    if (db.get().prepare("SELECT 1 FROM users WHERE email = ?").get(email)) {
      throw conflict("An account with that email already exists");
    }

    const userId = db.newId(role === "vendor" ? "ven" : "cus");
    let apiKey = null;
    let vendor = null;

    db.transaction((database) => {
      database
        .prepare(
          `INSERT INTO users (id, email, phone, name, role, password_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)`
        )
        .run(userId, email, phone, name, role, auth.hashPassword(password), db.now());

      if (role === "vendor") {
        const shopName = v.requiredString(ctx.body, "shop_name", { max: 80 });
        const generated = auth.generateApiKey();
        apiKey = generated.key;
        const vendorId = db.newId("shp");
        database
          .prepare(
            `INSERT INTO vendors (id, owner_user_id, name, slug, category, city,
                                  api_key_hash, api_key_prefix,
                                  earn_milli_points_per_rupee, redeem_milli_paise_per_point,
                                  min_redeem_points, max_redeem_bps, points_expiry_days, earn_on_net,
                                  allow_mela_conversion, mela_conversion_fee_bps, accepts_mela,
                                  active, created_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)`
          )
          .run(
            vendorId, userId, shopName, uniqueSlug(v.slugify(shopName)),
            v.optionalString(ctx.body, "category", { max: 40 }) || "general",
            v.optionalString(ctx.body, "city", { max: 60 }) || "",
            generated.hash, generated.prefix,
            VENDOR_DEFAULTS.earn_milli_points_per_rupee, VENDOR_DEFAULTS.redeem_milli_paise_per_point,
            VENDOR_DEFAULTS.min_redeem_points, VENDOR_DEFAULTS.max_redeem_bps,
            VENDOR_DEFAULTS.points_expiry_days, VENDOR_DEFAULTS.earn_on_net,
            VENDOR_DEFAULTS.allow_mela_conversion, VENDOR_DEFAULTS.mela_conversion_fee_bps,
            VENDOR_DEFAULTS.accepts_mela, db.now()
          );
        vendor = database.prepare("SELECT * FROM vendors WHERE id = ?").get(vendorId);
      }
    });

    const user = db.get().prepare("SELECT * FROM users WHERE id = ?").get(userId);
    const session = auth.createSession(userId);
    return {
      status: 201,
      body: {
        user: publicUser(user),
        token: session.token,
        expires_at: session.expiresAt,
        vendor: vendor ? loyalty.publicVendor(vendor) : null,
        // Shown exactly once. We only keep a hash of it.
        api_key: apiKey,
      },
    };
  });

  router.post("/api/auth/login", async (ctx) => {
    ctx.throttleAuth();
    const email = v.email(ctx.body);
    const password = v.requiredString(ctx.body, "password", { min: 1, max: 200 });

    const user = db.get().prepare("SELECT * FROM users WHERE email = ?").get(email);
    // Same error and roughly the same work either way, so this cannot be used to
    // discover which email addresses are registered.
    if (!user || !auth.verifyPassword(password, user.password_hash)) {
      throw unauthorized("Email or password is incorrect");
    }

    const session = auth.createSession(user.id);
    const vendor =
      user.role === "vendor"
        ? db.get().prepare("SELECT * FROM vendors WHERE owner_user_id = ?").get(user.id)
        : null;

    return {
      body: {
        user: publicUser(user),
        token: session.token,
        expires_at: session.expiresAt,
        vendor: vendor ? loyalty.publicVendor(vendor) : null,
      },
    };
  });

  router.post("/api/auth/logout", async (ctx) => {
    if (ctx.token) auth.destroySession(ctx.token);
    return { body: { ok: true } };
  });

  router.get("/api/auth/me", async (ctx) => {
    const user = ctx.requireUser();
    const vendor =
      user.role === "vendor"
        ? db.get().prepare("SELECT * FROM vendors WHERE owner_user_id = ?").get(user.id)
        : null;
    return { body: { user: publicUser(user), vendor: vendor ? loyalty.publicVendor(vendor) : null } };
  });
}

module.exports = { register, publicUser, VENDOR_DEFAULTS };
