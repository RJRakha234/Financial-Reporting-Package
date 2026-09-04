"use strict";
/**
 * auth.js - passwords, login sessions, and vendor API keys.
 *
 * Rules this file follows, all of which matter:
 *  - A password is never stored. We store a scrypt hash with a random salt.
 *  - A session token is never stored. We store its SHA-256 hash, so a leaked
 *    database still cannot be used to log in as anybody.
 *  - Secrets are compared with a timing-safe comparison, so an attacker cannot
 *    learn a secret one character at a time by measuring response times.
 */
const crypto = require("node:crypto");
const db = require("./db");
const config = require("./config");

const SCRYPT = { N: 16384, r: 8, p: 1, keyLength: 64 };

function hashPassword(password) {
  if (typeof password !== "string" || password.length < 8) {
    throw new Error("Password must be at least 8 characters");
  }
  const salt = crypto.randomBytes(16);
  const derived = crypto.scryptSync(password, salt, SCRYPT.keyLength, {
    N: SCRYPT.N,
    r: SCRYPT.r,
    p: SCRYPT.p,
    maxmem: 128 * SCRYPT.N * SCRYPT.r * 2,
  });
  return `scrypt$${SCRYPT.N}$${SCRYPT.r}$${SCRYPT.p}$${salt.toString("hex")}$${derived.toString("hex")}`;
}

function verifyPassword(password, stored) {
  try {
    const [scheme, N, r, p, saltHex, hashHex] = String(stored).split("$");
    if (scheme !== "scrypt") return false;
    const salt = Buffer.from(saltHex, "hex");
    const expected = Buffer.from(hashHex, "hex");
    const derived = crypto.scryptSync(password, salt, expected.length, {
      N: Number(N),
      r: Number(r),
      p: Number(p),
      maxmem: 128 * Number(N) * Number(r) * 2,
    });
    return crypto.timingSafeEqual(derived, expected);
  } catch {
    return false;
  }
}

const sha256 = (value) => crypto.createHash("sha256").update(value).digest("hex");

// ------------------------------------------------------------------ sessions

function createSession(userId) {
  const token = crypto.randomBytes(32).toString("base64url");
  const expiresAt = new Date(Date.now() + config.sessionTtlHours * 3600_000).toISOString();
  db.get()
    .prepare("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)")
    .run(sha256(token), userId, db.now(), expiresAt);
  return { token, expiresAt };
}

/** Returns the user for a session token, or null. Expired sessions are cleaned up. */
function userForToken(token) {
  if (!token) return null;
  const row = db
    .get()
    .prepare(
      `SELECT u.*, s.expires_at FROM sessions s
       JOIN users u ON u.id = s.user_id
       WHERE s.token_hash = ?`
    )
    .get(sha256(token));
  if (!row) return null;
  if (row.expires_at <= db.now()) {
    destroySession(token);
    return null;
  }
  delete row.password_hash;
  return row;
}

function destroySession(token) {
  db.get().prepare("DELETE FROM sessions WHERE token_hash = ?").run(sha256(token));
}

function purgeExpiredSessions() {
  return db.get().prepare("DELETE FROM sessions WHERE expires_at <= ?").run(db.now()).changes;
}

// --------------------------------------------------------------- vendor keys

/**
 * Creates a shop's POS key. The full key is returned exactly once - we keep only a
 * hash, so if the vendor loses it they must rotate rather than "look it up".
 */
function generateApiKey() {
  const secret = crypto.randomBytes(24).toString("base64url");
  const key = `mela_sk_${secret}`;
  return { key, hash: sha256(key), prefix: key.slice(0, 16) };
}

function vendorForApiKey(key) {
  if (!key || !key.startsWith("mela_sk_")) return null;
  const row = db.get().prepare("SELECT * FROM vendors WHERE api_key_hash = ? AND active = 1").get(sha256(key));
  return row || null;
}

module.exports = {
  hashPassword,
  verifyPassword,
  createSession,
  userForToken,
  destroySession,
  purgeExpiredSessions,
  generateApiKey,
  vendorForApiKey,
  sha256,
};
