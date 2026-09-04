const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");

// Helper: run the next transaction at an exact timestamp so vesting maths are precise.
const releaseAt = async (ts, fn) => {
  await time.setNextBlockTimestamp(ts);
  return fn();
};

const ONE = 10n ** 18n;
const CAP = 1_000_000_000n * ONE;
const GRANT = 12_000n * ONE;
const YEAR = 365 * 24 * 3600;

describe("MelaVesting", function () {
  let mela, vesting, admin, teamMember, other;
  let start, cliff;

  beforeEach(async function () {
    [admin, teamMember, other] = await ethers.getSigners();

    const MelaCoin = await ethers.getContractFactory("MelaCoin");
    mela = await MelaCoin.deploy(admin.address, CAP, 1_000_000n * ONE);

    const Vesting = await ethers.getContractFactory("MelaVesting");
    vesting = await Vesting.deploy(await mela.getAddress(), admin.address);

    await mela.transfer(await vesting.getAddress(), GRANT);

    start = BigInt(await time.latest());
    cliff = start + BigInt(YEAR); // classic 1-year cliff, 4-year vest
  });

  const createDefault = (revocable = false) =>
    vesting.createSchedule(teamMember.address, GRANT, start, cliff, BigInt(4 * YEAR), revocable);

  it("records the schedule and commits the tokens", async function () {
    await expect(createDefault())
      .to.emit(vesting, "ScheduleCreated")
      .withArgs(teamMember.address, GRANT, start, cliff, BigInt(4 * YEAR));
    expect(await vesting.totalCommitted()).to.equal(GRANT);
    expect(await vesting.unallocatedBalance()).to.equal(0n);
  });

  it("refuses a schedule the contract cannot cover", async function () {
    await expect(
      vesting.createSchedule(teamMember.address, GRANT + ONE, start, cliff, BigInt(4 * YEAR), false)
    ).to.be.revertedWithCustomError(vesting, "UnderFunded");
  });

  it("refuses a duplicate schedule and bad cliff bounds", async function () {
    await createDefault();
    await expect(createDefault()).to.be.revertedWithCustomError(vesting, "ScheduleExists");
    await expect(
      vesting.createSchedule(other.address, ONE, start, start - 1n, BigInt(YEAR), false)
    ).to.be.revertedWithCustomError(vesting, "CliffBeforeStart");
    await expect(
      vesting.createSchedule(other.address, ONE, start, start + BigInt(2 * YEAR), BigInt(YEAR), false)
    ).to.be.revertedWithCustomError(vesting, "CliffAfterEnd");
  });

  it("only the owner may create schedules", async function () {
    await expect(
      vesting.connect(other).createSchedule(other.address, ONE, start, cliff, BigInt(YEAR), false)
    ).to.be.revertedWithCustomError(vesting, "OwnableUnauthorizedAccount");
  });

  describe("vesting curve", function () {
    beforeEach(async function () {
      await createDefault();
    });

    it("releases nothing before the cliff", async function () {
      await time.increaseTo(cliff - 10n);
      expect(await vesting.releasable(teamMember.address)).to.equal(0n);
      await expect(vesting.connect(teamMember).release()).to.be.revertedWithCustomError(
        vesting,
        "NothingToRelease"
      );
    });

    it("releases one quarter at the one-year cliff", async function () {
      await time.increaseTo(cliff);
      expect(await vesting.releasable(teamMember.address)).to.equal(GRANT / 4n);
      await expect(releaseAt(cliff + 1n, () => vesting.connect(teamMember).release()))
        .to.emit(vesting, "Released");
      expect(await mela.balanceOf(teamMember.address)).to.be.closeTo(GRANT / 4n, ONE);
    });

    it("keeps vesting linearly and never double-pays", async function () {
      await releaseAt(cliff, () => vesting.connect(teamMember).release());
      expect(await mela.balanceOf(teamMember.address)).to.equal(GRANT / 4n);

      await releaseAt(start + BigInt(2 * YEAR), () => vesting.connect(teamMember).release());
      expect(await mela.balanceOf(teamMember.address)).to.equal(GRANT / 2n);
    });

    it("pays out exactly the grant at the end and no more", async function () {
      await time.increaseTo(start + BigInt(4 * YEAR) + 1n);
      await vesting.connect(teamMember).release();
      expect(await mela.balanceOf(teamMember.address)).to.equal(GRANT);
      expect(await vesting.totalCommitted()).to.equal(0n);
      await expect(vesting.connect(teamMember).release()).to.be.revertedWithCustomError(
        vesting,
        "NothingToRelease"
      );
    });

    it("lets anyone trigger a release but pays the beneficiary", async function () {
      await releaseAt(cliff, () => vesting.connect(other).releaseFor(teamMember.address));
      expect(await mela.balanceOf(teamMember.address)).to.equal(GRANT / 4n);
      expect(await mela.balanceOf(other.address)).to.equal(0n);
    });
  });

  describe("revocation", function () {
    it("cannot revoke a non-revocable schedule", async function () {
      await createDefault(false);
      await expect(vesting.revoke(teamMember.address)).to.be.revertedWithCustomError(vesting, "NotRevocable");
    });

    it("returns only the unvested remainder and leaves vested tokens claimable", async function () {
      await createDefault(true);
      const adminBefore = await mela.balanceOf(admin.address);

      // Revoke exactly halfway through the four-year term.
      await releaseAt(start + BigInt(2 * YEAR), () => vesting.revoke(teamMember.address));

      // Half the term elapsed, so exactly half comes back to treasury.
      expect((await mela.balanceOf(admin.address)) - adminBefore).to.equal(GRANT / 2n);

      // The clock has stopped: waiting longer does not vest another rupee.
      await time.increase(YEAR);
      expect(await vesting.releasable(teamMember.address)).to.equal(GRANT / 2n);

      await vesting.connect(teamMember).release();
      expect(await mela.balanceOf(teamMember.address)).to.equal(GRANT / 2n);
      await expect(vesting.revoke(teamMember.address)).to.be.revertedWithCustomError(vesting, "AlreadyRevoked");
    });
  });
});
