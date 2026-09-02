# The same five workflows, driven by a coding agent

Companion to the scripts in [`../workflows`](../workflows). Those answer "what
API calls is this workflow made of". This one answers "how much of it can a
coding agent just do for me" — through the Braintrust MCP server, the `bt`
CLI, and skills.

Everything below was verified by enumerating `https://api.braintrust.dev/mcp`
live, not read from documentation. Dates matter here; see
[the version caveat](#the-version-caveat).

---

## What exists today

### The MCP server ships 38 tools and 4 skills

As of 2026-09-01, against Braintrust SaaS:

**Read / discovery**
`search_docs` · `resolve_object` · `list_recent_objects` · `infer_schema` ·
`sql_query` · `summarize_experiment` · `generate_permalink` ·
`get_project_settings`

**Datasets & evals**
`edit_dataset_rows` · `run_eval` · `create_evaluator` · `test_evaluator` ·
`create_prompt`

**Online scoring & automations**
`update_online_scoring_rule` · `list_automations` · `set_automation_status` ·
`create_log_alert` · `create_threshold_alert` ·
`create_environment_update_alert` · `create_scheduled_loop_job`

**Monitoring**
`generate_monitor_chart` · `list_monitoring_views` · `get_monitoring_view` ·
`create_monitoring_view` · `update_monitoring_view`

**Topics / facets**
`enable_topics_automation` · `set_topics_automation` ·
`rewind_topics_automation` · `create_facet` · `create_preprocessor` ·
`set_project_default_preprocessor` · `test_preprocessor_on_trace` ·
`test_facet_on_trace`

**Investigation**
`get_trace_work_items` · `update_trace_work_report` · `update_debug_report`

**Skills**, loaded via `load_braintrust_skill`:
`braintrust/evaluator-workflow` · `braintrust/automations-workflow` ·
`braintrust/topics-workflow` · `braintrust/pattern-analysis`

`braintrust/evaluator-workflow` is the standout and it directly covers
workflows 02, 03 and 05. It runs a staged loop — scope the evaluator, ground it
in real traces via `sql_query`, hit a "problem clarity" milestone, build
code-or-LLM, test, deploy to an online rule, rewind to backfill. It explicitly
forbids inventing a taxonomy before looking at data, and it gates spend behind
confirmation. **Do not hand-roll a skill for online scoring; this one is
better than what you would write.**

### The `bt` CLI is the write surface MCP lacks

Verified on `bt` 0.15.1:

`bt datasets` (create / update / snapshots / pipeline) · `bt eval` ·
`bt experiments` · `bt functions push|pull` · `bt scorers` · `bt prompts` ·
`bt sql` · `bt sync` · `bt topics` · `bt view`

Setup is two commands:

```bash
bt setup skills --agent claude   # also: codex copilot cursor gemini opencode qwen
bt setup mcp     --agent claude
bt setup doctor                  # diagnose
```

`bt setup skills` generates agent skills locally and prefetches workflow docs
into `.bt/skills/docs/`, so the agent has the SQL reference on disk instead of
guessing at syntax.

---

## Workflow by workflow

| Workflow | MCP out of the box | Needs code | Worth a new skill |
|---|---|---|---|
| 01 Dataset lifecycle | partial — `edit_dataset_rows` caps at 100 rows/call | bulk import, snapshot naming | no |
| 02 Offline evals | **yes** — `run_eval` + `summarize_experiment` | only the task, if it is your app | thin: the ship gate |
| 03 Online evals | **yes** — the `evaluator-workflow` skill, end to end | tracing (always app code) | no |
| 04 Traces → dataset | partial — `sql_query` + `edit_dataset_rows` | >100 rows, snapshot | **yes** |
| 05 Flywheel | partial — every piece exists, nothing sequences them | — | **yes** |

### 01. Dataset lifecycle — mostly still code

`edit_dataset_rows` is capped at **100 rows per call**. That is a curation
tool, not an import tool, and it is the right design — but it means
"create/import a golden dataset" at any real size stays with
`bt datasets create --file` or the SDK. There is also **no dataset-snapshot
tool** on MCP, so "version it" needs `bt datasets snapshots create` or the
REST endpoint.

The agent is still the better tool for *inspect* and *filter/search*.
`sql_query` plus `infer_schema` beats writing BTQL by hand, because
`infer_schema` tells the agent which fields actually exist before it guesses.

### 02. Offline evals — MCP covers this today

`run_eval` takes `dataset_id`/`dataset_name` **plus `dataset_version` or
`dataset_environment`**, saved or inline tasks, saved or inline scorers, and
`base_experiment_name`/`base_experiment_id`. It runs server-side and needs no
sandbox credentials. `summarize_experiment` takes a
`comparison_experiment_id`. So select → version-pin → run → compare is a
single agent turn.

It also accepts **a prior experiment as its data source**, where that
experiment's outputs become the expected values — a neat way to build a
regression set out of a known-good run.

What is missing is the *decision*. Nothing encodes "this is the threshold at
which we do not ship". That is a thin skill, not a tool.

### 03. Online evals — the strongest out-of-the-box story

Load `braintrust/evaluator-workflow` and the agent does the whole thing: finds
representative traces with `sql_query`, proposes a rubric grounded in them,
builds the scorer with `create_evaluator`, checks it with `test_evaluator`,
deploys it with `update_online_scoring_rule` (sampling rate, scope,
`btql_filter`, `apply_to_root_span` — the same fields workflow 03 sets by
hand), and can rewind to backfill.

For dashboards, `generate_monitor_chart` + `create_monitoring_view` builds the
saved view. For surfacing regressions, `create_threshold_alert` fires when a
score crosses a bound.

The only part that cannot be agent-driven is trace capture itself. Emitting
spans is application code, and always will be.

### 04. Traces → dataset — the clearest gap

Every primitive exists: `sql_query` to find failures, `edit_dataset_rows` to
write them, `generate_permalink` for provenance. Nothing sequences them, and
the sequencing is where the judgment lives:

- deduplicate on input, because production repeats itself and a dataset with
  400 copies of one question measures nothing 400 times
- leave `expected` empty — what production produced is the observed output,
  not ground truth
- reuse the source span id as the dataset row id, so re-harvesting is
  idempotent and every row keeps a back-pointer to its trace
- check whether failures actually concentrate on a dimension before building
  the set at all; a failure mode spread evenly across every dimension is
  usually an eval problem, not a model problem

`bt datasets pipeline` (`run` / `pull` / `transform` / `push`) is the supported
managed version of exactly this, and it is the better target: a skill that
authors a pipeline file and runs it beats a skill that loops
`edit_dataset_rows` 100 rows at a time.

### 05. Flywheel — a sequencing skill over existing tools

Nothing new is needed tool-wise. `create_scheduled_loop_job` is the interesting
one: a Loop job on a cron or interval over a recent data window, with delivery
actions exposed to Loop as tools. That is the unattended version of the whole
loop — detect, harvest, report — with nobody in the seat.

---

## What to build

**Two skills, in this order.**

1. **`trace-to-dataset`** — workflow 04's sequencing, targeting
   `bt datasets pipeline` rather than raw row edits. Highest value: it is the
   step teams get wrong most often and the one with no coverage today.
2. **`eval-ship-gate`** — workflow 02's decision half. Pin the dataset version,
   run against a named baseline, apply explicit thresholds, exit non-zero.
   Small, but it is the difference between an eval and a gate.

**Skip:** anything for workflow 03. `braintrust/evaluator-workflow` already
does it well.

**Put in prompts/docs, not a skill:** the BTQL gotchas from the
[main README](../README.md#gotchas).
They bite the agent path exactly as hard, because `sql_query` runs the same
engine — verified live that `scores['Answer non-empty']` works through MCP and
the quoted form does not.

---

## The version caveat

**The MCP tool set is sharply version-dependent. Enumerate it against your own
endpoint before relying on any tool named here.**

The evidence is direct. A `braintrust` checkout at **v2.7.0 (2026-07-16)**
exposes **7 MCP tools** — `search_docs`, `resolve_object`,
`list_recent_objects`, `infer_schema`, `sql_query`, `summarize_experiment`,
`generate_permalink`. All read-only. The live SaaS server on 2026-09-01 exposed
**38**, with the entire write surface and all four skills. That is roughly six
weeks of divergence.

If you are on a self-hosted or BYOC data plane — especially one pinned to a
version — check what you actually have:

```bash
curl -s -X POST "$BRAINTRUST_API_URL/mcp" \
  -H "Authorization: Bearer $BRAINTRUST_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | sed 's/^data: //' \
  | python3 -c 'import sys,json
for line in sys.stdin.read().splitlines():
    line = line.strip()
    if not line or line.startswith("event:"): continue
    try: d = json.loads(line)
    except ValueError: continue
    for t in d.get("result", {}).get("tools", []): print(t["name"])'
```

If your version predates the write tools, the agent story on your cluster is
read-only discovery, and writes go through the `bt` CLI and SDK. Still useful,
just a different shape.

---

## Verified interop

The two paths are not parallel universes — they operate on the same objects.
After running `03_online_evals.py`, MCP `list_automations` returned the online
scoring rule the script had created, with its scorer function id intact, and
MCP `sql_query` returned the same failing traces `04_traces_to_dataset.py`
harvested. You can start a workflow in a script and finish it in an agent, or
the reverse.
