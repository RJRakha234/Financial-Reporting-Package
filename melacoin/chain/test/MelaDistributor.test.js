const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");

const ONE = 10n ** 18n;
const CAP = 1_000_000_000n * ONE;
const RESERVE = 100_000n * ONE;
const DAILY_CAP = 10_000n * ONE;

describe("MelaDistributor", function () {
  let mela, dist, admin, signerKey, alice, bob, relayer;

  async function voucher(signer, { to, amount, nonce, deadline }) {
    const net = await ethers.provider.getNetwork();
    return signer.signTypedData(
      {
        name: "MelaDistributor",
        version: "1",
        chainId: net.chainId,
        verifyingContract: await dist.getAddress(),
      },
      {
        Claim: [
          { name: "to", type: "address" },
          { name: "amount", type: "uint256" },
          { name: "nonce", type: "uint256" },
          { name: "deadline", type: "uint256" },
        ],
      },
      { to, amount, nonce, deadline }
    );
  }

  const future = async (secs = 3600) => BigInt((await time.latest()) + secs);

  beforeEach(async function () {
    [admin, signerKey, alice, bob, relayer] = await ethers.getSigners();

    const MelaCoin = await ethers.getContractFactory("MelaCoin");
    mela = await MelaCoin.deploy(admin.address, CAP, 1_000_000n * ONE);

    const Dist = await ethers.getContractFactory("MelaDistributor");
    dist = await Dist.deploy(await mela.getAddress(), admin.address, DAILY_CAP);

    // The backend's signing key authorises payouts but never holds tokens.
    await dist.grantRole(await dist.SIGNER_ROLE(), signerKey.address);
    await dist.revokeRole(await dist.SIGNER_ROLE(), admin.address);

    await mela.approve(await dist.getAddress(), RESERVE);
    await dist.fund(RESERVE);
  });

  it("starts with the funded reserve", async function () {
    expect(await dist.reserveBalance()).to.equal(RESERVE);
    expect(await dist.remainingDailyAllowance()).to.equal(DAILY_CAP);
  });

  describe("claim", function () {
    it("pays a valid voucher and marks the nonce used", async function () {
      const amount = 250n * ONE;
      const deadline = await future();
      const sig = await voucher(signerKey, { to: alice.address, amount, nonce: 1n, deadline });

      await expect(dist.connect(relayer).claim(alice.address, amount, 1n, deadline, sig))
        .to.emit(dist, "Claimed")
        .withArgs(alice.address, amount, 1n);

      expect(await mela.balanceOf(alice.address)).to.equal(amount);
      expect(await dist.nonceUsed(1n)).to.equal(true);
      expect(await dist.reserveBalance()).to.equal(RESERVE - amount);
    });

    it("rejects a replayed voucher", async function () {
      const amount = 10n * ONE;
      const deadline = await future();
      const sig = await voucher(signerKey, { to: alice.address, amount, nonce: 7n, deadline });

      await dist.claim(alice.address, amount, 7n, deadline, sig);
      await expect(dist.claim(alice.address, amount, 7n, deadline, sig))
        .to.be.revertedWithCustomError(dist, "VoucherAlreadyUsed")
        .withArgs(7n);
    });

    it("rejects an expired voucher", async function () {
      const deadline = await future(60);
      const sig = await voucher(signerKey, { to: alice.address, amount: ONE, nonce: 2n, deadline });
      await time.increaseTo(deadline + 1n);
      await expect(dist.claim(alice.address, ONE, 2n, deadline, sig)).to.be.revertedWithCustomError(
        dist,
        "VoucherExpired"
      );
    });

    it("rejects a voucher signed by the wrong key", async function () {
      const deadline = await future();
      const sig = await voucher(bob, { to: bob.address, amount: ONE, nonce: 3n, deadline });
      await expect(dist.claim(bob.address, ONE, 3n, deadline, sig)).to.be.revertedWithCustomError(
        dist,
        "BadSignature"
      );
    });

    it("rejects a tampered amount", async function () {
      const deadline = await future();
      const sig = await voucher(signerKey, { to: alice.address, amount: ONE, nonce: 4n, deadline });
      await expect(dist.claim(alice.address, 1000n * ONE, 4n, deadline, sig)).to.be.revertedWithCustomError(
        dist,
        "BadSignature"
      );
    });

    it("rejects a voucher redirected to another address", async function () {
      const deadline = await future();
      const sig = await voucher(signerKey, { to: alice.address, amount: ONE, nonce: 5n, deadline });
      await expect(dist.claim(bob.address, ONE, 5n, deadline, sig)).to.be.revertedWithCustomError(
        dist,
        "BadSignature"
      );
    });

    it("enforces the rolling daily cap and resets after 24h", async function () {
      const deadline = await future(10 * 24 * 3600);
      const sigA = await voucher(signerKey, { to: alice.address, amount: DAILY_CAP, nonce: 10n, deadline });
      await dist.claim(alice.address, DAILY_CAP, 10n, deadline, sigA);
      expect(await dist.remainingDailyAllowance()).to.equal(0n);

      const sigB = await voucher(signerKey, { to: alice.address, amount: ONE, nonce: 11n, deadline });
      await expect(dist.claim(alice.address, ONE, 11n, deadline, sigB)).to.be.revertedWithCustomError(
        dist,
        "DailyCapExceeded"
      );

      await time.increase(24 * 3600 + 1);
      await expect(dist.claim(alice.address, ONE, 11n, deadline, sigB)).to.not.be.reverted;
    });

    it("rejects a claim larger than the reserve", async function () {
      const amount = RESERVE + ONE;
      const deadline = await future();
      await dist.setDailyClaimCap(0); // remove the cap so the reserve check is what bites
      const sig = await voucher(signerKey, { to: alice.address, amount, nonce: 12n, deadline });
      await expect(dist.claim(alice.address, amount, 12n, deadline, sig)).to.be.revertedWithCustomError(
        dist,
        "InsufficientReserve"
      );
    });

    it("refuses to pay while paused", async function () {
      const deadline = await future();
      const sig = await voucher(signerKey, { to: alice.address, amount: ONE, nonce: 13n, deadline });
      await dist.pause();
      await expect(dist.claim(alice.address, ONE, 13n, deadline, sig)).to.be.revertedWithCustomError(
        dist,
        "EnforcedPause"
      );
    });
  });

  describe("spend", function () {
    const vendorRef = ethers.keccak256(ethers.toUtf8Bytes("chai-corner-koramangala"));

    beforeEach(async function () {
      await mela.transfer(alice.address, 500n * ONE);
    });

    it("burns the customer's tokens and names the vendor", async function () {
      const amount = 40n * ONE;
      const supplyBefore = await mela.totalSupply();
      await mela.connect(alice).approve(await dist.getAddress(), amount);

      await expect(dist.connect(alice).spend(amount, vendorRef))
        .to.emit(dist, "Spent")
        .withArgs(alice.address, amount, vendorRef);

      expect(await mela.totalSupply()).to.equal(supplyBefore - amount);
      expect(await mela.balanceOf(alice.address)).to.equal(460n * ONE);
      // Spent tokens are destroyed, not parked in the contract.
      expect(await dist.reserveBalance()).to.equal(RESERVE);
    });

    it("fails without an allowance", async function () {
      await expect(dist.connect(alice).spend(ONE, vendorRef)).to.be.revertedWithCustomError(
        mela,
        "ERC20InsufficientAllowance"
      );
    });

    it("supports one-transaction spend via permit", async function () {
      const amount = 15n * ONE;
      const deadline = await future();
      const net = await ethers.provider.getNetwork();
      const sig = await alice.signTypedData(
        { name: "MelaCoin", version: "1", chainId: net.chainId, verifyingContract: await mela.getAddress() },
        {
          Permit: [
            { name: "owner", type: "address" },
            { name: "spender", type: "address" },
            { name: "value", type: "uint256" },
            { name: "nonce", type: "uint256" },
            { name: "deadline", type: "uint256" },
          ],
        },
        {
          owner: alice.address,
          spender: await dist.getAddress(),
          value: amount,
          nonce: await mela.nonces(alice.address),
          deadline,
        }
      );
      const { v, r, s } = ethers.Signature.from(sig);

      await expect(dist.connect(alice).spendWithPermit(amount, vendorRef, deadline, v, r, s))
        .to.emit(dist, "Spent")
        .withArgs(alice.address, amount, vendorRef);
      expect(await mela.balanceOf(alice.address)).to.equal(485n * ONE);
    });
  });

  describe("treasury controls", function () {
    it("lets the treasurer sweep, and nobody else", async function () {
      await expect(dist.connect(alice).sweep(alice.address, ONE)).to.be.revertedWithCustomError(
        dist,
        "AccessControlUnauthorizedAccount"
      );
      await expect(dist.sweep(admin.address, 100n * ONE)).to.emit(dist, "Swept");
      expect(await dist.reserveBalance()).to.equal(RESERVE - 100n * ONE);
    });

    it("lets the admin change the daily cap", async function () {
      await expect(dist.setDailyClaimCap(42n)).to.emit(dist, "DailyClaimCapUpdated").withArgs(42n);
      expect(await dist.dailyClaimCap()).to.equal(42n);
    });
  });
});
