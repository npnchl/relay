"""Metered cost lookup from provider usage APIs.

Soniox: every run is tagged with client_reference_id = run name
(stream_soniox.py), so per-patch cost is exact - fetched from
GET /v1/usage-logs and summed per reference id.

Speechmatics: the /v2/usage endpoint reports nothing for realtime
WebSocket sessions (verified live - it tracks batch jobs only), so
realtime spend is only visible in the portal. Costs for Speechmatics
runs are therefore estimates (audio hours x published rate).
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

SONIOX_LOGS_URL = "https://api.soniox.com/v1/usage-logs"
SPEECHMATICS_USAGE_URL = "https://eu1.asr.api.speechmatics.com/v2/usage"


def _get_json(url: str, key: str) -> object:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def soniox_costs_by_reference(
    key: str, start: str, end: str
) -> dict[str, float]:
    """Metered USD per client_reference_id, from Soniox usage logs.

    start/end are ISO-8601 UTC timestamps (required by the API); pages
    via next_page_cursor. Each entry carries cost_usd and the
    client_reference_id we tagged the run with.
    """
    costs: dict[str, float] = {}
    cursor: str | None = None
    while True:
        url = (
            f"{SONIOX_LOGS_URL}?start_time={start}&end_time={end}"
            + (f"&page_cursor={cursor}" if cursor else "")
        )
        data = _get_json(url, key)
        if not isinstance(data, dict):
            raise SystemExit(f"unexpected usage-logs response: {data!r}")
        entries = data.get("usage_logs") or []
        if not isinstance(entries, list):
            raise SystemExit(f"unexpected usage-logs entries: {entries!r}")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ref = entry.get("client_reference_id")
            cost = entry.get("cost_usd")
            if ref and cost is not None:
                costs[str(ref)] = costs.get(str(ref), 0.0) + float(cost)
        cursor = data.get("next_page_cursor")
        if not cursor:
            return costs


def speechmatics_usage(key: str) -> object:
    """Raw account usage from Speechmatics /v2/usage (may be null for
    realtime-only accounts - see module docstring)."""
    return _get_json(SPEECHMATICS_USAGE_URL, key)


def main() -> int:
    """CLI: dump both providers' usage data (last 7 days for soniox)."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    soniox_key = os.environ.get("SONIOX_API_KEY")
    sm_key = os.environ.get("SPEECHMATICS_API_KEY")
    if soniox_key:
        print("soniox costs by client_reference_id:")
        for ref, cost in sorted(
            soniox_costs_by_reference(soniox_key, start, end).items()
        ):
            print(f"  {ref:<40} ${cost:.4f}")
    else:
        print("SONIOX_API_KEY not set; skipping soniox", file=sys.stderr)
    if sm_key:
        print("\nspeechmatics /v2/usage response:")
        print(json.dumps(speechmatics_usage(sm_key), indent=2))
    else:
        print("SPEECHMATICS_API_KEY not set; skipping speechmatics", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
