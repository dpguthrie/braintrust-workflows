# Codex task template: scoped regrade

Use this when an evaluator version is adopted. Fill the bracketed fields in a private task or interactive Codex message.

```text
Use the connected Braintrust MCP to regrade a bounded historical interval.

Project ID: <PROJECT_ID>
Saved evaluator ID: <EVALUATOR_ID>
Evaluator version or immutable copy to use: <VERSION_OR_COPY>
UTC start (inclusive): <START>
UTC end (exclusive): <END>
BTQL filter: <BOOLEAN EXPRESSION, e.g. input.source_topic IN ('topic/example_a', 'topic/example_b')>
Scored-row placement: <SPAN_OR_TRACE_AND_ROOT_SETTING>
Temporary rule name: <RULE_NAME>

1. Use sql_query to inspect a small set of rows in that interval and verify the input path, source values, and scored-row placement. Count eligible rows. Use list_automations with kind online_scoring to inspect existing rules and avoid changing an unrelated rule.
2. Save a dedicated online scoring rule with update_online_scoring_rule: operation=save, project_id, name, function_ids=[the saved evaluator ID], sampling_rate=1, scope/placement matching the inspected rows, and a btql_filter equal to (BTQL filter above) AND created >= START AND created < END. Use the filter expression as written; do not reconstruct it from separate fields. Use a distinct rule name. Start paused and inspect the saved rule. If an exact version is required, patch the rule through the project_score API so config.online.scorers contains the saved function ID and its version; preserve the full config including paused status. Then activate it with set_automation_status.
3. Call update_online_scoring_rule with operation=rewind, project_id, automation_id, and start_time=START exactly once. Rewind has no end-time parameter; the rule filter supplies the end bound. Do not resubmit rewind to poll.
4. Use sql_query on the same bounded interval to verify scores appear and count eligible rows still missing the new score. If incomplete, report the count and wait for processing rather than claiming completion. Pause the dedicated rule with set_automation_status after completion.
5. Report the rule ID, evaluator ID/version, exact filter, eligible count, scored count, and remaining count. Then build a separately named review dataset for that historical interval using the automated-review condition. Put the scorer ID/version in each dataset row's metadata and preserve its native origin. Do not put observed output in expected or change the golden dataset.

The MCP rule tool takes saved evaluator IDs and does not pin a function version in function_ids. The project_score API supports a version on each scorer entry. If the filter or scorer scope does not validate, stop and report the error; do not broaden the backfill.
```
