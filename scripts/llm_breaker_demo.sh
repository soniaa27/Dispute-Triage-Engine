#!/usr/bin/env bash
# Circuit breaker demo (requirement A5) against the running stack (docker compose up).
#   1. restart llm-gateway with the stub provider and LLM_FAULT_INJECTION=error_500
#   2. fire requests until the breaker opens (each fails after a retry)
#   3. show a 503 fast-fail while open (provider not called, a few ms)
#   4. restart llm-gateway with fault injection off: the breaker is still open (state in Redis)
#   5. wait for the reset window, then a half-open trial succeeds and the breaker closes
#   6. restore llm-gateway to the .env configuration
# Uses the stub provider, so it needs no key and spends no provider quota.
set -euo pipefail

PORT="${LLM_GATEWAY_HOST_PORT:-8100}"
URL="http://localhost:${PORT}"
THRESHOLD="${DEMO_CB_FAILURE_THRESHOLD:-3}"
RESET="${DEMO_CB_RESET_SECONDS:-15}"

# Demo settings override .env through compose interpolation (shell beats .env).
demo_env=(LLM_PROVIDER=stub STUB_DELAY_MS=100 LLM_MAX_RETRIES=1 LLM_BACKOFF_BASE_SECONDS=0.2
          LLM_CB_FAILURE_THRESHOLD="$THRESHOLD" LLM_CB_RESET_SECONDS="$RESET"
          LLM_MAX_REQUESTS_PER_MINUTE=600)

restart_gateway() {
  echo "-- restarting llm-gateway with: $*"
  env "$@" docker compose up -d --wait --no-deps llm-gateway >/dev/null 2>&1
}

PRINT_BREAKER=$(cat <<'PY'
import json, sys
b = json.load(sys.stdin)
t = b["trial_allowed_in_seconds"]
b["trial_allowed_in_seconds"] = "-" if t is None else f"{t}s"
print("   breaker: state={state} consecutive_failures={consecutive_failures}/{failure_threshold}"
      " times_opened={times_opened} fast_failures={fast_failures}"
      " trial_in={trial_allowed_in_seconds} fault_injection={fault_injection}".format(**b))
PY
)

breaker() {
  curl -fsS "$URL/v1/breaker" | python3 -c "$PRINT_BREAKER"
}

request() {
  local resp code secs
  resp=$(curl -sS -w '\n%{http_code} %{time_total}' -X POST "$URL/v1/complete" \
    -H 'Content-Type: application/json' -H "X-Correlation-ID: breaker-demo-$1" \
    -d '{"prompt":"breaker demo","prompt_version":"demo","step":"breaker_demo","run_id":"breaker-demo"}')
  read -r code secs <<<"$(tail -n1 <<<"$resp")"
  printf '   #%s HTTP %s in %4.0f ms  %s\n' "$1" "$code" "$(python3 -c "print($secs*1000)")" \
    "$(sed '$d' <<<"$resp")"
  LAST_CODE="$code"
}

echo "== 0. reset breaker state in Redis and start the faulty gateway"
docker compose exec -T redis redis-cli del llm:breaker llm:breaker:trial llm:ratelimit >/dev/null
restart_gateway "${demo_env[@]}" LLM_FAULT_INJECTION=error_500
breaker

echo
echo "== 1. requests while every provider call fails (HTTP 500 injected, 1 retry each)"
n=0
while :; do
  n=$((n + 1))
  request "$n"
  state=$(curl -fsS "$URL/v1/breaker" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
  [ "$state" = "open" ] && break
  [ "$n" -ge 10 ] && { echo "FAIL: breaker did not open"; exit 1; }
done
echo "   -> breaker OPEN after request #$n"
breaker

echo
echo "== 2. while open: fail fast with 503, provider not called"
request "fast-1"
[ "$LAST_CODE" = "503" ] || { echo "FAIL: expected 503"; exit 1; }
breaker

echo
echo "== 3. fix the fault (restart with fault injection off); state survives in Redis"
restart_gateway "${demo_env[@]}" LLM_FAULT_INJECTION=off
breaker
request "after-restart"

wait_s=$(curl -fsS "$URL/v1/breaker" | python3 -c 'import json,sys; print(json.load(sys.stdin)["trial_allowed_in_seconds"] or 0)')
echo
echo "== 4. wait ${wait_s}s for the reset window, then one half-open trial call"
sleep "$(python3 -c "print(float('$wait_s') + 0.5)")"
request "trial"
[ "$LAST_CODE" = "200" ] || { echo "FAIL: trial call should succeed"; exit 1; }
breaker

echo
echo "== 5. restore llm-gateway to the .env configuration"
docker compose up -d --wait --no-deps llm-gateway >/dev/null 2>&1
breaker
echo
echo "OK: closed -> open after $THRESHOLD failures -> 503 fast-fail -> half-open trial -> closed"
