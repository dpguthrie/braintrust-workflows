#!/usr/bin/env python3
"""Example: copy matching log rows into a dataset for human review."""

from datetime import datetime, timezone
import os

import braintrust
import requests


def main():
    api_url = os.environ.get("BRAINTRUST_API_URL", "https://api.braintrust.dev").rstrip("/")
    # MODIFY this project ID and dataset name to match the SQL below.
    dataset = braintrust.init_dataset(
        project_id="<project-id>",
        name=f"review-{datetime.now(timezone.utc):%Y-%m-%d}",
    )
    existing_ids = {row["id"] for row in dataset}
    matched = inserted = 0
    cursor = None
    while True:
        offset = "OFFSET '" + cursor.replace("'", "''") + "'" if cursor else ""
        # MODIFY THIS SQL for your project, time window, and review condition.
        # This example selects disagreements between a scorer and a human action.
        query = f"""
SELECT id, _xact_id, project_id, input, output, scores['Decision quality'] AS grader_score
FROM project_logs('<project-id>')
WHERE created > now() - interval 1 day
  AND is_root = true
  AND input.source_topic IN ('topic/example_a', 'topic/example_b')
  AND (
    (scores['Decision quality'] >= 0.5 AND output.human_action = 'reply')
    OR (scores['Decision quality'] < 0.5 AND output.human_action = 'handoff')
  )
ORDER BY _pagination_key
LIMIT 100
{offset}
"""
        response = requests.post(
            f"{api_url}/btql",
            headers={"Authorization": f"Bearer {os.environ['BRAINTRUST_API_KEY']}"},
            json={"query": query, "fmt": "json"},
            timeout=60,
        )
        response.raise_for_status()
        result = response.json()
        page = result["data"]
        matched += len(page)
        events = [
            {
                "id": row["id"],
                "input": row["input"],
                "metadata": {
                    "observed_output": row["output"],
                    "grader_score": row["grader_score"],
                    "scorer_function_id": "<saved-scorer-id>",  # MODIFY for your scorer.
                    "scorer_version": "<version-used-to-grade-these-rows>",  # MODIFY after regrading completes.
                },
                "origin": {
                    "object_type": "project_logs",
                    "object_id": row["project_id"],
                    "id": row["id"],
                    "_xact_id": row["_xact_id"],
                },
            }
            for row in page
            if row["id"] not in existing_ids
        ]
        if events:
            insert_response = requests.post(
                f"{api_url}/v1/dataset/{dataset.id}/insert",
                headers={"Authorization": f"Bearer {os.environ['BRAINTRUST_API_KEY']}"},
                json={"events": events},
                timeout=60,
            )
            insert_response.raise_for_status()
            existing_ids.update(event["id"] for event in events)
            inserted += len(events)
        cursor = result.get("cursor") or response.headers.get("x-bt-cursor")
        if not cursor or not page:
            break
    print(f"{matched} matching rows; {inserted} inserted; dataset: {dataset.id}")


if __name__ == "__main__":
    main()
