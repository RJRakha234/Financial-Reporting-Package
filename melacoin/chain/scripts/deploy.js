/**
 * Deploys the three MelaCoin contracts and prints the addresses your backend needs.
 *
 *   npx hardhat node                                    # terminal 1 (local chain)
 *   npx hardhat run scripts/deploy.js --network localhost   # terminal 2
 *
 * For a testnet, set DEPLOYER_PRIVATE_KEY and use --network polygonAmoy.
 * Never deploy to mainnet from a key that lives on a laptop - see docs/07-launch-checklist.md.
 */
const { ethers } = require("hardhat");

const ONE = 10n ** 18n;

// Supply plan. Change these before a real deployment and keep docs/03-tokenomics.md in sync.
const CAP = 1_000_000_000n * ONE;       // 1 billion MELA, forever
const GENESIS = 200_000_000n * ONE;     // minted at launch to the admin/treasury
const DISTRIBUTOR_RESERVE = 50_000_000n * ONE; // available for customer withdrawals
const DAILY_CLAIM_CAP = 250_000n * ONE; // blast radius if the signing key leaks

async function main() {
  const [deployer] = await ethers.getSigners();
  const net = await ethers.provider.getNetwork();
  console.log(`Network : ${net.name} (chainId ${net.chainId})`);
  console.log(`Deployer: ${deployer.address}`);
  console.log(`Balance : ${ethers.formatEther(await ethers.provider.getBalance(deployer.address))}\n`);

  const admin = process.env.ADMIN_ADDRESS || deployer.address;
  if (admin === deployer.address) {
    console.log("!! ADMIN_ADDRESS not set - using the deployer as admin.");
    console.log("!! Fine for local testing. For mainnet this MUST be a multisig.\n");
  }

  const MelaCoin = await ethers.getContractFactory("MelaCoin");
  const mela = await MelaCoin.deploy(admin, CAP, GENESIS);
  await mela.waitForDeployment();
  console.log(`MelaCoin       : ${await mela.getAddress()}`);

  const Distributor = await ethers.getContractFactory("MelaDistributor");
  const dist = await Distributor.deploy(await mela.getAddress(), admin, DAILY_CLAIM_CAP);
  await dist.waitForDeployment();
  console.log(`MelaDistributor: ${await dist.getAddress()}`);

  const Vesting = await ethers.getContractFactory("MelaVesting");
  const vesting = await Vesting.deploy(await mela.getAddress(), admin);
  await vesting.waitForDeployment();
  console.log(`MelaVesting    : ${await vesting.getAddress()}`);

  if (admin === deployer.address) {
    await (await mela.approve(await dist.getAddress(), DISTRIBUTOR_RESERVE)).wait();
    await (await dist.fund(DISTRIBUTOR_RESERVE)).wait();
    console.log(`\nFunded distributor with ${ethers.formatEther(DISTRIBUTOR_RESERVE)} MELA`);
  }

  const signer = process.env.VOUCHER_SIGNER_ADDRESS;
  if (signer && admin === deployer.address) {
    await (await dist.grantRole(await dist.SIGNER_ROLE(), signer)).wait();
    console.log(`Granted SIGNER_ROLE to ${signer}`);
  }

  console.log("\nAdd these to melacoin/app/.env:");
  console.log(`CHAIN_MODE=chain`);
  console.log(`MELA_TOKEN_ADDRESS=${await mela.getAddress()}`);
  console.log(`MELA_DISTRIBUTOR_ADDRESS=${await dist.getAddress()}`);
  console.log(`CHAIN_ID=${net.chainId}`);
}

main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
