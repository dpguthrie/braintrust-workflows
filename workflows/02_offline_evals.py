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
"""

from __future__ import annotations

import argparse
import json
import time

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


def build_scorers(use_llm: bool):
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="golden-support-qa")
    ap.add_argument("--dataset-version", default=None, help="pin to an _xact_id / snapshot")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--system-prompt", default="You are a concise product-support agent. Answer in one sentence.")
    ap.add_argument("--use-llm", action="store_true", help="invoke the saved prompt instead of a stub task")
    ap.add_argument("--environment", default=None, help="assign the prompt version to this environment slug")
    ap.add_argument("--baseline", default=None, help="baseline experiment name")
    ap.add_argument("--max-concurrency", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0, help="cap dataset rows (0 = all)")
    ap.add_argument("--min-score", type=float, default=0.6, help="ship gate: mean keyword_overlap")
    ap.add_argument("--max-regression", type=float, default=0.02, help="ship gate: allowed drop vs baseline")
    args = ap.parse_args()

    c.banner("Offline evals")
    pid = c.project_id()

    # ---------------------------------------------------------- 1. dataset
    c.step("Select the dataset")
    ds = braintrust.init_dataset(project=c.PROJECT_NAME, name=args.dataset, version=args.dataset_version)
    n_rows = c.btql_scalar(f"SELECT count(1) AS n FROM dataset('{ds.id}')") or 0
    if not n_rows:
        c.die(f"dataset '{args.dataset}' is empty -- run 01_dataset_lifecycle.py first")
    c.info(f"dataset_id={ds.id}  rows={n_rows:,}  pinned_version={args.dataset_version or 'latest (unpinned)'}")
    if not args.dataset_version:
        c.warn("Unpinned dataset. For a defensible baseline comparison, pin a snapshot xact_id.")

    data = list(ds)
    if args.limit:
        data = data[: args.limit]
        c.info(f"capped to {len(data):,} rows")

    # ------------------------------------------------ 2. prompt/model version
    c.step("Choose the model / prompt version")
    envs = [args.environment] if args.environment else None
    try:
        prompt = upsert_prompt(pid, args.model, args.system_prompt, envs)
    except c.BTError:
        if not envs:
            raise
        c.warn(f"environment '{args.environment}' not found; saving prompt without an environment")
        prompt = upsert_prompt(pid, args.model, args.system_prompt, None)
    prompt_version = str(prompt["_xact_id"])
    c.info(f"prompt id={prompt['id']} slug={PROMPT_SLUG} version={prompt_version} model={args.model}")

    if args.use_llm:
        def task(input_):
            out = braintrust.invoke(
                project_id=pid,
                slug=PROMPT_SLUG,
                version=prompt_version,  # pin: never let the eval drift onto a newer prompt
                input={"input": input_},
            )
            return {"answer": out if isinstance(out, str) else json.dumps(out)}
    else:
        def task(input_):
            # Stand-in for the system under test: recovers the fixture's answer
            # and drops one token, so the run produces a realistic score just
            # under 1.0 rather than a degenerate 0. Deterministic and free, so
            # it can be driven at any size.
            question = (input_ or {}).get("question", "")
            idx = int(question.split("case ")[-1].rstrip(")")) if "case " in question else 0
            words = c.synthetic_case(idx)["expected"]["answer"].split()
            return {"answer": " ".join(words[:-1]) if len(words) > 2 else " ".join(words)}
        c.info("using the stub task (no LLM spend). Add --use-llm to invoke the saved prompt.")

    # ----------------------------------------------------- 3. run evaluators
    c.step("Run the evaluators")
    exp_name = f"w02-{args.model}-{c.RUN_ID}"
    baseline = args.baseline or latest_baseline(pid, exclude=exp_name)
    c.info(f"experiment={exp_name}  baseline={baseline or '(none -- first run)'}")

    t0 = time.time()
    result = braintrust.Eval(
        c.PROJECT_NAME,
        data=data,
        task=task,
        scores=build_scorers(args.use_llm),
        experiment_name=exp_name,
        base_experiment_name=baseline,
        max_concurrency=args.max_concurrency,
        metadata={
            "model": args.model,
            "prompt_slug": PROMPT_SLUG,
            "prompt_version": prompt_version,
            "dataset": args.dataset,
            "dataset_version": args.dataset_version or "latest",
        },
        tags=["workflow-02", "offline"],
    )
    dur = time.time() - t0
    summary = result.summary
    c.info(f"{len(data):,} cases in {dur:.1f}s ({len(data) / max(dur, 1e-6):.1f} cases/s)")
    c.info(summary.experiment_url or "")

    c.step("Wait until eval results are queryable")
    c.wait_until_queryable(
        f"SELECT count(1) AS n FROM experiment('{summary.experiment_id}')",
        expect_at_least=len(data),
        label="eval_result_to_queryable",
    )

    # -------------------------------------------------- 4. inspect failures
    c.step("Inspect failures")
    errored = [r for r in result.results if r.error is not None]
    low = sorted(
        (r for r in result.results if r.error is None and (r.scores.get("keyword_overlap") or 0) < args.min_score),
        key=lambda r: r.scores.get("keyword_overlap") or 0,
    )
    c.info(f"{len(errored)} errored, {len(low)} below the {args.min_score} keyword_overlap bar")
    for r in low[:5]:
        q = (r.input or {}).get("question", "")
        c.info(f"  score={r.scores.get('keyword_overlap'):.2f}  q={q[:60]!r}")
    for r in errored[:3]:
        c.info(f"  ERROR {type(r.error).__name__}: {str(r.error)[:120]}")

    # The same triage from the server side, which is what the UI and any
    # downstream automation actually run:
    worst = c.btql(
        f"""
        SELECT id, input, output, expected, scores
        FROM experiment('{summary.experiment_id}')
        WHERE scores.keyword_overlap < {args.min_score}
        ORDER BY scores.keyword_overlap ASC
        LIMIT 10
        """
    )
    c.info(f"BTQL returned {len(worst)} failing rows for triage")
    for row in worst[:3]:
        c.info(f"  {c.log_permalink(summary.experiment_id, row['id'], object_type='experiment')}")

    # ------------------------------------------------ 5. compare to baseline
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

    # ------------------------------------------------------ 6. ship decision
    c.step("Ship decision")
    gate = scores.get("keyword_overlap")
    reasons: list[str] = []
    if errored:
        reasons.append(f"{len(errored)} cases errored")
    if gate is None:
        reasons.append("gate score keyword_overlap is missing")
    else:
        if gate.score < args.min_score:
            reasons.append(f"keyword_overlap {gate.score:.4f} < floor {args.min_score}")
        if gate.diff is not None and gate.diff < -args.max_regression:
            reasons.append(f"regressed {gate.diff:+.4f} vs baseline (max {-args.max_regression:+.4f})")

    if reasons:
        print("\n\033[31mBLOCK\033[0m " + "; ".join(reasons))
        print(f"      {summary.experiment_url}")
        raise SystemExit(1)
    print(f"\n\033[32mSHIP\033[0m  prompt {PROMPT_SLUG}@{prompt_version} on {args.model}")
    print(f"      {summary.experiment_url}")


if __name__ == "__main__":
    main()
