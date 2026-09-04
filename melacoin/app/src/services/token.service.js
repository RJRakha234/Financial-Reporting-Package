"use strict";
/**
 * token.service.js - the customer's MELA balance and the bridge to the blockchain.
 *
 * MELA lives in two places and it is important to keep them straight:
 *
 *   CUSTODIAL  - a row in our mela_ledger table. Fast, free, reversible by us.
 *                This is where MELA lands when points are converted.
 *   ON-CHAIN   - a real ERC-20 balance in the customer's own wallet. We cannot
 *                touch it. A customer moves custodial -> on-chain by withdrawing.
 *
 * The app runs in one of two modes (CHAIN_MODE in .env):
 *   mock  - no blockchain at all. Withdrawals are recorded but nothing is signed.
 *           This is the right mode for building and testing the business.
 *   chain - withdrawals produce a real EIP-712 voucher the customer redeems on
 *           MelaDistributor. Requires `npm install ethers` and a signer key.
 */
const crypto = require("node:crypto");
const db = require("../db");
const { RuleError } = require("../errors");
const config = require("../config");
const voucher = require("../voucher");

/** Sum of an append-only ledger, done in BigInt so 18-decimal amounts stay exact. */
function balanceWei(customerId) {
  const rows = db.get().prepare("SELECT wei_delta FROM mela_ledger WHERE customer_id = ?").all(customerId);
  return rows.reduce((total, row) => total + BigInt(row.wei_delta), 0n);
}

/**
 * Adds an entry to the MELA ledger. `weiDelta` is positive to credit, negative to debit.
 * Refuses to let a balance go negative - that would be minting money by accident.
 */
function post({ customerId, kind, weiDelta, refType, refId, note }) {
  const delta = BigInt(weiDelta);
  if (delta < 0n && balanceWei(customerId) + delta < 0n) {
    throw new RuleError("Not enough MELA");
  }
  const id = db.newId("mel");
  db.get()
    .prepare(
      `INSERT INTO mela_ledger (id, customer_id, kind, wei_delta, ref_type, ref_id, note, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
    )
    .run(id, customerId, kind, delta.toString(), refType || null, refId || null, note || null, db.now());
  return id;
}

function history(customerId, limit = 50) {
  return db
    .get()
    .prepare("SELECT * FROM mela_ledger WHERE customer_id = ? ORDER BY created_at DESC, id DESC LIMIT ?")
    .all(customerId, limit);
}

/** Total custodial MELA we owe all customers. The treasury must be able to cover this. */
function totalCustodialWei() {
  const rows = db.get().prepare("SELECT wei_delta FROM mela_ledger").all();
  return rows.reduce((total, row) => total + BigInt(row.wei_delta), 0n);
}

// ------------------------------------------------------------- withdrawals

/** Loads ethers only when chain mode is actually used, so the app has no dependencies by default. */
function loadEthers() {
  try {
    return require("ethers");
  } catch {
    throw new Error(
      "CHAIN_MODE=chain needs the ethers library. Run `npm install ethers` inside melacoin/app, " +
        "or set CHAIN_MODE=mock to keep running without a blockchain."
    );
  }
}

/**
 * Moves custodial MELA towards the customer's own wallet.
 *
 * In chain mode we do NOT send a transaction. We sign a voucher that says
 * "this address may withdraw this much, once, before this time". The customer
 * submits it to MelaDistributor.claim(). The signing key never holds tokens, so
 * leaking it costs at most the contract's daily cap instead of the whole reserve.
 */
async function requestWithdrawal({ customerId, wei, toAddress }) {
  const amount = BigInt(wei);
  if (amount <= 0n) throw new RuleError("Withdrawal amount must be greater than zero");
  if (!/^0x[0-9a-fA-F]{40}$/.test(toAddress || "")) throw new RuleError("Enter a valid wallet address (0x...)");

  const nonce = BigInt(`0x${crypto.randomBytes(16).toString("hex")}`).toString();
  const deadline = Math.floor(Date.now() / 1000) + config.chain.voucherTtlMinutes * 60;
  const id = db.newId("wdr");

  let signature = null;
  if (config.chainMode === "chain") {
    const { Wallet } = loadEthers();
    if (!config.chain.voucherSignerKey) throw new Error("VOUCHER_SIGNER_PRIVATE_KEY is not set");
    if (!config.chain.distributorAddress) throw new Error("MELA_DISTRIBUTOR_ADDRESS is not set");

    // The voucher shape lives in one file, shared with the contract's own test suite.
    signature = await voucher.sign(new Wallet(config.chain.voucherSignerKey), {
      chainId: config.chain.chainId,
      verifyingContract: config.chain.distributorAddress,
      to: toAddress,
      amount,
      nonce,
      deadline,
    });
  }

  // Debit first, then record. If the debit fails we never write a voucher.
  db.transaction(() => {
    post({
      customerId,
      kind: "WITHDRAW",
      weiDelta: -amount,
      refType: "withdrawal",
      refId: id,
      note: `to ${toAddress}`,
    });
    db.get()
      .prepare(
        `INSERT INTO withdrawals (id, customer_id, wei, to_address, nonce, deadline, signature, status, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, 'signed', ?)`
      )
      .run(id, customerId, amount.toString(), toAddress, nonce, deadline, signature, db.now());
  });

  return {
    id,
    mode: config.chainMode,
    wei: amount.toString(),
    to: toAddress,
    nonce,
    deadline,
    signature,
    contract: config.chain.distributorAddress || null,
    chainId: config.chain.chainId,
    instructions:
      config.chainMode === "chain"
        ? "Call MelaDistributor.claim(to, amount, nonce, deadline, signature) from any wallet to receive your MELA."
        : "Running in mock mode: the withdrawal is recorded but no blockchain transaction exists yet.",
  };
}

function withdrawals(customerId, limit = 20) {
  return db
    .get()
    .prepare("SELECT * FROM withdrawals WHERE customer_id = ? ORDER BY created_at DESC LIMIT ?")
    .all(customerId, limit);
}

module.exports = { balanceWei, post, history, totalCustodialWei, requestWithdrawal, withdrawals };
