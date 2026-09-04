const { expect } = require("chai");
const { ethers } = require("hardhat");

const ONE = 10n ** 18n;
const CAP = 1_000_000_000n * ONE;

describe("MelaCoin", function () {
  let mela, admin, minter, alice, bob;

  beforeEach(async function () {
    [admin, minter, alice, bob] = await ethers.getSigners();
    const MelaCoin = await ethers.getContractFactory("MelaCoin");
    mela = await MelaCoin.deploy(admin.address, CAP, 1000n * ONE);
    await mela.waitForDeployment();
  });

  it("has the expected identity and genesis supply", async function () {
    expect(await mela.name()).to.equal("MelaCoin");
    expect(await mela.symbol()).to.equal("MELA");
    expect(await mela.decimals()).to.equal(18n);
    expect(await mela.cap()).to.equal(CAP);
    expect(await mela.totalSupply()).to.equal(1000n * ONE);
    expect(await mela.balanceOf(admin.address)).to.equal(1000n * ONE);
  });

  it("rejects a zero admin", async function () {
    const MelaCoin = await ethers.getContractFactory("MelaCoin");
    await expect(MelaCoin.deploy(ethers.ZeroAddress, CAP, 0)).to.be.revertedWithCustomError(
      MelaCoin,
      "ZeroAddress"
    );
  });

  describe("minting", function () {
    it("lets a minter mint and logs the reason", async function () {
      await expect(mela.mint(alice.address, 500n * ONE, "treasury-tranche-1"))
        .to.emit(mela, "Minted")
        .withArgs(alice.address, 500n * ONE, "treasury-tranche-1");
      expect(await mela.balanceOf(alice.address)).to.equal(500n * ONE);
    });

    it("blocks non-minters", async function () {
      await expect(
        mela.connect(alice).mint(alice.address, ONE, "self-serve")
      ).to.be.revertedWithCustomError(mela, "AccessControlUnauthorizedAccount");
    });

    it("blocks minting past the hard cap", async function () {
      const room = CAP - (await mela.totalSupply());
      await mela.mint(alice.address, room, "fill-to-cap");
      expect(await mela.totalSupply()).to.equal(CAP);
      await expect(mela.mint(alice.address, 1n, "one-too-many")).to.be.revertedWithCustomError(
        mela,
        "ERC20ExceededCap"
      );
    });

    it("rejects zero address and zero amount", async function () {
      await expect(mela.mint(ethers.ZeroAddress, ONE, "x")).to.be.revertedWithCustomError(mela, "ZeroAddress");
      await expect(mela.mint(alice.address, 0, "x")).to.be.revertedWithCustomError(mela, "ZeroAmount");
    });

    it("honours a granted then revoked minter role", async function () {
      await mela.grantRole(await mela.MINTER_ROLE(), minter.address);
      await mela.connect(minter).mint(bob.address, ONE, "granted");
      await mela.revokeRole(await mela.MINTER_ROLE(), minter.address);
      await expect(mela.connect(minter).mint(bob.address, ONE, "revoked")).to.be.reverted;
    });
  });

  describe("burning", function () {
    it("permanently reduces supply", async function () {
      const before = await mela.totalSupply();
      await mela.burn(100n * ONE);
      expect(await mela.totalSupply()).to.equal(before - 100n * ONE);
    });

    it("frees room under the cap after a burn", async function () {
      const room = CAP - (await mela.totalSupply());
      await mela.mint(alice.address, room, "fill");
      await mela.connect(alice).burn(10n * ONE);
      await expect(mela.mint(alice.address, 10n * ONE, "refill")).to.not.be.reverted;
    });
  });

  describe("pausing", function () {
    it("stops transfers while paused and resumes after", async function () {
      await mela.transfer(alice.address, 10n * ONE);
      await mela.pause();
      await expect(mela.connect(alice).transfer(bob.address, ONE)).to.be.revertedWithCustomError(
        mela,
        "EnforcedPause"
      );
      await mela.unpause();
      await expect(mela.connect(alice).transfer(bob.address, ONE)).to.not.be.reverted;
    });

    it("only lets PAUSER_ROLE pause", async function () {
      await expect(mela.connect(alice).pause()).to.be.revertedWithCustomError(
        mela,
        "AccessControlUnauthorizedAccount"
      );
    });
  });

  describe("permit (EIP-2612)", function () {
    it("approves via signature with no approve() transaction", async function () {
      await mela.transfer(alice.address, 100n * ONE);
      const value = 25n * ONE;
      const deadline = BigInt((await ethers.provider.getBlock("latest")).timestamp + 3600);
      const nonce = await mela.nonces(alice.address);
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
        { owner: alice.address, spender: bob.address, value, nonce, deadline }
      );
      const { v, r, s } = ethers.Signature.from(sig);

      await mela.permit(alice.address, bob.address, value, deadline, v, r, s);
      expect(await mela.allowance(alice.address, bob.address)).to.equal(value);
    });
  });
});
