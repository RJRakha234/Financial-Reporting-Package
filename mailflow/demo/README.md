# mailflow — live demo & presentation

This folder shows mailflow working **end to end** in a corporate scenario
(Acme Corp's weekly financial report), and a slide deck to present it.

## Run the live demo

```bash
cd mailflow
bash demo/run_demo.sh
```

It is fully self-contained — **no internet and no real mailbox required**:

1. starts a tiny **capture SMTP relay** (`smtp_capture.py`) on `127.0.0.1:8025`
   that saves whatever it receives;
2. generates a sample CSV report to attach;
3. writes a corporate `acme.yaml` config (internal relay, `auth: false`);
4. runs the whole lifecycle and prints real output:
   `validate → test-connection → dry-run → send → status (awaiting) →
    mark-replied → status (reverted) → show the archived .eml`.

Everything it creates lands in `demo/_run/` (git-ignored): the SQLite database,
the `archive/` of sent `.eml` files, and the relay's `inbox/`.

Set `DEMO_PAUSE=2` to slow the script down for a live audience:

```bash
DEMO_PAUSE=2 bash demo/run_demo.sh
```

## Run the Excel-driven demo

```bash
cd mailflow
bash demo/run_excel_demo.sh
```

Builds a 3-row spreadsheet (one mail per row), sends the **due** rows for real
through the local relay — resolving `{Region}` mail-merge placeholders — and
prints the sheet before/after. It then simulates the CFO's revert so you can
watch the `Received Status`, `Received From`, and `Received Path` columns fill
in. The resulting `acme_mails.xlsx` (under `demo/_xlsx_run/`) is the live status
board; the IMAP matching logic is real and unit-tested, fed one canned reply so
no mailbox is needed.

## The presentation

Open **`presentation.html`** in any browser (double-click it — no build step).
Navigate with the arrow keys, space, or the on-screen Prev/Next buttons. It
covers the problem, the solution, the architecture, the live demo output,
scheduling options, security/compliance, and a getting-started cheat sheet.

## Files

| File | Purpose |
|---|---|
| `run_demo.sh` | Orchestrates the end-to-end live demo. |
| `smtp_capture.py` | Dependency-free SMTP server that captures delivered mail. |
| `presentation.html` | Self-contained corporate slide deck. |
