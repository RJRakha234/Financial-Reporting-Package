# The legal and tax reality in India

> **I am not a lawyer and this is not legal advice.** This is an orientation map so
> that you can walk into a lawyer's office already knowing which questions to ask,
> instead of paying them to explain the basics. Rules in this area change often —
> verify everything below against the current position before you rely on it.
>
> The single most expensive mistake in this space is building for a year and *then*
> asking whether it was allowed.

---

## The short version

Crypto is **not illegal** in India. It is also **not legal tender**, it is taxed
heavily and specifically, and anyone running an exchange-like service has real
anti-money-laundering obligations.

Three separate bodies of law touch this project, and they are easy to confuse:

1. **Tax law** — treats MelaCoin as a Virtual Digital Asset (VDA). Well defined.
2. **Anti-money-laundering law** — treats VDA service providers as reporting
   entities. Well defined.
3. **Payments and securities law** — treats *loyalty points that become transferable
   value* as an unsettled question. This is where your actual risk lives.

---

## 1. Tax: the VDA regime

Introduced by the Finance Act 2022. MelaCoin almost certainly qualifies as a
Virtual Digital Asset under section 2(47A) of the Income-tax Act.

**Section 115BBH — 30% on gains.** Income from the *transfer* of a VDA is taxed at a
flat 30% (plus surcharge and cess). No deductions are allowed except the cost of
acquisition. No expenses, no infrastructure costs, nothing.

**Losses cannot be set off.** A loss on one VDA cannot offset a gain on another, and
cannot be carried forward. This is unusually harsh and catches people out.

**Section 194S — 1% TDS on transfers.** Tax is deducted at source on payment for the
transfer of a VDA. Thresholds are small (₹50,000 a year for specified individuals,
₹10,000 otherwise), so in practice most activity crosses them.

**This is the operationally hard one.** Section 194S may make *you* responsible for
deducting and depositing TDS on transfers happening through your platform, and for
issuing the relevant certificates. Get this designed in from the start — retrofitting
TDS onto a live ledger is miserable.

**Receiving a VDA for free** can be taxable in the recipient's hands as a gift under
section 56(2)(x). Ask specifically whether a customer converting loyalty points into
MELA is a taxable event for them, and at what value. **You need a clear answer
before launch**, because it determines what you must tell every customer at the
moment they press "Convert".

**Ask your CA:**
- Is points → MELA conversion a "transfer" triggering 194S? Who deducts?
- Is our conversion fee a service liable to GST at 18%?
- What must appear on the customer's screen and in their annual statement?

---

## 2. Anti-money-laundering: FIU-IND

Since a March 2023 notification, activities involving VDAs — exchange between VDAs
and fiat, transfer, safekeeping, and administration — are covered under the
Prevention of Money Laundering Act.

If you do those things, you are a **Reporting Entity** and must:

- **Register with FIU-IND** (Financial Intelligence Unit – India).
- Run **KYC** on customers.
- Keep records, typically for years.
- File **Suspicious Transaction Reports**.
- Appoint a Principal Officer.

**Read this carefully:** holding custodial MELA balances for customers is
"safekeeping". Converting points into a transferable token, and letting people
withdraw it to their own wallets, looks a great deal like "transfer" and "exchange".
Assume you are in scope until a lawyer tells you in writing that you are not.

This has a direct product consequence: the demo app has no KYC. Before real money
moves, you need identity verification wired into signup — at minimum for any customer
who converts or withdraws.

---

## 3. The genuinely unsettled part: are your points a payment instrument?

This is where the real risk sits, and where you will get the least clear answer.

**Ordinary loyalty points are usually fine.** A closed-loop, non-transferable scheme
— stamps that only work at the shop that issued them, cannot be sold, cannot be
cashed out — is generally treated as a marketing arrangement rather than a payment
instrument, and sits outside the RBI's Prepaid Payment Instrument framework.

**Your points stop being ordinary the moment they can become MelaCoin.** Now they
have a cash-equivalent value, that value moves between merchants, and it can leave
the system entirely into a market where it can be sold for rupees. Every feature that
makes this product interesting also makes it look more like stored value.

Questions to put to a payments lawyer, in this order:

1. Do our loyalty points constitute a **Prepaid Payment Instrument** under the RBI's
   PPI Master Directions, given that they are convertible to a transferable token?
2. Does holding rupees collected from shops, against tokens we owe customers, amount
   to accepting **deposits** under the Banning of Unregulated Deposit Schemes Act, 2019?
3. Could MELA be a **security** or a collective investment scheme in SEBI's view —
   particularly if any of our marketing implies a return?
4. What **GST** applies to issuing points, to the conversion fee, and to a shop
   accepting MELA as payment? (There is existing case law on vouchers and loyalty
   points that your CA should check.)
5. Do we need an **FEMA** view if any customer or shop is outside India?

**A pragmatic risk reduction, if the answers come back uncomfortable:** launch the
loyalty network with no token at all (the app already runs this way with
`CHAIN_MODE=mock`). Nothing above bites until points become convertible. That is
free optionality, and it is why the roadmap puts the token in Phase 4, not Phase 1.

---

## 4. Advertising rules

The Advertising Standards Council of India requires that advertisements for virtual
digital assets carry a prominent disclaimer, along the lines of:

> *Crypto products and NFTs are unregulated and can be highly risky. There may be no
> regulatory recourse for any loss from such transactions.*

There are also rules on placement, prominence, and the use of celebrities or
influencers. Check the current guidance before your first campaign.

**And a rule for yourself, not just for the regulator:** never describe MelaCoin as
an investment, never project a future price, never imply returns. Beyond the legal
exposure, doing so changes who your users are — from shoppers who want a better
loyalty programme to speculators who want an exit — and the second group will destroy
the first.

---

## 5. Corporate housekeeping

- **Companies Act disclosure.** Companies must disclose crypto/VDA holdings, profit
  or loss on them, and related deposits or advances in their financial statements.
- **Terms of service** must state plainly: points are not money, they can expire,
  shops set their own rates, conversion is one-way, and MELA's value can fall.
- **Segregate the treasury.** The rupees backing outstanding MELA should not sit in
  your operating account funding salaries. If you ever need to prove the backing
  ratio to a regulator or an exchange, commingled funds make it impossible.
- **Insurance and audit.** Get the smart contracts audited before mainnet, and get a
  statutory auditor comfortable with your ledger design early.

---

## A sane order of operations

1. Build and run the loyalty network with **no token**. Almost none of this applies.
2. In parallel, engage **a payments lawyer and a CA** with actual VDA experience.
   Budget ₹1.5–5 lakh for a proper opinion. This is cheap next to the alternative.
3. Get written answers on PPI status, 194S mechanics, and FIU-IND registration.
4. Only then build the conversion feature, with KYC and TDS already designed in.
5. Only then consider a listing — which brings its own scrutiny.

The roadmap follows exactly this order.
