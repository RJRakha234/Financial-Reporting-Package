"use strict";
/** Endpoints that need no login: the shop directory, token facts, health. */
const db = require("../db");
const money = require("../money");
const config = require("../config");
const settings = require("../services/settings.service");
const loyalty = require("../services/loyalty.service");
const tokenService = require("../services/token.service");

function register(router) {
  router.get("/api/health", async () => ({
    body: { ok: true, chain_mode: config.chainMode, time: db.now() },
  }));

  router.get("/api/public/vendors", async () => {
    const rows = db.get().prepare("SELECT * FROM vendors WHERE active = 1 ORDER BY name").all();
    return { body: { vendors: rows.map(loyalty.publicVendor) } };
  });

  /** The facts a customer - or an exchange - would ask about MELA. */
  router.get("/api/public/token", async () => {
    const pricePaise = settings.melaPricePaise();
    const custodialWei = tokenService.totalCustodialWei();
    return {
      body: {
        name: "MelaCoin",
        symbol: "MELA",
        decimals: 18,
        chain_mode: config.chainMode,
        chain_id: config.chain.chainId,
        token_address: config.chain.tokenAddress || null,
        distributor_address: config.chain.distributorAddress || null,
        price_paise: pricePaise,
        price_display: money.formatPaise(pricePaise),
        custodial_supply_wei: custodialWei.toString(),
        custodial_supply_display: money.formatMela(custodialWei),
      },
    };
  });
}

module.exports = { register };
