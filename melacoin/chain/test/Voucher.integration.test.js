"use strict";
/**
 * Proves the backend and the contract agree on what a withdrawal voucher is.
 *
 * This test imports the SAME module the running server uses (app/src/voucher.js),
 * signs a voucher with it, and redeems it against a real deployed MelaDistributor.
 * If anyone edits the EIP-712 domain, the version string, or the field order on
 * either side, this goes red immediately instead of in production.
 */
const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");

const voucher = require("../../app/src/voucher");

const ONE = 10n ** 18n;

describe("Voucher integration (backend module <-> contract)", function () {
  let mela, dist, admin, backendSigner, customer;

  beforeEach(async function () {
    [admin, backendSigner, customer] = await ethers.getSigners();

    const MelaCoin = await ethers.getContractFactory("MelaCoin");
    mela = await MelaCoin.deploy(admin.address, 1_000_000_000n * ONE, 1_000_000n * ONE);

    const Distributor = await ethers.getContractFactory("MelaDistributor");
    dist = await Distributor.deploy(await mela.getAddress(), admin.address, 100_000n * ONE);

    await dist.grantRole(await dist.SIGNER_ROLE(), backendSigner.address);
    await mela.approve(await dist.getAddress(), 500_000n * ONE);
    await dist.fund(500_000n * ONE);
  });

  it("the domain the backend builds matches the contract's own", async function () {
    // ethers exposes the contract's EIP-712 domain via eip712Domain() (ERC-5267).
    const onChain = await dist.eip712Domain();
    const offChain = voucher.domain({ chainId: 1, verifyingContract: await dist.getAddress() });

    expect(onChain.name).to.equal(offChain.name);
    expect(onChain.version).to.equal(offChain.version);
    expect(onChain.verifyingContract).to.equal(offChain.verifyingContract);
  });

  it("a voucher signed by the backend module is accepted on-chain", async function () {
    const { chainId } = await ethers.provider.getNetwork();
    const amount = 980n * ONE; // what 10,000 points became in the app's example
    const nonce = 424242n;
    const deadline = BigInt((await time.latest()) + 3600);

    const signature = await voucher.sign(backendSigner, {
      chainId,
      verifyingContract: await dist.getAddress(),
      to: customer.address,
      amount,
      nonce,
      deadline,
    });

    await expect(dist.claim(customer.address, amount, nonce, deadline, signature))
      .to.emit(dist, "Claimed")
      .withArgs(customer.address, amount, nonce);

    expect(await mela.balanceOf(customer.address)).to.equal(amount);
  });

  it("the same voucher cannot be used twice", async function () {
    const { chainId } = await ethers.provider.getNetwork();
    const deadline = BigInt((await time.latest()) + 3600);
    const args = { chainId, verifyingContract: await dist.getAddress(), to: customer.address, amount: ONE, nonce: 7n, deadline };
    const signature = await voucher.sign(backendSigner, args);

    await dist.claim(customer.address, ONE, 7n, deadline, signature);
    await expect(dist.claim(customer.address, ONE, 7n, deadline, signature)).to.be.revertedWithCustomError(
      dist,
      "VoucherAlreadyUsed"
    );
  });

  it("a voucher for the wrong chain is rejected", async function () {
    const { chainId } = await ethers.provider.getNetwork();
    const deadline = BigInt((await time.latest()) + 3600);

    // Sign as if this were a different network - a real risk when moving testnet -> mainnet.
    const signature = await voucher.sign(backendSigner, {
      chainId: chainId + 1n,
      verifyingContract: await dist.getAddress(),
      to: customer.address,
      amount: ONE,
      nonce: 99n,
      deadline,
    });

    await expect(dist.claim(customer.address, ONE, 99n, deadline, signature)).to.be.revertedWithCustomError(
      dist,
      "BadSignature"
    );
  });
});
