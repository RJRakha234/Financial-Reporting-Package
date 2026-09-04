# Launch checklist

Work down this list. Do not skip ahead because a step is boring — the boring steps
are the ones that stop you losing other people's money.

## Before you deploy contracts anywhere real

- [ ] `cd chain && npm test` — all 38 tests green.
- [ ] Deploy to a **local** chain (`npx hardhat node`) and run the whole app against it.
- [ ] Deploy to **Polygon Amoy testnet**. Live there for at least two weeks with real
      people doing real conversions and withdrawals.
- [ ] Fix the supply numbers in `scripts/deploy.js` and make them match
      `docs/03-tokenomics.md` exactly. Publish them before launch, not after.
- [ ] Get a **security audit** from a reputable firm. Budget ₹4–15 lakh. Non-negotiable.
- [ ] Fix everything the audit finds. Publish the report.

## Keys and control

- [ ] `ADMIN_ADDRESS` is a **multisig** (e.g. a Safe with 3 signers, 2 required).
      Never a single key, never a key on a laptop.
- [ ] The deployer key is used once and then holds nothing.
- [ ] `VOUCHER_SIGNER_PRIVATE_KEY` lives in a KMS or hardware signer, not a `.env` file.
- [ ] The distributor's **daily claim cap** is set to something you could survive
      losing in one day.
- [ ] Write down, in advance, who can pause the token and under what circumstances.
- [ ] Team allocation is locked in `MelaVesting.sol`, funded, and the address published.

## Legal, before a single rupee moves

- [ ] Written opinion from a payments lawyer on PPI status (`docs/05-compliance-india.md`).
- [ ] A CA has answered the section 194S / TDS questions and you have built for the answer.
- [ ] FIU-IND position confirmed in writing; register if you are in scope.
- [ ] KYC is wired into signup for anyone who converts or withdraws.
- [ ] Terms of service and a privacy policy are live and honest.
- [ ] The ASCI disclaimer is on all marketing.
- [ ] The treasury sits in a separate account from operating funds.

## The application

- [ ] Move from SQLite to Postgres if you are past a few hundred shops.
- [ ] Automated backups, and a restore you have actually tested.
- [ ] HTTPS everywhere, behind a real reverse proxy.
- [ ] Email and phone verification on signup.
- [ ] Password reset.
- [ ] A **price feed** replacing the admin typing the MELA price.
- [ ] Alerting on the backing ratio. If it drops below 100%, someone should be woken up.
- [ ] Rate limits reviewed for production traffic.
- [ ] Logs that let you reconstruct any customer's balance from history.

## Operations

- [ ] Shops are invoiced on a schedule, and conversions stop for shops in arrears.
- [ ] A per-shop and per-day cap on conversions, so one shop cannot drain the treasury.
- [ ] A written procedure for a customer disputing their points.
- [ ] Someone whose actual job is watching the backing ratio every morning.

## Before an exchange listing

- [ ] Real usage numbers you are not embarrassed by.
- [ ] The audit report, published.
- [ ] The legal opinion.
- [ ] The vesting contract, funded and public.
- [ ] A public, unchanging tokenomics page.
- [ ] Liquidity on a decentralised exchange first, to establish a price history.
- [ ] Know that nobody can guarantee you a listing. Anyone who says they can, for a
      fee, is running a scam.
