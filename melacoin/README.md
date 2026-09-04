# MelaCoin

A loyalty network where **each shop sets its own rules**, and where points can
graduate into **one token that works everywhere**.

- A shop decides how many points ₹1 earns, and what one point is worth.
- A customer earns points automatically when they buy.
- They can spend those points at that shop — or convert them into **MelaCoin (MELA)**,
  a real ERC-20 token they can spend at *any* shop in the network, or move to their
  own crypto wallet.

Everything here runs. It is not a mockup.

---

## Run it in 30 seconds

You need [Node.js](https://nodejs.org) version 22.5 or newer. Nothing else — no
database to install, no `npm install` for the app.

```bash
cd melacoin/app
npm run seed     # creates a demo shop network with sample customers
npm start        # then open http://localhost:4000
```

Sign in with any of these (password `melacoin123` for all):

| Who | Email | What you'll see |
|---|---|---|
| Shopper | `anita@example.test` | Points at two shops, a MELA wallet, a convert button |
| Shop owner | `ramesh@chaicorner.test` | Rate settings, a working till, customers, money owed |
| Platform admin | `admin@melacoin.test` | Token price, backing ratio, shop invoices |

Run the tests with `npm test` (58 tests), and the contract tests with
`cd ../chain && npm install && npm test` (38 tests).

---

## What's in the box

```
melacoin/
├── app/          The application. Zero npm dependencies - plain Node.js.
│   ├── src/      Server, database, business rules
│   ├── public/   The four screens (shopper, shop, admin, landing)
│   └── test/     58 tests
├── chain/        The MelaCoin smart contracts (Solidity + Hardhat)
└── docs/         Start with 01-how-it-works.md
```

## Read next

| Document | Why you'd read it |
|---|---|
| [docs/01-how-it-works.md](docs/01-how-it-works.md) | **Start here.** The whole idea in plain English, no jargon. |
| [docs/02-architecture.md](docs/02-architecture.md) | How the pieces fit together, and why each one exists. |
| [docs/03-tokenomics.md](docs/03-tokenomics.md) | Supply, price, and the honest version of "where does value come from?" |
| [docs/04-money-math.md](docs/04-money-math.md) | Every formula, worked through with rupees. |
| [docs/05-compliance-india.md](docs/05-compliance-india.md) | The legal and tax reality in India. Read before you take a single rupee. |
| [docs/06-api.md](docs/06-api.md) | Every endpoint, with curl examples. |
| [docs/07-launch-checklist.md](docs/07-launch-checklist.md) | What must be true before you deploy to a real blockchain. |
| [docs/08-glossary.md](docs/08-glossary.md) | Every crypto word used here, defined simply. |
| [ROADMAP.md](ROADMAP.md) | **The plan.** Six phases from today to a listed token. |

---

## The one thing to understand before anything else

There are **two different kinds of value** in this system, and confusing them is
the mistake that sinks projects like this one.

**Points** are a promise by one shop. They only work at that shop. If Chai Corner
gives you 500 points, Chai Corner owes you a discount. Nobody else does. The shop
controls the rate, the expiry, and the cap.

**MelaCoin** is a token that works across the whole network and can leave the app
entirely. The moment a customer converts, the debt stops being the shop's problem
and becomes *yours*. That is why the app bills the shop the full rupee value at the
instant of conversion, and why the admin screen leads with a **backing ratio**: the
rupees you have actually collected, divided by the value of all the MELA you have
issued. If that number goes below 100%, you are printing money you cannot honour.

The code enforces this. `docs/04-money-math.md` shows the arithmetic.

---

## Honest warnings

This is working software and a sound design, but three things are genuinely hard,
and no amount of code fixes them:

1. **A token needs a reason to exist beyond going up in price.** Here that reason is
   burn-to-spend at real shops. If nobody spends it, MELA is just a number.
   Get the loyalty network working with *zero* token first. See the roadmap.
2. **India regulates this closely.** 30% tax on gains, 1% TDS on transfers,
   FIU-IND registration for anyone running an exchange-like service, and possible
   RBI prepaid-instrument rules the moment points become transferable value.
   `docs/05-compliance-india.md` has the detail. Get a lawyer before launch, not after.
3. **Listing on an exchange is the beginning of your obligations, not the end.**
   A listed token means a public price, public holders, and public scrutiny of every
   number on your admin dashboard.

None of that means don't build it. It means build the business first and the token
second — which is exactly how the roadmap is ordered.
