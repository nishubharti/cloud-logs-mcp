#!/usr/bin/env python3
"""
Measure real MCP tool response sizes for the same 3 scenarios.
Starts the MCP server binary against the cxint instance and sends
JSON-RPC tool calls, measuring actual response bytes.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

BINARY = os.path.join(os.path.dirname(__file__), "..", "bin", "logs-mcp-server")
OUT_DIR = "/tmp/iteration-tax/mcp"
BYTES_PER_TOKEN = 3.79

for var in ("LOGS_SERVICE_URL", "LOGS_API_KEY"):
    if not os.environ.get(var):
        print(f"Error: {var} must be set", file=sys.stderr)
        print(f"  export LOGS_API_KEY='your-api-key'", file=sys.stderr)  # pragma: allowlist secret
        print(f"  export LOGS_SERVICE_URL='https://<instance>.api.<region>.logs.cloud.ibm.com'", file=sys.stderr)
        sys.exit(1)

ENV = {
    **os.environ,
    "ENVIRONMENT": "production",
    "LOGS_HEALTH_PORT": "0",
}

# Time range for queries (24h)
now = datetime.now(timezone.utc)
start = now - timedelta(hours=24)
START_DATE = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
END_DATE = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

os.makedirs(OUT_DIR, exist_ok=True)

# ── JSON-RPC helpers ──────────────────────────────────────────────

msg_id = 0


def make_request(method, params=None):
    global msg_id
    msg_id += 1
    req = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params:
        req["params"] = params
    return req


def send_recv(proc, request, label, timeout=120):
    """Send JSON-RPC request and read response matching request id.
    Skips server notifications (no 'id' field)."""
    target_id = request.get("id")
    req_line = json.dumps(request) + "\n"
    proc.stdin.write(req_line.encode())
    proc.stdin.flush()

    start_t = time.time()
    while True:
        if time.time() - start_t > timeout:
            print(f"  TIMEOUT waiting for response to {label} (id={target_id})")
            return None, b""

        line = proc.stdout.readline()
        if not line:
            time.sleep(0.05)
            continue

        try:
            msg = json.loads(line.decode())
        except json.JSONDecodeError:
            continue

        # Skip notifications (no id)
        if "id" not in msg:
            continue

        # Match by id
        if msg["id"] == target_id:
            return msg, line

        # Wrong id — log and skip
        continue


def log_step(scenario, label, raw_bytes, category, ledger):
    byte_count = len(raw_bytes)
    tokens = round(byte_count / BYTES_PER_TOKEN)
    entry = {
        "scenario": scenario,
        "label": label,
        "bytes": byte_count,
        "tokens": tokens,
        "category": category,
    }
    ledger.append(entry)
    marker = "✓" if category != "error_retry" else "✗"
    print(f"  {marker} {label:<55s} {tokens:>6,} tokens ({byte_count:,} bytes)")
    return entry


# ── Start MCP server ──────────────────────────────────────────────

print("Starting MCP server binary...")
proc = subprocess.Popen(
    [BINARY],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env=ENV,
)
time.sleep(2)

ledger = []

# ── Initialize ────────────────────────────────────────────────────

print()
print("═══ MCP Protocol Initialization ═══")
print()

init_req = make_request("initialize", {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "benchmark", "version": "1.0"},
})
init_resp, init_raw = send_recv(proc, init_req, "initialize", timeout=10)
if init_resp:
    with open(f"{OUT_DIR}/00-init.json", "w") as f:
        json.dump(init_resp, f, indent=2)
    log_step("init", "Initialize handshake", init_raw, "protocol", ledger)

    # Send initialized notification
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    proc.stdin.write((json.dumps(notif) + "\n").encode())
    proc.stdin.flush()
    time.sleep(1)
else:
    print("  FAILED to initialize!")
    proc.terminate()
    sys.exit(1)

# ── tools/list (fixed overhead) ───────────────────────────────────

print()
print("═══ Fixed Overhead: tools/list ═══")
print()

list_req = make_request("tools/list", {})
list_resp, list_raw = send_recv(proc, list_req, "tools/list", timeout=15)
if list_resp:
    with open(f"{OUT_DIR}/01-tools-list.json", "w") as f:
        json.dump(list_resp, f, indent=2)
    tool_count = len(list_resp.get("result", {}).get("tools", []))
    log_step("overhead", f"tools/list ({tool_count} tools)", list_raw, "fixed_overhead", ledger)
else:
    print("  FAILED to get tools/list!")
    proc.terminate()
    sys.exit(1)


# ── Helper to call a tool ─────────────────────────────────────────

def call_tool(name, arguments, scenario, label, timeout=120):
    req = make_request("tools/call", {"name": name, "arguments": arguments})
    resp, raw = send_recv(proc, req, label, timeout=timeout)
    category = "tool_response"
    if resp:
        result = resp.get("result", {})
        if result.get("isError"):
            category = "error_retry"
        # Also check for error content
        content = result.get("content", [])
        for c in content:
            text = c.get("text", "")
            if "error" in text.lower()[:50] and len(text) < 500:
                category = "error_retry"
                break
        fname = f"{OUT_DIR}/{scenario}-{name}-{msg_id}.json"
        with open(fname, "w") as f:
            json.dump(resp, f, indent=2)
    else:
        category = "error_retry"
    log_step(scenario, label, raw, category, ledger)
    return resp, raw


# ══════════════════════════════════════════════════════════════════
# SCENARIO 1: Incident Investigation
# ══════════════════════════════════════════════════════════════════

print()
print("═══ SCENARIO 1: Incident Investigation (MCP) ═══")
print()

# investigate_incident — server-side multi-query + summarization
call_tool("investigate_incident", {
    "time_range": "24h",
    "severity": "error",
}, "s1", "investigate_incident (global, 24h)", timeout=180)

# suggest_alert — SRE-grade alert recommendation
call_tool("suggest_alert", {
    "service_type": "web_service",
    "slo_target": 0.999,
    "use_case": "high error rate on radiant service",
    "query": "source logs | filter $l.applicationname == 'radiant' && $m.severity >= ERROR",
}, "s1", "suggest_alert (radiant)")

# Create the alert definition
call_tool("create_alert_definition", {
    "definition": {
        "name": "radiant-error-rate-benchmark",
        "description": "Benchmark: Alert on radiant error rate",
        "is_active": True,
        "severity": "error",
        "condition": {
            "logs_threshold": {
                "rules": [{
                    "condition": {
                        "threshold": 100,
                        "time_window": "LOGS_TIME_WINDOW_VALUE_MINUTES_10",
                        "condition_type": "MORE_THAN"
                    }
                }],
                "notification_payload_filter": ["applicationname:radiant AND severity:error"]
            }
        }
    },
    "dry_run": True,
}, "s1", "create_alert_definition")


# ══════════════════════════════════════════════════════════════════
# SCENARIO 2: Cost Optimization
# ══════════════════════════════════════════════════════════════════

print()
print("═══ SCENARIO 2: Cost Optimization (MCP) ═══")
print()

# List TCO policies
call_tool("list_policies", {}, "s2", "list_policies")

# Query volume by severity
call_tool("query_logs", {
    "query": "source logs | groupby $m.severity aggregate count() as volume | orderby -volume",
    "tier": "archive",
    "start_date": START_DATE,
    "end_date": END_DATE,
}, "s2", "query_logs (volume by severity)")

# Query volume by application
call_tool("query_logs", {
    "query": "source logs | groupby $l.applicationname aggregate count() as volume | orderby -volume | limit 20",
    "tier": "archive",
    "start_date": START_DATE,
    "end_date": END_DATE,
}, "s2", "query_logs (volume by app)")

# Estimate query cost
call_tool("estimate_query_cost", {
    "query": "source logs | groupby $l.applicationname, $m.severity aggregate count() as volume | orderby -volume | limit 50",
    "tier": "archive",
}, "s2", "estimate_query_cost")

# Create a TCO policy
call_tool("create_policy", {
    "policy": {
        "name": "archive-info-logs-benchmark",
        "description": "Benchmark: Route INFO logs to archive tier",
        "priority": "type_medium",
        "application_rule": {"name": "*", "rule_type_id": "is"},
        "subsystem_rule": {"name": "*", "rule_type_id": "is"},
        "log_rules": {"severities": ["info"]},
        "enabled": True,
    },
    "dry_run": True,
}, "s2", "create_policy (INFO→archive)")


# ══════════════════════════════════════════════════════════════════
# SCENARIO 3: Monitoring Setup
# ══════════════════════════════════════════════════════════════════

print()
print("═══ SCENARIO 3: Monitoring Setup (MCP) ═══")
print()

# Discover applications
call_tool("query_logs", {
    "query": "source logs | groupby $l.applicationname aggregate count() as volume | orderby -volume | limit 20",
    "tier": "archive",
    "start_date": START_DATE,
    "end_date": END_DATE,
}, "s3", "query_logs (discover apps)")

# Suggest alert for the target app
call_tool("suggest_alert", {
    "service_type": "web_service",
    "slo_target": 0.999,
    "use_case": "monitor radiant service error rate and latency",
    "query": "source logs | filter $l.applicationname == 'radiant' && $m.severity >= ERROR",
}, "s3", "suggest_alert (radiant monitoring)")

# Create alert definition
call_tool("create_alert_definition", {
    "definition": {
        "name": "radiant-monitor-benchmark",
        "description": "Benchmark: Monitor radiant error rate",
        "is_active": True,
        "severity": "error",
        "condition": {
            "logs_threshold": {
                "rules": [{
                    "condition": {
                        "threshold": 50,
                        "time_window": "LOGS_TIME_WINDOW_VALUE_MINUTES_5",
                        "condition_type": "MORE_THAN"
                    }
                }],
                "notification_payload_filter": ["applicationname:radiant AND severity:error"]
            }
        }
    },
    "dry_run": True,
}, "s3", "create_alert_definition (monitor)")

# List webhooks
call_tool("list_outgoing_webhooks", {}, "s3", "list_outgoing_webhooks")

# Create dashboard
call_tool("create_dashboard", {
    "name": "Radiant Monitoring Benchmark",
    "description": "Benchmark: RED monitoring dashboard",
    "layout": {
        "sections": [{
            "id": {"value": "s1"},
            "rows": [{
                "id": {"value": "r1"},
                "appearance": {"height": 19},
                "widgets": [{
                    "id": {"value": "w1"},
                    "title": "Error Rate",
                    "definition": {
                        "line_chart": {
                            "query_definitions": [{
                                "id": "q1",
                                "query": {
                                    "logs": {
                                        "lucene_query": {"value": "applicationname:radiant AND severity:error"},
                                        "group_by": [],
                                        "aggregations": [{"type": "count"}]
                                    }
                                }
                            }]
                        }
                    }
                }]
            }]
        }]
    },
    "dry_run": True,
}, "s3", "create_dashboard")


# ══════════════════════════════════════════════════════════════════
# ANALYSIS
# ══════════════════════════════════════════════════════════════════

print()
print("═══════════════════════════════════════════════════════════════")
print("  MCP MEASURED RESULTS")
print("═══════════════════════════════════════════════════════════════")
print()

# Group by scenario
scenarios = {}
for entry in ledger:
    s = entry["scenario"]
    if s not in scenarios:
        scenarios[s] = []
    scenarios[s].append(entry)

# Fixed overhead
overhead_entries = scenarios.get("overhead", [])
fixed_overhead_tokens = sum(e["tokens"] for e in overhead_entries)
fixed_overhead_bytes = sum(e["bytes"] for e in overhead_entries)

print(f"━━━ Fixed Overhead ━━━")
print(f"  tools/list: {fixed_overhead_tokens:,} tokens ({fixed_overhead_bytes:,} bytes)")
print()

# Per-scenario breakdown
for sid, label in [("s1", "Scenario 1: Incident Investigation"),
                   ("s2", "Scenario 2: Cost Optimization"),
                   ("s3", "Scenario 3: Monitoring Setup")]:
    entries = scenarios.get(sid, [])
    if not entries:
        print(f"━━━ {label} ━━━  (no data)")
        continue

    response_tokens = sum(e["tokens"] for e in entries)
    response_bytes = sum(e["bytes"] for e in entries)
    total_tokens = fixed_overhead_tokens + response_tokens
    error_tokens = sum(e["tokens"] for e in entries if e["category"] == "error_retry")

    print(f"━━━ {label} ━━━")
    print()
    for e in entries:
        marker = "✗" if e["category"] == "error_retry" else "✓"
        print(f"  {marker} {e['label']:<55s} {e['tokens']:>6,} tokens ({e['bytes']:,} bytes)")
    print()
    print(f"  Fixed overhead (tools/list): {fixed_overhead_tokens:>6,} tokens")
    print(f"  Tool responses:              {response_tokens:>6,} tokens ({len(entries)} calls)")
    if error_tokens > 0:
        print(f"  Error responses:             {error_tokens:>6,} tokens")
    print(f"  TOTAL:                       {total_tokens:>6,} tokens")
    print()

# Grand totals
all_response_tokens = sum(e["tokens"] for e in ledger
                          if e["scenario"] in ("s1", "s2", "s3"))
grand_total = fixed_overhead_tokens + all_response_tokens

print("━━━ GRAND TOTALS (MCP) ━━━")
print()
print(f"  Fixed overhead:    {fixed_overhead_tokens:>6,} tokens (tools/list)")
print(f"  All responses:     {all_response_tokens:>6,} tokens")
print(f"  TOTAL:             {grand_total:>6,} tokens")
print()

# Write JSON results
output = {
    "fixed_overhead_tokens": fixed_overhead_tokens,
    "fixed_overhead_bytes": fixed_overhead_bytes,
    "scenarios": {},
    "grand_total_tokens": grand_total,
}

for sid in ["s1", "s2", "s3"]:
    entries = scenarios.get(sid, [])
    response_tokens = sum(e["tokens"] for e in entries)
    output["scenarios"][sid] = {
        "steps": [{"label": e["label"], "bytes": e["bytes"],
                   "tokens": e["tokens"], "category": e["category"]}
                  for e in entries],
        "response_tokens": response_tokens,
        "total_tokens": fixed_overhead_tokens + response_tokens,
    }

with open(f"{OUT_DIR}/mcp-measured.json", "w") as f:
    json.dump(output, f, indent=2)
    f.write("\n")

print(f"✓ Results: {OUT_DIR}/mcp-measured.json")

# Shutdown
proc.stdin.close()
proc.terminate()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
