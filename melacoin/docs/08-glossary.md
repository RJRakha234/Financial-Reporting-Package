# Glossary

Every technical word used in this project, in plain language.

**Backing ratio** — The rupees you have actually collected, divided by the value of
all the MelaCoin you have issued. Below 100% means you owe more than you hold.

**Basis point (bps)** — One hundredth of a percent. 100 bps = 1%, 10000 bps = 100%.
Used so percentages can be stored as whole numbers.

**Burn** — Permanently destroying tokens. They are gone; total supply drops. MELA is
burned when it is spent at a shop.

**Cap (supply cap)** — The maximum number of tokens that can ever exist, enforced by
the contract. Minting past it fails.

**Custodial** — You hold it on the customer's behalf. Custodial MELA is a row in your
database. Non-custodial means it is in their own wallet and you cannot touch it.

**EIP-712 / typed signature** — A standard way to sign a structured message (not a
transaction) so a smart contract can verify who signed it. How withdrawal vouchers work.

**EIP-2612 / permit** — Lets someone approve a token spend with a signature instead of
a separate on-chain transaction. Saves the customer a step and a fee.

**ERC-20** — The standard every ordinary token on Ethereum-style chains follows. It
is why any wallet or exchange can handle MELA without custom work.

**EVM** — Ethereum Virtual Machine. The engine that runs smart contracts. Polygon,
Arbitrum, Base and others all run it, so the same contract works on all of them.

**Gas** — The fee paid to put a transaction on a blockchain. On Ethereum this can be
several hundred rupees; on Polygon it is a fraction of a rupee. This is why the chain
choice matters.

**Idempotency key** — A unique label on a request so that sending it twice does the
work once. A shop's bill number. Protects against flaky wi-fi charging a customer twice.

**Lot** — A dated parcel of points with its own expiry date. Balances are the sum of
live lots. Lets expiry be exact and provable.

**Mint** — Creating new tokens. Only addresses with `MINTER_ROLE` can, and never past
the cap.

**Multisig** — A wallet that needs several people to approve any action. Where your
admin rights should live on mainnet, so one stolen laptop is not fatal.

**Nonce** — A number used once. Each withdrawal voucher has one, so a voucher cannot
be replayed.

**Paise** — 1/100 of a rupee. All money in this system is stored as whole paise.

**PPI (Prepaid Payment Instrument)** — RBI's category for stored-value instruments.
Whether your points fall into it is the key legal question. See the compliance doc.

**Reserve** — The MELA sitting in the distributor contract, available for customers to
withdraw.

**Scrypt** — The password-hashing function used here. Deliberately slow, which makes
guessing passwords expensive.

**Settlement** — Squaring up who owes whom between a shop and the platform.

**Smart contract** — A program that runs on a blockchain. Once deployed, it does
exactly what it says and nobody, including you, can change it (unless you built in a
way to).

**Testnet** — A practice blockchain using worthless tokens. Where you deploy first,
for weeks, before touching real money.

**TDS** — Tax Deducted at Source. Section 194S imposes 1% on VDA transfers in India.

**Utility token** — A token you use for something, as opposed to one you hold hoping
the price rises. MELA is meant to be the former. Regulators look closely at whether
you actually behave that way.

**VASP** — Virtual Asset Service Provider. If you are one, FIU-IND registration and
AML obligations apply.

**VDA (Virtual Digital Asset)** — India's legal term for crypto. Triggers the 30% tax
and 1% TDS regime.

**Vesting** — Releasing tokens gradually over time instead of all at once. A cliff
means nothing is released before a certain date.

**Voucher** — Here, a signed message saying an address may withdraw a set amount once
before a deadline. Lets customers pull tokens without your server holding any.

**Wei** — The smallest unit of a token. 1 MELA = 10^18 wei, because MELA has 18
decimal places. Too big for ordinary JavaScript numbers, so the code uses BigInt.
