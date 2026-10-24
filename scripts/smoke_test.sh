#!/usr/bin/env bash
# End-to-end smoke test against a running stack (docker compose up).
#   1. a malformed request -> 422 with field errors, present in /quarantine
#   2. a valid request -> 202, polled until terminal, prints the RunResult
# Usage: scripts/smoke_test.sh            (API_URL defaults to http://localhost:${API_HOST_PORT:-8000})
set -euo pipefail

API_URL="${API_URL:-http://localhost:${API_HOST_PORT:-8000}}"
POLL_TIMEOUT_S="${POLL_TIMEOUT_S:-60}"
pretty() { python3 -m json.tool; }
field() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

echo "== readiness: $API_URL/readyz"
curl -fsS "$API_URL/readyz" | pretty

echo
echo "== 1. malformed request (negative amount; cross-field rules only run once fields are valid)"
MALFORMED='{
  "customer_id": "CUST-SMOKE-BAD",
  "account_type": "SAVINGS",
  "channel": "UPI",
  "transaction_id": "TXN-SMOKE-BAD",
  "transaction_amount_inr": -10,
  "transaction_datetime": "2026-09-10T10:00:00+05:30",
  "reported_datetime": "2026-09-01T10:00:00+05:30",
  "home_branch_state": "Karnataka",
  "complaint_text": "Malformed smoke-test request.",
  "customer_claims": {"card_in_possession": true}
}'
BAD_BODY=$(mktemp)
BAD_CODE=$(curl -sS -o "$BAD_BODY" -w '%{http_code}' -X POST "$API_URL/disputes" \
  -H 'Content-Type: application/json' -H 'X-Correlation-ID: smoke-malformed' -d "$MALFORMED")
echo "HTTP $BAD_CODE"
pretty < "$BAD_BODY"
[ "$BAD_CODE" = "422" ] || { echo "FAIL: expected 422"; exit 1; }
QID=$(field "['quarantine_id']" < "$BAD_BODY")
rm -f "$BAD_BODY"

echo
echo "== quarantine entry $QID"
curl -fsS "$API_URL/quarantine?limit=20" \
  | python3 -c "import json,sys; rows=[r for r in json.load(sys.stdin) if r['id']==$QID]; print(json.dumps(rows, indent=2)); sys.exit(0 if rows else 1)" \
  || { echo "FAIL: quarantine row $QID not found"; exit 1; }

echo
echo "== 2. valid request"
TXN_TIME=$(python3 -c "from datetime import datetime,timedelta,timezone as tz; print((datetime.now(tz.utc)-timedelta(days=2)).isoformat())")
REP_TIME=$(python3 -c "from datetime import datetime,timedelta,timezone as tz; print((datetime.now(tz.utc)-timedelta(days=1)).isoformat())")
VALID=$(cat <<JSON
{
  "customer_id": "CUST-SMOKE-01",
  "account_type": "SAVINGS",
  "channel": "CARD_NOT_PRESENT",
  "transaction_id": "TXN-SMOKE-0001",
  "transaction_amount_inr": 4999.0,
  "transaction_datetime": "$TXN_TIME",
  "bank_alert_datetime": "$TXN_TIME",
  "reported_datetime": "$REP_TIME",
  "home_branch_state": "Karnataka",
  "complaint_text": "An online purchase I did not make appeared on my card.",
  "customer_claims": {"shared_otp_or_pin": false, "card_in_possession": true, "received_alert": true}
}
JSON
)
ACCEPTED=$(curl -fsS -X POST "$API_URL/disputes" -H 'Content-Type: application/json' \
  -H 'X-Correlation-ID: smoke-valid' -d "$VALID")
echo "$ACCEPTED" | pretty
STATUS_URL=$(echo "$ACCEPTED" | field "['status_url']")

echo
echo "== polling $STATUS_URL"
deadline=$((SECONDS + POLL_TIMEOUT_S))
while :; do
  VIEW=$(curl -fsS "$API_URL$STATUS_URL")
  STATUS=$(echo "$VIEW" | field "['status']")
  echo "status=$STATUS"
  case "$STATUS" in completed|escalated|failed) break ;; esac
  [ $SECONDS -lt $deadline ] || { echo "FAIL: run not finished after ${POLL_TIMEOUT_S}s"; exit 1; }
  sleep 1
done
echo "$VIEW" | pretty
[ "$STATUS" = "completed" ] || { echo "FAIL: expected completed, got $STATUS"; exit 1; }

echo
echo "SMOKE TEST PASSED"
