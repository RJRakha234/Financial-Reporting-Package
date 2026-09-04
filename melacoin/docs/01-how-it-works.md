# How it works, in plain English

No jargon in this document. If a word needs explaining, it is explained in
[08-glossary.md](08-glossary.md).

---

## The everyday version

You know the paper card at a coffee shop where they stamp a square each time you
buy, and the tenth coffee is free? MelaCoin is that, with three changes:

1. **It's digital**, so nobody loses the card.
2. **Every shop sets its own deal.** The chai shop can be generous, the grocery
   store can be careful. Neither has to ask you.
3. **The stamps can leave the shop.** If you have a pile of chai-shop points you'll
   never use, you can turn them into MelaCoin and spend it at the grocery store —
   or keep it, or send it to a friend.

That third change is the whole product. Everything else is plumbing.

---

## The four people involved

**The shopper.** Buys things. Earns points automatically. Spends them, or upgrades
them to MelaCoin.

**The shop.** Decides the rules for its own shop. Gives discounts to bring people
back. Gets a dashboard and a "till key" so its billing counter can talk to the app.

**You, the platform.** Run the software. Take a small fee when points become
MelaCoin. Hold the money that backs the token.

**The token holder.** Anyone holding MelaCoin, including shoppers who converted and
people who bought it on an exchange later.

---

## A day in the life

Anita buys chai for **₹500** at Chai Corner.

**Chai Corner's settings** (the owner chose these):
- 2 points per ₹1 spent
- 1 point is worth ₹0.05
- Points can pay at most 50% of any bill
- Points expire after 180 days

**Step 1 — She earns.** ₹500 × 2 = **1,000 points**. Those points are worth
1,000 × ₹0.05 = **₹50** at Chai Corner. So Chai Corner effectively gave a 10%
discount, payable later. (10% is high — the dashboard warns the owner about this.)

**Step 2 — She spends them.** Next visit, a ₹400 bill. She uses 1,000 points,
which knock **₹50** off. She pays **₹350** in cash.

She earns points on the ₹350, not on the ₹400. This matters: if points earned
points, a customer could bounce value back and forth and the shop's costs would
compound. The shop can switch this off, but shouldn't.

**Step 3 — Or she upgrades instead.** Say Anita is moving cities and will never
visit Chai Corner again. Her 1,000 points are about to become worthless. So she
converts:

```
1,000 points  ×  ₹0.05        =  ₹50.00     what they're worth at Chai Corner
                −2% platform fee =  −₹1.00
                                   ------
                                    ₹49.00   value she keeps
₹49.00  ÷  ₹1.00 per MELA      =  49 MELA
```

Three things happen at that exact moment, all together or not at all:

1. Her 1,000 Chai Corner points are **destroyed**.
2. She is credited **49 MELA**.
3. **Chai Corner is billed ₹50** — the full value of the promise it no longer owes her.

Step 3 is the one everybody forgets. Skip it and you are paying for every shop's
marketing out of your own pocket, silently, until you run out of money.

**Step 4 — She spends MelaCoin at a different shop.** FreshMart accepts MELA. She
pays a ₹200 bill with 49 MELA (₹49). FreshMart gave away ₹49 of groceries, so
**you owe FreshMart ₹49** — paid out of the ₹50 you collected from Chai Corner.
You kept ₹1 as the fee. The books balance.

**Step 5 — Or she takes it out of the app.** She has a crypto wallet. She
withdraws, and the MELA becomes hers on the public blockchain. You can never take
it back. If MELA is listed on an exchange, she can sell it there.

---

## Why would anyone want MelaCoin?

Be suspicious of any answer that is only "because it will go up in price". Here are
the real ones:

**For the shopper:** points that would have expired at one shop become spendable
everywhere. That is a genuine upgrade in usefulness, and it is the reason they will
convert.

**For the shop:** accepting MELA brings in customers who earned their points
somewhere else. It's footfall you didn't pay for. And when a customer converts, that
shop's loyalty liability is *cleared* — the debt leaves its books for a known fee.

**For you:** a fee on every conversion, and a treasury that holds real rupees behind
the tokens.

**For the token to have value at all:** every MELA spent at a shop is **burned** —
destroyed permanently. So genuine usage shrinks the supply. That is a real economic
link between "people use this" and "this is worth something", which most tokens do
not have.

---

## What the token is not

It is **not** an investment product, and you must not market it as one. The moment
you say "buy MelaCoin, it will be worth more later" you have probably created an
unregistered security, and in India you have walked into a whole body of law you do
not want to meet by accident. See [05-compliance-india.md](05-compliance-india.md).

It is **not** money. It is a voucher that happens to be transferable.

It is **not** free to issue. Every MELA in existence is a rupee you owe someone.

---

## The one number that tells you if you're solvent

The admin screen shows a **backing ratio**:

```
              rupees you actually collected from shops
backing  =  --------------------------------------------
             rupee value of all the MelaCoin you issued
```

At 100%, every token is covered. Below 100%, you have issued promises you cannot
keep, and the only question left is when people find out. The app calculates this
from the real ledgers, not from an estimate.

---

## What to build first

Not the token.

Build the loyalty network. Get 20 shops actually using it, with a till key plugged
into their real billing counter, giving real points to real customers. That part is
useful on day one and needs no blockchain at all — the app already runs this way
(`CHAIN_MODE=mock`).

Only when shops are using it daily does MelaCoin add anything. A token with nobody
to spend it with is just a spreadsheet with extra legal risk.

The [roadmap](../ROADMAP.md) is ordered exactly this way.
