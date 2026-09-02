# braintrust-workflows

Runnable reference implementations of the five workflows most teams actually
run on [Braintrust](https://www.braintrust.dev), written to be read as much as
run.

Each script is one workflow, end to end, in plain Python against the SDK and
REST API. No framework, no abstraction layer — the point is that you can read
one file and see exactly which calls a workflow is made of, then lift the parts
you need.

| Script | Workflow |
|---|---|
| [`01_dataset_lifecycle.py`](workflows/01_dataset_lifecycle.py) | create/import a golden dataset → inspect → filter/search → edit → version → reuse across experiments |
| [`02_offline_evals.py`](workflows/02_offline_evals.py) | select dataset → choose model/prompt version → run evaluators → inspect failures → compare to baseline → ship decision |
| [`03_online_evals.py`](workflows/03_online_evals.py) | production request → trace captured → online scoring on sampled traffic → dashboard aggregates → investigate bad traces |
| [`04_traces_to_dataset.py`](workflows/04_traces_to_dataset.py) | find problematic traces → filter/select → convert to a dataset → annotate expected → version |
| [`05_flywheel.py`](workflows/05_flywheel.py) | detect a production issue → dataset from failures → fix the prompt → offline regression → compare → deploy → monitor again |

Two things make these more than snippets:

**They are instrumented.** Every script emits a JSON timing record per step, so
the same file doubles as a load-test probe — see
[Using these as load-test probes](#using-these-as-load-test-probes).

**They were verified against a live org**, not just written. That surfaced
several API behaviours that are easy to get wrong and are documented in
[Things worth knowing](#things-worth-knowing-before-reading-the-code).

See [`docs/agents-and-mcp.md`](docs/agents-and-mcp.md) for the same five
workflows driven by a coding agent through the Braintrust MCP server instead of
by scripts — what the MCP tools and built-in skills already cover, what still
needs the `bt` CLI or the SDK, and where the gaps are.

## Setup

```bash
pip install -r requirements.txt

export BRAINTRUST_API_KEY=...
export BT_PROJECT=braintrust-workflows          # project the scripts write into

# Self-hosted / BYOC only:
export BRAINTRUST_API_URL=https://<your-data-plane>/
export BRAINTRUST_APP_URL=https://<your-app>/   # used for permalinks
export BRAINTRUST_ORG_NAME='...'                # if your key spans several orgs
```

`autoevals` is only needed for `02_offline_evals.py --use-llm`. Everything else
runs on `braintrust` + `requests`.

### mTLS / custom CA

If your data plane sits behind client-cert auth:

```bash
export BRAINTRUST_CLIENT_CERT=/path/client.pem
export BRAINTRUST_CLIENT_KEY=/path/client.key
export BRAINTRUST_CA_BUNDLE=/path/ca.pem     # or "0" to skip verification
```

`_common.session()` picks these up for direct REST/BTQL calls, and
`_common.install_sdk_tls()` — called at the top of every script — pushes the
same config into the SDK via `braintrust.set_http_adapter()`. That adapter's
`send()` is the single choke point both paths go through; setting
`session.cert` alone is **not** enough, because the SDK opens its own sessions.

## Run them

```bash
python workflows/01_dataset_lifecycle.py --rows 1000
python workflows/02_offline_evals.py --dataset golden-support-qa
python workflows/03_online_evals.py --requests 200
python workflows/04_traces_to_dataset.py --select low-score
python workflows/05_flywheel.py
```

Scripts 02 and 04 read objects the earlier ones create, so run them in order
the first time. `05_flywheel.py` is independent — it generates its own
incident and closes the whole loop in one invocation.

Every script has `--help`. The data is a small synthetic support-QA corpus
defined in `workflows/_common.py`; swap `_TOPICS` and `synthetic_case()` for
your own domain when you adapt these.

### The LLM is optional everywhere

Every workflow defaults to a deterministic stub task, so it runs for free, at
any size, with byte-identical inputs across runs. `--use-llm` on
`02_offline_evals.py` swaps in `braintrust.invoke()` against the saved prompt,
pinned to a specific version.

## Using these as load-test probes

Every script emits one JSON record per timed step to stderr, and to
`$BT_METRICS_FILE` as JSONL when set:

```bash
export BT_METRICS_FILE=/tmp/run.jsonl
export BT_RUN_ID=run-3
python workflows/01_dataset_lifecycle.py --rows 250000 --batch 5000 --skip-experiments

jq -s 'group_by(.step) | map({step: .[0].step, p95: (map(.ms) | sort | .[(length*0.95)|floor])})' /tmp/run.jsonl
```

Every BTQL request is tagged `query_source=braintrust_workflows` (override with
`BT_QUERY_SOURCE`), so workflow traffic is separable from real traffic when
reading query-performance data during a run.

| Metric emitted | What it measures |
|---|---|
| `dataset_ingest_to_queryable`, `log_ingest_to_queryable` | ingest → queryable lag. A span is not really ingested until a query can see it, and the poller counts it that way. |
| `dataset_insert`, `dataset_write`, `trace_generation` | write throughput (rows/s, rps) |
| `online_scoring_lag` | how long after a trace lands its online score appears, and whether that lag stabilises under sustained load |
| `monitor_timeseries_query`, `regression_by_dimension_query` | the aggregate shapes a monitoring dashboard runs |
| `trace_export` | paginated export throughput out of `project_logs` |
| `eval_run`, `eval_result_to_queryable` | eval throughput and eval-result visibility lag |
| `transient_400_retry` | how often a transient API error had to be retried |
| `ship_decision`, `flywheel_ship_decision` | the gate outcome, so a run's verdict is machine-readable |

## Things worth knowing before reading the code

These are the behaviours that cost time to discover. All were verified live.

**Always range-filter `project_logs()`.** Every query here carries
`created > now() - interval N minute`. Without a range filter (or a specific
`root_span_id`/`id`), the query scans the whole project history and will be
slow or time out on a large project. Datasets and experiments are bounded
objects and do not need one.

**`ORDER BY _pagination_key` is what makes a cursor advance.** `btql_all()`
appends `OFFSET '<cursor>'`; without a cursor-compatible sort the export
silently re-reads page one.

**SQL mode vs BTQL mode.** The parser detects which you wrote. These scripts use
SQL throughout. `INCLUDES` and `CONTAINS` are BTQL-only — in SQL use
`tags IN ('triage')`. Online-scoring filters additionally reject `!=`; use
`IS NOT`.

**Reference score keys with a subscript, not a quoted identifier.** The key an
online scoring rule writes is the scorer's *display name*, which normally
contains spaces. `scores."Answer non-empty"` crashes the BTQL parser —
HTTP 400 `RuntimeError: memory access out of bounds`, reproducibly.
`scores['Answer non-empty']` parses correctly. `score_ref()` in workflows 03
and 04 handles this. Because the key is not caller-controlled, workflow 03
also discovers it from the first scored row rather than hardcoding it.

**A 400 saying `memory access out of bounds` is often transient.** It has been
observed on otherwise-valid `/btql` and `/v1/prompt?environment=` requests that
succeed on retry. `_common._request()` retries this specific 400 up to
`BT_RETRIES` (default 3) times and emits a `transient_400_retry` metric each
time, so the retry rate stays visible rather than being silently absorbed. This
is *not* the same as the reproducible quoted-key crash above, which no amount
of retrying fixes.

**`metrics.duration` only exists on the `summary` shape.** On the default
`spans` shape it is null; derive wall time from the root span's own
`metrics.end - metrics.start`. This matters because `GROUP BY` over the
`summary` shape is rejected under strict lint, so a monitor-style timeseries
has to run on `spans` and therefore cannot use `metrics.duration`.

**`MATCH` is ordered-phrase full-text, not substring.** The default tokenizer
splits on any non-alphanumeric character, underscores included, so
`function_call` indexes as `function` + `call`. It only prunes at the index
level when the field is inverted-indexed.

**Dataset versions are transaction ids.** `_xact_id` is the version. A snapshot
(`POST /v1/dataset_snapshot`) is a name bound to one `_xact_id`; reading with
`version=<xact_id>` reproduces that exact state, edits and deletes included.
Workflow 01 proves this by editing *after* the snapshot and reading both.

Getting the current version through the SDK's `dataset.version` property
fetches every row to compute a max. At scale, ask the query engine instead:
`SELECT max(_xact_id) FROM dataset('<id>')`. `_common.dataset_version()` does
that, with the SDK property as a fallback.

**Online scoring rules are project scores, not automations.** They live at
`POST/PUT /v1/project_score` with `score_type: "online"` and the config under
`config.online`. `POST` returns an existing rule *unchanged* if the name
already exists, silently ignoring your new config — use `PUT` to
create-or-replace. `/v1/project_automation` is a different thing: alerts,
exports and retention.

**Scorer functions can be pushed as inline source.** `function_data` of
`{type: "code", data: {type: "inline", runtime_context: {runtime: "python",
version: "3.12"}, code: "..."}}` with a `def handler(input, output, expected,
metadata)` entry point. No bundling step, which is what keeps these scripts
self-contained. For real scorers with dependencies, `bt functions push` with a
`--requirements` file is the maintained path.

**The eval gate is the exit code.** `02_offline_evals.py` exits 1 on a blocked
ship. The PR comment a CI eval action posts is cosmetic; the job's exit code is
what a required status check reads.

## Managed alternatives these scripts deliberately avoid

Each of these exists and is worth knowing about. The explicit path is what
these scripts show, because it is stable and it is what a load test can drive:

- **`braintrust.DatasetPipeline`** declares workflow 04's
  source/transform/target and runs under `bt datasets pipeline run`.
  Experimental — the API may change across minor versions.
- **Prompt environments** (`environment_slugs` on `POST/PUT /v1/prompt`, then
  `load_prompt(environment=...)`) replace version pinning as the deploy
  mechanism. Workflow 05 uses it when `--environment` is passed and falls back
  to version pinning otherwise.
- **BTQL export automations** (`event_type: "btql_export"`) push results to S3
  on an interval, which beats polling BTQL for any pipeline that runs
  continuously.

## Caveats

Verified against Braintrust SaaS. Behaviour on a self-hosted data plane depends
on its version — in particular the set of MCP tools available varies sharply
(see [`docs/agents-and-mcp.md`](docs/agents-and-mcp.md)). Anything called out
as a bug above was accurate when tested and may since be fixed.

Not an official Braintrust project.

## License

MIT
