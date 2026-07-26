# secverify — assurance provided, and its limits

For circulation to reviewers and management before the tool is relied on.
Every figure below was measured on our own filed exhibits, not estimated. The
commands that produce them are in the tool, so any claim here can be re-run.

---

## 1. What the tool is

It compares a filed HTML exhibit against the source PDF(s) and produces a
highlighted review copy plus a list of findings. It is a **filter that narrows
where a human must look**. It is not a sign-off, and it does not replace review.

The most important thing to understand about it: the tool is **honest about what
it did not check**, but that honesty lives in the findings list and the
"unchecked ledger", not in the colours. A green figure means "verified". It does
not mean "everything about this row was verified".

---

## 2. Assurance it does provide — measured

On correctly paired inputs:

| Exhibit | Figures machine-verified |
|---|---|
| IFRS-USD press release (exv99w01) | 126 / 127 |
| IFRS-USD press release Q3 (exv99w01) | 227 / 228 |
| IFRS-INR press release (exv99w02) | 240 / 241 |
| IFRS-INR financials + auditor's report (exv99w08) | 1,584 / 1,586 |
| AD (exv99w03) | 282 / 283 |

**Adversarial test.** 20 deliberate conversion errors were injected one at a
time into a real exhibit — wrong / transposed / extra / dropped digits, a moved
decimal, a dropped `%`, a swapped currency, a swapped scale word, a sign flip,
values exchanged between rows, transposed comparative columns, a whole line
relocated (including an adjacent swap), a deleted / duplicated table row, a
deleted / duplicated paragraph, shifted dates, a renamed label, content hidden
by CSS. **All 20 were reported.** The command is in §6 so this can be repeated
on any filing.

**Six further error classes were closed after the measurements above, each
found by stress-testing rather than by reading reports:**

* **words moved across a paragraph boundary.** Canonical comparison strips
  whitespace and punctuation into one continuous string, so moving two words
  from one paragraph to the next produced a **byte-identical** document — no
  check could have seen it. Paragraph boundaries are now anchored to PDF line
  boundaries, which survive canonicalisation.
* **a figure hidden by a stylesheet.** The hidden-content check only inspected
  inline `style=` attributes, so `display:none` applied through a class passed
  as verified while the reader saw a blank. Stylesheet rules, `overflow`
  clipping and CSS-injected text (`content:'('`, which turns a positive into a
  negative for the reader only) are now covered.
* **a footnote marker or a date's day-of-month leaking into a row's key**,
  which had silently excluded every footnote-marked segment row — Financial
  Services, Retail, Communication, Life Sciences — from the order check.
* **a run of words moved to a different paragraph.** The destination paragraph
  is flagged by the boundary check above; the source paragraph previously got
  only a generic amber "close wording match", which reads as probably-fine. The
  moved words are now named explicitly as an error — *"in individual segments"
  sits at the end of this block in the HTML, but in the PDF those words appear
  in a DIFFERENT paragraph*.
* **a reworded line where the document repeats it.** A signature block's
  designation — PDF *"Chief Managaing Officer and Executive Director"* against
  HTML *"Chief Executive Officer and Managing Director"* — passed as **green**.
  The exhibit carries two signature blocks, so the HTML's copy matched the PDF's
  *other, unaltered* occurrence and every text check stamped it verified; the
  altered occurrence surfaced only as an "omission" in the summary panel, which
  reads as *missing content* rather than *wrong designation*. Reworded lines are
  now named — both versions quoted side by side — and **every** HTML block
  carrying that wording is recoloured, because only one copy corresponds to the
  differing PDF occurrence and nothing in either document says which.
* **the PDF being the wrong *period* of the right document** — see §5, where it
  is quantified. This was the most consequential of the fourteen.

---

## 3. The most significant limitation: row-order coverage is 71%

A line that moves **with its values** passes every value check — same figures,
same totals, footing still ties. Only an order check can see it. Measured across
**26 real filings, 10,383 statement rows**:

> **7,389 rows (71%) are order-checked. 2,994 rows (29%) are not.**
> **If a line moves within that 29%, nothing reports it.**

Per exhibit the range is wide, and it is worst exactly where the risk is highest
— the largest statements:

| Exhibit type | Order coverage |
|---|---|
| Press releases | 96–100% |
| AD / additional disclosures | 79–96% |
| Quarterly financial statements | 67–86% |
| Annual consolidated financials (largest) | **48%** |
| Factsheet | **51%** |

Why the 29% is not covered:

| Share of all rows | Reason |
|---|---|
| 14.9% | the PDF's parsed row does not match the HTML's — a label wrapped across print lines, or the two sides split columns differently |
| 10.6% | the same row matches several places in the PDF, so its position is ambiguous |
| 2.6% | the label is too short to locate reliably ("Total", "Net") |
| 0.8% | an identical row appears twice in the same HTML table |

**This is the number to quote to management.** Value accuracy is strong; row
*sequence* in large statements is only partly machine-checked, and the tool does
not currently tell you which rows fell outside it during a normal run — you have
to run the coverage command in §6.

---

## 4. What the tool cannot check at all

**Numbers rendered as images.** Confirmed on our own press release: the headline
metrics — CC growth, operating margin, EPS growth, large-deal TCV, free cash
flow — are inside `growth-percentage.gif`. The tool reports that the image
exists and that the PDF text has no HTML counterpart, but it **cannot read the
figures inside the image**. If the image itself carries a wrong number, no check
will see it. This affects the most-read numbers in the document.

**Scanned PDF pages.** A page with no text layer cannot be compared. The tool
flags such pages rather than passing them silently.

