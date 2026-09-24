#!/usr/bin/env python3
"""Example: copy matching log rows into a dataset for human review."""

from datetime import datetime, timezone
import os

import braintrust
import requests


# MODIFY THIS SQL for your project, time window, and review condition.
# This example selects disagreements between a scorer and a human action.
QUERY = """
SELECT id, root_span_id, input, output, scores['Decision quality'] AS grader_score
FROM project_logs('<project-id>')
WHERE created > now() - interval 1 day
  AND is_root = true
  AND input.source_topic IN ('topic/example_a', 'topic/example_b')
  AND (
    (scores['Decision quality'] >= 0.5 AND output.human_action = 'reply')
    OR (scores['Decision quality'] < 0.5 AND output.human_action = 'handoff')
  )
LIMIT 100
"""


def main():
    api_url = os.environ.get("BRAINTRUST_API_URL", "https://api.braintrust.dev").rstrip("/")
    response = requests.post(
        f"{api_url}/btql",
        headers={"Authorization": f"Bearer {os.environ['BRAINTRUST_API_KEY']}"},
        json={"query": QUERY, "fmt": "json"},
        timeout=60,
    )
    response.raise_for_status()
    rows = response.json()["data"]
    if len(rows) == 100:
        raise RuntimeError("Query hit LIMIT 100. Narrow the SQL or add pagination before scheduling it.")

    # MODIFY this project ID and dataset name to match the SQL above.
    dataset = braintrust.init_dataset(
        project_id="<project-id>",
        name=f"review-{datetime.now(timezone.utc):%Y-%m-%d}",
    )
    existing_ids = {row["id"] for row in dataset}
    for row in rows:
        if row["id"] in existing_ids:
            continue  # Keep any review label already added to this row.
        dataset.insert(
            id=row["id"],
            input=row["input"],
            metadata={
                "source_root_span_id": row["root_span_id"],
                "observed_output": row["output"],
                "grader_score": row["grader_score"],
            },
        )  # Leave expected empty for the reviewer.
    dataset.flush()
    print(f"{len(rows)} matching rows; dataset: {dataset.id}")


if __name__ == "__main__":
    main()
