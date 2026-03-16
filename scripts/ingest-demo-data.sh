#!/usr/bin/env bash
# Ingest sample log data simulating a payment-service incident.
# This creates realistic data for demoing the MCP server's investigate_incident tool.
#
# Usage:
#   export LOGS_SERVICE_URL="https://<instance-id>.api.<region>.logs.cloud.ibm.com"
#   export LOGS_API_KEY="your-api-key"  # pragma: allowlist secret
#   ./scripts/ingest-demo-data.sh
#
# The script simulates:
#   - Normal baseline traffic (payment-service, order-service, gateway)
#   - A deployment event (v2.4.1) triggering payment gateway timeouts
#   - Error spike in payment-service starting ~30 min ago
#   - Cascading failures to order-service ~3 min later
#   - Mix of severities: INFO, WARNING, ERROR, CRITICAL

set -euo pipefail

# --- Config ---
if [[ -z "${LOGS_SERVICE_URL:-}" || -z "${LOGS_API_KEY:-}" ]]; then
  echo "Error: LOGS_SERVICE_URL and LOGS_API_KEY must be set"
  echo "Usage:"
  echo "  export LOGS_SERVICE_URL='https://<instance-id>.api.<region>.logs.cloud.ibm.com'"
  echo "  export LOGS_API_KEY='your-api-key'"  # pragma: allowlist secret
  echo "  $0"
  exit 1
fi

# Convert API URL to ingestion URL (.api. → .ingress.)
INGRESS_URL="${LOGS_SERVICE_URL/.api./.ingress.}"
ENDPOINT="${INGRESS_URL}/logs/v1/singles"

echo "Ingestion endpoint: ${ENDPOINT}"

# --- Helper ---
send_logs() {
  local payload="$1"
  local response
  response=$(curl -s -w "\n%{http_code}" -X POST "${ENDPOINT}" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${LOGS_API_KEY}" \
    -d "${payload}")

  local http_code
  http_code=$(echo "$response" | tail -1)
  local body
  body=$(echo "$response" | head -n -1)

  if [[ "${http_code}" -ge 200 && "${http_code}" -lt 300 ]]; then
    return 0
  else
    echo "  Failed (HTTP ${http_code}): ${body}"
    return 1
  fi
}

# --- Timestamps ---
# Incident timeline (all times relative to now):
#   -60m to -35m : normal baseline traffic
#   -32m         : deployment v2.4.1 event
#   -30m         : payment gateway timeouts begin
#   -30m to now  : error spike + cascading failures
NOW=$(date +%s)
ts() { echo "$(( NOW + $1 * 60 )).000000000"; }

echo ""
echo "=== Ingesting demo data ==="
echo "Timeline:"
echo "  -60m to -35m : Normal baseline traffic"
echo "  -32m         : Deployment v2.4.1"
echo "  -30m to now  : Payment-service incident"
echo ""

# --- Phase 1: Normal baseline (60m to 35m ago) ---
echo "[1/5] Ingesting normal baseline traffic..."
BASELINE='['
for offset in -60 -58 -55 -52 -50 -48 -45 -42 -40 -38 -36 -35; do
  T=$(ts $offset)
  BASELINE+='
  {"applicationName":"payment-service","subsystemName":"gateway","severity":3,"timestamp":'"${T}"',"text":"Payment processed successfully for order ord-'$((RANDOM % 9000 + 1000))'","json":{"response_time_ms":'$((RANDOM % 150 + 50))',"status":"success","endpoint":"/api/v1/payments"}},
  {"applicationName":"payment-service","subsystemName":"gateway","severity":3,"timestamp":'"${T}"',"text":"Payment gateway health check passed","json":{"response_time_ms":'$((RANDOM % 50 + 10))',"gateway":"stripe","status":"healthy"}},
  {"applicationName":"order-service","subsystemName":"checkout","severity":3,"timestamp":'"${T}"',"text":"Order created successfully for customer cust-'$((RANDOM % 9000 + 1000))'","json":{"response_time_ms":'$((RANDOM % 100 + 30))',"order_total":'$((RANDOM % 500 + 10))'.99}},
  {"applicationName":"gateway","subsystemName":"router","severity":3,"timestamp":'"${T}"',"text":"Request routed to payment-service","json":{"path":"/api/v1/payments","method":"POST","upstream_time_ms":'$((RANDOM % 200 + 50))'}}'
done
# Remove trailing comma and close array
BASELINE="${BASELINE%,}]"
send_logs "${BASELINE}"
echo "  Sent $(echo "${BASELINE}" | grep -c '"text"') log entries"

# --- Phase 2: Deployment event ---
echo "[2/5] Ingesting deployment event..."
DEPLOY_T=$(ts -32)
send_logs '[
  {"applicationName":"payment-service","subsystemName":"deployer","severity":3,"timestamp":'"${DEPLOY_T}"',"text":"Deployment started: payment-service v2.4.1","json":{"version":"v2.4.1","previous_version":"v2.4.0","deployer":"ci-pipeline","commit":"a3f8b2c"}},
  {"applicationName":"payment-service","subsystemName":"deployer","severity":3,"timestamp":'"$(ts -31)"',"text":"Deployment completed: payment-service v2.4.1 rolled out to all pods","json":{"version":"v2.4.1","pods_updated":4,"rollout_duration_s":58}}
]'
echo "  Sent 2 log entries"

