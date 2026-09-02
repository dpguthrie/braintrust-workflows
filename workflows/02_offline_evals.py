#!/usr/bin/env python3
"""Workflow 02 -- Offline evals.

    select dataset
      -> choose model / prompt / agent version
      -> run evaluators
      -> inspect failures
      -> compare against baseline
      -> decide whether to ship

Run:
    python workflows/02_offline_evals.py                    # stub task, free
    python workflows/02_offline_evals.py --use-llm          # invoke the real prompt
    python workflows/02_offline_evals.py --baseline w02-v1-abc123

Exit code is the ship decision: 0 = ship, 1 = block. That is the whole
contract a CI required-status-check needs -- the PR comment is cosmetic, the
job exit code is the gate.

Each step below is a standalone function. `main()` at the bottom composes them
into the full workflow; lift any one of them on its own. `ship_decision()` is
the one to replace first -- the thresholds there are a placeholder for your
own policy.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import braintrust

import _common as c

PROMPT_SLUG = "support-answerer"


# --------------------------------------------------------------------------
# scorers
# --------------------------------------------------------------------------


def answer_present(output, expected) -> float:
    """Cheap structural check -- runs at any scale, no LLM."""
    if not isinstance(output, dict):
        return 0.0
    return 1.0 if str(output.get("answer", "")).strip() else 0.0


def keyword_overlap(output, expected) -> float | None:
    """Token-recall of the expected answer. Deterministic, no LLM."""
    if not expected:
        return None
    exp = str(expected.get("answer", "") if isinstance(expected, dict) else expected).lower()
    got = str(output.get("answer", "") if isinstance(output, dict) else output).lower()
    exp_tokens = {t for t in exp.replace(",", " ").split() if len(t) > 3}
    if not exp_tokens:
        return None
    return len(exp_tokens & set(got.split())) / len(exp_tokens)


def build_scorers(use_llm: bool) -> list[Any]:
    """The scorers this eval runs. Deterministic ones always; Factuality only
    when an LLM is in play, since it costs money per case."""
    scorers = [answer_present, keyword_overlap]
    if use_llm:
        try:
            from autoevals import Factuality

            scorers.append(Factuality())
        except ImportError:
            c.warn("autoevals not installed; skipping Factuality")
    return scorers


# --------------------------------------------------------------------------
# prompt / model version
# --------------------------------------------------------------------------


def upsert_prompt(pid: str, model: str, system_prompt: str, environment_slugs: list[str] | None) -> dict:
    """Create or replace the prompt, producing a new immutable version.

    PUT is create-or-replace by (project_id, slug). Every replace bumps
    `_xact_id`, and that value is the version you pin an experiment to.
    """
    body = {
        "project_id": pid,
        "name": "Support answerer",
        "slug": PROMPT_SLUG,
        "description": "Answers product-support questions from a knowledge base",
        "prompt_data": {
            "prompt": {
                "type": "chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "{{input.question}}"},
                ],
            },
            "options": {"model": model, "params": {"temperature": 0}},
        },
    }
    if environment_slugs:
        body["environment_slugs"] = environment_slugs
    return c.api_put("/v1/prompt", body)


def latest_baseline(pid: str, exclude: str, prefix: str = "w02-") -> str | None:
    """Most recent prior experiment to baseline against.

    Prefer one from this same workflow: comparing against an experiment that ran
    a different task over a different dataset produces a diff that means
    nothing. In CI the baseline is normally the merge-base commit's experiment,
    passed in explicitly via --baseline.
    """
    resp = c.api_get("/v1/experiment", {"project_id": pid, "limit": 100})
    objs = resp.get("objects", []) if isinstance(resp, dict) else resp
    objs = [o for o in objs if o.get("name") != exclude]
    objs.sort(key=lambda o: o.get("created") or "", reverse=True)
    same_workflow = [o for o in objs if str(o.get("name", "")).startswith(prefix)]
    if same_workflow:
        return same_workflow[0]["name"]
    return None


# --------------------------------------------------------------------------
# 1. dataset
# --------------------------------------------------------------------------


def select_dataset(name: str, version: str | None, limit: int = 0) -> tuple[braintrust.Dataset, list[Any]]:
    """Open a dataset, optionally pinned to a version, and read its rows.

    An unpinned eval is not reproducible: the dataset can change between the
    baseline run and this one, so a score delta stops meaning anything.
    """
    c.step("Select the dataset")
    dataset = braintrust.init_dataset(project=c.PROJECT_NAME, name=name, version=version)
    n_rows = c.btql_scalar(f"SELECT count(1) AS n FROM dataset('{dataset.id}')") or 0
    if not n_rows:
        c.die(f"dataset '{name}' is empty -- run 01_dataset_lifecycle.py first")
    c.info(f"dataset_id={dataset.id}  rows={n_rows:,}  pinned_version={version or 'latest (unpinned)'}")
    if not version:
        c.warn("Unpinned dataset. For a defensible baseline comparison, pin a snapshot xact_id.")

    rows = list(dataset)
    if limit:
        rows = rows[:limit]
        c.info(f"capped to {len(rows):,} rows")
    return dataset, rows


# --------------------------------------------------------------------------
# 2. the thing under test
# --------------------------------------------------------------------------


def choose_prompt_version(
    project_id: str, model: str, system_prompt: str, environment: str | None
) -> str:
    """Save the prompt and return the version to pin the eval to."""
    c.step("Choose the model / prompt version")
    envs = [environment] if environment else None
    try:
        prompt = upsert_prompt(project_id, model, system_prompt, envs)
    except c.BTError:
        if not envs:
            raise
        c.warn(f"environment '{environment}' not found; saving prompt without an environment")
        prompt = upsert_prompt(project_id, model, system_prompt, None)
    version = str(prompt["_xact_id"])
    c.info(f"prompt id={prompt['id']} slug={PROMPT_SLUG} version={version} model={model}")
    return version


def llm_task(project_id: str, prompt_version: str):
    """Invoke the saved prompt server-side, pinned to one version.

    Pinning matters: without it the eval silently drifts onto whatever version
    of the prompt happens to be current when it runs.
    """

    def task(input_: dict[str, Any]) -> dict[str, Any]:
        out = braintrust.invoke(
            project_id=project_id,
            slug=PROMPT_SLUG,
            version=prompt_version,
            input={"input": input_},
        )
        return {"answer": out if isinstance(out, str) else json.dumps(out)}

    return task


def stub_task(input_: dict[str, Any]) -> dict[str, Any]:
    """Stand-in for the system under test. Deterministic and free.

    Answers correctly except on every fourth case, where it returns something
    unhelpful. That gives the run a mean score comfortably above the ship gate
    while still leaving real failures for the triage step to find -- which is
    what an eval you are about to ship normally looks like.
    """
    question = (input_ or {}).get("question", "")
    idx = int(question.split("case ")[-1].rstrip(")")) if "case " in question else 0
    if idx % 4 == 0:
        return {"answer": "Please check the documentation for details."}
    return {"answer": c.synthetic_case(idx)["expected"]["answer"]}


# --------------------------------------------------------------------------
# 3. run
# --------------------------------------------------------------------------


def run_eval(
    rows: list[Any],
    task: Any,
    scorers: list[Any],
    experiment_name: str,
    baseline: str | None,
    metadata: dict[str, Any],
    max_concurrency: int = 10,
) -> Any:
    """Run the experiment, then wait for its rows to be queryable.

    `base_experiment_name` is what makes the summary carry a diff against the
    baseline instead of bare scores.
    """
    c.step("Run the evaluators")
    c.info(f"experiment={experiment_name}  baseline={baseline or '(none -- first run)'}")
    result = braintrust.Eval(
        c.PROJECT_NAME,
        data=rows,
        task=task,
        scores=scorers,
        experiment_name=experiment_name,
        base_experiment_name=baseline,
        max_concurrency=max_concurrency,
        metadata=metadata,
        tags=["workflow-02", "offline"],
    )
    c.info(result.summary.experiment_url or "")

    c.step("Wait until eval results are queryable")
    c.wait_until_queryable(
        f"SELECT count(1) AS n FROM experiment('{result.summary.experiment_id}')",
        expect_at_least=len(rows),
    )
    return result


# --------------------------------------------------------------------------
# 4. triage
# --------------------------------------------------------------------------


def failing_rows(experiment_id: str, score_name: str, floor: float, limit: int = 10) -> list[dict[str, Any]]:
    """Server-side triage query -- what the UI and any automation would run."""
    return c.btql(
        f"""
        SELECT id, input, output, expected, scores
        FROM experiment('{experiment_id}')
        WHERE scores.{score_name} < {floor}
        ORDER BY scores.{score_name} ASC
        LIMIT {limit}
        """
    )


def inspect_failures(result: Any, score_name: str, floor: float) -> list[Any]:
    """Report errored and low-scoring cases. Returns the errored ones.

    Errors and low scores are different failures: an error means the task never
    produced an answer, and no amount of prompt tuning fixes it.
    """
    c.step("Inspect failures")
    errored = [r for r in result.results if r.error is not None]
    low = sorted(
        (r for r in result.results if r.error is None and (r.scores.get(score_name) or 0) < floor),
        key=lambda r: r.scores.get(score_name) or 0,
    )
    c.info(f"{len(errored)} errored, {len(low)} below the {floor} {score_name} bar")
    for r in low[:5]:
        question = (r.input or {}).get("question", "")
        c.info(f"  score={r.scores.get(score_name):.2f}  q={question[:60]!r}")
    for r in errored[:3]:
        c.info(f"  ERROR {type(r.error).__name__}: {str(r.error)[:120]}")

    experiment_id = result.summary.experiment_id
    worst = failing_rows(experiment_id, score_name, floor)
    c.info(f"BTQL returned {len(worst)} failing rows for triage")
    for row in worst[:3]:
        c.info(f"  {c.log_permalink(experiment_id, row['id'], object_type='experiment')}")
    return errored


# --------------------------------------------------------------------------
# 5. compare
# --------------------------------------------------------------------------


def compare_to_baseline(summary: Any) -> dict[str, Any]:
    """Print the score/metric diff and return the score map."""
    c.step("Compare against the baseline")
    if not summary.comparison_experiment_name:
        c.warn("no baseline resolved -- this run becomes the baseline for the next one")
    else:
        c.info(f"{summary.experiment_name} vs {summary.comparison_experiment_name}")

    scores, metrics = c.summary_scores(summary)
    for name, s in scores.items():
        delta = f"{s.diff:+.4f}" if s.diff is not None else "  n/a "
        c.info(f"  {name:<20} {s.score:.4f}  delta={delta}  +{s.improvements or 0}/-{s.regressions or 0}")
    for name, m in metrics.items():
        c.info(f"  {name:<20} {m.metric}{m.unit}")
    return scores


# --------------------------------------------------------------------------
# 6. decide
# --------------------------------------------------------------------------


def ship_decision(
    scores: dict[str, Any],
    errored: list[Any],
    score_name: str,
    min_score: float,
    max_regression: float,
) -> list[str]:
    """Return the reasons not to ship. Empty list means ship.

    Kept separate from the printing on purpose: this is the part you replace
    with your own policy, and it is the only part CI actually depends on.
    """
    gate = scores.get(score_name)
    reasons: list[str] = []
    if errored:
        reasons.append(f"{len(errored)} cases errored")
    if gate is None:
        reasons.append(f"gate score {score_name} is missing")
        return reasons
    if gate.score < min_score:
        reasons.append(f"{score_name} {gate.score:.4f} < floor {min_score}")
    if gate.diff is not None and gate.diff < -max_regression:
        reasons.append(f"regressed {gate.diff:+.4f} vs baseline (max {-max_regression:+.4f})")
    return reasons


# --------------------------------------------------------------------------
# the workflow
# --------------------------------------------------------------------------

GATE_SCORE = "keyword_overlap"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="golden-support-qa")
    ap.add_argument("--dataset-version", default=None, help="pin to an _xact_id / snapshot")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--system-prompt", default="You are a concise product-support agent. Answer in one sentence.")
    ap.add_argument("--use-llm", action="store_true", help="invoke the saved prompt instead of a stub task")
    ap.add_argument("--environment", default=None, help="assign the prompt version to this environment slug")
    ap.add_argument("--baseline", default=None, help="baseline experiment name")
    ap.add_argument("--max-concurrency", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0, help="cap dataset rows (0 = all)")
    ap.add_argument("--min-score", type=float, default=0.5, help=f"ship gate: floor on mean {GATE_SCORE}")
    ap.add_argument("--max-regression", type=float, default=0.02, help="ship gate: allowed drop vs baseline")
    args = ap.parse_args()

    c.banner("Offline evals")
    pid = c.project_id()

    _, rows = select_dataset(args.dataset, args.dataset_version, args.limit)

    prompt_version = choose_prompt_version(pid, args.model, args.system_prompt, args.environment)
    if args.use_llm:
        task = llm_task(pid, prompt_version)
    else:
        task = stub_task
        c.info("using the stub task (no LLM spend). Add --use-llm to invoke the saved prompt.")

    experiment_name = f"w02-{args.model}-{c.RUN_ID}"
    result = run_eval(
        rows,
        task,
        build_scorers(args.use_llm),
        experiment_name,
        baseline=args.baseline or latest_baseline(pid, exclude=experiment_name),
        metadata={
            "model": args.model,
            "prompt_slug": PROMPT_SLUG,
            "prompt_version": prompt_version,
            "dataset": args.dataset,
            "dataset_version": args.dataset_version or "latest",
        },
        max_concurrency=args.max_concurrency,
    )

    errored = inspect_failures(result, GATE_SCORE, args.min_score)
    scores = compare_to_baseline(result.summary)

    c.step("Ship decision")
    reasons = ship_decision(scores, errored, GATE_SCORE, args.min_score, args.max_regression)
    url = result.summary.experiment_url
    if reasons:
        print("\n\033[31mBLOCK\033[0m " + "; ".join(reasons))
        print(f"      {url}")
        raise SystemExit(1)
    print(f"\n\033[32mSHIP\033[0m  prompt {PROMPT_SLUG}@{prompt_version} on {args.model}")
    print(f"      {url}")


if __name__ == "__main__":
    main()
