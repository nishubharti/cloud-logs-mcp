#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests"]
# ///
"""
General-purpose query runner with SSE parsing and result compaction.
Runs any DataPrime query against IBM Cloud Logs and returns compacted results
instead of raw SSE payloads. Saves 5-50x tokens in agent context windows.

Usage:
    export LOGS_SERVICE_URL="https://<instance>.api.<region>.logs.cloud.ibm.com"
    export LOGS_API_KEY="your-api-key"  # pragma: allowlist secret

    python3 scripts/query-compact.py \\
      --query "source logs | filter $m.severity >= ERROR | groupby $l.applicationname aggregate count() as error_count | orderby -error_count | limit 20" \\
      --time-range 1h --format markdown

    python3 scripts/query-compact.py \\
      --query "source logs | filter $l.applicationname == 'api-gateway' | limit 50" \\
      --format json --max-events 20 --output-file /tmp/results.json

License: Apache-2.0
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# ── Constants ─────────────────────────────────────────────────────

IAM_TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"

METADATA_FIELDS_TO_REMOVE = {
    "logid", "branchid", "templateid", "priorityclass",
    "processingoutputtimestampnanos", "processingoutputtimestampmicros",
    "timestampmicros", "ingresstimestamp",
}

MESSAGE_FIELDS = ["message", "error", "error_message", "msg", "text"]


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
    """Parse IBM Cloud Logs SSE response into a list of event dicts."""
    events = []
    for line in raw_text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data: "):
            data = line[6:]
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue

            # Skip query_id messages
            if "query_id" in obj and len(obj) <= 2:
                continue

            # Aggregation format: {"result": {"results": [{"user_data": "..."}]}}
            if "result" in obj and isinstance(obj.get("result"), dict):
                results = obj["result"].get("results", [])
                for item in results:
                    user_data = item.get("user_data")
                    if user_data and isinstance(user_data, str):
                        try:
                            events.append(json.loads(user_data))
                        except json.JSONDecodeError:
                            events.append(item)
                    elif item:
                        events.append(item)

            # Direct log format: {"metadata": {...}, "labels": {...}, "user_data": "..."}
            elif "user_data" in obj:
                user_data = obj["user_data"]
                if isinstance(user_data, str):
                    try:
                        parsed = json.loads(user_data)
                        # Merge labels and metadata into parsed data
                        if "labels" in obj:
                            parsed["_labels"] = obj["labels"]
                        if "metadata" in obj:
                            parsed["_metadata"] = obj["metadata"]
                        events.append(parsed)
                    except json.JSONDecodeError:
                        events.append(obj)
                elif isinstance(user_data, dict):
                    events.append(user_data)
    return events


# ── Event Compaction ──────────────────────────────────────────────

def extract_message(event: dict) -> str:
    """Extract the most relevant message field from an event."""
    for field in MESSAGE_FIELDS:
        if field in event and isinstance(event[field], str):
            return event[field]
        # Check nested user_data
        if "user_data" in event and isinstance(event["user_data"], dict):
            if field in event["user_data"]:
                return event["user_data"][field]
    return ""


def clean_event(event: dict) -> dict:
    """Remove noisy metadata fields from an event."""
    return {k: v for k, v in event.items()
            if k.lower() not in METADATA_FIELDS_TO_REMOVE}


def compact_events(events: list[dict], max_events: int, max_msg_len: int) -> list[dict]:
    """Compact events: clean, truncate messages, limit count."""
    compacted = []
    for event in events[:max_events]:
        cleaned = clean_event(event)
        # Truncate long message fields
        for field in MESSAGE_FIELDS:
            if field in cleaned and isinstance(cleaned[field], str) and len(cleaned[field]) > max_msg_len:
                cleaned[field] = cleaned[field][:max_msg_len - 3] + "..."
        compacted.append(cleaned)
    return compacted


def deduplicate_by_message(events: list[dict]) -> list[dict]:
    """Group events by normalized message, return unique patterns with counts."""
    groups: dict[str, dict] = {}
    for event in events:
        msg = extract_message(event)
        key = msg.lower()[:50] if msg else str(event)[:50]
        if key not in groups:
            groups[key] = {"event": event, "count": 1}
        else:
            groups[key]["count"] += 1

    result = []
    for group in sorted(groups.values(), key=lambda g: g["count"], reverse=True):
        entry = group["event"].copy()
        if group["count"] > 1:
            entry["_occurrences"] = group["count"]
        result.append(entry)
    return result


# ── Query Execution ───────────────────────────────────────────────

def run_query(service_url: str, token: str, query: str, tier: str,
              start_date: str, end_date: str, limit: int) -> str:
    """Execute a DataPrime query and return raw SSE response text."""
    payload = {
        "query": query,
        "metadata": {
            "startDate": start_date,
            "endDate": end_date,
            "defaultSource": "logs",
            "tier": tier,
            "syntax": "dataprime",
            "limit": limit,
        }
    }
    resp = requests.post(
        f"{service_url}/v1/query",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.text


# ── Output Formatters ─────────────────────────────────────────────

def format_markdown(events: list[dict], query: str, total_raw: int) -> str:
    """Format compacted events as markdown."""
    unique = deduplicate_by_message(events)
    lines = [
        f"# Query Results ({total_raw} events, {len(unique)} unique patterns)",
        f"Query: `{query}`",
        "",
    ]

    if not events:
        lines.append("No results found.")
        return "\n".join(lines)

    # Detect if this is an aggregation result (has numeric fields, no message)
    sample = events[0]
    numeric_fields = [k for k, v in sample.items()
                      if isinstance(v, (int, float)) and not k.startswith("_")]
    string_fields = [k for k, v in sample.items()
                     if isinstance(v, str) and not k.startswith("_")]

    if numeric_fields and not extract_message(sample):
        # Aggregation result — render as table
        all_fields = string_fields + numeric_fields
        header = "| " + " | ".join(all_fields) + " |"
        sep = "|" + "|".join("---" for _ in all_fields) + "|"
        lines.extend([header, sep])
        for event in events:
            row = "| " + " | ".join(str(event.get(f, "")) for f in all_fields) + " |"
            lines.append(row)
    else:
        # Log entries — render as numbered list
        for i, event in enumerate(unique, 1):
            msg = extract_message(event)
            if not msg:
                msg = json.dumps(event, default=str)[:200]
            count_str = f" (x{event['_occurrences']})" if "_occurrences" in event else ""
            severity = event.get("severity", event.get("_metadata", {}).get("severity", ""))
            app = event.get("applicationname", event.get("_labels", {}).get("applicationname", ""))
            prefix = f"[{severity}] " if severity else ""
            app_prefix = f"({app}) " if app else ""
            lines.append(f"{i}. {prefix}{app_prefix}{msg[:200]}{count_str}")

    if total_raw > len(events):
        lines.append(f"\n*Showing {len(events)} of {total_raw} events. "
                      f"Use --max-events to adjust.*")
    return "\n".join(lines)


def format_json(events: list[dict]) -> str:
    """Format compacted events as JSON."""
    return json.dumps(events, indent=2, default=str)


def format_csv(events: list[dict]) -> str:
    """Format compacted events as TSV."""
    if not events:
        return ""
    fields = list(events[0].keys())
    lines = ["\t".join(fields)]
    for event in events:
        lines.append("\t".join(str(event.get(f, "")) for f in fields))
    return "\n".join(lines)


# ── Time Range Parsing ────────────────────────────────────────────

def parse_time_range(time_range: str) -> tuple[str, str]:
    """Convert a time range string to (start_date, end_date) ISO strings."""
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
        description="Run a DataPrime query with automatic SSE parsing and result compaction.")
    parser.add_argument("--query", required=True, help="DataPrime query string")
    parser.add_argument("--tier", default="archive", choices=["archive", "frequent_search"],
                        help="Log tier (default: archive)")
    parser.add_argument("--time-range", default="1h",
                        help="Time range: 15m, 1h, 6h, 24h, 7d (default: 1h)")
    parser.add_argument("--limit", type=int, default=200,
                        help="Max events from API (default: 200)")
    parser.add_argument("--max-events", type=int, default=50,
                        help="Max events in output (default: 50)")
    parser.add_argument("--max-message-len", type=int, default=200,
                        help="Max message length in chars (default: 200)")
    parser.add_argument("--format", default="markdown", choices=["markdown", "json", "csv"],
                        help="Output format (default: markdown)")
    parser.add_argument("--deduplicate", action="store_true", default=True,
                        help="Group similar messages (default: true)")
    parser.add_argument("--output-file", help="Write output to file instead of stdout")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print query without executing")
    args = parser.parse_args()

    service_url = os.environ.get("LOGS_SERVICE_URL")
    api_key = os.environ.get("LOGS_API_KEY")

    if not service_url or not api_key:
        print("Error: LOGS_SERVICE_URL and LOGS_API_KEY must be set", file=sys.stderr)
        sys.exit(1)

    start_date, end_date = parse_time_range(args.time_range)

    if args.dry_run:
        print(f"Query: {args.query}")
        print(f"Tier: {args.tier}")
        print(f"Time: {start_date} → {end_date}")
        print(f"Limit: {args.limit}")
        return

    # Auth
    token = get_iam_token(api_key)

    # Execute
    raw_text = run_query(service_url, token, args.query, args.tier,
                         start_date, end_date, args.limit)

    # Parse SSE
    events = parse_sse_response(raw_text)
    total_raw = len(events)

    # Compact
    compacted = compact_events(events, args.max_events, args.max_message_len)

    # Format
    if args.format == "markdown":
        output = format_markdown(compacted, args.query, total_raw)
    elif args.format == "json":
        if args.deduplicate:
            compacted = deduplicate_by_message(compacted)
        output = format_json(compacted)
    else:
        output = format_csv(compacted)

    # Output
    if args.output_file:
        with open(args.output_file, "w") as f:
            f.write(output)
            f.write("\n")
        print(f"Wrote {len(output)} bytes to {args.output_file}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
