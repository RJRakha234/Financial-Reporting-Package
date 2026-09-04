"use strict";
/** Platform-wide settings that an admin can change while the server is running. */
const db = require("../db");
const { RuleError } = require("../errors");
const config = require("../config");

const MELA_PRICE_KEY = "mela_price_paise";

function readSetting(key) {
  const row = db.get().prepare("SELECT value FROM settings WHERE key = ?").get(key);
  return row ? row.value : null;
}

function writeSetting(key, value) {
  db.get()
    .prepare(
      `INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
       ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`
    )
    .run(key, String(value), db.now());
}

/**
 * The price of 1 MELA in paise.
 *
 * Before listing, you set this yourself and it is effectively a promise you are
 * making. After listing, an exchange decides it and this value should be updated
 * from a price feed - see docs/03-tokenomics.md for why that difference matters.
 */
function melaPricePaise() {
  const stored = readSetting(MELA_PRICE_KEY);
  return stored === null ? config.defaultMelaPricePaise : Number(stored);
}

function setMelaPricePaise(paise) {
  if (!Number.isInteger(paise) || paise <= 0) throw new RuleError("Price must be a whole number of paise > 0");
  writeSetting(MELA_PRICE_KEY, paise);
  return paise;
}

module.exports = { readSetting, writeSetting, melaPricePaise, setMelaPricePaise, MELA_PRICE_KEY };
