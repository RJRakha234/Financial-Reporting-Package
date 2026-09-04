# Keeping shops from funding each other

The problem this solves, and the rule that solves it.

---

## The asymmetry nobody notices until it hurts

A shop discharges a loyalty promise one of two ways, and they are **not** equivalent:

| | What it costs the shop | What the shop gets |
|---|---|---|
| Customer redeems **in the shop** | a discount off its own bill | a return visit — it paid for footfall and received it |
| Customer converts to **MelaCoin** | the same rupees, in cash | **nothing** — the footfall lands on a different shop |

Conversion is strictly worse for the issuing shop. And there is a second cost that is
easy to miss:

> In an ordinary loyalty scheme a large share of points are **never redeemed**. That
> unclaimed value is silent profit. Conversion destroys it — a promise that might have
> cost the shop nothing becomes a guaranteed cash outflow.

So a shop with a generous earn rate that does not accept MelaCoin is a **one-way
valve**. Money leaves every month and never comes back. It will not notice for a
quarter, and then it will leave and tell the whole street.

---

## The rule

Over a rolling 30-day window, for each shop:

```
net outflow = (value of its points converted away)
            − (value of MelaCoin it accepted)

cap         = max(floor, tolerance_bps × its own sales in the window)
headroom    = cap − net outflow
```

A conversion is allowed only while it fits inside `headroom`. Defaults are a
**1% tolerance** and a **₹500 floor**.

Four deliberate choices in that formula:

1. **It is the net, not the gross.** A shop that gives and takes in rough balance is
   never constrained. That is the behaviour we want, so it is the behaviour that costs
   nothing.
2. **Accepting MelaCoin restores headroom, rupee for rupee.** The incentive to
   participate is arithmetic, not a lecture.
3. **The cap scales with the shop's own sales.** A shop doing ₹5 lakh a month can
   absorb ₹5,000 of leakage; a golgappa stall cannot. The floor stops a new shop from
   being frozen out on day one.
4. **Settlement payments do not count.** A shop paying its invoice does not mean it
   stopped being a net donor. This cap is about *fairness between shops*; how much a
   shop currently owes you is *credit risk*, a different number in
   `settlement.service.js`. Conflating them is the classic mistake.

### The rule of thumb to give a shopkeeper

> **Set your outflow tolerance at least as high as your loyalty programme already
> costs you.** At 2% earn cost and 2% tolerance, every point you issue can leave,
> because your own sales fund it. Set it lower and you are deliberately rationing.

There is a test for both sides of that sentence.

---

## What a customer sees when the cap bites

Never a dead end. The refusal names the exact number of points that *would* work:

```
Gupta Sweets can only release ₹500.00 more to MelaCoin this period,
which is 50000 point(s). Convert 50000 or fewer, or spend the rest at Gupta Sweets.
```

And critically: **the customer's points are never touched.** They stay in the shop
that issued them and can still be spent there as normal. The shop is protected; the
customer loses nothing.

---

## The better fix: steer, don't block

Blocking is the safety rail, not the mechanism. The mechanism is routing.

`balance.shopsNeedingFootfall()` ranks shops by how much more they have paid into the
network than taken back out. When a customer is holding MelaCoin and asking "where do
I spend this?", the app shows those shops first. Footfall flows back to whoever funded
it, the imbalance closes on its own, and nobody is ever refused.

This is also the same ranking machinery that later sells promoted placement — the
highest-margin revenue line in [the growth plan](../ROADMAP.md). The rebalancing
mechanism and the monetisation mechanism are one piece of code.

---

## Where it lives

| | |
|---|---|
| The rule | `app/src/services/balance.service.js` |
| Enforcement | `app/src/services/conversion.service.js` (in `quoteConversion`, so a customer is told *before* pressing the button) |
| Shop's view | `GET /api/vendor/balance`, and the dashboard card |
| Customer routing | `GET /api/wallet/spend-here` |
| Platform view | `GET /api/admin/balances` — sorted worst-first, with one-way valves named |
| Settings | `net_outflow_tolerance_bps`, `net_outflow_floor_paise` (platform-set), `conversion_budget_paise` (shop-set, can only tighten) |
| Tests | `app/test/balance.test.js` — 13 tests |

---

## What this does not fix

**A shop can still be a net donor within its cap.** That is intentional — some leakage
is the cost of being in a network. The cap bounds it; it does not eliminate it.

**A determined free-rider can accept MelaCoin and never issue points.** It costs you
nothing (it is reimbursed in rupees for real goods) and it costs other shops nothing
directly, but it takes footfall without contributing any. If this becomes common, the
answer is commercial, not technical: charge acceptance-only shops a higher
subscription tier.

**The window is rolling, so a shop's allowance recovers over time.** A shop being
drained slowly over many months will not trip the cap. Watch the trend on
`/api/admin/balances`, not just the status flag.
