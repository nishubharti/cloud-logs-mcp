#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests"]
# ///
"""
Companion investigation script for IBM Cloud Logs agent skills.
Replicates the MCP smart_investigate workflow as a standalone script:
mode selection, multi-query strategy, SSE parsing, heuristic matching,
evidence synthesis — returns a compact markdown report.

Saves 5-10x tokens compared to manual multi-step queries in agent context.

Usage:
    export LOGS_SERVICE_URL="https://<instance>.api.<region>.logs.cloud.ibm.com"
    export LOGS_API_KEY="your-api-key"  # pragma: allowlist secret

    # Global scan (all applications)
    python3 scripts/investigate.py --time-range 1h

    # Component deep-dive
    python3 scripts/investigate.py --application api-gateway --time-range 1h

    # Request tracing
    python3 scripts/investigate.py --trace-id abc123

    # Write to file for agent consumption
    python3 scripts/investigate.py --application api-gateway --output-file /tmp/report.md

License: Apache-2.0
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field

import requests

# ── Constants ─────────────────────────────────────────────────────

IAM_TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"
MESSAGE_FIELDS = ["message", "error", "error_message", "msg", "text"]

# ── Heuristic Patterns (matching Go implementation) ───────────────

HEURISTICS = {
    "timeout": {
        "patterns": [
            "timeout", "timed out", "deadline exceeded",
            "context deadline", "read timeout", "write timeout",
            "connection timeout", "request timeout", "504",
        ],
        "description": "Timeout detected — downstream service or network latency issue",
        "action": "Check downstream service health and network latency",
    },
    "memory": {
        "patterns": [
            "out of memory", "oom", "heap space", "memory limit",
            "gc overhead", "allocation failure", "java.lang.outofmemory",
            "fatal error: runtime: out of memory", "oomkilled",
            "memory pressure", "memory leak",
        ],
        "description": "Memory pressure — container or process memory exhaustion",
        "action": "Check container memory limits, look for memory leaks",
    },
    "database": {
        "patterns": [
            "connection pool", "too many connections", "deadlock",
            "lock wait timeout", "cannot acquire", "database",
            "sql", "query failed", "transaction", "postgres", "mysql",
            "mongodb", "redis", "connection refused", "max_connections",
            "slow query", "query timeout",
        ],
        "description": "Database issue — connection pool, deadlock, or query failure",
        "action": "Check database connection pools, slow query logs, and locks",
    },
    "auth": {
        "patterns": [
            "unauthorized", "forbidden", "401", "403",
            "authentication failed", "invalid token", "expired token",
            "access denied", "permission denied", "invalid credentials",
            "jwt", "oauth", "saml",
        ],
        "description": "Authentication/authorization failure",
        "action": "Verify credentials, token expiry, and permission configuration",
    },
    "rate_limit": {
        "patterns": [
            "rate limit", "429", "too many requests", "throttled",
            "quota exceeded", "limit exceeded", "backoff",
        ],
        "description": "Rate limiting — service is throttling requests",
        "action": "Review rate limits, implement backoff, or request quota increase",
    },
    "network": {
        "patterns": [
            "connection refused", "connection reset", "no route to host",
            "network unreachable", "dns", "econnrefused", "econnreset",
            "socket", "tcp", "ssl", "tls", "certificate",
            "502", "503", "bad gateway", "service unavailable",
        ],
        "description": "Network connectivity issue",
        "action": "Check network connectivity, DNS resolution, and TLS certificates",
    },
}

DEPENDENCY_PATTERNS = {
    "connection refused": "Network/service connectivity failure",
    "timeout": "Downstream service not responding",
    "econnreset": "Connection reset by peer",
    "etimedout": "Connection timed out",
    "pool exhausted": "Connection pool exhaustion",
    "deadlock": "Database deadlock detected",
    "too many connections": "Connection limit exceeded",
}


# ── Data Types ────────────────────────────────────────────────────

@dataclass
class Finding:
    severity: str  # critical, high, medium, low
    summary: str
    detail: str = ""
    finding_type: str = ""  # error, spike, dependency, heuristic
    confidence: float = 0.8


@dataclass
class InvestigationResult:
    mode: str
    queries_ok: int = 0
    queries_failed: int = 0
    findings: list = field(default_factory=list)
    heuristic_matches: list = field(default_factory=list)
    root_cause: str = ""
    confidence: float = 0.0
    next_actions: list = field(default_factory=list)


# ── IAM Authentication ────────────────────────────────────────────

def get_iam_token(api_key: str) -> str:
    resp = requests.post(IAM_TOKEN_URL, data={
        "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
        "apikey": api_key,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


# ── SSE Response Parser ──────────────────────────────────────────

def parse_sse_response(raw_text: str) -> list[dict]:
    """Parse IBM Cloud Logs SSE response into event dicts."""
    events = []
    for line in raw_text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data: "):
            continue
        try:
            obj = json.loads(line[6:])
        except json.JSONDecodeError:
            continue

        if "query_id" in obj and len(obj) <= 2:
            continue

        # Aggregation: {"result": {"results": [{"user_data": "..."}]}}
        if "result" in obj and isinstance(obj.get("result"), dict):
            for item in obj["result"].get("results", []):
                ud = item.get("user_data")
                if ud and isinstance(ud, str):
                    try:
                        events.append(json.loads(ud))
                    except json.JSONDecodeError:
                        events.append(item)
                elif item:
                    events.append(item)

        # Direct: {"metadata": {...}, "user_data": "..."}
        elif "user_data" in obj:
            ud = obj["user_data"]
            if isinstance(ud, str):
                try:
                    parsed = json.loads(ud)
                    if "labels" in obj:
                        parsed["_labels"] = obj["labels"]
                    events.append(parsed)
                except json.JSONDecodeError:
                    events.append(obj)
            elif isinstance(ud, dict):
                events.append(ud)
    return events


# ── Query Execution ───────────────────────────────────────────────

def run_query(service_url: str, token: str, query: str,
              start_date: str, end_date: str, tier: str = "archive",
              limit: int = 200) -> tuple[list[dict], bool]:
    """Execute a query and return (events, success)."""
    try:
        resp = requests.post(
            f"{service_url}/v1/query",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "metadata": {
                    "startDate": start_date,
                    "endDate": end_date,
                    "defaultSource": "logs",
                    "tier": tier,
                    "syntax": "dataprime",
                    "limit": limit,
                }
            },
            timeout=120,
        )
        resp.raise_for_status()
        events = parse_sse_response(resp.text)
        return events, True
    except Exception as e:
        print(f"  Query failed: {e}", file=sys.stderr)
        return [], False


# ── Message Extraction ────────────────────────────────────────────

def extract_message(event: dict) -> str:
    for f in MESSAGE_FIELDS:
        if f in event and isinstance(event[f], str):
            return event[f]
        if "user_data" in event and isinstance(event["user_data"], dict):
            if f in event["user_data"]:
                return event["user_data"][f]
    return ""


def normalize_message(msg: str) -> str:
    msg = msg.lower()
    return msg[:80] if len(msg) > 80 else msg


def severity_label(count) -> str:
    count = float(count) if count else 0
    if count > 500:
        return "critical"
    elif count > 100:
        return "high"
    elif count > 20:
        return "medium"
    return "low"


# ── Query Strategies ──────────────────────────────────────────────

def global_queries(start: str, end: str) -> list[tuple[str, str]]:
    return [
        ("global-error-rate",
         "source logs | filter $m.severity >= ERROR "
         "| groupby $l.applicationname aggregate count() as error_count "
         "| orderby -error_count | limit 20"),
        ("global-error-timeline",
         "source logs | filter $m.severity >= WARNING "
         "| groupby roundTime($m.timestamp, 1m) as time_bucket aggregate count() as errors "
         "| orderby time_bucket"),
        ("global-critical-errors",
         "source logs | filter $m.severity == CRITICAL | limit 50"),
    ]


def component_queries(service: str) -> list[tuple[str, str]]:
    return [
        ("component-error-patterns",
         f"source logs | filter $l.applicationname == '{service}' && $m.severity >= ERROR "
         f"| groupby $d.message:string aggregate count() as occurrences "
         f"| orderby -occurrences | limit 20"),
        ("component-subsystems",
         f"source logs | filter $l.applicationname == '{service}' && $m.severity >= WARNING "
         f"| groupby $l.subsystemname aggregate count() as errors "
         f"| orderby -errors"),
        ("component-dependencies",
         f"source logs | filter $l.applicationname == '{service}' "
         f"&& ($d.message:string.contains('connection') "
         f"|| $d.message:string.contains('timeout') "
         f"|| $d.message:string.contains('refused')) | limit 100"),
    ]


def flow_queries(trace_id: str = None, correlation_id: str = None) -> list[tuple[str, str]]:
    queries = []
    if trace_id:
        queries.append(("flow-by-trace",
                        f"source logs | filter $d.trace_id == '{trace_id}' "
                        f"| orderby $m.timestamp asc | limit 500"))
    if correlation_id:
        queries.append(("flow-by-correlation",
                        f"source logs | filter $d.correlation_id == '{correlation_id}' "
                        f"| orderby $m.timestamp asc | limit 500"))
    return queries


# ── Analysis Functions ────────────────────────────────────────────

def analyze_error_rates(events: list[dict]) -> list[Finding]:
    """Analyze global error rate results."""
    findings = []
    for event in events:
        app = event.get("applicationname", event.get("$l.applicationname",
              event.get("_expr0", "unknown")))
        count = event.get("error_count", 0)
        if isinstance(count, str):
            try:
                count = int(count)
            except ValueError:
                continue
        if count > 10:
            sev = severity_label(count)
            findings.append(Finding(
                severity=sev,
                summary=f"High error volume: {count:,} errors in {app}",
                finding_type="error",
                confidence=0.9,
            ))
    return findings


def analyze_timeline(events: list[dict]) -> list[Finding]:
    """Detect error spikes in timeline data."""
    findings = []
    counts = []
    for event in events:
        c = event.get("errors", 0)
        if isinstance(c, str):
            try:
                c = int(c)
            except ValueError:
                continue
        counts.append(c)

    if len(counts) < 3:
        return findings

    avg = sum(counts) / len(counts)
    for i, c in enumerate(counts):
        if c > avg * 3 and c > 10:  # 3x multiplier, minimum 10
            findings.append(Finding(
                severity="high",
                summary=f"Error spike detected: {c} errors at bucket {i} (avg: {avg:.0f})",
                finding_type="spike",
                confidence=0.85,
            ))
            break  # Report first spike only
    return findings


def analyze_error_patterns(events: list[dict]) -> list[Finding]:
    """Analyze recurring error patterns."""
    findings = []
    for event in events:
        msg = event.get("message", event.get("$d.message",
              event.get("_expr0", "")))
        count = event.get("occurrences", 0)
        if isinstance(count, str):
            try:
                count = int(count)
            except ValueError:
                continue
        if count > 5:
            findings.append(Finding(
                severity=severity_label(count),
                summary=f"Recurring error: {msg[:100]} ({count} occurrences)",
                finding_type="error",
                confidence=0.85,
            ))
    return findings


def analyze_subsystems(events: list[dict]) -> list[Finding]:
    """Analyze subsystem error distribution."""
    findings = []
    for event in events:
        sub = event.get("subsystemname", event.get("$l.subsystemname",
              event.get("_expr0", "unknown")))
        count = event.get("errors", 0)
        if isinstance(count, str):
            try:
                count = int(count)
            except ValueError:
                continue
        if count > 20:
            findings.append(Finding(
                severity=severity_label(count),
                summary=f"High errors in subsystem {sub}: {count:,} errors",
                finding_type="error",
                confidence=0.8,
            ))
    return findings


def analyze_dependencies(events: list[dict]) -> list[Finding]:
    """Detect dependency issues from log messages."""
    findings = []
    pattern_counts: dict[str, int] = {}
    for event in events:
        msg = extract_message(event).lower()
        for pattern, desc in DEPENDENCY_PATTERNS.items():
            if pattern in msg:
                pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1

    for pattern, count in sorted(pattern_counts.items(), key=lambda x: -x[1]):
        if count >= 3:
            findings.append(Finding(
                severity="high" if count > 10 else "medium",
                summary=f"Dependency issue: {DEPENDENCY_PATTERNS[pattern]} ({count} occurrences)",
                finding_type="dependency",
                confidence=0.85,
            ))
    return findings


def analyze_critical_errors(events: list[dict]) -> list[Finding]:
    """Analyze critical error patterns."""
    findings = []
    msg_counts: dict[str, int] = {}
    for event in events:
        msg = normalize_message(extract_message(event))
        if msg:
            msg_counts[msg] = msg_counts.get(msg, 0) + 1

    for msg, count in sorted(msg_counts.items(), key=lambda x: -x[1]):
        if count >= 3:
            findings.append(Finding(
                severity="critical",
                summary=f"Recurring critical error: {msg[:80]} ({count} occurrences)",
                finding_type="error",
                confidence=0.9,
            ))
    return findings


# ── Heuristic Engine ──────────────────────────────────────────────

def run_heuristics(events: list[dict]) -> list[dict]:
    """Match events against heuristic patterns."""
    all_messages = []
    for event in events:
        msg = extract_message(event).lower()
        if msg:
            all_messages.append(msg)

    matches = []
    for name, heuristic in HEURISTICS.items():
        hit_count = 0
        for msg in all_messages:
            for pattern in heuristic["patterns"]:
                if pattern in msg:
                    hit_count += 1
                    break
        if hit_count > 0:
            matches.append({
                "heuristic": name,
                "hits": hit_count,
                "description": heuristic["description"],
                "action": heuristic["action"],
            })

    return sorted(matches, key=lambda m: m["hits"], reverse=True)


# ── Evidence Synthesis ────────────────────────────────────────────

def synthesize(result: InvestigationResult):
    """Synthesize root cause and next actions from findings."""
    if not result.findings:
        result.root_cause = "No significant issues detected"
        result.confidence = 0.0
        result.next_actions = ["Expand time range and retry", "Check log ingestion configuration"]
        return

    # Sort by severity
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    result.findings.sort(key=lambda f: severity_order.get(f.severity, 4))

    # Prioritize dependency issues
    dep_findings = [f for f in result.findings if f.finding_type == "dependency"]
    error_findings = [f for f in result.findings if f.finding_type in ("error", "spike")]

    if dep_findings:
        result.root_cause = f"Dependency failure: {dep_findings[0].summary}"
    elif error_findings:
        result.root_cause = f"Error pattern: {error_findings[0].summary}"
    else:
        result.root_cause = result.findings[0].summary

    result.confidence = sum(f.confidence for f in result.findings) / len(result.findings)

    # Build next actions from heuristic matches
    actions = []
    for match in result.heuristic_matches[:3]:
        actions.append(match["action"])
    if not actions:
        actions = [
            "Drill down into the highest-error application",
            "Check recent deployments for correlation",
            "Review downstream service health",
        ]
    result.next_actions = actions


# ── Report Formatting ─────────────────────────────────────────────

def format_report(result: InvestigationResult, time_range: str) -> str:
    lines = [
        "# Investigation Report",
        f"**Mode:** {result.mode} | **Time:** last {time_range} | "
        f"**Queries:** {result.queries_ok} OK, {result.queries_failed} failed",
        "",
    ]

    # Root cause
    conf_pct = round(result.confidence * 100)
    lines.append("## Root Cause")
    lines.append(f"> {result.root_cause} (confidence: {conf_pct}%)")
    lines.append("")

    # Findings
    if result.findings:
        lines.append(f"## Findings (top {min(len(result.findings), 10)})")
        for i, f in enumerate(result.findings[:10], 1):
            sev_tag = f"[{f.severity.upper()}]"
            lines.append(f"{i}. {sev_tag} {f.summary}")
        lines.append("")

    # Heuristic matches
    if result.heuristic_matches:
        lines.append("## Heuristic Matches")
        for m in result.heuristic_matches[:5]:
            lines.append(f"- **{m['heuristic']}** ({m['hits']} hits): {m['description']}")
        lines.append("")

    # Next actions
    lines.append("## Next Actions")
    for i, action in enumerate(result.next_actions[:5], 1):
        lines.append(f"{i}. {action}")

    return "\n".join(lines)


# ── Investigation Runner ─────────────────────────────────────────

def investigate(service_url: str, token: str, args) -> InvestigationResult:
    """Run the full investigation pipeline."""
    # Determine mode
    if args.trace_id or args.correlation_id:
        mode = "flow"
    elif args.application:
        mode = "component"
    else:
        mode = "global"

    start_date, end_date = parse_time_range(args.time_range)
    result = InvestigationResult(mode=mode)
    all_events = []

    # Get queries for mode
    if mode == "global":
        queries = global_queries(start_date, end_date)
    elif mode == "component":
        queries = component_queries(args.application)
    else:
        queries = flow_queries(args.trace_id, args.correlation_id)

    # Execute queries
    for name, query in queries:
        print(f"  Running {name}...", file=sys.stderr)
        events, ok = run_query(service_url, token, query, start_date, end_date,
                               tier=args.tier)
        if ok:
            result.queries_ok += 1
        else:
            result.queries_failed += 1

        # Analyze based on query type
        if "error-rate" in name:
            result.findings.extend(analyze_error_rates(events))
        elif "timeline" in name:
            result.findings.extend(analyze_timeline(events))
        elif "critical" in name:
            result.findings.extend(analyze_critical_errors(events))
        elif "error-patterns" in name:
            result.findings.extend(analyze_error_patterns(events))
        elif "subsystems" in name:
            result.findings.extend(analyze_subsystems(events))
        elif "dependencies" in name:
            result.findings.extend(analyze_dependencies(events))

        all_events.extend(events)

    # Run heuristics on all collected events
    result.heuristic_matches = run_heuristics(all_events)

    # Synthesize
    synthesize(result)

    return result


# ── Time Range Parsing ────────────────────────────────────────────

def parse_time_range(time_range: str) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    units = {"m": "minutes", "h": "hours", "d": "days"}
    unit = time_range[-1]
    value = int(time_range[:-1])
    delta = timedelta(**{units[unit]: value})
    start = now - delta
    return start.strftime("%Y-%m-%dT%H:%M:%S.000Z"), now.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Run full incident investigation pipeline against IBM Cloud Logs.")
    parser.add_argument("--application", help="Application name for component deep-dive")
    parser.add_argument("--trace-id", help="Trace ID for request flow tracing")
    parser.add_argument("--correlation-id", help="Correlation ID for request flow tracing")
    parser.add_argument("--time-range", default="1h",
                        help="Time range: 15m, 1h, 6h, 24h, 7d (default: 1h)")
    parser.add_argument("--tier", default="archive", choices=["archive", "frequent_search"],
                        help="Log tier (default: archive)")
    parser.add_argument("--output-file", help="Write report to file instead of stdout")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print queries without executing")
    args = parser.parse_args()

    service_url = os.environ.get("LOGS_SERVICE_URL")
    api_key = os.environ.get("LOGS_API_KEY")

    if not service_url or not api_key:
        print("Error: LOGS_SERVICE_URL and LOGS_API_KEY must be set", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        start, end = parse_time_range(args.time_range)
        if args.trace_id or args.correlation_id:
            queries = flow_queries(args.trace_id, args.correlation_id)
            mode = "flow"
        elif args.application:
            queries = component_queries(args.application)
            mode = "component"
        else:
            queries = global_queries(start, end)
            mode = "global"
        print(f"Mode: {mode}")
        print(f"Time: {start} → {end}")
        for name, query in queries:
            print(f"  {name}: {query}")
        return

    # Auth
    print("Authenticating...", file=sys.stderr)
    token = get_iam_token(api_key)

    # Investigate
    print(f"Running investigation...", file=sys.stderr)
    result = investigate(service_url, token, args)

    # Format report
    report = format_report(result, args.time_range)

    # Output
    if args.output_file:
        with open(args.output_file, "w") as f:
            f.write(report)
            f.write("\n")
        print(f"Report written to {args.output_file} ({len(report)} bytes)", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
