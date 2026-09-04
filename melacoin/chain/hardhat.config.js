require("@nomicfoundation/hardhat-ethers");
require("@nomicfoundation/hardhat-chai-matchers");
require("@nomicfoundation/hardhat-network-helpers");

const path = require("path");
const { subtask } = require("hardhat/config");
const { TASK_COMPILE_SOLIDITY_GET_SOLC_BUILD } = require("hardhat/builtin-tasks/task-names");

const SOLC_VERSION = "0.8.24";

/**
 * By default Hardhat downloads the Solidity compiler from binaries.soliditylang.org.
 * Plenty of networks (corporate proxies, CI sandboxes) block that host, and the error
 * it produces is not obvious. We ship the compiler as the npm package `solc` instead
 * and point Hardhat at it, so `npm install && npm test` works with no extra downloads.
 * Delete this block if you would rather use Hardhat's normal download path.
 */
subtask(TASK_COMPILE_SOLIDITY_GET_SOLC_BUILD, async (args, hre, runSuper) => {
  if (args.solcVersion === SOLC_VERSION) {
    try {
      const compilerPath = path.join(require.resolve("solc"), "..", "soljson.js");
      return {
        compilerPath,
        isSolcJs: true,
        version: args.solcVersion,
        longVersion: require("solc").version(),
      };
    } catch {
      // solc isn't installed - fall through to Hardhat's downloader.
    }
  }
  return runSuper();
});

/**
 * Networks are read from environment variables so no key ever lands in git.
 * Copy .env.example to .env and fill it in only when you are ready to deploy.
 */
const DEPLOYER_KEY = process.env.DEPLOYER_PRIVATE_KEY;
const accounts = DEPLOYER_KEY ? [DEPLOYER_KEY] : [];

module.exports = {
  solidity: {
    version: SOLC_VERSION,
    settings: {
      optimizer: { enabled: true, runs: 200 },
      evmVersion: "paris",
    },
  },
  networks: {
    hardhat: {},
    localhost: { url: "http://127.0.0.1:8545" },
    // Testnet first. Always. See docs/07-launch-checklist.md.
    polygonAmoy: {
      url: process.env.POLYGON_AMOY_RPC_URL || "https://rpc-amoy.polygon.technology",
      chainId: 80002,
      accounts,
    },
    polygon: {
      url: process.env.POLYGON_RPC_URL || "https://polygon-rpc.com",
      chainId: 137,
      accounts,
    },
  },
};
