"use strict";
/**
 * db.js - the database. SQLite, built into Node, so there is nothing to install.
 *
 * Two conventions worth knowing before you read the schema:
 *
 * 1. Money is stored in paise as INTEGER; token amounts are stored as TEXT holding
 *    a decimal "wei" string. Tokens use 18 decimals, which overflows a JavaScript
 *    number, so we keep them as text and do the maths with BigInt.
 *
 * 2. Balances are never stored as a single mutable number. Points live in "lots"
 *    (each earning creates a lot with its own expiry) and MELA lives in an
 *    append-only ledger. A balance is always the SUM of history. This is how real
 *    accounting systems work: you can always answer "why is my balance this?".
 */
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { DatabaseSync } = require("node:sqlite");
const config = require("./config");

const SCHEMA = `
-- People. A user is a customer, a vendor owner, or an admin.
CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE,
  phone         TEXT,
  name          TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('customer','vendor','admin')),
  password_hash TEXT NOT NULL,
  wallet_address TEXT,
  created_at    TEXT NOT NULL
);

-- Logged-in browser sessions. We store only a hash of the token, never the token.
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- Shops. Every rate a vendor can tune lives here.
CREATE TABLE IF NOT EXISTS vendors (
  id            TEXT PRIMARY KEY,
  owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  slug          TEXT NOT NULL UNIQUE,
  category      TEXT NOT NULL DEFAULT 'general',
  city          TEXT NOT NULL DEFAULT '',
  api_key_hash  TEXT,
  api_key_prefix TEXT,

  -- earn: points per rupee, x1000.  2500 = 2.5 points per rupee
  earn_milli_points_per_rupee INTEGER NOT NULL DEFAULT 1000,
  -- redeem: paise per point, x1000. 10000 = 1 point is worth 10 paise
  redeem_milli_paise_per_point INTEGER NOT NULL DEFAULT 10000,
  min_redeem_points   INTEGER NOT NULL DEFAULT 100,
  max_redeem_bps      INTEGER NOT NULL DEFAULT 3000,  -- points may pay at most 30% of a bill
  points_expiry_days  INTEGER NOT NULL DEFAULT 365,   -- 0 = points never expire
  earn_on_net         INTEGER NOT NULL DEFAULT 1,     -- earn on cash paid, not on the discounted part

  allow_mela_conversion   INTEGER NOT NULL DEFAULT 1,
  mela_conversion_fee_bps INTEGER NOT NULL DEFAULT 200, -- 2% spread when points become MELA
  accepts_mela            INTEGER NOT NULL DEFAULT 1,

  -- Protection against becoming a net donor to the network. See balance.service.js.
  -- A shop's customers may only convert points away faster than the shop takes
  -- MelaCoin in, up to this much of the shop's OWN sales in the rolling window.
  net_outflow_tolerance_bps INTEGER NOT NULL DEFAULT 100,    -- 1% of own sales
  net_outflow_floor_paise   INTEGER NOT NULL DEFAULT 50000,  -- but never less than Rs 500
  conversion_budget_paise   INTEGER NOT NULL DEFAULT 0,      -- shop's own cap; 0 = no extra cap

  active     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,

  CHECK (earn_milli_points_per_rupee >= 0),
  CHECK (redeem_milli_paise_per_point > 0),
  CHECK (max_redeem_bps BETWEEN 0 AND 10000),
  CHECK (mela_conversion_fee_bps BETWEEN 0 AND 10000),
  CHECK (points_expiry_days >= 0),
  CHECK (net_outflow_tolerance_bps BETWEEN 0 AND 10000),
  CHECK (net_outflow_floor_paise >= 0),
  CHECK (conversion_budget_paise >= 0)
);
CREATE INDEX IF NOT EXISTS idx_vendors_owner ON vendors(owner_user_id);

-- One row per bill settled at a shop.
CREATE TABLE IF NOT EXISTS purchases (
  id                    TEXT PRIMARY KEY,
  vendor_id             TEXT NOT NULL REFERENCES vendors(id),
  customer_id           TEXT NOT NULL REFERENCES users(id),
  idempotency_key       TEXT NOT NULL,
  gross_paise           INTEGER NOT NULL,
  points_redeemed       INTEGER NOT NULL DEFAULT 0,
  points_discount_paise INTEGER NOT NULL DEFAULT 0,
  mela_wei_paid         TEXT NOT NULL DEFAULT '0',
  mela_discount_paise   INTEGER NOT NULL DEFAULT 0,
  net_paise             INTEGER NOT NULL,
  points_earned         INTEGER NOT NULL DEFAULT 0,
  bill_ref              TEXT,
  created_at            TEXT NOT NULL,
  UNIQUE (vendor_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_purchases_customer ON purchases(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_purchases_vendor ON purchases(vendor_id, created_at DESC);

-- Points are held in dated lots so that expiry is oldest-first and provable.
CREATE TABLE IF NOT EXISTS point_lots (
  id              TEXT PRIMARY KEY,
  vendor_id       TEXT NOT NULL REFERENCES vendors(id),
  customer_id     TEXT NOT NULL REFERENCES users(id),
  purchase_id     TEXT REFERENCES purchases(id),
  points_earned   INTEGER NOT NULL,
  points_remaining INTEGER NOT NULL,
  earned_at       TEXT NOT NULL,
  expires_at      TEXT,
  CHECK (points_remaining >= 0),
  CHECK (points_remaining <= points_earned)
);
CREATE INDEX IF NOT EXISTS idx_lots_balance ON point_lots(customer_id, vendor_id, expires_at);

-- Append-only history of every point movement. Never updated, never deleted.
CREATE TABLE IF NOT EXISTS point_events (
  id           TEXT PRIMARY KEY,
  vendor_id    TEXT NOT NULL REFERENCES vendors(id),
  customer_id  TEXT NOT NULL REFERENCES users(id),
  kind         TEXT NOT NULL CHECK (kind IN ('EARN','REDEEM','EXPIRE','CONVERT','ADJUST')),
  points_delta INTEGER NOT NULL,
  lot_id       TEXT REFERENCES point_lots(id),
  ref_type     TEXT,
  ref_id       TEXT,
  note         TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_point_events_customer ON point_events(customer_id, created_at DESC);

-- Points turned into MELA.
CREATE TABLE IF NOT EXISTS conversions (
  id               TEXT PRIMARY KEY,
  customer_id      TEXT NOT NULL REFERENCES users(id),
  vendor_id        TEXT NOT NULL REFERENCES vendors(id),
  points           INTEGER NOT NULL,
  gross_paise      INTEGER NOT NULL,
  fee_paise        INTEGER NOT NULL,
  net_paise        INTEGER NOT NULL,
  mela_price_paise INTEGER NOT NULL,
  mela_wei         TEXT NOT NULL,
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversions_customer ON conversions(customer_id, created_at DESC);

-- Append-only MELA ledger. A customer's balance is the SUM of wei_delta.
CREATE TABLE IF NOT EXISTS mela_ledger (
  id          TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL REFERENCES users(id),
  kind        TEXT NOT NULL CHECK (kind IN ('CONVERT_IN','SPEND','WITHDRAW','DEPOSIT','ADJUST')),
  wei_delta   TEXT NOT NULL,
  ref_type    TEXT,
  ref_id      TEXT,
  note        TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mela_ledger_customer ON mela_ledger(customer_id, created_at DESC);

-- Requests to move custodial MELA onto the public blockchain.
CREATE TABLE IF NOT EXISTS withdrawals (
  id          TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL REFERENCES users(id),
  wei         TEXT NOT NULL,
  to_address  TEXT NOT NULL,
  nonce       TEXT NOT NULL UNIQUE,
  deadline    INTEGER NOT NULL,
  signature   TEXT,
  status      TEXT NOT NULL CHECK (status IN ('signed','claimed','expired','failed')),
  tx_hash     TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_withdrawals_customer ON withdrawals(customer_id, created_at DESC);

-- Who owes whom. Positive = the vendor owes the platform treasury.
CREATE TABLE IF NOT EXISTS vendor_settlements (
  id           TEXT PRIMARY KEY,
  vendor_id    TEXT NOT NULL REFERENCES vendors(id),
  kind         TEXT NOT NULL CHECK (kind IN ('CONVERSION_DEBIT','MELA_ACCEPTANCE_CREDIT','PAYMENT','ADJUST')),
  amount_paise INTEGER NOT NULL,
  ref_type     TEXT,
  ref_id       TEXT,
  note         TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settlements_vendor ON vendor_settlements(vendor_id, created_at DESC);

-- Platform-wide key/value settings, e.g. the current MELA price.
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Every sensitive action, kept for disputes and audits.
CREATE TABLE IF NOT EXISTS audit_log (
  id             TEXT PRIMARY KEY,
  actor_user_id  TEXT,
  action         TEXT NOT NULL,
  entity         TEXT,
  entity_id      TEXT,
  meta           TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
`;

