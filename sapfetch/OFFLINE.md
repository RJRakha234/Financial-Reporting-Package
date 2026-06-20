# Running sapfetch in an air-gapped / corporate environment

This is a copy-paste runbook for deploying `sapfetch` on a machine with **no
internet access**. The portal and your SSO/AD servers live on the internal
network, so `sapfetch` reaches them normally — the only challenge is installing
the dependencies offline.

There are two strategies. **Strategy A (corporate browser) is strongly
recommended on Windows** — it needs only two Python wheels and nothing else.

---

## Before you start: what must be true on the target machine

- **Python 3.10+** installed.
- A network route to `https://is.ad.infosys.com/irj/portal` and the SSO/AD
  endpoints (test it in a normal browser first).
- Either a corporate-managed **Edge/Chrome** already installed (Strategy A) or
  the ability to copy a ~150 MB browser folder onto the box (Strategy B).

---

## Strategy A — use the corporate browser (recommended)

Drive the Edge/Chrome that is already installed and managed on the machine. No
Chromium download, and on Linux no system libraries to install.

### 1. Stage the Python packages (on a CONNECTED machine, same OS + Python)

```bash
pip download playwright PyYAML -d ./sapfetch_wheels
```

Copy `sapfetch_wheels/` and the `sapfetch/` source folder to the air-gapped box.

### 2. Install offline (on the AIR-GAPPED machine)

```bash
pip install --no-index --find-links ./sapfetch_wheels playwright PyYAML
```

### 3. Point the config at the installed browser

In `config.yaml`, under `portal:`:

```yaml
portal:
  browser_channel: "msedge"     # or "chrome"
  # If channel auto-detection fails, give the exact path instead:
  # executable_path: "C:/Program Files/Microsoft/Edge/Application/msedge.exe"
```

### 4. Verify, log in, run

```bash
python -m sapfetch -c config.yaml doctor          # all browser/dep checks
python -m sapfetch -c config.yaml login           # one-time SSO (opens Edge)
python -m sapfetch -c config.yaml doctor --check-portal   # confirm reachability
python -m sapfetch -c config.yaml download \
    --fiscal-year 2025 --from-period 1 --to-period 10
```

That's it — no Chromium binary, no system libs.

---

## Strategy B — stage Playwright's bundled Chromium

Use this on Linux, or when there is no usable corporate browser.

### 1. Stage wheels AND the browser (on a CONNECTED machine, same OS)

```bash
pip download playwright PyYAML -d ./sapfetch_wheels
pip install playwright            # temporarily, just to fetch the browser
PLAYWRIGHT_BROWSERS_PATH=./ms-playwright playwright install chromium
```

Copy `sapfetch_wheels/`, the `ms-playwright/` folder, and the `sapfetch/` source
to the air-gapped box. **The browser build is tied to the exact Playwright
version**, so stage them from the same `pip install`.

### 2. Install offline + point Playwright at the staged browser

```bash
pip install --no-index --find-links ./sapfetch_wheels playwright PyYAML

# Linux / macOS:
export PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
# Windows (persisted):
#   setx PLAYWRIGHT_BROWSERS_PATH C:\ms-playwright
```

### 3. Linux only — system libraries

Chromium needs these shared libs (and basic fonts):

```
libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2
libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2
libgbm1 libasound2 libpango-1.0-0 libcairo2
```

Install them from your **internal apt/yum mirror**, or have your platform team
bake them into the base VM/container image. (Windows needs none of this.)

### 4. Verify and run

```bash
python -m sapfetch -c config.yaml doctor
python -m sapfetch -c config.yaml login
python -m sapfetch -c config.yaml download --fiscal-year 2025 --from-period 1 --to-period 10
```

---

## The `doctor` command is your friend

In an air-gapped box you can't watch a browser fail or pip-install on the fly, so
run `doctor` first. It checks Python, the deps, that the **configured browser
actually launches**, the config parses, and the session exists:

```text
$ python -m sapfetch -c config.yaml doctor
  ✓ Python >= 3.10  (found 3.11.5)
  ✓ import playwright
  ✓ import yaml
  ✓ PLAYWRIGHT_BROWSERS_PATH  (/opt/ms-playwright)
  ✓ browser launches  (channel=msedge)
  ✗ saved SSO session  (.sap_session.json not found — run `sapfetch login`)
```

Add `--check-portal` to also confirm the portal URL responds with the saved
session. It exits non-zero if any check fails, so you can gate a scheduled job
on it.

---

## Wheel/platform gotchas

- `pip download` produces wheels for the machine you run it on. If the target
  differs, pin it: `pip download --platform win_amd64 --python-version 3.11
  --only-binary=:all: playwright PyYAML -d ./sapfetch_wheels`.
- Match the **Python minor version** (3.11 ↔ 3.11) between staging and target.
- Keep the Playwright wheel and the staged Chromium from the **same version**.

## Security notes

- No passwords are stored — only the browser session cookie
  (`.sap_session.json`), which is git-ignored. Sessions expire; just re-run
  `login`.
- `config.yaml` and `downloads/` are git-ignored so credentials and financial
  data never land in the repo.
