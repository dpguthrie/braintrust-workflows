# Codex task: create a daily review dataset

Schedule a Codex task with Braintrust MCP connected. Give it the edited SQL from [`run.py`](run.py) and this instruction:

```text
Run the review selection expressed by the SQL in workflows/automated_review/run.py for the previous 24 hours. Use Braintrust MCP's sql_query tool: put the query's SELECT fields in select, project_logs and its project ID in object_type/object_ids, and the query's WHERE condition in where. The tool does not accept a whole SQL string.

Treat the SQL as the complete definition of which rows to review. Do not invent additional filters or decision rules. If the result reaches the query limit, report that it is incomplete and stop before writing.

Create or resolve today's review dataset, then use edit_dataset_rows to insert the matching source input and metadata containing the source row ID, root span ID, observed output, and grader score. Leave expected unset for human labeling. Before inserting, query the destination dataset for existing source row IDs and skip those rows so a rerun does not duplicate or overwrite labels. MCP inserts generate new row IDs; do not assume the source row ID becomes the dataset row ID.

Report the dataset link and the counts queried, inserted, and already present. Do not modify a golden dataset.
```

Adapt the SQL and dataset name for your project before scheduling. `edit_dataset_rows` accepts at most 100 changes per call; batch smaller result sets if needed.
