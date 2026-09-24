# Automated review dataset

This example copies matching log rows from the previous 24 hours into a daily dataset for human review. The matching logic is **one SQL statement** in [`run.py`](run.py). Its example condition finds disagreements between a scorer and a human action.

## Change the example

1. Edit the SQL under `MODIFY THIS SQL`: project ID, time window, source filter, scorer field, and disagreement condition. The SQL decides which rows go into the dataset. Keep `id`, `root_span_id`, `input`, `output`, and a `grader_score` alias in the `SELECT` list, or update the small insert block accordingly.
2. Change the project ID in `braintrust.init_dataset()` to the same project. Change the dataset name if desired.
3. Run it with an API key that can read logs and write datasets:

```bash
pip install -r requirements.txt
export BRAINTRUST_API_KEY='...'
python workflows/automated_review/run.py
```

The example reads **at most 100 rows**. If the query returns 100, it stops before writing so a scheduled run cannot silently omit matches. Narrow the SQL or add pagination when adapting it to a larger workload. Existing dataset row IDs are skipped, preserving labels from earlier reviews. The script leaves `expected` empty for the reviewer.

For an agent-driven version, use [`CODEX.md`](CODEX.md). It uses Braintrust MCP's `sql_query` and `edit_dataset_rows` tools. The MCP `sql_query` tool accepts SQL clauses as separate arguments; the agent should use the `SELECT` and `WHERE` clauses from the example query.

For a webhook implementation, trigger the same query and insert logic only after the score and human action are both available. Keep a daily sweep to catch late events.
