#!/usr/bin/env python3
"""Workflow 05 -- The flywheel.

    detect production issue
      -> create dataset from failures
      -> fix prompt/model/agent
      -> run offline regression
      -> compare
      -> deploy
      -> monitor again

One run closes the whole loop. To make the loop observable in a single
invocation the script generates its own production traffic: a healthy wave,
then a degraded wave (the "incident"), then a healthy wave after the fix.

Run:
    python workflows/05_flywheel.py
    python workflows/05_flywheel.py --environment production
"""

from __future__ import annotations

import argparse
import time
from typing import Any
from concurrent.futures import ThreadPoolExecutor

import braintrust

import _common as c

PROMPT_SLUG = "flywheel-answerer"
WORKFLOW_TAG = "flywheel"

GOOD_PROMPT = "You are a product-support agent. Answer the question in one complete sentence."
BAD_PROMPT = "Answer only if you are absolutely certain. Otherwise reply exactly: I don't know."


# --------------------------------------------------------------------------
# traffic
# --------------------------------------------------------------------------


def serve(logger: Any, i: int, release: str, broken: bool) -> None:
    """One traced request. The broken release refuses on the hard cases.

    Scores are attached inline so the loop does not depend on the online
    scoring worker; in production that score comes from an online rule
    (workflow 03) and everything downstream reads it identically.
    """
    case = c.synthetic_case(i)
    question = case["input"]["question"]
    with logger.start_span(name="handle_request", type="function") as root:
        root.log(
            input={"question": question},
            metadata={
                "workflow": WORKFLOW_TAG,
                "release": release,
                "surface": case["metadata"]["surface"],
                "run_id": c.RUN_ID,
            },
        )
        with root.start_span(name="generate", type="llm") as llm:
            # The broken release refuses on the hard cases.
            refuse = broken and case["metadata"]["difficulty"] == "hard"
            answer = "I don't know." if refuse else case["expected"]["answer"]
            llm.log(
                input=[{"role": "user", "content": question}],
                output={"answer": answer},
                metadata={"model": "gpt-4o-mini"},
                metrics={"prompt_tokens": 180, "completion_tokens": 40, "tokens": 220},
            )
            # Score inline so the loop does not depend on the online-scoring
            # worker. In production this score comes from an online rule
            # (workflow 03) -- everything downstream reads it the same way.
            root.log(output={"answer": answer}, scores={"answered": 0.0 if refuse else 1.0})


def wave(logger: Any, n: int, concurrency: int, release: str, broken: bool, offset: int) -> None:
    """Serve `n` requests concurrently under one release label."""
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(lambda i: serve(logger, offset + i, release, broken), range(n)))
    logger.flush()
    dur = time.time() - t0
    c.info(f"{release}: {n:,} requests in {dur:.1f}s")


def window_stats(pid: str, release: str, minutes: int) -> dict[str, Any]:
    """Trace count, mean score and failure count for one release in the window."""
    rows = c.btql(
        f"""
        SELECT count_distinct(root_span_id) AS traces,
               avg(scores.answered)         AS avg_score,
               count_if(scores.answered < 0.5) AS failures
        FROM project_logs('{pid}')
        WHERE created > now() - interval {minutes} minute
          AND is_root
          AND metadata.workflow = '{WORKFLOW_TAG}'
          AND metadata.run_id = '{c.RUN_ID}'
          AND metadata.release = '{release}'
        """
    )
    return rows[0] if rows else {"traces": 0, "avg_score": None, "failures": 0}


# --------------------------------------------------------------------------


