# Architecture

## The shape of it

```
   Shop's billing counter                 Customer's phone            Your console
   (x-api-key)                            (login token)               (login token)
          │                                     │                           │
          └──────────────┬──────────────────────┴───────────────────────────┘
                         ▼
              ┌──────────────────────┐
              │   Node.js server     │   No frameworks, no dependencies.
              │   app/src/index.js   │
              └──────────┬───────────┘
                         │
       ┌─────────────────┼──────────────────┬────────────────────┐
       ▼                 ▼                  ▼                    ▼
  loyalty.service   conversion.service  token.service    settlement.service
  earn / redeem     points → MELA       MELA balances    who owes whom
  expire / lots     (the 3-step commit)  + vouchers
       │                 │                  │                    │
       └─────────────────┴────────┬─────────┴────────────────────┘
                                  ▼
                          ┌───────────────┐
                          │    SQLite     │   Built into Node. One file on disk.
                          └───────────────┘
                                  │
                     CHAIN_MODE=chain only ▼
                          ┌────────────────────────┐
                          │  Polygon (or any EVM)  │
                          │  MelaCoin ─ Distributor│
                          │           └─ Vesting   │
                          └────────────────────────┘
```

## Why these choices

**No npm dependencies in the app.** Every package is a supply-chain risk and a
future upgrade problem, and for a financial ledger that trade is not worth it. Node
22 ships an HTTP server, a SQLite database, and a crypto library. That is the whole
requirement. It also means `npm start` works forever, offline, with nothing to install.

**SQLite, not Postgres.** One file, no server, transactional, and good for millions
of rows. When you outgrow it you will know, because writes will start queueing —
that is a Phase 3 problem, not a Phase 1 one. The schema is ordinary SQL and moves
to Postgres with small changes.

**Balances are never stored.** Points live in dated *lots*; MELA lives in an
append-only *ledger*. A balance is always a SUM over history. A stored balance can
silently drift from its history and you can never prove which one is right; a derived
balance cannot.

**The blockchain is optional.** `CHAIN_MODE=mock` runs the entire product with no
chain at all — and that is the correct mode for your first year. Flipping to `chain`
changes only what happens at withdrawal.

---

## The files that matter

| File | What lives there |
|---|---|
| `app/src/money.js` | Every formula. No money maths anywhere else. |
| `app/src/db.js` | The schema, and the `transaction()` helper. |
| `app/src/services/loyalty.service.js` | Earning, redeeming, expiry, lots. |
| `app/src/services/conversion.service.js` | Points → MELA, the three-step commit. |
| `app/src/services/token.service.js` | MELA balances and withdrawal vouchers. |
| `app/src/services/settlement.service.js` | The shop ↔ platform account, and the backing ratio. |
| `app/src/index.js` | The HTTP server and the request context. |
| `chain/contracts/` | The three Solidity contracts. |

---

## Three ideas worth understanding

### 1. Quote and commit run the same code

`quotePurchase()` computes a bill and changes nothing. `recordPurchase()` calls
`quotePurchase()` and then writes the result. So the number a cashier sees while
typing and the number that gets saved cannot drift apart — there is one
implementation, not two that must be kept in sync.

The conversion flow does the same, and additionally lets the client send back the
MELA amount it was quoted. If the price moved in between, the request is refused
rather than silently repriced.

### 2. Everything that must happen together, happens together

Converting points to MELA touches four tables. If the process died halfway, a
customer could lose points and receive nothing. So the whole thing runs inside one
database transaction: all four writes commit, or none do.

There is a test that attempts an oversized conversion and then asserts that the
points, the token balance, the settlement ledger, and the conversions table are all
exactly as they were.

### 3. The backend never holds tokens

When a customer withdraws MELA on-chain, the server does not send a transaction. It
signs a **voucher** — "this address may claim this much, once, before this deadline"
— and the customer submits it to `MelaDistributor.claim()`.

The reserve therefore sits in a contract, not in a hot wallet. The signing key can
authorise but not take, and the contract enforces a **daily payout ceiling**, so the
worst case if that key leaks is bounded and visible. Vouchers carry one-time nonces;
replaying one reverts.

---

## Authentication

There are two kinds of caller and they are deliberately different.

**People** (shoppers, shop owners, admins) log in with an email and password and get
a bearer token. Passwords are hashed with scrypt and a random salt. Session tokens
are stored only as SHA-256 hashes, so a stolen database cannot be used to log in as
anyone.

**Tills** authenticate with an API key in the `x-api-key` header — a shop's billing
counter should not hold the owner's password. The key is shown exactly once at
creation; only its hash is stored. Rotating it invalidates the old one immediately.

Role checks are enforced server-side on every request. There are tests asserting that
a customer token gets a 403 on vendor and admin endpoints.

---

## What is deliberately missing

This is an honest list of what you must add before real money moves. None of it is
hard; all of it is skipped in a demo.

- **KYC** on customers who convert or withdraw (see the compliance doc).
- **TDS handling** under section 194S.
- **Email/SMS verification** — right now any email address can register.
- **Password reset.**
- **Backups.** SQLite is one file; copy it somewhere else on a schedule.
- **A price feed** once MELA is listed, instead of an admin typing the price.
- **Postgres and a real job queue** when one server stops being enough.
- **Structured logging and alerting**, especially on the backing ratio.
