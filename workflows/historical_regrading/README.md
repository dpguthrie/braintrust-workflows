# Historical regrading

**Goal:** after adopting a new evaluator definition, reprocess a chosen set of historical log rows so the next review dataset reflects the current grading policy.

This example uses Braintrust's **online scoring rule rewind** through MCP. It needs no polling script. The rule's filter selects the rows; `rewind` resets processing to an inclusive start time. Since rewind has **no end-time argument**, include the exclusive end time in the rule's `btql_filter` when you need a closed historical window.

## Codex + Braintrust MCP

Use [`CODEX.md`](CODEX.md) as an interactive promotion checklist. It uses `list_automations`, `update_online_scoring_rule`, `set_automation_status`, and `sql_query`.

The key configuration looks like this (illustrative values only):

```text
scope: span, apply_to_root_span: true
sampling_rate: 1
btql_filter:
  input.source_topic IN ('topic/example_a', 'topic/example_b')
  AND created >= '2026-09-01T00:00:00Z'
  AND created < '2026-09-08T00:00:00Z'
```

Match the scope and row placement to where the input and score actually live. Check a few rows with `sql_query` before applying the rule. If you need to compare old and new grades side by side, use a separately named evaluator so it writes a distinct score key.

## After the rewind

Wait for scoring to finish, then query the bounded window for the expected score and count missing scores. Pause the temporary rule when done. To build a **separately named** review dataset from that historical interval, edit the SQL and dataset name in [automated review](../automated_review/README.md). Use the newly adopted score key in that SQL.

The MCP rule accepts saved evaluator IDs, not a version pin in `function_ids`. Keep that evaluator definition stable while the rewind runs, or use an immutable copy for the promotion. Record the evaluator ID and version in your run notes. This matters if an evaluator is edited during a long backfill.

## Filter placement

Use the same source predicate in the scoring rule's `btql_filter` and the automated review SQL. The rule also includes the historical time bounds. A filter in the review query cannot undo grading work already performed on unrelated traces.