def save_prompt(project_id: str, model: str, system_prompt: str, environment: str | None = None) -> dict[str, Any]:
    """Create-or-replace the prompt. Every replace produces a new version.

    PUT is create-or-replace by (project_id, slug); the returned `_xact_id` is
    the version you pin an eval or a consumer to.
    """
    body: dict[str, Any] = {
        "project_id": project_id,
        "name": "Flywheel answerer",
        "slug": PROMPT_SLUG,
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
    if environment:
        body["environment_slugs"] = [environment]
    return c.api_put("/v1/prompt", body)


def detect_regression(
    project_id: str, requests: int, window_minutes: int, threshold: float
) -> tuple[float, float] | None:
    """Compare the two releases in the monitoring window.

    Returns (before, after) if the drop exceeds `threshold`, else None. This is
    the query an alert automation runs on a schedule; here it runs inline so
    the loop is visible in one process.
    """
    c.step("Detect the regression")
    c.wait_until_queryable(
        f"""SELECT count_distinct(root_span_id) AS n FROM project_logs('{project_id}')
            WHERE created > now() - interval {window_minutes} minute
              AND is_root AND metadata.run_id = '{c.RUN_ID}' AND metadata.release = 'v2'""",
        expect_at_least=requests,
        timeout_s=300,
    )
    before = window_stats(project_id, "v1", window_minutes)
    after = window_stats(project_id, "v2", window_minutes)
    b = before.get("avg_score") or 0.0
    a = after.get("avg_score") or 0.0
    c.info(f"v1: traces={before['traces']:,} avg_score={b:.3f} failures={before['failures']}")
    c.info(f"v2: traces={after['traces']:,} avg_score={a:.3f} failures={after['failures']}")
    if b - a < threshold:
        c.warn(f"drop {b - a:.3f} is under the {threshold} threshold; nothing to chase")
        return None
    print(f"    \033[31mREGRESSION\033[0m avg_score {b:.3f} -> {a:.3f} ({a - b:+.3f})")
    return b, a


def harvest_failures(project_id: str, window_minutes: int, limit: int) -> list[dict[str, Any]]:
    """Pull the failing traces from the degraded release."""
    predicate = (
        f"created > now() - interval {window_minutes} minute "
        f"AND is_root AND metadata.run_id = '{c.RUN_ID}' "
        f"AND metadata.release = 'v2' AND scores.answered < 0.5"
    )
    return list(
        c.btql_all(
            f"""
            SELECT id, root_span_id, input, output, metadata
            FROM project_logs('{project_id}')
            WHERE {predicate}
            ORDER BY _pagination_key
            LIMIT 500
            """,
            max_rows=limit,
        )
    )


def build_regression_dataset(
    project_id: str, name: str, failures: list[dict[str, Any]]
) -> tuple[braintrust.Dataset, str]:
    """Turn the failures into a pinned dataset. Returns (dataset, xact_id).

    Ground truth comes from the synthetic fixture here. In a real loop this is
    workflow 04's annotate step, where a human supplies it.
    """
    c.step("Build a regression dataset from the failing traces")
    c.info(f"harvested {len(failures):,} failing traces")
    if not failures:
        c.die("no failing traces found -- widen --window-minutes")

    dataset = braintrust.init_dataset(
        project=c.PROJECT_NAME,
        name=name,
        description=f"Failures from release v2, run {c.RUN_ID}",
        metadata={"source": "05_flywheel", "incident_release": "v2", "run_id": c.RUN_ID},
    )
    seen: set[str] = set()
    written = 0
    for failure in failures:
        question = (failure.get("input") or {}).get("question", "")
        if question in seen:
            continue
        seen.add(question)
        idx = int(question.split("case ")[-1].rstrip(")")) if "case " in question else 0
        dataset.insert(
            id=failure["id"],
            input=failure.get("input"),
            expected=c.synthetic_case(idx)["expected"],
            tags=["from-production", "incident-v2"],
            metadata={
                "source_root_span_id": failure.get("root_span_id"),
                "source_permalink": c.log_permalink(project_id, failure["id"]),
                "observed_output": failure.get("output"),
            },
        )
        written += 1
    dataset.flush()
    c.info(f"wrote {written:,} unique cases to dataset {dataset.id}")

    c.wait_until_queryable(
        f"SELECT count(1) AS n FROM dataset('{dataset.id}')", expect_at_least=written
    )
    xact_id = c.dataset_version(dataset)
    snapshot = c.snapshot_dataset(
        dataset.id, f"incident-{c.RUN_ID}", xact_id, description=f"{written} cases from v2"
    )
    c.info(f"pinned as snapshot {snapshot['name']} @ {xact_id}")
    return dataset, xact_id


def make_task(fixed: bool):
    """Stand-in for the system under test at each prompt version.

    Swap for `braintrust.invoke(project_id=..., slug=PROMPT_SLUG, version=...)`
    to exercise the real prompts. The stub keeps the loop free and deterministic.
    """

    def task(input_: dict[str, Any]) -> dict[str, Any]:
        question = (input_ or {}).get("question", "")
        idx = int(question.split("case ")[-1].rstrip(")")) if "case " in question else 0
        case = c.synthetic_case(idx)
        if not fixed and case["metadata"]["difficulty"] == "hard":
            return {"answer": "I don't know."}
        return {"answer": case["expected"]["answer"]}

    return task


def answered(output: dict[str, Any], expected: dict[str, Any] | None) -> float:
    """Did the model actually answer, or refuse."""
    text = str((output or {}).get("answer", "")).lower()
    return 0.0 if (not text or "i don't know" in text) else 1.0


def run_regression(dataset_name: str, xact_id: str, broken_version: str, fixed_version: str):
    """Run broken and fixed against the same pinned dataset. Returns (base, cand).

    Both read the same xact_id, so the delta between them is the fix and
    nothing else.
    """
    c.step("Run the offline regression against the pinned dataset")
    pinned = braintrust.init_dataset(project=c.PROJECT_NAME, name=dataset_name, version=xact_id)

    baseline_name = f"w05-broken-{c.RUN_ID}"
    base = braintrust.Eval(
        c.PROJECT_NAME,
        data=pinned,
        task=make_task(fixed=False),
        scores=[answered],
        experiment_name=baseline_name,
        metadata={"prompt_version": broken_version, "variant": "broken", "dataset_version": xact_id},
        tags=["workflow-05", "baseline"],
    )
    c.info(f"baseline: {base.summary.experiment_url}")

    cand = braintrust.Eval(
        c.PROJECT_NAME,
        data=pinned,
        task=make_task(fixed=True),
        scores=[answered],
        experiment_name=f"w05-fixed-{c.RUN_ID}",
        base_experiment_name=baseline_name,
        metadata={"prompt_version": fixed_version, "variant": "fixed", "dataset_version": xact_id},
        tags=["workflow-05", "candidate"],
    )
    c.info(f"candidate: {cand.summary.experiment_url}")
    return base, cand


def compare_and_decide(base: Any, cand: Any, max_regression: float) -> bool:
    """Does the candidate beat the broken baseline on the harvested cases."""
    c.step("Compare candidate to baseline")
    cand_scores, _ = c.summary_scores(cand.summary)
    base_scores, _ = c.summary_scores(base.summary)
    gate = cand_scores.get("answered")
    base_gate = base_scores.get("answered")
    for name, s in cand_scores.items():
        delta = f"{s.diff:+.4f}" if s.diff is not None else "n/a"
        c.info(f"  {name:<12} {s.score:.4f}  delta={delta}  +{s.improvements or 0}/-{s.regressions or 0}")
    return bool(
        gate
        and (gate.diff is None or gate.diff >= -max_regression)
        and gate.score > (base_gate.score if base_gate else 0)
    )


def deploy(project_id: str, model: str, fixed_prompt: str, version: str, environment: str | None) -> None:
    """Promote the validated prompt, by environment if one is given.

    Environment assignment is the deploy that is not a code change: consumers
    call `load_prompt(environment=...)` and pick up the new version without
    redeploying.
    """
    c.step("Deploy the fixed prompt")
    if not environment:
        c.info(
            f"pin consumers to the validated version: "
            f"braintrust.load_prompt(project_id=pid, slug='{PROMPT_SLUG}', version='{version}')"
        )
        c.info("pass --environment production to promote by environment instead of by version")
        return

    try:
        c.api_post("/environment", {"name": environment, "slug": environment})
    except c.BTError:
        c.info(f"environment '{environment}' already exists")
    save_prompt(project_id, model, fixed_prompt, environment=environment)
    c.info(f"assigned {PROMPT_SLUG} to environment '{environment}'")
    c.info(
        f"consumers load it with: braintrust.load_prompt(project_id=pid, "
        f"slug='{PROMPT_SLUG}', environment='{environment}')"
    )


def monitor_after_deploy(
    project_id: str, logger: Any, requests: int, concurrency: int, window_minutes: int,
    before: float, during: float, max_regression: float,
) -> None:
    """Serve the fixed release and check the window recovers."""
    c.step("Monitor the fixed release")
    wave(logger, requests, concurrency, release="v3", broken=False, offset=requests * 2)
    c.wait_until_queryable(
        f"""SELECT count_distinct(root_span_id) AS n FROM project_logs('{project_id}')
            WHERE created > now() - interval {window_minutes} minute
              AND is_root AND metadata.run_id = '{c.RUN_ID}' AND metadata.release = 'v3'""",
        expect_at_least=requests,
        timeout_s=300,
    )
    after = window_stats(project_id, "v3", window_minutes).get("avg_score") or 0.0
    c.info(f"v1={before:.3f}  v2={during:.3f}  v3={after:.3f}")
    if after >= before - max_regression:
        print(f"\n\033[32mRECOVERED\033[0m avg_score back to {after:.3f} (pre-incident {before:.3f})")
    else:
        print(f"\n\033[33mSTILL DEGRADED\033[0m {after:.3f} vs pre-incident {before:.3f} -- loop again")


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=int, default=300, help="requests per wave")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--window-minutes", type=int, default=60)
    ap.add_argument("--regression-threshold", type=float, default=0.05, help="score drop that trips detection")
    ap.add_argument("--harvest-limit", type=int, default=200)
    ap.add_argument("--dataset", default="flywheel-regressions")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--environment", default=None, help="promote the fixed prompt to this environment slug")
    ap.add_argument("--max-regression", type=float, default=0.02)
    args = ap.parse_args()

    c.banner("Online -> offline flywheel")
    pid = c.project_id()
    logger = braintrust.init_logger(project=c.PROJECT_NAME, project_id=pid)

    # 0. the deployed prompt, behaving
    c.step("Deploy the current prompt (v1) and serve healthy traffic")
    v1 = save_prompt(pid, args.model, GOOD_PROMPT)
    c.info(f"prompt {PROMPT_SLUG} v1 _xact_id={v1['_xact_id']}")
    wave(logger, args.requests, args.concurrency, release="v1", broken=False, offset=0)

    # 1. a change ships and breaks it
    c.step("A change ships. Serve the degraded release")
    v2 = save_prompt(pid, args.model, BAD_PROMPT)
    c.info(f"prompt {PROMPT_SLUG} v2 (over-cautious) _xact_id={v2['_xact_id']}")
    wave(logger, args.requests, args.concurrency, release="v2", broken=True, offset=args.requests)

    # 2. detect
    detected = detect_regression(pid, args.requests, args.window_minutes, args.regression_threshold)
    if not detected:
        return
    before, during = detected

    # 3. harvest into a pinned dataset
    failures = harvest_failures(pid, args.window_minutes, args.harvest_limit)
    _, xact_id = build_regression_dataset(pid, args.dataset, failures)

    # 4. fix
    c.step("Fix the prompt")
    fixed_prompt = GOOD_PROMPT + " Never reply 'I don't know'; give your best answer."
    v3 = save_prompt(pid, args.model, fixed_prompt)
    v3_version = str(v3["_xact_id"])
    c.info(f"prompt {PROMPT_SLUG} v3 (fixed) _xact_id={v3_version}")

    # 5 + 6. regress and compare
    base, cand = run_regression(args.dataset, xact_id, str(v2["_xact_id"]), v3_version)
    if not compare_and_decide(base, cand, args.max_regression):
        print("\n\033[31mBLOCK\033[0m the fix does not beat the broken baseline on the harvested cases")
        raise SystemExit(1)
    print(f"\n\033[32mSHIP\033[0m  {PROMPT_SLUG}@{v3_version}")

    # 7 + 8. deploy, then watch it recover
    deploy(pid, args.model, fixed_prompt, v3_version, args.environment)
    monitor_after_deploy(
        pid, logger, args.requests, args.concurrency, args.window_minutes,
        before, during, args.max_regression,
    )

    print(
        "\nThe loop is closed. In production the pieces are wired as:\n"
        "  detection  -> a project_automation alert on the score filter (workflow 03)\n"
        "  harvest    -> a scheduled job running workflow 04's query + insert\n"
        "  regression -> workflow 02 in CI, exit code as the required status check\n"
        "  deploy     -> prompt environment assignment, not a code change"
    )


if __name__ == "__main__":
    main()
