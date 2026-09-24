# Automated review dataset

This example copies matching log rows from the previous 24 hours into a daily dataset for human review. The matching logic is **one SQL statement** in [`run.py`](run.py). Its example condition finds disagreements between a scorer and a human action.

## Change the example

1. Edit the SQL under `MODIFY THIS SQL`: project ID, time window, source filter, scorer field, and disagreement condition. The SQL decides which rows go into the dataset. Keep `id`, `_xact_id`, `project_id`, `input`, `output`, and a `grader_score` alias in the `SELECT` list, or update the insert block accordingly.
2. Change the project ID in `braintrust.init_dataset()` to the same project. Change the dataset name if desired.
3. Set `scorer_function_id` and `scorer_version` in the dataset row metadata to the exact saved scorer and version used for these scores. Run after historical regrading finishes; a version label is only accurate when the selected scores were produced by that version.
4. Run it with an API key that can read logs and write datasets:

```bash
pip install -r requirements.txt
export BRAINTRUST_API_KEY='...'
python workflows/automated_review/run.py
```

The query reads 100 rows per page and follows Braintrust's cursor until it has all matches. Keep `ORDER BY _pagination_key`, `LIMIT 100`, and the `OFFSET` line when editing the SQL; those lines make pagination work. Existing dataset row IDs are skipped, preserving labels from earlier reviews. The script leaves `expected` empty for the reviewer.

The insert request uses the dataset API so it can set the native top-level `origin` field, just as the UI does when copying a log. `origin` contains `object_type: project_logs`, the source `project_id`, row `id`, and `_xact_id`. The source row ID is also used as the dataset row ID to make reruns idempotent. Review context and the scorer ID/version go in `metadata`; they do not replace `origin`.

For an agent-driven version, use [`CODEX.md`](CODEX.md). It uses Braintrust MCP's `sql_query` tool. The MCP `sql_query` tool accepts SQL clauses as separate arguments; the agent should use the `SELECT` and `WHERE` clauses from the example query. The MCP dataset edit tool does not expose native `origin` fields, so that version uses the dataset insert API for writes.

For a webhook implementation, trigger the same query and insert logic only after the score and human action are both available. Keep a daily sweep to catch late events.