let db = null;

function open(dbPath = config.dbPath) {
  if (db) return db;
  if (dbPath !== ":memory:") fs.mkdirSync(path.dirname(dbPath), { recursive: true });

  db = new DatabaseSync(dbPath);
  db.exec("PRAGMA journal_mode = WAL");
  db.exec("PRAGMA foreign_keys = ON");
  db.exec("PRAGMA busy_timeout = 5000");
  db.exec(SCHEMA);
  migrate(db);
  return db;
}

/**
 * Adds columns that were introduced after the first release.
 *
 * SQLite cannot express "ALTER TABLE ... ADD COLUMN IF NOT EXISTS", so we read the
 * table's current columns and add only what is missing. Safe to run on every start,
 * and it leaves existing rows on the stated default.
 */
const ADDED_COLUMNS = {
  vendors: [
    ["net_outflow_tolerance_bps", "INTEGER NOT NULL DEFAULT 100"],
    ["net_outflow_floor_paise", "INTEGER NOT NULL DEFAULT 50000"],
    ["conversion_budget_paise", "INTEGER NOT NULL DEFAULT 0"],
  ],
};

function migrate(database) {
  for (const [table, columns] of Object.entries(ADDED_COLUMNS)) {
    const existing = new Set(database.prepare(`PRAGMA table_info(${table})`).all().map((c) => c.name));
    for (const [name, definition] of columns) {
      if (!existing.has(name)) database.exec(`ALTER TABLE ${table} ADD COLUMN ${name} ${definition}`);
    }
  }
}

function get() {
  if (!db) open();
  return db;
}

function close() {
  if (db) {
    db.close();
    db = null;
  }
}

/**
 * Runs `fn` inside a database transaction. If `fn` throws, every write it made is
 * undone. Use this for anything that touches more than one table - a purchase
 * writes to four, and a half-written purchase would corrupt a customer's balance.
 */
function transaction(fn) {
  const database = get();
  database.exec("BEGIN IMMEDIATE");
  try {
    const result = fn(database);
    database.exec("COMMIT");
    return result;
  } catch (error) {
    try {
      database.exec("ROLLBACK");
    } catch {
      /* rollback of an already-closed transaction is not interesting */
    }
    throw error;
  }
}

/** Short, readable, unguessable identifiers: "cus_9dK2m..." */
function newId(prefix) {
  return `${prefix}_${crypto.randomBytes(9).toString("base64url")}`;
}

/** One timestamp format everywhere: UTC ISO-8601, which sorts correctly as text. */
function now() {
  return new Date().toISOString();
}

module.exports = { open, get, close, transaction, newId, now, SCHEMA };
