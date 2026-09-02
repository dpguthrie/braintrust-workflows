# braintrust-workflows

Runnable reference implementations of the five workflows most teams actually
run on [Braintrust](https://www.braintrust.dev), written to be read as much as
run.

Each script is one workflow, end to end, in plain Python against the SDK and
REST API. No framework, no abstraction layer — read one file and you can see
exactly which calls a workflow is made of, then lift the parts you need.

| Script | Workflow |
|---|---|
| [`01_dataset_lifecycle.py`](workflows/01_dataset_lifecycle.py) | create/import a golden dataset → inspect → filter/search → edit → version → reuse across experiments |
| [`02_offline_evals.py`](workflows/02_offline_evals.py) | select dataset → choose model/prompt version → run evaluators → inspect failures → compare to baseline → ship decision |
| [`03_online_evals.py`](workflows/03_online_evals.py) | production request → trace captured → online scoring on sampled traffic → dashboard aggregates → investigate bad traces |
| [`04_traces_to_dataset.py`](workflows/04_traces_to_dataset.py) | find problematic traces → filter/select → convert to a dataset → annotate expected → version |
| [`05_flywheel.py`](workflows/05_flywheel.py) | detect a production issue → dataset from failures → fix the prompt → offline regression → compare → deploy → monitor again |

These were verified against a live org, not just written. That surfaced
several API behaviours that are easy to get wrong — see
[Gotchas](#gotchas).

See [`docs/agents-and-mcp.md`](docs/agents-and-mcp.md) for the same five
workflows driven by a coding agent through the Braintrust MCP server instead
of by scripts.

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

Every script has `--help`. The data is a small synthetic support-QA corpus in
`workflows/_common.py`; swap `_TOPICS` and `synthetic_case()` for your own
domain when you adapt these.

## How the scripts are laid out

Each step of a workflow is a standalone function with a docstring explaining
the call it makes and why. `main()` at the bottom of each file composes them,
so the file reads as a set of parts plus one assembly:

```python
def main() -> None:
    ...
    dataset = import_dataset(args.dataset, args.rows, args.batch)
    wait_for_import(dataset.id, args.rows)

    profile(dataset)
    hits = search_demo(dataset, args.rows)

    correct_examples(dataset, targets)
    snapshot, xact_id = snapshot_version(dataset, label=..., description=...)
    compare_versions(dataset, targets[0], xact_id)
```

Lift any one function into your own code — they take plain arguments and
return plain values. The `c.step()` / `c.info()` calls are narration for the
demo; delete them when you copy a function out.

**The LLM is optional everywhere.** Every workflow defaults to a deterministic
stub task, so it runs for free and reproducibly. `--use-llm` on
`02_offline_evals.py` swaps in `braintrust.invoke()` against the saved prompt,
pinned to a specific version.

## Gotchas

The behaviours that cost time to discover. All verified live.

**Querying**

- **Always range-filter `project_logs()`.** Without `created > now() - interval N minute`
  (or a specific `root_span_id`/`id`) the query scans your whole project
  history. Datasets and experiments are bounded and don't need one.
- **`ORDER BY _pagination_key` is what makes a cursor advance.** Without a
  cursor-compatible sort, a paginated export silently re-reads page one.
- **SQL and BTQL are different dialects** and the parser detects which you
  wrote. `INCLUDES`/`CONTAINS` are BTQL-only — in SQL use `tags IN ('triage')`.
  Online-scoring filters also reject `!=`; use `IS NOT`.
- **`MATCH` is ordered-phrase full-text, not substring.** The tokenizer splits
  on any non-alphanumeric character, underscores included, so `function_call`
  indexes as `function` + `call`.
- **`metrics.duration` only exists on the `summary` shape** — it's null on
  `spans`. Derive wall time from `metrics.end - metrics.start`. This matters
  because `GROUP BY` over `summary` is rejected under strict lint, so a
  timeseries has to run on `spans`.

**Scores**

- **Reference score keys with a subscript, not a quoted identifier.**
  `scores."Answer non-empty"` reproducibly crashes the parser with HTTP 400
  `RuntimeError: memory access out of bounds`. `scores['Answer non-empty']`
  works. See `score_ref()` in workflows 03 and 04.
- **The score key is the scorer's display name, not its slug** — so it usually
  contains spaces, and it isn't something the caller picks. Workflow 03
  discovers it from the first scored row instead of hardcoding it.

**Writes**

- **Writes aren't immediately queryable.** Anything that writes rows then reads
  them back has to wait; `wait_until_queryable()` is its own step in every
  workflow rather than a hidden sleep.
- **Dataset versions are transaction ids.** A snapshot
  (`POST /v1/dataset_snapshot`) binds a name to one `_xact_id`; reading with
  `version=<xact_id>` reproduces that exact state. Don't read the current
  version via the SDK's `dataset.version` property — it fetches every row to
  compute a max. Use `SELECT max(_xact_id) FROM dataset('<id>')`.
- **Online scoring rules are project scores, not automations.**
  `POST/PUT /v1/project_score` with `score_type: "online"` and config under
  `config.online`. Use `PUT` — `POST` returns an existing same-named rule
  *unchanged*, silently ignoring your config. `/v1/project_automation` is a
  different thing (alerts, exports, retention).
- **Scorer functions can be pushed as inline source** — `function_data` of
  `{type: "code", data: {type: "inline", runtime_context: {runtime: "python",
  version: "3.12"}, code: "..."}}` with a `def handler(input, output, expected,
  metadata)` entry point. No bundling step. For scorers with dependencies,
  `bt functions push --requirements` is the maintained path.

**Other**

- **A 400 saying `memory access out of bounds` is often transient.** Seen on
  valid `/btql` and `/v1/prompt?environment=` requests that succeed on retry;
  `_common._request()` retries it up to `BT_RETRIES` (default 3) times. Not the
  same as the reproducible quoted-key crash above, which retrying won't fix.
- **The eval gate is the exit code.** `02_offline_evals.py` exits 1 on a
  blocked ship. A CI eval action's PR comment is cosmetic; the job's exit code
  is what a required status check reads.

## Managed alternatives these scripts deliberately avoid

Each exists and is worth knowing about. The explicit path is what's shown here
because it's stable and it makes the underlying calls visible:

- **`braintrust.DatasetPipeline`** declares workflow 04's
  source/transform/target and runs under `bt datasets pipeline run`.
  Experimental — the API may change across minor versions.
- **Prompt environments** (`environment_slugs` on `POST/PUT /v1/prompt`, then
  `load_prompt(environment=...)`) replace version pinning as the deploy
  mechanism. Workflow 05 uses it when `--environment` is passed.
- **BTQL export automations** (`event_type: "btql_export"`) push results to S3
  on an interval, which beats polling for any continuously-running pipeline.

## Caveats

Verified against Braintrust SaaS. Behaviour on a self-hosted data plane depends
on its version — the set of MCP tools in particular varies sharply (see
[`docs/agents-and-mcp.md`](docs/agents-and-mcp.md)). Anything called out as a
bug above was accurate when tested and may since be fixed.

A personal project, not an official Braintrust one.

## License

MIT
