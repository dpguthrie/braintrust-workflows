# Braintrust workflows

Small, adaptable examples for work around Braintrust traces, scoring, and datasets. Each workflow lives in its own directory and explains which lines to change for your data.

| Workflow | What it does | Paths |
| --- | --- | --- |
| [Automated review](workflows/automated_review/README.md) | Select matching production rows and place new cases in a daily dataset for human review | Minimal Python script or Codex + Braintrust MCP |
| [Historical regrading](workflows/historical_regrading/README.md) | Reprocess a bounded historical window after adopting a new evaluator | Codex + Braintrust MCP online scoring rule rewind |

Start with **automated review**. It is the daily job. One SQL statement defines both the source filter and the disagreement condition. **Historical regrading** is an occasional promotion step and can feed a separate review dataset.

These are templates. Set the project ID, field paths, scorer name, and allowed source values for your own organization before running anything. Do not put API keys or private trace data in this repository.

## Requirements

- Braintrust project logs with the fields your review condition uses
- A Braintrust API key for the Python example, or a Codex connection to Braintrust MCP for the agent example
- Python 3.10+ and `pip install -r requirements.txt` for the script

For self-hosted data planes, set `BRAINTRUST_API_URL` and `BRAINTRUST_APP_URL` as required by your deployment. The MCP URL is shown in your data plane settings. [Braintrust MCP](https://www.braintrust.dev/blog/braintrust-mcp) supports SQL queries and dataset operations; the exact tool set depends on the deployed data plane version.

This is a community example repository, not an official Braintrust product.

## References

- [Braintrust SQL reference](https://www.braintrust.dev/docs/reference/sql)
- [Braintrust datasets guide](https://www.braintrust.dev/docs/guides/datasets)
- [Braintrust MCP](https://www.braintrust.dev/blog/braintrust-mcp)
