# Historical regrading

**Goal:** after adopting a new evaluator definition, reprocess a chosen set of historical log rows so the next review dataset reflects the current grading policy.

This example uses Braintrust's **online scoring rule rewind**. You can trigger it through MCP or the data plane API that the UI calls. The rule's filter selects the rows; rewind resets processing to an inclusive start time. Since rewind has **no end-time argument**, include the exclusive end time in the rule's `btql_filter` when you need a closed historical window.

## Direct API

The public [`project_score` API](https://www.braintrust.dev/docs/api-reference/projectscores/partially-update-project_score) configures the online scoring rule. Its `config.online.scorers` entries can include `{ "type": "function", "id": "<scorer-id>", "version": "<version>" }`, so pin the adopted scorer version before a rewind. Preserve the rule's other config fields when patching: `config` is replaced as a whole. Keep the rule active during the backfill and set `sampling_rate` to `1` if every matching row must be graded.

[`rewind.py`](rewind.py) calls `POST /brainstore/automation/reset-cursors`, the data plane route used by the UI. It accepts a rule ID, `project_logs:<project-id>`, and a transaction ID derived from the UTC start time. This route is not in the public OpenAPI reference, so verify it on your deployment before relying on it as a long-lived integration. It requires a data plane version that supports online scoring rewind (v2.3.0 or later).

The conversion in the script is specific to Braintrust's transaction cursor format: a fixed prefix, Unix time in seconds, and a 16-bit sequence number. The script subtracts one from the first cursor at the requested second so that second is included. If you use MCP instead, `update_online_scoring_rule(operation="rewind")` accepts `start_time` directly and performs this conversion for you.

```bash
pip install -r requirements.txt
export BRAINTRUST_API_KEY='...'
python workflows/historical_regrading/rewind.py
```

Edit the three values marked `MODIFY` in the script first. Run it once; repeated calls reset processing again. Confirm the rule's scope, `btql_filter`, scorer ID/version, and active status before calling it. The endpoint starts asynchronous reprocessing and does not wait for all scores to finish.

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

The MCP rule tool accepts saved evaluator IDs but does not expose a version pin in `function_ids`. For an exact version, use the public project score API to set `config.online.scorers[].version` before rewinding, or use an immutable scorer copy. Record the scorer ID and version in the resulting review dataset row metadata as shown in [automated review](../automated_review/README.md). A metadata label alone does not pin the scorer; the rule configuration controls what actually runs.

## Filter placement

Use the same source predicate in the scoring rule's `btql_filter` and the automated review SQL. The rule also includes the historical time bounds. A filter in the review query cannot undo grading work already performed on unrelated traces.
