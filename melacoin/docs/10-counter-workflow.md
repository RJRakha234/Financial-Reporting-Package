# The counter workflow

What actually happens at the till, second by second, and the rules behind it.

Open it at **`/counter.html`** on the phone that lives at the counter. Add it to the
home screen. It asks for the till key once, then never again on that phone.

---

## The whole loop

```
      phone number  ──▶  known?  ──yes──▶  their points  ──▶  bill amount  ──▶  done
                            │                                                    │
                            no                                                   │
                            ▼                                                    ▼
                       "Add 9800000001"                                       Undo
                       (one tap, name optional)
```

Four taps from "who is this" to "done". Never more.

| Step | What the shopkeeper does | What the screen shows |
|---|---|---|
| 1 | Types the phone number | Big numeric field, nothing else |
| 2 | Taps **Find** | Their name, their points, and either **FREE CHAI READY** or a progress bar to the next one |
| 3 | Types the bill | A **Use N points** toggle if they have enough, and a button that says exactly what cash to take: *Take ₹10.00* |
| 4 | Taps the button | A tick, the receipt, and how many points until their next free one |

The button never says "Submit". It says the amount of money to put in the drawer.

---

## Signing someone up

A chai customer will give a phone number. They will not give an email, invent a
password, or install anything. So the counter creates the account:

```
POST /api/pos/enroll   { "phone": "+91 98000 00001", "name": "Snigdha" }
```

- **The number is normalised.** `+91 98000 00001`, `098000-00001` and `9800000001`
  are one person. At a counter nobody types canonically.
- **A name is optional.** Without one they become *Customer 0001* — recognisable
  because it ends in the digits they just read out. If a shop later learns the real
  name, it replaces the placeholder. A name the *customer* chose is never overwritten.
- **Calling it twice is safe.** Two taps, or a customer another shop already enrolled,
  returns the same person. Customers belong to the network, not to the shop that
  happened to add them first.
- **The account has no password.** It cannot be logged into until the customer claims
  it by registering with the same phone number — at which point they keep every point
  a shop ever gave them. Without that path, anyone enrolled at a till could never
  reach their own balance.

---

## Undoing a mistake

Somebody will type 200 instead of 20 within the first hour. There is a window —
**60 minutes by default** — in which the counter can cancel a bill:

```
POST /api/pos/void   { "purchase_id": "pur_...", "reason": "typed 2000 not 20" }
```

Cancelling puts everything back exactly:

- the points the bill **gave** are withdrawn
- the points the bill **spent** go back into the *same lots* they came from, so
  expiry dates are preserved
- any MelaCoin is returned and the shop's settlement entry is reversed
- the bill stops counting towards the shop's takings, its outflow cap and platform stats

Nothing is deleted. The bill is marked cancelled and every movement is recorded, so a
dispute can always be reconstructed.

### When it refuses, and why

| It refuses when | Because |
|---|---|
| More than 60 minutes have passed | Points get spent. Clawing them back a day later is worse than a correcting sale. |
| The customer already spent the points that bill gave | Removing them would take value they legitimately earned and used elsewhere. |
| The bill is already cancelled | |
| It belongs to another shop | |

A refusal always says which of these it is. When it refuses, ring up a correcting
sale instead — that is a normal transaction with a full audit trail.

**A cancelled bill's reference cannot be silently reused.** Sending the same
`idempotency_key` again fails loudly rather than reporting a sale that never happened.

---

## The rules the counter enforces for you

- **The counter invents the bill reference**, not the shopkeeper. Re-tapping the
  button on a bad signal cannot ring the sale up twice.
- **Points are priced live.** The *Use N points* toggle only appears when the customer
  is over the shop's minimum, and it never offers more than the shop's percentage cap
  on that bill.
- **Earning is on the cash actually paid**, not the pre-discount total.
- **A login is not a till key.** Counter endpoints accept `x-api-key` only, so a
  customer's session can never ring up a sale.

---

## What to tell a shopkeeper on day one

1. Only enrol **regulars**, and only in the **quiet hours**. A queue at 8am is not the
   time.
2. If you type it wrong, **Undo is at the bottom of the screen** for an hour.
3. When it says **FREE CHAI READY**, say so out loud. That moment is the entire
   programme — a customer who never hears it never comes back for it.

---

## Where it lives

| | |
|---|---|
| The screen | `app/public/counter.html`, `app/public/page-counter.js` |
| Enrolment, claiming, voiding | `app/src/services/counter.service.js` |
| Endpoints | `/api/pos/me`, `/api/pos/enroll`, `/api/pos/quote`, `/api/pos/purchase`, `/api/pos/voidable`, `/api/pos/void` |
| Window setting | `VOID_WINDOW_MINUTES` (default 60) |
| Tests | `app/test/counter.test.js` plus counter cases in `app/test/api.test.js` |
