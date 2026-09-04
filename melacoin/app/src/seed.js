"use strict";
/**
 * seed.js - fills the database with a small, believable demo so you can click
 * around immediately. Safe to re-run: it wipes the demo database first.
 *
 *   npm run seed
 */
const fs = require("node:fs");
const config = require("./config");
const db = require("./db");
const auth = require("./auth");
const money = require("./money");
const loyalty = require("./services/loyalty.service");
const conversion = require("./services/conversion.service");
const settings = require("./services/settings.service");

const PASSWORD = "melacoin123";

function reset() {
  db.close();
  for (const suffix of ["", "-wal", "-shm"]) {
    const file = `${config.dbPath}${suffix}`;
    if (fs.existsSync(file)) fs.unlinkSync(file);
  }
  db.open();
}

function createUser({ role, name, email, phone }) {
  const id = db.newId(role === "vendor" ? "ven" : role === "admin" ? "adm" : "cus");
  db.get()
    .prepare("INSERT INTO users (id, email, phone, name, role, password_hash, created_at) VALUES (?,?,?,?,?,?,?)")
    .run(id, email, phone || null, name, role, auth.hashPassword(PASSWORD), db.now());
  return db.get().prepare("SELECT * FROM users WHERE id = ?").get(id);
}

function createVendor(ownerId, shop) {
  const id = db.newId("shp");
  const key = auth.generateApiKey();
  db.get()
    .prepare(
      `INSERT INTO vendors (id, owner_user_id, name, slug, category, city, api_key_hash, api_key_prefix,
                            earn_milli_points_per_rupee, redeem_milli_paise_per_point, min_redeem_points,
                            max_redeem_bps, points_expiry_days, earn_on_net, allow_mela_conversion,
                            mela_conversion_fee_bps, accepts_mela, active, created_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)`
    )
    .run(id, ownerId, shop.name, shop.slug, shop.category, shop.city, key.hash, key.prefix,
         shop.earn, shop.redeem, shop.minRedeem, shop.maxRedeemBps, shop.expiryDays,
         shop.earnOnNet ? 1 : 0, shop.allowConversion ? 1 : 0, shop.feeBps, shop.acceptsMela ? 1 : 0, db.now());
  return { vendor: db.get().prepare("SELECT * FROM vendors WHERE id = ?").get(id), apiKey: key.key };
}

function main() {
  reset();
  settings.setMelaPricePaise(100); // MELA starts at Rs 1.00

  const admin = createUser({ role: "admin", name: "Platform Admin", email: "admin@melacoin.test" });

  // Three shops with deliberately different economics, so the demo shows that each
  // vendor really does control their own rates.
  const shops = [
    {
      owner: { name: "Ramesh Iyer", email: "ramesh@chaicorner.test" },
      shop: {
        name: "Chai Corner", slug: "chai-corner", category: "cafe", city: "Bengaluru",
        // Generous on earning, cheap points: 2 points per rupee, 1 point = 5 paise
        earn: 2000, redeem: 5000, minRedeem: 50, maxRedeemBps: 5000, expiryDays: 180,
        earnOnNet: true, allowConversion: true, feeBps: 200, acceptsMela: true,
      },
    },
    {
      owner: { name: "Priya Nair", email: "priya@freshmart.test" },
      shop: {
        name: "FreshMart Grocery", slug: "freshmart", category: "grocery", city: "Bengaluru",
        // Thin margins: 0.5 points per rupee, but each point is worth 10 paise
        earn: 500, redeem: 10000, minRedeem: 100, maxRedeemBps: 2000, expiryDays: 365,
        earnOnNet: true, allowConversion: true, feeBps: 300, acceptsMela: true,
      },
    },
    {
      owner: { name: "Arjun Mehta", email: "arjun@stitchstyle.test" },
      shop: {
        name: "Stitch & Style", slug: "stitch-style", category: "apparel", city: "Pune",
        // High margin: 5 points per rupee, 1 point = 2 paise, no conversion allowed
        earn: 5000, redeem: 2000, minRedeem: 500, maxRedeemBps: 1000, expiryDays: 730,
        earnOnNet: false, allowConversion: false, feeBps: 0, acceptsMela: false,
      },
    },
  ];

  const created = shops.map(({ owner, shop }) => {
    const user = createUser({ role: "vendor", ...owner });
    return { ...createVendor(user.id, shop), owner: user };
  });

  const customers = [
    createUser({ role: "customer", name: "Anita Sharma", email: "anita@example.test", phone: "9800000001" }),
    createUser({ role: "customer", name: "Vikram Rao", email: "vikram@example.test", phone: "9800000002" }),
    createUser({ role: "customer", name: "Sara Khan", email: "sara@example.test", phone: "9800000003" }),
  ];

  // A month of shopping, so the dashboards have something to show.
  const bills = [
    [0, 0, 45000], [0, 0, 32000], [0, 0, 28000], [0, 1, 19000],
    [1, 0, 250000], [1, 0, 180000], [1, 2, 95000],
    [2, 1, 450000], [2, 2, 320000], [0, 2, 15000], [1, 1, 120000], [0, 0, 22000],
  ];
  let counter = 0;
  for (const [shopIndex, customerIndex, grossPaise] of bills) {
    loyalty.recordPurchase({
      vendor: created[shopIndex].vendor,
      customerId: customers[customerIndex].id,
      grossPaise,
      idempotencyKey: `seed-${++counter}`,
      billRef: `INV-${1000 + counter}`,
    });
  }

  // Anita pays part of a Chai Corner bill with the points she has built up.
  const anitaPoints = loyalty.pointsBalance(customers[0].id, created[0].vendor.id);
  loyalty.recordPurchase({
    vendor: created[0].vendor,
    customerId: customers[0].id,
    grossPaise: 40000,
    pointsToRedeem: Math.min(anitaPoints, 1000),
    idempotencyKey: "seed-redeem-1",
    billRef: "INV-2001",
  });

  // Vikram turns his FreshMart points into MelaCoin.
  const vikramPoints = loyalty.pointsBalance(customers[1].id, created[1].vendor.id);
  if (vikramPoints >= created[1].vendor.min_redeem_points) {
    conversion.convert({
      vendor: created[1].vendor,
      customerId: customers[1].id,
      points: vikramPoints,
    });
  }

  console.log("\n  Demo data ready.\n");
  console.log(`  Password for every account below: ${PASSWORD}\n`);
  console.log(`  Admin     ${admin.email}`);
  for (const entry of created) {
    console.log(`  Vendor    ${entry.owner.email}   (${entry.vendor.name})`);
    console.log(`            POS API key: ${entry.apiKey}`);
  }
  for (const customer of customers) {
    const total = loyalty
      .balancesByVendor(customer.id)
      .reduce((sum, row) => sum + row.value_paise, 0);
    console.log(`  Customer  ${customer.email}   points worth ${money.formatPaise(total)}`);
  }
  console.log(`\n  Start the app with: npm start\n`);
  db.close();
}

main();
