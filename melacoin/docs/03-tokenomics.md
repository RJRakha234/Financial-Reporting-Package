# Tokenomics

"Tokenomics" just means: how many tokens exist, who gets them, and why anyone would
want one. Most token projects fail this document, not the code.

---

## The supply

| Item | Value | Why |
|---|---|---|
| Name / symbol | MelaCoin / **MELA** | |
| Decimals | 18 | The ERC-20 standard. Don't be clever here. |
| Maximum supply | **1,000,000,000** (1 billion) | Enforced in the contract. Minting past it reverts — it is not a policy you can quietly change. |
| Minted at launch | 200,000,000 (20%) | Everything else must be minted deliberately, later. |

The cap is in `MelaCoin.sol` via OpenZeppelin's `ERC20Capped`. There is a test that
mints right up to the cap and proves the next wei reverts. This matters because
"we promise not to print more" is worth nothing, and "the code cannot print more" is
worth everything.

## A starting allocation

These numbers are a **suggestion**, not a law. Decide them yourself, publish them,
and then never quietly change them.

| Bucket | Share | Purpose | Lock-up |
|---|---:|---|---|
| Rewards reserve | 50% | Backs customer conversions. This is the working supply. | Released as conversions happen |
| Treasury / operations | 20% | Running costs, partnerships, market making | 2-year linear release |
| Team & founders | 15% | You | **1-year cliff, 4-year vest**, on-chain in `MelaVesting.sol` |
| Shop incentives | 10% | Paying shops to join early | Released against signed shops |
| Liquidity | 5% | Exchange listing and market depth | At listing |

The team lock-up is not optional in practice. It is the first thing a serious
exchange, investor, or journalist checks. `MelaVesting.sol` makes it verifiable by
anyone: deploy it, fund it, create the schedule, and publish the address.

---

## Where does the value actually come from?

Be ruthless with yourself here, because this is the question that gets asked.

**The honest answer:** each MELA was created because a shop owed a customer that
much money in loyalty value, and that shop has been billed for it in rupees. The
token is backed by a receivable. Its floor is that you, the platform, will accept it
at face value for goods at any participating shop.

**The demand side:** MELA is burned when it is spent. A customer paying a shop with
MELA destroys those tokens permanently — `MelaDistributor.spend()` calls `burnFrom`,
and there is a test proving total supply drops. So real usage permanently shrinks the
float. Supply goes down as the network grows.

**The dishonest answer to avoid:** "the price will go up because supply is limited."
Scarcity alone is not value. There is a limited supply of my old college notes too.

---

## Price: two completely different worlds

**Before listing**, the price is a number you set in the admin console. It is not a
market price; it is a promise. Treat it that way:

- Start at a round number: **1 MELA = ₹1.00**. It makes the maths legible to shops
  and customers, which matters more than sophistication at this stage.
- Change it rarely and announce it in advance.
- Never raise it to make your treasury look better. The backing ratio is calculated
  at the current price, so raising the price *lowers* your backing. The admin
  dashboard will show you this immediately.

**After listing**, the market sets the price and you no longer control it. This
changes the system, and you must plan for it:

- Feed your `mela_price_paise` from the exchange price, on a schedule. Do not type it.
- If the app's price is *above* the market price, people buy cheap MELA on the
  exchange and spend it at shops at your inflated rate. You pay the difference,
  forever, until you notice.
- If the app's price is *below* market, customers stop converting and stop spending
  in-app, and your token's actual utility dies quietly.
- Consider a spread (buy/sell rates a few percent apart) so small movements do not
  bleed you.

This asymmetry is the single biggest operational risk in the whole design.

---

## Which blockchain

**Recommendation: Polygon PoS.** Reasons that matter for this specific product:

- Transaction fees are fractions of a rupee. Your customers are converting ₹50 of
  chai points; a ₹200 gas fee makes the whole thing absurd.
- Full Ethereum tooling, so every wallet and exchange already understands it.
- Indian users and exchanges are broadly familiar with it.

Ethereum mainnet is wrong here — fees can exceed the value being moved. A private or
permissioned chain is also wrong: if you control the chain, the token is a database
row with extra steps, and no exchange will list it.

The contracts compile for `evmVersion: paris`, so they also deploy unchanged to
Arbitrum, Base, BSC, and most other EVM chains if you change your mind later.

---

## The gas problem, and how this design dodges it

A customer converting ₹50 of points does not have MATIC to pay for a transaction,
does not have a wallet, and probably does not want one.

So the app does **not** put every customer on-chain. MELA balances live in the app
database (custodial) until the customer explicitly withdraws. Most never will, and
that is fine — they can still spend at shops.

When someone does withdraw, the backend does not send a transaction either. It
**signs a voucher** — a message saying "this address may claim this much, once,
before this deadline" — and the customer (or a relayer you pay for) submits it to
`MelaDistributor.claim()`.

Why that shape:

- Your backend key never holds tokens, so leaking it cannot drain the reserve.
- The contract has a **daily payout cap**, so even a leaked signing key has a bounded
  blast radius. There is a test for it.
- Each voucher has a one-time nonce, so replaying it reverts. There are tests for
  replay, expiry, tampering with the amount, and redirecting to another address.

---

## Getting listed on an exchange

The order that actually works:

1. **Real usage first.** Exchanges ask for user numbers and transaction volume. "We
   have 40 shops and 5,000 monthly active users" is a conversation. A whitepaper is not.
2. **A security audit.** Budget ₹4–15 lakh for a reputable firm. Non-negotiable for
   a token holding other people's money.
3. **Legal opinion** that MELA is a utility token in the jurisdictions you operate in.
4. **A decentralised exchange first** (Uniswap/QuickSwap on Polygon). Permissionless,
   you can list tomorrow, and it creates the price history a centralised exchange
   will ask to see.
5. **Then Indian exchanges** (CoinDCX, WazirX) or international ones. They will ask
   for the audit, the legal opinion, the vesting contract, and the usage numbers.

Listing fees at centralised exchanges range from "free, if they want you" to tens of
lakhs. Anyone promising a guaranteed listing for an upfront fee is running a scam.

---

## Three ways this design can still go wrong

1. **Shops don't pay their settlement invoices.** Your backing ratio silently falls
   while the dashboard still shows tokens issued. Mitigation: collect a deposit from
   shops, or settle weekly rather than monthly, and stop conversions for shops in arrears.
2. **A shop sets an absurd earn rate** to farm tokens out of your treasury. The app
   caps rates and shows a live warning, but you should also review new shops manually
   and cap total conversions per shop per month.
3. **Everyone converts at once**, e.g. after a price rumour. You owe every shop's
   liability in cash immediately. Mitigation: minimum conversion amounts, a per-day
   conversion cap, and never letting your treasury dip below 100% backing.
