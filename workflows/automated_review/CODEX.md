# Codex task: create a daily review dataset

Schedule a Codex task with Braintrust MCP connected. Give it the edited SQL from [`run.py`](run.py) and this instruction:

```text
Run the review selection expressed by the SQL in workflows/automated_review/run.py for the previous 24 hours. Use Braintrust MCP's sql_query tool: put the query's SELECT fields in select, project_logs and its project ID in object_type/object_ids, and the query's WHERE condition in where. The tool does not accept a whole SQL string.

Treat the SQL as the complete definition of which rows to review. Do not invent additional filters or decision rules. The MCP sql_query tool does not accept a cursor for paging. If the result reaches the query limit, run workflows/automated_review/run.py instead; it follows the SQL cursor until all pages are read.

Create or resolve today's review dataset. Before inserting, query the destination dataset for existing row IDs and skip those rows so a rerun does not overwrite labels. For each new row, use POST /v1/dataset/{dataset_id}/insert with an events array. Set the dataset row id to the source row id; copy input; leave expected unset. Set top-level origin to {object_type: "project_logs", object_id: source project_id, id: source row id, _xact_id: source _xact_id}. Set metadata.observed_output and metadata.grader_score from the query, and metadata.scorer_function_id and metadata.scorer_version to the exact saved scorer/version that produced the selected scores. Use batches of at most 100 events. The MCP edit_dataset_rows tool does not expose top-level origin, so use the dataset API for this insert.

Report the dataset link and the counts queried, inserted, and already present. Do not modify a golden dataset.
```

Adapt the SQL, dataset name, and scorer ID/version for your project before scheduling. Include `project_id` and `_xact_id` in the MCP query results. Run this task after any historical regrade has finished.
