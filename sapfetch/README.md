# sapfetch — automated SAP BI / Group Reporting downloader

`sapfetch` logs in to the **SAP NetWeaver Enterprise Portal** (`/irj/portal`)
once via your corporate SSO, then **downloads multiple Web Intelligence reports
for a chosen reporting period** — e.g. *GR INDAS Consolidated PL* plus the
*IFRS INR* and *IFRS USD* variants — filling each report's prompt screen
(Consol Group, Fiscal Year, From Period, To Period) and exporting to Excel or
PDF.

It is a fully self-contained package — it has no dependency on anything else in
this repository.

It is driven by [Playwright](https://playwright.dev/python/), so it works on any
browser-accessible portal even behind SSO — no backend API access required.

```
sapfetch login              # one-time: complete SSO, session is cached
sapfetch download           # pull all reports for a period
   --fiscal-year 2025 --from-period 1 --to-period 10
```

## Why this design

| Decision | Reason |
|----------|--------|
| **Playwright, not a REST API** | The reports are WebI documents embedded in the `/irj/portal` Enterprise Portal; there is no self-service API exposed to you. Playwright drives the exact screen you use. |
| **Saved SSO session** (`storage_state`) | `is.ad.infosys.com` (`ad` = Active Directory) means Kerberos/SAML SSO. You log in *once* interactively; every later run is headless and unattended. |
| **Config-driven** | URLs, report paths, prompt labels and DOM selectors live in YAML — the code never hard-codes anything environment-specific. |
| **Batch + per-period** | Add a report by adding a YAML entry; one `download` run pulls them all for one `Period`. |
| **No secrets on disk** | No passwords are stored — only the browser session cookie, which is `.gitignore`d. |

## Install

```bash
pip install -r requirements.txt
playwright install chromium      # one-time browser download
```

## Configure

Copy the template and edit it to match exactly what you see in the portal:

```bash
cp config.example.yaml config.yaml
```

The important parts:

```yaml
portal:
  base_url: "https://is.ad.infosys.com/irj/portal/reports"
reports:
  - name: "GR INDAS Consolidated PL"
    open_path: ["Group Reporting", "IND-AS INR", "GR INDAS Consolidated PL"]
  - name: "GR IFRS Consolidated PL - INR"
    open_path: ["Group Reporting", "IFRS INR & USD", "GR IFRS Consolidated PL INR"]
    prompts: {"Currency": "INR"}
  - name: "GR IFRS Consolidated PL - USD"
    open_path: ["Group Reporting", "IFRS INR & USD", "GR IFRS Consolidated PL USD"]
    prompts: {"Currency": "USD"}
```

`open_path` is the click path through the portal tabs/tree to each report — copy
the labels exactly. `prompt_labels` defaults already match the GR INDAS prompt
screen (Consol Group / Fiscal Year / From Period / From To).

## Use it

```bash
# 1. One-time: open a browser, finish SSO, save the session
sapfetch login -c config.yaml

# 2. Download every configured report for FY2025, periods 1..10
sapfetch download -c config.yaml --fiscal-year 2025 --from-period 1 --to-period 10

# Just one or two reports
sapfetch download -c config.yaml --fiscal-year 2025 --from-period 1 --to-period 10 \
    --report "GR IFRS Consolidated PL - INR" --report "GR IFRS Consolidated PL - USD"

# Override the consolidation group; watch it run in a visible browser
sapfetch download -c config.yaml --fiscal-year 2025 --from-period 1 --to-period 10 \
    --consol-group G_GRUP --show-browser
```

Files land in `downloads/` named by report and period, e.g.
`GR_INDAS_Consolidated_PL_FY2025_P01-10.xlsx`. One report failing does not abort
the batch — each line reports its own success/failure and the process exits
non-zero if any failed (handy for cron/Airflow/CI).

### Library use

```python
from sapfetch import load, Period, download_reports

cfg = load("config.yaml")
results = download_reports(cfg, Period(fiscal_year=2025, from_period=1, to_period=10))
for r in results:
    print(r.report, "->", r.path if r.ok else r.error)
```

## Calibrating selectors (the one manual step)

Navigation, prompt-filling and export depend on the live WebI/portal DOM. The
defaults in `config.example.yaml` are best-effort and **may need tuning for your
exact portal build** — this is expected and is the only part that can't be
guessed without seeing the live page.

To calibrate: run `sapfetch login` (it opens a real browser) or
`sapfetch download --show-browser`, open the browser dev-tools, and refine the
`selectors:` block in your YAML (each value is a standard Playwright selector).
The fields are documented inline in `config.example.yaml`. Most portals only
need `nav_item`, `prompt_row_by_label`, `run_button` and the three `export_*`
selectors adjusted once.

## How it works

1. **session.py** — `sapfetch login` opens a headed browser; you finish SSO; the
   authenticated `storage_state` is saved. Later runs load it headless.
2. **portal.py** — for each report: walk `open_path` to open it, resolve the
   WebI iframe, fill each period prompt by its visible label, run, then click
   Export and capture the download via Playwright's `expect_download`.
3. **downloader.py** — orchestrates the batch: merges config + report prompts
   with the period, computes output filenames, and collects per-report results
   so partial success is reported cleanly.

## Safety & scope

- **No credentials are stored** — only the browser session cookie
  (`.sap_session.json`, git-ignored). Downloaded files and `config.yaml` are
  git-ignored too, so financial data never lands in the repo.
- Automate only reports you are authorised to access; this targets an internal
  corporate portal, so confirm it is sanctioned by your organisation.

## Run the tests

```bash
python -m pytest          # config + period logic (no browser needed)
```
