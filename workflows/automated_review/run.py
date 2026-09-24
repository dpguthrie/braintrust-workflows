#!/usr/bin/env python3
"""Put a bounded UTC window's new disagreements into a Braintrust review dataset."""

import argparse
from datetime import date, datetime, time, timedelta, timezone
import os

import braintrust
import requests


# CUSTOMIZE: match the row that carries both the score and human decision.
PROJECT_ID = os.environ.get("BT_PROJECT_ID", "")
SOURCE_TOPICS = ("topic/example_a", "topic/example_b")
ROW_FILTER = "is_root = true"
SCORE_NAME = "Decision quality"
HUMAN_ACTION_PATH = ("output", "human_action")
HUMAN_POSITIVE = {"handoff"}
HUMAN_NEGATIVE = {"reply"}
POSITIVE_THRESHOLD = 0.5
MAX_ROWS = 5000


def field(row, path):
    for part in path:
        if not isinstance(row, dict):
            return None
        row = row.get(part)
    return row


def is_review_candidate(row):
    """Return True for a disagreement, False for agreement, None if incomplete."""
    score = (row.get("scores") or {}).get(SCORE_NAME)
    action = field(row, HUMAN_ACTION_PATH)
    if isinstance(score, bool):
        score = int(score)
    if not isinstance(score, (int, float)) or not 0 <= score <= 1 or action is None:
        return None
    action = str(action).strip().lower()
    if action in HUMAN_POSITIVE:
        human_yes = True
    elif action in HUMAN_NEGATIVE:
        human_yes = False
    else:
        return None
    return (score >= POSITIVE_THRESHOLD) != human_yes


def sql_quote(value):
    return "'" + value.replace("'", "''") + "'"


def source_rows(start_day, end_day):
    start = datetime.combine(start_day, time.min, timezone.utc)
    end = datetime.combine(end_day, time.min, timezone.utc)
    topics = ", ".join(sql_quote(topic) for topic in SOURCE_TOPICS)
    query = f"""SELECT id, root_span_id, created, input, output, scores
FROM project_logs({sql_quote(PROJECT_ID)})
WHERE created >= {sql_quote(start.isoformat().replace('+00:00', 'Z'))}
  AND created < {sql_quote(end.isoformat().replace('+00:00', 'Z'))}
  AND input.source_topic IN ({topics})
  AND ({ROW_FILTER})
ORDER BY _pagination_key
LIMIT 100"""
    url = os.environ.get("BRAINTRUST_API_URL", "https://api.braintrust.dev").rstrip("/")
    headers = {"Authorization": "Bearer " + os.environ["BRAINTRUST_API_KEY"]}
    rows, cursor, seen_cursors = [], None, set()
    while True:
        page_query = query + ("\nOFFSET " + sql_quote(cursor) if cursor else "")
        response = requests.post(url + "/btql", headers=headers,
                                 json={"query": page_query, "fmt": "json"}, timeout=60)
        response.raise_for_status()
        result = response.json()
        page = result["data"]
        rows.extend(page)
        if len(rows) >= MAX_ROWS:
            raise RuntimeError(f"Reached MAX_ROWS={MAX_ROWS}; narrow the window or raise the cap")
        cursor = result.get("cursor")
        if not cursor or not page:
            break
        if cursor in seen_cursors:
            raise RuntimeError("BTQL repeated a pagination cursor")
        seen_cursors.add(cursor)
    # Check exact values again before writes, even if the SQL filter changes.
    return [row for row in rows if field(row, ("input", "source_topic")) in SOURCE_TOPICS]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="UTC day, YYYY-MM-DD; default: yesterday")
    parser.add_argument("--start", help="inclusive UTC date for a historical interval")
    parser.add_argument("--end", help="exclusive UTC date for a historical interval")
    parser.add_argument("--dataset-name", help="override the daily dataset name")
    parser.add_argument("--apply", action="store_true", help="create/update the review dataset")
    args = parser.parse_args()
    if not PROJECT_ID or "BRAINTRUST_API_KEY" not in os.environ:
        parser.error("set BT_PROJECT_ID and BRAINTRUST_API_KEY")
    if args.start or args.end:
        if not args.start or not args.end or args.date:
            parser.error("use both --start and --end, without --date")
        start_day, end_day = date.fromisoformat(args.start), date.fromisoformat(args.end)
        if start_day >= end_day:
            parser.error("--start must precede --end")
        if not args.dataset_name:
            parser.error("historical intervals require --dataset-name")
    else:
        start_day = date.fromisoformat(args.date) if args.date else datetime.now(timezone.utc).date() - timedelta(days=1)
        end_day = start_day + timedelta(days=1)
    name = args.dataset_name or f"review-{start_day.isoformat()}"
    rows = source_rows(start_day, end_day)
    candidates = [(row, is_review_candidate(row)) for row in rows]
    disagreements = [row for row, match in candidates if match is True]
    skipped = sum(match is None for _, match in candidates)
    print(f"{name}: scanned={len(rows)} disagreements={len(disagreements)} incomplete={skipped}")
    if not args.apply or not disagreements:
        return

    dataset = braintrust.init_dataset(project_id=PROJECT_ID, name=name)
    existing = {field(row, ("metadata", "source_row_id")) for row in dataset}
    added = 0
    for row in disagreements:
        if row["id"] in existing:
            continue
        dataset.insert(
            id=row["id"],
            input=row.get("input"),
            metadata={
                "source_row_id": row["id"],
                "source_root_span_id": row.get("root_span_id"),
                "source_created": row.get("created"),
                "source_topic": field(row, ("input", "source_topic")),
                "observed_output": row.get("output"),
                "grader_score": (row.get("scores") or {})[SCORE_NAME],
                "human_action": field(row, HUMAN_ACTION_PATH),
                "score_name": SCORE_NAME,
            },
        )
        added += 1
    dataset.flush()
    print(f"dataset_id={dataset.id} inserted={added} already_present={len(disagreements) - added}")


if __name__ == "__main__":
    main()
