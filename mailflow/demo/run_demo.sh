#!/usr/bin/env bash
# Live, self-contained demo of mailflow in a corporate setting.
#
# Scenario: Acme Corp's Finance Automation account emails the weekly financial
# report to the CFO and Controller every Monday, archives a copy for audit, and
# tracks whether the CFO reverts. This script runs the whole lifecycle for real
# against a local capture SMTP relay — no internet or real mailbox required.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PKG="$(dirname "$HERE")"
WORK="$HERE/_run"
PORT=8025

export PYTHONPATH="$PKG"
MF=(python3 -m mailflow -c "$WORK/acme.yaml")

rule() { printf '\n\033[1;34m=== %s ===\033[0m\n' "$1"; }
pause() { sleep "${DEMO_PAUSE:-0}"; }

# --- clean slate ------------------------------------------------------------
rm -rf "$WORK"; mkdir -p "$WORK/reports"
cd "$WORK"

# --- a sample report to attach ---------------------------------------------
cat > "reports/weekly_$(date +%Y%m%d).csv" <<'CSV'
Metric,This Week,Last Week,Delta
Revenue,1240500,1188200,+4.4%
Operating Expenses,815300,829100,-1.7%
Net Margin,18.2%,16.9%,+1.3pp
Cash Position,5320000,5105000,+4.2%
CSV

# --- corporate config (internal relay, no auth; IMAP omitted for the demo) --
cat > acme.yaml <<YAML
database: acme.db
smtp:
  host: 127.0.0.1
  port: $PORT
  security: none
  auth: false                       # internal relay accepts unauthed mail
  username: finance-automation@acme.example
  from_addr: finance-automation@acme.example
defaults:
  save_dir: archive
  track_replies: true
  reply_window_days: 14
jobs:
  - name: weekly-financials
    to: [cfo@acme.example, controller@acme.example]
    cc: [audit@acme.example]
    subject: "Acme Weekly Financial Report - {date}"
    body: |
      Hi team,

      Please find this week's financial summary attached.
      Kindly revert with approval or corrections by EOD Tuesday.

      Regards,
      Finance Automation
    attachments:
      - reports/weekly_*.csv
    schedule: {every: weekly, weekday: mon, at: "08:00"}
YAML

# --- start the capture relay ------------------------------------------------
rule "Starting internal mail relay (capture server) on port $PORT"
python3 "$HERE/smtp_capture.py" --port "$PORT" --out inbox >relay.log 2>&1 &
RELAY=$!
trap 'kill $RELAY 2>/dev/null || true' EXIT
sleep 1
cat relay.log

rule "1. Validate the configuration and preview the schedule"
"${MF[@]}" validate
pause

rule "2. Test connectivity to the mail relay"
"${MF[@]}" test-connection
pause

rule "3. Dry run — what WOULD be sent right now (sends nothing)"
"${MF[@]}" send weekly-financials --dry-run
pause

rule "4. Send the weekly report for real (through the relay)"
"${MF[@]}" send weekly-financials
sleep 1
echo "--- relay log ---"; cat relay.log | sed -n '2,$p'
pause

rule "5. Status — the report is sent and AWAITING the CFO's revert"
"${MF[@]}" status
pause

rule "6. The CFO replies. Record the revert (manual; IMAP does this automatically)"
"${MF[@]}" mark-replied 1 --from cfo@acme.example
pause

rule "7. Status — revert RECEIVED and recorded with who/when"
"${MF[@]}" status
pause

rule "8. Proof of archiving — a verbatim .eml copy was saved for audit"
find archive -type f
echo "--- first lines of the archived message ---"
head -n 12 "$(find archive -type f | head -n1)"
pause

rule "9. Captured copy on the relay side (what the recipients received)"
find inbox -type f

rule "Demo complete"
echo "Sent + archived + revert-tracked, end to end. No mail left the machine."