# --- Phase 3: Error spike begins (30m ago) ---
echo "[3/5] Ingesting payment-service errors..."
ERRORS='['
for offset in -30 -29 -28 -27 -26 -25 -24 -23 -22 -21 -20 -19 -18 -17 -16 -15 -14 -13 -12 -11 -10 -9 -8 -7 -6 -5 -4 -3 -2 -1; do
  T=$(ts $offset)
  # Multiple errors per minute to simulate a real spike
  ERRORS+='
  {"applicationName":"payment-service","subsystemName":"gateway","severity":5,"timestamp":'"${T}"',"text":"Payment gateway timeout after 12000ms for order ord-'$((RANDOM % 9000 + 1000))'","json":{"response_time_ms":12000,"status":"timeout","endpoint":"/api/v1/payments","gateway":"stripe","error_code":"GATEWAY_TIMEOUT"}},
  {"applicationName":"payment-service","subsystemName":"gateway","severity":5,"timestamp":'"${T}"',"text":"Payment processing failed: upstream timeout exceeded","json":{"response_time_ms":12034,"status":"error","customer_id":"cust-'$((RANDOM % 9000 + 1000))'","transaction_amount":'$((RANDOM % 500 + 10))'.99}},
  {"applicationName":"payment-service","subsystemName":"gateway","severity":4,"timestamp":'"${T}"',"text":"Circuit breaker OPEN for payment gateway - failure rate 78%","json":{"circuit_state":"open","failure_rate":0.78,"threshold":0.5,"window":"60s"}}'

  # Add some successful requests too (not everything fails)
  if (( RANDOM % 3 == 0 )); then
    ERRORS+='
    ,{"applicationName":"payment-service","subsystemName":"gateway","severity":3,"timestamp":'"${T}"',"text":"Payment processed successfully (retry) for order ord-'$((RANDOM % 9000 + 1000))'","json":{"response_time_ms":'$((RANDOM % 3000 + 8000))',"status":"success","retry_attempt":2}}'
  fi
  ERRORS+=','
done
ERRORS="${ERRORS%,}]"
send_logs "${ERRORS}"
echo "  Sent $(echo "${ERRORS}" | grep -c '"text"') log entries"

# --- Phase 4: Cascading failures to order-service (27m ago) ---
echo "[4/5] Ingesting cascading failures (order-service)..."
CASCADE='['
for offset in -27 -26 -25 -24 -23 -22 -21 -20 -18 -16 -14 -12 -10 -8 -6 -4 -2; do
  T=$(ts $offset)
  CASCADE+='
  {"applicationName":"order-service","subsystemName":"checkout","severity":5,"timestamp":'"${T}"',"text":"Order creation failed: payment-service returned 504 Gateway Timeout","json":{"response_time_ms":15000,"status":"error","payment_service_status":504,"order_id":"ord-'$((RANDOM % 9000 + 1000))'"}},
  {"applicationName":"order-service","subsystemName":"checkout","severity":4,"timestamp":'"${T}"',"text":"Retry exhausted for payment call - order placed in pending queue","json":{"retries":3,"queue":"payment-retry","order_id":"ord-'$((RANDOM % 9000 + 1000))'"}},
  {"applicationName":"gateway","subsystemName":"router","severity":5,"timestamp":'"${T}"',"text":"Upstream error: POST /api/v1/checkout returned 500","json":{"path":"/api/v1/checkout","method":"POST","upstream":"order-service","status":500,"response_time_ms":'$((RANDOM % 5000 + 10000))'}},
  {"applicationName":"gateway","subsystemName":"router","severity":4,"timestamp":'"${T}"',"text":"High error rate detected for /api/v1/checkout - 23% of requests failing","json":{"path":"/api/v1/checkout","error_rate":0.23,"window":"5m"}},'
done
CASCADE="${CASCADE%,}]"
send_logs "${CASCADE}"
echo "  Sent $(echo "${CASCADE}" | grep -c '"text"') log entries"

# --- Phase 5: Critical alerts and resource saturation ---
echo "[5/5] Ingesting critical alerts..."
send_logs '[
  {"applicationName":"payment-service","subsystemName":"gateway","severity":6,"timestamp":'"$(ts -25)"',"text":"CRITICAL: Payment service error rate exceeded SLO threshold - 99.9% SLO violated","json":{"slo_target":0.999,"current_success_rate":0.77,"violated_since":"'"$(date -u -v-25M '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '-25 minutes' '+%Y-%m-%dT%H:%M:%SZ')"'","burn_rate":14.4}},
  {"applicationName":"payment-service","subsystemName":"resources","severity":4,"timestamp":'"$(ts -20)"',"text":"Connection pool exhaustion warning: 95% of connections in use","json":{"pool_size":100,"active":95,"waiting":23,"timeout_ms":12000}},
  {"applicationName":"payment-service","subsystemName":"resources","severity":5,"timestamp":'"$(ts -15)"',"text":"Thread pool saturated - rejecting new payment requests","json":{"pool":"payment-executor","active_threads":50,"max_threads":50,"queue_size":200,"rejected":47}},
  {"applicationName":"order-service","subsystemName":"checkout","severity":6,"timestamp":'"$(ts -12)"',"text":"CRITICAL: Checkout flow completely blocked - all payment requests timing out","json":{"failed_transactions":847,"affected_customers":312,"error_rate":0.23,"started_at":"'"$(date -u -v-30M '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '-30 minutes' '+%Y-%m-%dT%H:%M:%SZ')"'"}}
]'
echo "  Sent 4 log entries"

TOTAL_ERRORS=$(echo "${ERRORS}" | grep -c '"text"')
TOTAL_CASCADE=$(echo "${CASCADE}" | grep -c '"text"')
echo ""
echo "=== Done ==="
echo "Total log entries ingested: ~$((48 + 2 + TOTAL_ERRORS + TOTAL_CASCADE + 4))"
echo ""
echo "Now record your demo:"
echo "  asciinema rec demo.cast"
echo "  # Then in Claude Code:"
echo "  # > Investigate payment-service errors in the last hour"
