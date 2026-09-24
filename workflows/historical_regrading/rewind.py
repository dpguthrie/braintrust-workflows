#!/usr/bin/env python3
"""Rewind an existing, active online scoring rule to a UTC start time."""

from datetime import datetime
import os

import requests


def main():
    # MODIFY these three values. Configure the rule's scorer version and BTQL
    # filter first; the rewind runs the rule as it is currently configured.
    project_id = "<project-id>"
    automation_id = "<online-scoring-rule-id>"
    start_time = "2026-09-01T00:00:00Z"

    start_seconds = int(datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp())
    # The rewind API takes a transaction cursor, not a timestamp. This is a
    # search boundary, not a new transaction ID: Brainstore allocates real IDs.
    # The format is a fixed prefix, UTC seconds, and a 16-bit sequence number.
    first_cursor_at_start = (0x0DE1 << 48) | (start_seconds << 16)
    api_url = os.environ.get("BRAINTRUST_API_URL", "https://api.braintrust.dev").rstrip("/")
    response = requests.post(
        f"{api_url}/brainstore/automation/reset-cursors",
        headers={"Authorization": f"Bearer {os.environ['BRAINTRUST_API_KEY']}"},
        json={
            "automation_id": automation_id,
            "object_id": f"project_logs:{project_id}",
            "start_xact_id": str(first_cursor_at_start - 1),  # Inclusive start.
        },
        timeout=60,
    )
    response.raise_for_status()
    print(response.json())


if __name__ == "__main__":
    main()
