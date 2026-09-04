"use strict";
/** The customer's MelaCoin wallet: convert points in, spend or withdraw out. */
const db = require("../db");
const v = require("../validate");
const money = require("../money");
const { notFound, badRequest } = require("../http");
const conversion = require("../services/conversion.service");
const tokenService = require("../services/token.service");
const settings = require("../services/settings.service");
const balance = require("../services/balance.service");
const config = require("../config");

function vendorOr404(vendorId) {
  const vendor = db.get().prepare("SELECT * FROM vendors WHERE id = ?").get(vendorId);
  if (!vendor) throw notFound("Shop not found");
  return vendor;
}

function register(router) {
  router.get("/api/wallet", async (ctx) => {
    const user = ctx.requireRole("customer");
    const wei = tokenService.balanceWei(user.id);
    const pricePaise = settings.melaPricePaise();
    return {
      body: {
        mela_wei: wei.toString(),
        mela_value_paise: money.paiseForMelaWei(wei, pricePaise),
        mela_price_paise: pricePaise,
        wallet_address: user.wallet_address,
        chain_mode: config.chainMode,
        ledger: tokenService.history(user.id),
        withdrawals: tokenService.withdrawals(user.id),
        display: {
          mela: money.formatMela(wei),
          value: money.formatPaise(money.paiseForMelaWei(wei, pricePaise)),
          price: money.formatPaise(pricePaise),
        },
      },
    };
  });

  /**
   * Where to spend MelaCoin, best shops first.
   *
   * "Best" means the shops that have paid the most into the network and taken the
   * least back out. Steering customers there closes the imbalance without ever
   * refusing anybody, which is far better than blocking conversions after the fact.
   */
  router.get("/api/wallet/spend-here", async (ctx) => {
    ctx.requireRole("customer");
    return {
      body: {
        shops: balance.shopsNeedingFootfall(20),
        why: "These shops have funded more of the network than they have received back. Spending here keeps the network in balance.",
      },
    };
  });

  /** Preview a points -> MELA conversion. Nothing changes. */
  router.post("/api/wallet/convert/quote", async (ctx) => {
    const user = ctx.requireRole("customer");
    return {
      body: conversion.quoteConversion({
        vendor: vendorOr404(v.requiredString(ctx.body, "vendor_id")),
        customerId: user.id,
        points: v.requiredInt(ctx.body, "points", { min: 1 }),
      }),
    };
  });

  /**
   * Do the conversion. The client sends back the MELA amount it was quoted; if the
   * price moved in between we refuse rather than silently give a different amount.
   */
  router.post("/api/wallet/convert", async (ctx) => {
    const user = ctx.requireRole("customer");
    const vendor = vendorOr404(v.requiredString(ctx.body, "vendor_id"));
    const points = v.requiredInt(ctx.body, "points", { min: 1 });
    const expectedWei = ctx.body.expect_mela_wei;

    if (expectedWei !== undefined && expectedWei !== null && expectedWei !== "") {
      const preview = conversion.quoteConversion({ vendor, customerId: user.id, points });
      if (BigInt(preview.mela_wei) !== BigInt(expectedWei)) {
        throw badRequest("The MelaCoin price changed while you were deciding. Please review the new quote.");
      }
    }

    return { status: 201, body: conversion.convert({ vendor, customerId: user.id, points }) };
  });

  /** Move custodial MELA to the customer's own on-chain wallet. */
  router.post("/api/wallet/withdraw", async (ctx) => {
    const user = ctx.requireRole("customer");
    const toAddress =
      v.optionalString(ctx.body, "to_address", { pattern: /^0x[0-9a-fA-F]{40}$/, max: 42 }) || user.wallet_address;
    if (!toAddress) throw badRequest("Add a wallet address to your profile first");

    const wei = ctx.body.mela_wei ? BigInt(ctx.body.mela_wei) : tokenService.balanceWei(user.id);
    const result = await tokenService.requestWithdrawal({ customerId: user.id, wei, toAddress });
    return { status: 201, body: result };
  });
}

module.exports = { register };
