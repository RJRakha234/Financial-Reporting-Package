"use strict";
/**
 * config.js - all tunable settings, read from environment variables.
 *
 * Nothing secret is hard-coded here. Copy .env.example to .env, edit it, and the
 * server loads it at startup (see loadDotEnv below - no library needed).
 */
const fs = require("node:fs");
const path = require("node:path");

/** Reads a simple KEY=value file into process.env without overwriting real env vars. */
function loadDotEnv(file) {
  if (!fs.existsSync(file)) return;
  for (const rawLine of fs.readFileSync(file, "utf8").split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const separator = line.indexOf("=");
    if (separator === -1) continue;
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim().replace(/^["']|["']$/g, "");
    if (!(key in process.env)) process.env[key] = value;
  }
}

loadDotEnv(path.join(__dirname, "..", ".env"));

const ROOT = path.join(__dirname, "..");

function num(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  const parsed = Number(raw);
  if (!Number.isInteger(parsed)) throw new Error(`${name} must be a whole number, got "${raw}"`);
  return parsed;
}

const config = {
  port: num("PORT", 4000),
  host: process.env.HOST || "127.0.0.1",
  env: process.env.NODE_ENV || "development",
  dbPath: process.env.DB_PATH || path.join(ROOT, "data", "melacoin.db"),
  publicDir: path.join(ROOT, "public"),

  /** Session lifetime for logged-in users. */
  sessionTtlHours: num("SESSION_TTL_HOURS", 24 * 7),

  /**
   * "mock"  - MELA balances live only in our database. Perfect for development
   *           and for running the business before the token is listed.
   * "chain" - withdrawals produce real signed vouchers for MelaDistributor.
   */
  chainMode: (process.env.CHAIN_MODE || "mock").toLowerCase(),
  chain: {
    chainId: num("CHAIN_ID", 31337),
    tokenAddress: process.env.MELA_TOKEN_ADDRESS || "",
    distributorAddress: process.env.MELA_DISTRIBUTOR_ADDRESS || "",
    /** Private key that signs withdrawal vouchers. Keep it in a KMS in production. */
    voucherSignerKey: process.env.VOUCHER_SIGNER_PRIVATE_KEY || "",
    voucherTtlMinutes: num("VOUCHER_TTL_MINUTES", 60),
  },

  /** Starting MELA price in paise. 100 = Rs 1.00. Admins change this at runtime. */
  defaultMelaPricePaise: num("DEFAULT_MELA_PRICE_PAISE", 100),

  /**
   * How long a shop has to undo a mistyped bill. Long enough that a busy counter
   * notices at the end of a rush; short enough that points cannot be quietly
   * clawed back days later.
   */
  voidWindowMinutes: num("VOID_WINDOW_MINUTES", 60),

  /**
   * The rolling window over which a shop's give-and-take with the network is judged.
   * Shorter reacts faster but punishes normal lumpiness; 30 days matches how shops
   * already think about a marketing budget.
   */
  settlementWindowDays: num("SETTLEMENT_WINDOW_DAYS", 30),

  /** Failed-login throttle: this many attempts per IP per window. */
  rateLimit: {
    windowMs: num("RATE_LIMIT_WINDOW_MS", 60_000),
    maxAuthAttempts: num("RATE_LIMIT_AUTH_MAX", 20),
    maxRequests: num("RATE_LIMIT_MAX", 600),
  },
};

module.exports = config;
