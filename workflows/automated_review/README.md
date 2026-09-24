# Automated review dataset

**Goal:** once a day, find new production rows that meet your condition and add them to a dataset where people can supply the correct answer. This example uses a disagreement between a numeric evaluator score and a human action. Change the condition for your workflow.

The dataset is named `review-YYYY-MM-DD` for the UTC day being processed. Each row keeps the source row ID, observed output, evaluator score, and human action in metadata. It deliberately leaves `expected` empty for the reviewer.

## Customize the Python example

Open [`run.py`](run.py) and edit the small **CUSTOMIZE** block:

1. Replace `BTQL_FILTER` with the exact Boolean expression you want in the SQL `WHERE` clause. The example combines `is_root = true` with two exact `input.source_topic` values. Keep the daily time bounds supplied by the script.
2. Set `SCORE_NAME` to the **score key shown on the log row** (often the evaluator's display name).
3. Set `HUMAN_ACTION_PATH` and the recognized positive/negative values.
4. Edit `is_review_candidate()` if your definition of a disagreement differs. Keep it a pure function over one row.
5. If the scored row is a child span, change the `is_root = true` portion of `BTQL_FILTER` to a stable condition for that span.

The time window is one complete UTC day. Schedule it after online scoring has settled. Run the same day again to catch late scores; the script checks existing dataset source IDs before inserting, so an ordinary rerun does not erase reviewer labels. Run only one copy of the job for a given day at a time.

```bash
pip install -r requirements.txt
export BRAINTRUST_API_KEY='...'
export BT_PROJECT_ID='<project-id>'

# Yesterday UTC, read-only preview
python workflows/automated_review/run.py

# A specific UTC day, write the dataset
python workflows/automated_review/run.py --date 2026-09-23 --apply

# After a historical regrade, use a separately named review dataset
python workflows/automated_review/run.py --start 2026-09-01 --end 2026-09-08 \
  --dataset-name review-backfill-v3 --apply
```

The script stops if the query reaches `MAX_ROWS`; narrow the window or raise the cap after checking volume. It paginates the API query by `_pagination_key` rather than taking only the first page. The dry run prints counts without creating a dataset. For a historical review, change `SCORE_NAME` to the newly adopted score key first, then confirm the selected rows actually carry that score.

## Codex scheduled task, using Braintrust MCP

Use [`CODEX.md`](CODEX.md) as a template for a daily Codex task. Fill the private task configuration with your actual project ID, one BTQL filter expression, score name, field paths, and decision mapping. The MCP path uses `sql_query` and `edit_dataset_rows`; it needs no Python runtime or API key in the task prompt. It queries the destination dataset before inserting because the MCP insert operation generates row IDs.

For interactive inspection, the Braintrust CLI can run the same SQL:

```bash
bt sql --non-interactive "SELECT id, input.source_topic, scores FROM project_logs('<project-id>') WHERE created > now() - interval 1 day AND (is_root = true AND input.source_topic IN ('topic/example_a', 'topic/example_b')) LIMIT 20"
```

## Adapting the cadence

Daily polling is easiest to operate. For lower latency, send a trace-ready event to a queue and run this same selection/write logic in a worker. Wait until both the evaluator score and human action are present, authenticate the webhook, and keep a scheduled sweep for late or missed events. A trace webhook that fires before scoring completes cannot reliably create a disagreement row on its first attempt.
