# MelaCoin contracts

Three Solidity contracts. Together they are the on-chain half of the MelaCoin
network; the app in `../app` is the off-chain half.

| Contract | What it is |
|---|---|
| `MelaCoin.sol` | The MELA token itself. ERC-20, hard supply cap, burnable, pausable, EIP-2612 permit. |
| `MelaDistributor.sol` | The bridge. Customers withdraw MELA here using a signed voucher, and burn MELA here to pay a shop. |
| `MelaVesting.sol` | Cliff + linear vesting, so the team allocation is provably locked. |

## Run the tests

```bash
npm install
npm test
```

38 tests cover the supply cap, role permissions, voucher replay, expiry, tampering,
the daily payout cap, burn-to-spend, and the vesting curve including revocation.

> The Solidity compiler is pinned as the npm package `solc` rather than downloaded
> from `binaries.soliditylang.org`, so this works behind a corporate proxy. See the
> comment at the top of `hardhat.config.js`.

## Deploy to a local chain

```bash
npx hardhat node                                        # terminal 1
npx hardhat run scripts/deploy.js --network localhost   # terminal 2
```

The script prints the three addresses and the exact lines to paste into
`../app/.env`. Then set `CHAIN_MODE=chain` there and run `npm install ethers`
inside `../app`.

## Before you deploy anywhere real

Read `../docs/07-launch-checklist.md` first. The short version: testnet for weeks,
a multisig as admin, an audit before mainnet, and never a deployer key on a laptop.
