# The money math

Every formula in the system, worked through with real rupees. If you only read one
technical document, read this one — these rules are what the code actually enforces,
and they are tested in `app/test/money.test.js` and `app/test/loyalty.test.js`.

---

## Rule zero: no decimals, ever

Money is stored in **paise** (1 rupee = 100 paise) as whole numbers. Points are
whole numbers. Nothing is ever stored as `2.5` or `19.99`.

Why: computers store decimals approximately. `0.1 + 0.2` genuinely equals
`0.30000000000000004`. Over a million transactions, those slivers add up to real
money going missing, and you will not be able to explain where. Whole numbers can
never drift.

So a vendor's "2.5 points per rupee" is stored as `2500` **milli-points per rupee**,
and "1 point = 5 paise" is stored as `5000` **milli-paise per point**.

**Rounding is always in the customer-unfavourable direction for us**: we round *down*
what we give out and *up* what we ask for. A rounding error can then never create
value out of thin air. There is a test that proves converting points → rupees →
points can never give you back more than you started with.

---

## What a vendor controls

| Setting | Stored as | Example |
|---|---|---|
| Points per ₹1 spent | `earn_milli_points_per_rupee` | 2.5 points/₹ → `2500` |
| Value of one point | `redeem_milli_paise_per_point` | ₹0.10 → `10000` |
| Smallest redemption | `min_redeem_points` | `100` |
| Max share of a bill points may pay | `max_redeem_bps` | 30% → `3000` |
| Points expiry | `points_expiry_days` | `365` (0 = never) |
| Earn on cash paid, not full bill | `earn_on_net` | `true` |
| Allow conversion to MELA | `allow_mela_conversion` | `true` |
| Conversion fee | `mela_conversion_fee_bps` | 2% → `200` |
| Accept MELA as payment | `accepts_mela` | `true` |

"bps" means basis points: hundredths of a percent. 10000 bps = 100%. Percentages are
stored this way for the same reason as money — no decimals.

---

## Earning

```
points_earned = floor( paise × milli_points_per_rupee ÷ 100,000 )
```

The 100,000 is `100` (paise per rupee) × `1000` (the milli- multiplier).

**Worked example** — ₹500 bill at 2.5 points per rupee:

```
50000 paise × 2500 ÷ 100000  =  1250 points
```

Sanity check: ₹500 × 2.5 = 1250. ✓

Rounding down means a ₹9.99 bill at 1 point/₹ earns 9 points, not 10.

### On the bill, or on the cash?

If `earn_on_net` is on (recommended), the earn is calculated on what the customer
actually paid after their points discount. If it is off, it is on the full bill.

Why it matters: with it off, points earn points. A customer redeems ₹50 of points,
and gets fresh points on that ₹50 too. Small per transaction, compounding over a
year, and it silently widens every discount the shop thought it was giving.

---

## Redeeming

```
discount_paise = floor( points × milli_paise_per_point ÷ 1000 )
```

**Worked example** — 1250 points at ₹0.10 each:

```
1250 × 10000 ÷ 1000  =  12500 paise  =  ₹125.00
```

### The cap

```
max_discount_paise  = min( bill, floor(bill × max_redeem_bps ÷ 10000) )
max_points_usable   = floor( max_discount_paise × 1000 ÷ milli_paise_per_point )
```

**Worked example** — a ₹1000 bill, 30% cap, points worth ₹0.10:

```
max_discount = floor(100000 × 3000 ÷ 10000) = 30000 paise = ₹300
max_points   = floor(30000 × 1000 ÷ 10000)  = 3000 points
```

Offering 3001 points is rejected with a message naming the real limit, rather than
being silently trimmed — the cashier needs to know why the number changed.

### The full bill

```
net_paise = bill − points_discount − mela_payment
```

---

## Expiry, and why points live in "lots"

Every earning creates a **lot**: a dated parcel of points with its own expiry.
A balance is the sum of the live lots, never a single stored number.

This costs a little complexity and buys three things:

1. **Correct expiry.** Points earned in March expire in March, not "some points expire".
2. **A provable balance.** You can always show a customer which lots make up their total.
3. **The friendliest spend order.** When points are spent, the app drains the lot that
   **expires soonest** first, so the customer loses as little as possible to expiry.

Expiry runs automatically before every balance read, and writes an `EXPIRE` event, so
a customer whose points vanished can be shown exactly when and why.

---

## Converting points into MelaCoin

```
gross_paise = floor( points × milli_paise_per_point ÷ 1000 )
fee_paise   = floor( gross_paise × fee_bps ÷ 10000 )
net_paise   = gross_paise − fee_paise
mela_wei    = net_paise × 10^18 ÷ mela_price_paise
```

`wei` is how token amounts are counted — MELA has 18 decimal places, so 1 MELA is
`1000000000000000000` wei. That number is too big for ordinary JavaScript numbers,
so the code uses BigInt for it. (This is standard for every ERC-20 token.)

**Worked example** — 10,000 points at ₹0.10 each, 2% fee, MELA at ₹1.00:

```
gross = 10000 × 10000 ÷ 1000  = 100000 paise = ₹1000.00
fee   = 100000 × 200 ÷ 10000  =   2000 paise =    ₹20.00
net   =                          98000 paise =   ₹980.00
mela  = 98000 × 10^18 ÷ 100   = 980 × 10^18  =   980 MELA
```

If MELA were priced at ₹4.00 instead, the same points would give 245 MELA. The
customer's *rupee value* is identical; only the token count changes.

---

## Who owes whom

This is the part that decides whether the business is solvent. There are two ledgers
and one sign convention:

> **Positive = the shop owes the platform. Negative = the platform owes the shop.**

**When a customer converts points to MELA:**

| Party | Effect |
|---|---|
| Customer | loses points, gains MELA |
| Shop | **+ gross_paise** — its loyalty debt moved to you, so it owes you the full value |
| Platform | holds `net_paise` as backing, keeps `fee_paise` as revenue |

**When a customer pays a shop with MELA:**

| Party | Effect |
|---|---|
| Customer | loses MELA |
| Shop | **− value_paise** — it gave real goods for tokens, so you owe it rupees |
| Platform | pays out of the treasury; the MELA is retired |

At the end of a cycle each shop has one net figure and you make one transfer in one
direction. In the demo data, Chai Corner owes ₹50 (a customer converted) while a
shop that accepted MELA is owed rupees.

**Redeeming points normally creates no settlement entry at all.** The shop gave its
own discount, on its own bill, funded by its own margin. Nothing moves between you.

---

## The backing ratio

```
                  rupees collected from conversions − rupees paid out to shops
backing_ratio = ------------------------------------------------------------------
                        rupee value of all MelaCoin currently outstanding
```

100% or more means every token in circulation is covered by money you hold. Below
100% means you have issued tokens you cannot honour.

There is a test asserting this lands at exactly 100% after a conversion. If you ever
change the settlement logic, that test is the one that will tell you that you broke
the business model.

---

## Idempotency: why bills need a key

Every POS call carries an `idempotency_key` — the shop's own bill number works well.
Send the same key twice and you get the *original* receipt back, not a second sale.

This is not a nicety. Shop wi-fi drops mid-request all the time. Without this, a
retry charges the customer's points twice and there is no way to tell a genuine
duplicate from a customer who really did buy two identical chais.
