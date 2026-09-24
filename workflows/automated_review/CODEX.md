# Codex task template: daily review dataset

Connect Codex to the Braintrust MCP server and schedule the text below once a day, after online scoring has settled. Replace the bracketed values in the **private scheduled task**.

```text
Build the review dataset for the previous complete UTC day in Braintrust.

Project ID: <PROJECT_ID>
BTQL filter: <BOOLEAN EXPRESSION, e.g. is_root = true AND input.source_topic IN ('topic/example_a', 'topic/example_b')>
Score key on that row: <SCORE_NAME>
Human action path: <PATH, e.g. output.human_action>
Human positive values: <VALUES>
Human negative values: <VALUES>
Score threshold for positive: <NUMBER, e.g. 0.5>
Dataset name: review-YYYY-MM-DD, using the UTC day selected above

Use Braintrust MCP tools for all Braintrust reads and writes.

1. Use infer_schema or a small sql_query to verify the score and human action paths on actual rows. Query project_logs with a bounded created range AND the BTQL filter above in the where clause. Use the filter expression as written; do not rebuild it from separate topic or row settings. Select id, root_span_id, created, input, output, and scores. Keep one source row per result.
2. Read every matching row with sql_query limit=100. Full input/output fields can have tighter limits than narrow projections. Split the day into smaller nonoverlapping time windows whenever a window returns 100 rows; continue until every window is below 100. Deduplicate source row IDs across windows. Do not treat a capped result as complete.
3. For each row, classify the numeric score at the configured threshold and the human action using only the listed values. Skip and count rows with a missing score, missing human action, or unrecognized action. Keep rows where the two decisions differ.
4. Resolve the day's dataset if it exists. For each batch of at most 100 candidate source IDs, query that dataset for matching metadata.source_row_id values and exclude IDs already present. If the dataset does not exist, all candidate IDs are new. edit_dataset_rows inserts generate new row IDs, so this source-ID check is required for safe reruns. Do not run two copies of this task concurrently.
5. Call edit_dataset_rows with dataset_name, project_id, create_if_missing=true, and at most 100 insert changes per call. Each inserted row has input = the source input; metadata = source_row_id, source_root_span_id, source_created, source_topic, observed_output, grader_score, human_action, and score_name. Omit expected; an observed output is not a correct label.
6. Report the dataset link, scanned rows, disagreements, inserted rows, already present rows, skipped rows, and any incomplete window. Never modify the golden dataset or invent an SME label.

If a required MCP tool fails, stop and report the failed step and the precise error. Do not silently substitute a sample or an unbounded query.
```

The script in this directory is a deterministic alternative. Its `is_review_candidate()` function is the place to express a different condition.
