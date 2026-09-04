"use strict";
/**
 * voucher.js - the one definition of a withdrawal voucher.
 *
 * A voucher is a signed message saying "this address may claim this much MELA, once,
 * before this deadline". The backend signs it; MelaDistributor.claim() verifies it.
 *
 * The signature only verifies if BOTH sides agree on the domain name, the version,
 * the chain id, the contract address, and the exact field order of the Claim struct.
 * Change one character on one side and every withdrawal silently fails with
 * "BadSignature" - a bug that will not show up until real customers are affected.
 *
 * So this file is the single source of truth, and `chain/test/Voucher.integration.test.js`
 * imports it and checks it against the real deployed contract. If the Solidity and
 * this file ever drift apart, that test goes red.
 */

/** Must match `EIP712("MelaDistributor", "1")` in MelaDistributor.sol. */
const DOMAIN_NAME = "MelaDistributor";
const DOMAIN_VERSION = "1";

/** Must match CLAIM_TYPEHASH in MelaDistributor.sol, including the field order. */
const CLAIM_TYPES = {
  Claim: [
    { name: "to", type: "address" },
    { name: "amount", type: "uint256" },
    { name: "nonce", type: "uint256" },
    { name: "deadline", type: "uint256" },
  ],
};

function domain({ chainId, verifyingContract }) {
  return { name: DOMAIN_NAME, version: DOMAIN_VERSION, chainId, verifyingContract };
}

/**
 * Signs a voucher.
 * @param signer an ethers Wallet (or anything with signTypedData)
 */
function sign(signer, { chainId, verifyingContract, to, amount, nonce, deadline }) {
  return signer.signTypedData(domain({ chainId, verifyingContract }), CLAIM_TYPES, {
    to,
    amount,
    nonce,
    deadline,
  });
}

module.exports = { DOMAIN_NAME, DOMAIN_VERSION, CLAIM_TYPES, domain, sign };
