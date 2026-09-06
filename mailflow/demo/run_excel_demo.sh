#!/usr/bin/env bash
# Live demo of mailflow's Excel-driven mode: a spreadsheet where each row is a
# mail, sent for real through a local capture relay, with Sent/Received status
# written back into the sheet. Self-contained — no internet or real mailbox.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PKG="$(dirname "$HERE")"
WORK="$HERE/_xlsx_run"
PORT=8027
export PYTHONPATH="$PKG"

rm -rf "$WORK"; mkdir -p "$WORK"; cd "$WORK"

echo "[relay] starting capture SMTP server on port $PORT"
python3 "$HERE/smtp_capture.py" --port "$PORT" --out inbox >relay.log 2>&1 &
RELAY=$!
trap 'kill $RELAY 2>/dev/null || true' EXIT
sleep 1

python3 "$HERE/excel_demo.py" --port "$PORT"

echo
echo "[relay] delivered messages:"
sed -n '2,$p' relay.log || true
echo
echo "Open $WORK/acme_mails.xlsx to see the live status board."
