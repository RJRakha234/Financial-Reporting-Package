"use strict";
/** Shared test setup: a throwaway in-memory database with one shop and one customer. */
process.env.DB_PATH = ":memory:";
process.env.CHAIN_MODE = "mock";

const db = require("../src/db");
const auth = require("../src/auth");

function freshDb() {
  db.close();
  db.open(":memory:");
  return db.get();
}

function makeUser({ role = "customer", name = "Test Person", email, phone = null } = {}) {
  const id = db.newId(role.slice(0, 3));
  db.get()
    .prepare("INSERT INTO users (id, email, phone, name, role, password_hash, created_at) VALUES (?,?,?,?,?,?,?)")
    .run(id, email || `${id}@example.test`, phone, name, role, auth.hashPassword("melacoin123"), db.now());
  return db.get().prepare("SELECT * FROM users WHERE id = ?").get(id);
}

/** A shop with sane defaults; pass overrides for the rate you want to test. */
function makeVendor(overrides = {}) {
  const owner = makeUser({ role: "vendor" });
  const id = db.newId("shp");
  const key = auth.generateApiKey();
  const settings = {
    name: "Test Shop",
    slug: `shop-${id}`,
    category: "cafe",
    city: "Bengaluru",
    earn_milli_points_per_rupee: 2500,   // 2.5 points per rupee
    redeem_milli_paise_per_point: 10000, // 1 point = 10 paise
    min_redeem_points: 0,
    max_redeem_bps: 5000,
    points_expiry_days: 365,
    earn_on_net: 1,
    allow_mela_conversion: 1,
    mela_conversion_fee_bps: 200,
    accepts_mela: 1,
    ...overrides,
  };

  db.get()
    .prepare(
      `INSERT INTO vendors (id, owner_user_id, name, slug, category, city, api_key_hash, api_key_prefix,
                            earn_milli_points_per_rupee, redeem_milli_paise_per_point, min_redeem_points,
                            max_redeem_bps, points_expiry_days, earn_on_net, allow_mela_conversion,
                            mela_conversion_fee_bps, accepts_mela, active, created_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)`
    )
    .run(id, owner.id, settings.name, settings.slug, settings.category, settings.city, key.hash, key.prefix,
         settings.earn_milli_points_per_rupee, settings.redeem_milli_paise_per_point, settings.min_redeem_points,
         settings.max_redeem_bps, settings.points_expiry_days, settings.earn_on_net,
         settings.allow_mela_conversion, settings.mela_conversion_fee_bps, settings.accepts_mela, db.now());

  return { vendor: db.get().prepare("SELECT * FROM vendors WHERE id = ?").get(id), owner, apiKey: key.key };
}

module.exports = { freshDb, makeUser, makeVendor, db };