**Prose numbers that only change position.** A substituted value in a sentence is
reported individually. A number merely *moved* within prose is reported as a
count (`seq-prose-order: N numbers`), not itemised, because PDF text extraction
routinely reorders a paragraph's numbers on its own. You get the count; you do
not get the location.

**Rows the grid cannot compare** are counted and named in an "unchecked ledger"
finding. On the Q2 IFRS-INR exhibit that was 67 rows. They are neither passes nor
failures — nothing is asserted about them.

**Judgement.** Whether a disclosure is adequate, correctly classified, or
compliant is entirely outside scope.

---

## 5. Operating requirements — the largest practical risk

**The PDF must be the exhibit's actual source.** This is the failure we have
actually hit, twice:

- An exhibit paired with the **previous quarter's** PDF. Both downloads were
  named `IFRS_USD_PR.pdf`. Only 44 of 239 figures matched; the run produced 568
  findings that said nothing about the filing, and it looked like the tool had
  failed.
- An exhibit paired with **only the auditor's-report PDF**, omitting the
  financial-statements PDF. Row-order coverage measured **0%**.

The tool detects both and prints a `STOP — THE PDF DOES NOT MATCH THIS EXHIBIT`
banner at the top of the report, and the audit harness refuses to run.

**How that detection was found to be inadequate, and what replaced it.** The
banner originally fired on match *ratios* — figures validated and text blocks
matched. Tested against eight deliberately mispaired runs of our own filings
(the annual PDF against the Q3 exhibit, Q1 against Q3, Q2 against Q3, and so
on), **five of the eight passed the banner in complete silence**, each producing
1,700–2,900 findings measured against a source that never contained those
numbers:

| Deliberate mispair | Figures matched | Text blocks matched | Banner fired? |
|---|---|---|---|
| annual PDF vs Q3 consolidated exhibit | 51.9% | 85.7% | **no** |
| Q3 PDF vs Q1 consolidated exhibit | 56.4% | 88.4% | **no** |
| Q1 PDF vs Q3 consolidated exhibit | 43.4% | 86.5% | **no** |
| Q2 PDF vs Q3 standalone exhibit | 57.3% | 88.1% | **no** |
| Q1/Q3 PDF vs Q2 IFRS-INR exhibit | 45.4% / 51.3% | 87.7% / 87.0% | **no** |

The reason is structural: consecutive filings of the same entity share almost
all their wording, so text-block agreement stays high however wrong the period
is. And **no threshold can fix it** — a *correctly* paired factsheet validates
only **51.2%** of its figures, sitting inside the mispair range of 24–57%. The
ratios simply do not separate these documents.

The exhibit's own **reporting period** does, exactly. The period an exhibit
states most often is what it reports on, and a genuine source PDF always states
it too — measured 1 to 28 times across **16 correctly paired filings**, and
**zero** times in **all eight** mispairs. That is a test of presence rather than
a ratio, so it needs no tuning and has no threshold to drift. All eight mispairs
now raise the banner, naming the period the exhibit reports and the periods the
PDF actually covers; all sixteen correct pairs stay silent.

**A reviewer must still confirm the pairing** — the check reads the period from
text, so an exhibit that states no period phrase at all cannot be judged this
way and falls back to the ratios. If an exhibit contains an auditor's report as
well as the statements, **pass both PDFs**:

```
python -m secverify.toolsigma auditorsreport.pdf statement.pdf exhibit.htm
```

**One PDF may cover more than one exhibit.** Our own AD PDF is a single file
serving both `exv99w03` and `exv99w06`. Paired with either exhibit alone it
correctly reports the other exhibit's content as not reflected — on `exv99w03`
that is 21 red "omission" findings from page 16 onward, none of which are
errors. Check whether a block of unreflected pages sits at the start or end of
the PDF before treating omissions as real.

**Read the findings list, not only the colours.** Blue and bright-yellow marks
are "not verified", not "verified".

---

## 6. How to verify these claims independently

Nothing here needs to be taken on trust:

```bash
# Does a wrong document actually fail? Injects ~20 real conversion errors.
python tools/mutation_audit.py statement.pdf exhibit.htm

# What share of this exhibit's rows are order-checked, and why not the rest?
python tools/order_coverage.py statement.pdf exhibit.htm

# Why is one specific row not order-checked?
python tools/diagnose_row_order.py statement.pdf exhibit.htm --label "Life Sciences"

# 187 regression tests
python -m pytest -q
```

---

## 7. Residual risk — stated plainly

During development, **fourteen defects of one particular kind were found and
fixed**: a check that silently declined to run, while the review copy looked
fully green. Examples: footnote markers excluding every marked segment row from
the order check; a date's day-of-month leaking into a row's figure key; the
prose symbol check disabled on every real filing because it tested for
`<table>` membership when our exhibits nest prose inside layout tables; a
reworded designation matching the document's *other* copy of the same line; and
the wrong-period-PDF banner that five of eight mispairs walked straight past.

Each was invisible from the output. Each was found only by asking "what is
**not** being checked?" — never by reading a report. The last of them was not
even a faulty check: moving words across a paragraph boundary left the two
documents byte-identical after canonicalisation, so the comparison itself could
not distinguish them. That is the clearest illustration of why the honest
position is a measured scope rather than a claim of correctness.

The honest conclusion: **this class of defect should be expected to recur.** The
mitigation is not a claim of correctness but the three commands in §6, which
measure coverage instead of assuming it. They should be run on a new exhibit
type before the tool's output is relied on for it.

**Recommended positioning:** an effective first-pass filter that reliably catches
value, sign, symbol, date and structural errors, materially reducing what a
reviewer must check by eye — with row sequence in large statements, and anything
rendered as an image, remaining a human responsibility.
