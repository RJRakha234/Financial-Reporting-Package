# Roadmap

Six phases, roughly 18 months, ordered by one principle:

> **Build the business first, the token second.**

The reason is not caution for its own sake. A token is only worth something if there
is somewhere to spend it. Launch the token first and you get speculators, legal
exposure, and no shops. Launch the loyalty network first and the token becomes an
obvious upgrade to something people already use.

Each phase has a **gate** — a condition that must be true before you move on. If a
gate is not met, the answer is to stay in that phase, not to push forward and hope.

---

## Phase 0 — Understand what you have · *this week*

Nothing to build. The code is written.

- [ ] Run it: `cd app && npm run seed && npm start`
- [ ] Sign in as all three roles. Do a sale. Redeem points. Convert to MELA.
- [ ] Read [docs/01-how-it-works.md](docs/01-how-it-works.md), then
      [docs/04-money-math.md](docs/04-money-math.md).
- [ ] Read [docs/05-compliance-india.md](docs/05-compliance-india.md) and book a
      call with a payments lawyer. Start this now — it has the longest lead time of
      anything in this document.

**Gate:** you can explain, without notes, what happens to the money when a customer
converts points into MelaCoin.

---

## Phase 1 — One real shop · *month 1–2*

Prove a shopkeeper will actually use this. No token. No blockchain.

**Build**
- Email/phone verification and password reset (the demo has neither).
- A phone-friendly customer view (the current UI works on a phone; make it good).
- A shop-facing "enter bill amount, look up customer" screen the counter staff can
  use without training.

**Do**
- Find **one** shop. Ideally someone you know, who already runs a paper punch card.
- Sit at their counter for a full day. Watch what actually happens at billing.
- Set their rates with them, using the live warning on the dashboard.

**Cost:** essentially nothing. Rent a ₹500/month server.

**Gate:** that shop uses it for 30 consecutive days without you being in the room,
and the owner asks for something rather than forgetting it exists.

---

## Phase 2 — Twenty shops · *month 3–6*

Turn one anecdote into a pattern. Still no token.

**Build**
- Self-service shop onboarding.
- Integrations with whatever billing software your shops actually use (in India,
  often Petpooja, Zoho Books, Vyapar, or a plain Android POS). The
  [POS API](docs/06-api.md) is already designed for this.
- A daily digest email for shop owners: sales, points issued, liability outstanding.
- Move to Postgres if you are creaking. You probably are not yet.
- Backups you have tested restoring.

**Do**
- Sign 20 shops in **one neighbourhood**, not 20 scattered across a city. Density is
  what makes a network useful to a customer.
- Watch which rates shops choose. If most pick something that costs them 10%+ of
  revenue, your defaults are wrong and they will churn when they notice.

**Cost:** ₹1–3 lakh (a developer's time, hosting, some shop incentives).

**Gate:** 20 active shops, 500+ monthly active customers, and — the number that
actually matters — customers who earned at one shop are *asking* whether they can use
their points at another. That question is the demand for MelaCoin. If nobody asks it,
do not build the token.

---

## Phase 3 — Legal and financial groundwork · *month 6–9, in parallel*

This runs alongside Phase 2. Do not leave it until you need it.

**Do**
- Engage a payments lawyer **and** a CA with genuine VDA experience. Budget
  ₹1.5–5 lakh for written opinions.
- Get answers on: PPI status, section 194S mechanics, FIU-IND registration,
  GST treatment. The questions are listed in
  [docs/05-compliance-india.md](docs/05-compliance-india.md).
- Register the company properly if you have not. Open a **segregated** treasury
  account.
- Design KYC into the signup flow, and TDS into the conversion flow, before writing
  either.

**Gate:** you have written legal advice saying what you may and may not do. If it
comes back saying the token is a bad idea in India as designed, **stop here** — you
still have a working loyalty business, which is a real business, and you have lost
nothing.

---

## Phase 4 — The token, on a testnet · *month 9–12*

Only now.

**Build**
- Deploy `MelaCoin`, `MelaDistributor`, `MelaVesting` to **Polygon Amoy testnet**.
- Switch the app to `CHAIN_MODE=chain` and run the full conversion and withdrawal
  flow with test tokens.
- Add KYC gating on conversion and withdrawal.
- Add TDS handling per your CA's advice.
- Add per-shop and per-day conversion caps.
- Build shop invoicing and settlement collection. **Conversions must stop for shops
  in arrears** — otherwise your backing ratio quietly falls.
- Alerting on the backing ratio.

**Do**
- Commission a **security audit**. ₹4–15 lakh. Fix everything, publish the report.
- Set up a multisig for admin rights. Move every role to it.
- Publish the tokenomics page and the vesting contract address.
- Run 50–100 real customers through conversion on testnet.

**Cost:** ₹6–20 lakh, mostly the audit.

**Gate:** audit passed and published, legal opinion in hand, multisig live, and a
month of testnet conversions with no accounting discrepancies. Work through
[docs/07-launch-checklist.md](docs/07-launch-checklist.md) line by line.

---

## Phase 5 — Mainnet · *month 12–15*

**Do**
- Deploy to Polygon mainnet from a clean key, with the multisig as admin.
- Fund the distributor reserve conservatively — start small, top it up.
- Set the daily claim cap to an amount you could survive losing.
- Enable conversion for a **small subset** of shops first. Watch the backing ratio
  daily.
- Publish contract addresses everywhere: your site, the docs, the block explorer
  verification.

**Gate:** 30 days on mainnet, backing ratio never below 100%, no incidents, and
shops settling their invoices on time.

---

## Phase 6 — Liquidity and listing · *month 15–18+*

**Do**
- Provide liquidity on a decentralised exchange first (QuickSwap or Uniswap on
  Polygon). Permissionless, immediate, and it creates the price history a centralised
  exchange will ask to see.
- Switch the app's price to a **feed** from that market. Stop typing it.
- Build the spread protection described in
  [docs/03-tokenomics.md](docs/03-tokenomics.md) — if your in-app price and the
  market price diverge, arbitrage will bleed you.
- Only then approach centralised exchanges, with: usage numbers, the audit, the legal
  opinion, the vesting contract, and a public tokenomics page.

**Gate:** there is no gate here. This phase never really ends — a listed token means
permanent public scrutiny of every number on your admin dashboard.

---

## What could kill this, ranked

| Risk | Where it bites | What to do about it |
|---|---|---|
| **Shops don't care** | Phase 1–2 | The whole reason those phases come first. If shop 1 forgets you exist, the token cannot save you. |
| **Legal answer is "no"** | Phase 3 | Ask early, when the cost of stopping is a few months rather than two years. |
| **Shops don't pay settlements** | Phase 5 | Weekly invoicing, deposits, and hard-stop conversions for anyone in arrears. |
| **Price divergence after listing** | Phase 6 | A price feed and a spread, built *before* listing, not after. |
| **A treasury run** | Phase 5+ | Conversion caps, minimum amounts, and never dipping below 100% backing. |
| **You get bored of the boring part** | Everywhere | The loyalty network *is* the product. The token is a feature of it. |

---

## The honest summary

You have a working loyalty platform today. That alone is a real business, it is legal,
and it can make money from shop subscriptions with no crypto involved at all.

MelaCoin makes it substantially more interesting — but it adds tax complexity, AML
obligations, a security surface, and permanent public scrutiny. Add it when shops and
customers are asking for it, not before.

If you take one thing from this roadmap: **the gate at the end of Phase 2 is the real
decision point.** If customers are asking to use their points across shops, build the
token. If they are not, you have still built something worth having.
