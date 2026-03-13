#!/usr/bin/env bash
# Scenario Benchmark: Measures token-relevant byte sizes for skills-only workflows
# against the 3 blog scenarios, using real IBM Cloud Logs queries.
#
# Usage:
#   export LOGS_API_KEY="your-key"  # pragma: allowlist secret
#   export LOGS_SERVICE_URL="https://..."
#   ./scripts/scenario-benchmark.sh

set -euo pipefail

RESULTS_DIR="/tmp/scenario-benchmark"
rm -rf "$RESULTS_DIR"
mkdir -p "$RESULTS_DIR"/{scenario1,scenario2,scenario3,skills}

# ── Authentication ──────────────────────────────────────────────────
TOKEN=$(curl -s -X POST "https://iam.cloud.ibm.com/identity/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey=$LOGS_API_KEY" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "✓ Authenticated"

# Helper: run a DataPrime query and save response
run_query() {
  local name="$1"
  local query="$2"
  local outfile="$3"

  local payload
  payload=$(python3 -c "
import json, sys
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
start = now - timedelta(hours=6)
q = {
  'query': sys.argv[1],
  'metadata': {
    'startDate': start.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
    'endDate': now.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
    'defaultSource': 'logs',
    'tier': 'frequent_search',
    'syntax': 'dataprime',
    'limit': 200
  }
}
print(json.dumps(q))
" "$query")

  local http_code
  http_code=$(curl -s -w '%{http_code}' -o "$outfile" \
    -X POST "${LOGS_SERVICE_URL}/v1/query" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$payload")

  local size
  size=$(wc -c < "$outfile" | tr -d ' ')
  echo "  [$name] HTTP $http_code — $size bytes"
}

# Helper: measure a skill file
measure_skill() {
  local path="$1"
  local name
  name=$(basename "$path")
  if [ -f "$path" ]; then
    local size
    size=$(wc -c < "$path" | tr -d ' ')
    echo "  [$name] $size bytes"
    echo "$size" >> "$RESULTS_DIR/skills/sizes.txt"
  fi
}

# ══════════════════════════════════════════════════════════════════════
# SKILL FILES: Measure what enters context for each scenario
# ══════════════════════════════════════════════════════════════════════
echo ""
echo "═══ Measuring Skill Files ═══"

SKILLS_BASE=".agents/skills"

echo ""
echo "── Scenario 1 Skills (Incident Investigation) ──"
S1_SKILL_BYTES=0
for f in \
  "$SKILLS_BASE/ibm-cloud-logs-incident-investigation/SKILL.md" \
  "$SKILLS_BASE/ibm-cloud-logs-query/SKILL.md" \
  "$SKILLS_BASE/ibm-cloud-logs-incident-investigation/references/investigation-queries.md" \
  "$SKILLS_BASE/ibm-cloud-logs-incident-investigation/references/heuristic-details.md" \
  "$SKILLS_BASE/ibm-cloud-logs-alerting/SKILL.md" \
  "$SKILLS_BASE/ibm-cloud-logs-alerting/references/strategy-matrix.md" \
  "$SKILLS_BASE/ibm-cloud-logs-alerting/references/burn-rate-math.md"; do
  if [ -f "$f" ]; then
    sz=$(wc -c < "$f" | tr -d ' ')
    S1_SKILL_BYTES=$((S1_SKILL_BYTES + sz))
    measure_skill "$f"
  fi
done
echo "  TOTAL: $S1_SKILL_BYTES bytes"

echo ""
echo "── Scenario 2 Skills (Cost Optimization) ──"
S2_SKILL_BYTES=0
for f in \
  "$SKILLS_BASE/ibm-cloud-logs-cost-optimization/SKILL.md" \
  "$SKILLS_BASE/ibm-cloud-logs-query/SKILL.md" \
  "$SKILLS_BASE/ibm-cloud-logs-cost-optimization/references/tco-policies.md" \
  "$SKILLS_BASE/ibm-cloud-logs-cost-optimization/references/e2m-guide.md"; do
  if [ -f "$f" ]; then
    sz=$(wc -c < "$f" | tr -d ' ')
    S2_SKILL_BYTES=$((S2_SKILL_BYTES + sz))
    measure_skill "$f"
  fi
done
echo "  TOTAL: $S2_SKILL_BYTES bytes"

echo ""
echo "── Scenario 3 Skills (Monitoring Setup) ──"
S3_SKILL_BYTES=0
for f in \
  "$SKILLS_BASE/ibm-cloud-logs-query/SKILL.md" \
  "$SKILLS_BASE/ibm-cloud-logs-alerting/SKILL.md" \
  "$SKILLS_BASE/ibm-cloud-logs-alerting/references/component-profiles.md" \
  "$SKILLS_BASE/ibm-cloud-logs-alerting/references/strategy-matrix.md" \
  "$SKILLS_BASE/ibm-cloud-logs-alerting/references/burn-rate-math.md" \
  "$SKILLS_BASE/ibm-cloud-logs-alerting/references/runbook-templates.md" \
  "$SKILLS_BASE/ibm-cloud-logs-dashboards/SKILL.md" \
  "$SKILLS_BASE/ibm-cloud-logs-dashboards/references/dashboard-schema.md"; do
  if [ -f "$f" ]; then
    sz=$(wc -c < "$f" | tr -d ' ')
    S3_SKILL_BYTES=$((S3_SKILL_BYTES + sz))
    measure_skill "$f"
  fi
done
echo "  TOTAL: $S3_SKILL_BYTES bytes"

# ══════════════════════════════════════════════════════════════════════
# SCENARIO 1: Incident Investigation (Component Mode)
# Following: ibm-cloud-logs-incident-investigation/SKILL.md
# ══════════════════════════════════════════════════════════════════════
echo ""
echo "═══ Scenario 1: Incident Investigation ═══"
echo "Target: find top error-producing application, then deep-dive"
echo ""

# Phase 0: Check TCO policies to determine tier
echo "── Phase 0: TCO Policy Check ──"
curl -s -o "$RESULTS_DIR/scenario1/tco-policies.json" \
  -H "Authorization: Bearer $TOKEN" \
  "${LOGS_SERVICE_URL}/v1/tco_policies" 2>/dev/null || true
echo "  [tco-policies] $(wc -c < "$RESULTS_DIR/scenario1/tco-policies.json" | tr -d ' ') bytes"

# Phase 1: Global scan — error rate per application (Global Mode query 1)
echo ""
echo "── Phase 1: Error Reconnaissance (Global) ──"
run_query "global-error-rate" \
  "source logs | filter \$m.severity >= ERROR | groupby \$l.applicationname aggregate count() as error_count | orderby -error_count | limit 20" \
  "$RESULTS_DIR/scenario1/01-global-error-rate.json"

# Phase 1b: Error timeline
run_query "global-error-timeline" \
  "source logs | filter \$m.severity >= WARNING | groupby roundTime(\$m.timestamp, 1m) as time_bucket aggregate count() as errors | orderby time_bucket" \
  "$RESULTS_DIR/scenario1/02-global-error-timeline.json"

# Phase 1c: Critical errors
run_query "global-critical-errors" \
  "source logs | filter \$m.severity == CRITICAL | limit 50" \
  "$RESULTS_DIR/scenario1/03-global-critical.json"

# Phase 2: Component deep-dive (using top app from Phase 1)
echo ""
echo "── Phase 2: Component Deep-Dive ──"

# Get top application from phase 1
TOP_APP=$(python3 -c "
import json
try:
    with open('$RESULTS_DIR/scenario1/01-global-error-rate.json') as f:
        data = json.load(f)
    if 'results' in data and len(data['results']) > 0:
        # Try to find applicationname in the first result
        r = data['results'][0]
        if isinstance(r, dict):
            for k,v in r.items():
                if 'application' in k.lower():
                    print(v)
                    break
            else:
                print(list(r.values())[0] if r else 'unknown')
        else:
            print('unknown')
    else:
        print('unknown')
except:
    print('unknown')
" 2>/dev/null || echo "unknown")
echo "  Top application: $TOP_APP"

# Component errors
run_query "component-errors" \
  "source logs | filter \$l.applicationname == '$TOP_APP' && \$m.severity >= ERROR | choose \$m.timestamp, \$m.severity, \$l.subsystemname, \$d.message | orderby \$m.timestamp desc | limit 200" \
  "$RESULTS_DIR/scenario1/04-component-errors.json"

# Component error patterns
run_query "component-error-patterns" \
  "source logs | filter \$l.applicationname == '$TOP_APP' && \$m.severity >= ERROR | groupby \$d.message:string aggregate count() as occurrences | orderby -occurrences | limit 20" \
  "$RESULTS_DIR/scenario1/05-component-patterns.json"

# Component subsystems
run_query "component-subsystems" \
  "source logs | filter \$l.applicationname == '$TOP_APP' && \$m.severity >= WARNING | groupby \$l.subsystemname aggregate count() as error_count | orderby -error_count" \
  "$RESULTS_DIR/scenario1/06-component-subsystems.json"

# Component dependencies
run_query "component-dependencies" \
  "source logs | filter \$l.applicationname == '$TOP_APP' && (\$d.message:string.contains('connection') || \$d.message:string.contains('timeout') || \$d.message:string.contains('refused')) | limit 100" \
  "$RESULTS_DIR/scenario1/07-component-deps.json"

# ══════════════════════════════════════════════════════════════════════
# SCENARIO 2: Cost Optimization
# Following: ibm-cloud-logs-cost-optimization/SKILL.md
# ══════════════════════════════════════════════════════════════════════
echo ""
echo "═══ Scenario 2: Cost Optimization ═══"
echo ""

# Step 1: List TCO policies
echo "── Step 1: List TCO Policies ──"
curl -s -o "$RESULTS_DIR/scenario2/01-policies.json" \
  -H "Authorization: Bearer $TOKEN" \
  "${LOGS_SERVICE_URL}/v1/tco_policies" 2>/dev/null || true
echo "  [list-policies] $(wc -c < "$RESULTS_DIR/scenario2/01-policies.json" | tr -d ' ') bytes"

# Step 2: Volume by severity
echo ""
echo "── Step 2: Analyze Volume by Severity ──"
run_query "volume-by-severity" \
  "source logs | groupby \$m.severity aggregate count() as volume | orderby -volume" \
  "$RESULTS_DIR/scenario2/02-volume-severity.json"

# Step 3: Volume by application
run_query "volume-by-app" \
  "source logs | groupby \$l.applicationname aggregate count() as volume | orderby -volume | limit 20" \
  "$RESULTS_DIR/scenario2/03-volume-app.json"

# Step 4: Volume by application + severity (for tier recommendation)
run_query "volume-by-app-severity" \
  "source logs | groupby \$l.applicationname, \$m.severity aggregate count() as volume | orderby -volume | limit 50" \
  "$RESULTS_DIR/scenario2/04-volume-app-severity.json"

# ══════════════════════════════════════════════════════════════════════
# SCENARIO 3: Monitoring Setup
# Following: ibm-cloud-logs-alerting + ibm-cloud-logs-dashboards SKILLs
# ══════════════════════════════════════════════════════════════════════
echo ""
echo "═══ Scenario 3: Monitoring Setup ═══"
echo "Target: pick a real application and set up monitoring"
echo ""

# Step 1: Discover what applications exist
echo "── Step 1: Discover Applications ──"
run_query "discover-apps" \
  "source logs | groupby \$l.applicationname aggregate count() as volume, approx_count_distinct(\$l.subsystemname) as components | orderby -volume | limit 20" \
  "$RESULTS_DIR/scenario3/01-discover-apps.json"

# Pick first app for monitoring target
MONITOR_APP=$(python3 -c "
import json
try:
    with open('$RESULTS_DIR/scenario3/01-discover-apps.json') as f:
        data = json.load(f)
    if 'results' in data and len(data['results']) > 0:
        r = data['results'][0]
        if isinstance(r, dict):
            for k,v in r.items():
                if 'application' in k.lower():
                    print(v)
                    break
            else:
                print(list(r.values())[0] if r else 'unknown')
        else:
            print('unknown')
    else:
        print('unknown')
except:
    print('unknown')
" 2>/dev/null || echo "unknown")
echo "  Monitor target: $MONITOR_APP"

# Step 2: Discover log patterns
echo ""
echo "── Step 2: Discover Log Patterns ──"
run_query "app-patterns" \
  "source logs | filter \$l.applicationname == '$MONITOR_APP' | groupby \$l.subsystemname, \$m.severity aggregate count() as volume | orderby -volume | limit 20" \
  "$RESULTS_DIR/scenario3/02-app-patterns.json"

# Step 3: Error rate (for alert threshold baseline)
run_query "app-error-rate" \
  "source logs | filter \$l.applicationname == '$MONITOR_APP' && \$m.severity >= ERROR | groupby roundTime(\$m.timestamp, 5m) as bucket aggregate count() as errors | orderby bucket" \
  "$RESULTS_DIR/scenario3/03-error-rate.json"

# Step 4: List existing webhooks
echo ""
echo "── Step 3: Check Existing Webhooks ──"
curl -s -o "$RESULTS_DIR/scenario3/04-webhooks.json" \
  -H "Authorization: Bearer $TOKEN" \
  "${LOGS_SERVICE_URL}/v1/outgoing_webhooks" 2>/dev/null || true
echo "  [webhooks] $(wc -c < "$RESULTS_DIR/scenario3/04-webhooks.json" | tr -d ' ') bytes"

# Step 5: List existing alert definitions
echo ""
echo "── Step 4: Check Existing Alerts ──"
curl -s -o "$RESULTS_DIR/scenario3/05-alerts.json" \
  -H "Authorization: Bearer $TOKEN" \
  "${LOGS_SERVICE_URL}/v1/alert_defs" 2>/dev/null || true
echo "  [alert-defs] $(wc -c < "$RESULTS_DIR/scenario3/05-alerts.json" | tr -d ' ') bytes"

# Step 6: List existing dashboards
echo ""
echo "── Step 5: Check Existing Dashboards ──"
curl -s -o "$RESULTS_DIR/scenario3/06-dashboards.json" \
  -H "Authorization: Bearer $TOKEN" \
  "${LOGS_SERVICE_URL}/v1/dashboards" 2>/dev/null || true
echo "  [dashboards] $(wc -c < "$RESULTS_DIR/scenario3/06-dashboards.json" | tr -d ' ') bytes"


# ══════════════════════════════════════════════════════════════════════
# SUMMARY: Collect all byte sizes
# ══════════════════════════════════════════════════════════════════════
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  SCENARIO BENCHMARK RESULTS"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Scenario 1 totals
S1_QUERY_BYTES=0
for f in "$RESULTS_DIR"/scenario1/*.json; do
  sz=$(wc -c < "$f" | tr -d ' ')
  S1_QUERY_BYTES=$((S1_QUERY_BYTES + sz))
done

# Scenario 2 totals
S2_QUERY_BYTES=0
for f in "$RESULTS_DIR"/scenario2/*.json; do
  sz=$(wc -c < "$f" | tr -d ' ')
  S2_QUERY_BYTES=$((S2_QUERY_BYTES + sz))
done

# Scenario 3 totals
S3_QUERY_BYTES=0
for f in "$RESULTS_DIR"/scenario3/*.json; do
  sz=$(wc -c < "$f" | tr -d ' ')
  S3_QUERY_BYTES=$((S3_QUERY_BYTES + sz))
done

echo "Scenario 1 (Incident Investigation):"
echo "  Skill files loaded:  $S1_SKILL_BYTES bytes"
echo "  Query responses:     $S1_QUERY_BYTES bytes (8 queries)"
echo "  Total skills input:  $((S1_SKILL_BYTES + S1_QUERY_BYTES)) bytes"
echo ""
echo "Scenario 2 (Cost Optimization):"
echo "  Skill files loaded:  $S2_SKILL_BYTES bytes"
echo "  Query responses:     $S2_QUERY_BYTES bytes (4 queries)"
echo "  Total skills input:  $((S2_SKILL_BYTES + S2_QUERY_BYTES)) bytes"
echo ""
echo "Scenario 3 (Monitoring Setup):"
echo "  Skill files loaded:  $S3_SKILL_BYTES bytes"
echo "  Query responses:     $S3_QUERY_BYTES bytes (5 queries + 3 API calls)"
echo "  Total skills input:  $((S3_SKILL_BYTES + S3_QUERY_BYTES)) bytes"
echo ""

# MCP comparison
MCP_FIXED=71195
echo "MCP comparison (fixed overhead: $MCP_FIXED bytes for 98 tool defs):"
echo ""
echo "Scenario 1 MCP: $MCP_FIXED + query responses"
echo "  MCP tool calls: discover_tools, describe_tools, investigate_incident,"
echo "    session_context, suggest_alert, create_alert, create_outgoing_webhook (7 calls)"
echo ""
echo "Scenario 2 MCP: $MCP_FIXED + query responses"
echo "  MCP tool calls: discover_tools, list_policies, query_logs (x2),"
echo "    estimate_query_cost, create_policy (x2) (7 calls)"
echo ""
echo "Scenario 3 MCP: $MCP_FIXED + query responses"
echo "  MCP tool calls: query_logs, suggest_alert, create_alert,"
echo "    create_outgoing_webhook, create_dashboard, pin_dashboard (6 calls)"

# Write JSON summary for the benchmark script
python3 << 'PYEOF'
import json, os, glob

results_dir = "/tmp/scenario-benchmark"

def file_sizes(pattern):
    sizes = {}
    for f in sorted(glob.glob(pattern)):
        name = os.path.basename(f)
        sizes[name] = os.path.getsize(f)
    return sizes

summary = {
    "scenario1": {
        "name": "Incident Investigation",
        "skill_files_bytes": int(os.environ.get("S1_SKILL_BYTES", 0)),
        "query_files": file_sizes(f"{results_dir}/scenario1/*.json"),
        "query_total_bytes": sum(os.path.getsize(f) for f in glob.glob(f"{results_dir}/scenario1/*.json")),
        "query_count": len(glob.glob(f"{results_dir}/scenario1/*.json")),
        "mcp_tool_calls": ["discover_tools", "describe_tools", "investigate_incident", "session_context", "suggest_alert", "create_alert", "create_outgoing_webhook"],
        "mcp_tool_count": 7,
    },
    "scenario2": {
        "name": "Cost Optimization",
        "skill_files_bytes": int(os.environ.get("S2_SKILL_BYTES", 0)),
        "query_files": file_sizes(f"{results_dir}/scenario2/*.json"),
        "query_total_bytes": sum(os.path.getsize(f) for f in glob.glob(f"{results_dir}/scenario2/*.json")),
        "query_count": len(glob.glob(f"{results_dir}/scenario2/*.json")),
        "mcp_tool_calls": ["discover_tools", "list_policies", "query_logs", "query_logs", "estimate_query_cost", "create_policy", "create_policy"],
        "mcp_tool_count": 7,
    },
    "scenario3": {
        "name": "Monitoring Setup",
        "skill_files_bytes": int(os.environ.get("S3_SKILL_BYTES", 0)),
        "query_files": file_sizes(f"{results_dir}/scenario3/*.json"),
        "query_total_bytes": sum(os.path.getsize(f) for f in glob.glob(f"{results_dir}/scenario3/*.json")),
        "query_count": len(glob.glob(f"{results_dir}/scenario3/*.json")),
        "mcp_tool_calls": ["query_logs", "suggest_alert", "create_alert", "create_outgoing_webhook", "create_dashboard", "pin_dashboard"],
        "mcp_tool_count": 6,
    },
    "mcp_fixed_overhead_bytes": 71195,
    "mcp_fixed_overhead_tokens": 18794,
    "mcp_avg_response_tokens": 593,
}

with open(f"{results_dir}/scenario-benchmark.json", "w") as f:
    json.dump(summary, f, indent=2)
    f.write("\n")

print(f"\n✓ Results written to {results_dir}/scenario-benchmark.json")
PYEOF
