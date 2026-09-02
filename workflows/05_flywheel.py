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


def serve(logger, i: int, release: str, broken: bool) -> None:
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


def wave(logger, n: int, concurrency: int, release: str, broken: bool, offset: int) -> None:
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(lambda i: serve(logger, offset + i, release, broken), range(n)))
    logger.flush()
    dur = time.time() - t0
    c.info(f"{release}: {n:,} requests in {dur:.1f}s")


def window_stats(pid: str, release: str, minutes: int) -> dict:
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

    # ------------------------------------------- 0. the deployed prompt (v1)
    c.step("Deploy the current prompt (v1) and serve healthy traffic")
    v1 = c.api_put(
        "/v1/prompt",
        {
            "project_id": pid,
            "name": "Flywheel answerer",
            "slug": PROMPT_SLUG,
            "prompt_data": {
                "prompt": {"type": "chat", "messages": [
                    {"role": "system", "content": GOOD_PROMPT},
                    {"role": "user", "content": "{{input.question}}"},
                ]},
                "options": {"model": args.model, "params": {"temperature": 0}},
            },
        },
    )
    c.info(f"prompt {PROMPT_SLUG} v1 _xact_id={v1['_xact_id']}")
    wave(logger, args.requests, args.concurrency, release="v1", broken=False, offset=0)

    # ------------------------------------------------------ 1. the incident
    c.step("A change ships. Serve the degraded release")
    v2_bad = c.api_put(
        "/v1/prompt",
        {
            "project_id": pid,
            "name": "Flywheel answerer",
            "slug": PROMPT_SLUG,
            "prompt_data": {
                "prompt": {"type": "chat", "messages": [
                    {"role": "system", "content": BAD_PROMPT},
                    {"role": "user", "content": "{{input.question}}"},
                ]},
                "options": {"model": args.model, "params": {"temperature": 0}},
            },
        },
    )
    c.info(f"prompt {PROMPT_SLUG} v2 (over-cautious) _xact_id={v2_bad['_xact_id']}")
    wave(logger, args.requests, args.concurrency, release="v2", broken=True, offset=args.requests)

    # ------------------------------------------------------- 2. detect it
    c.step("Detect the regression")
    c.wait_until_queryable(
        f"""SELECT count_distinct(root_span_id) AS n FROM project_logs('{pid}')
            WHERE created > now() - interval {args.window_minutes} minute
              AND is_root AND metadata.run_id = '{c.RUN_ID}' AND metadata.release = 'v2'""",
        expect_at_least=args.requests,
        label="incident_detection_lag",
        timeout_s=300,
    )
    before = window_stats(pid, "v1", args.window_minutes)
    after = window_stats(pid, "v2", args.window_minutes)
    b, a = before.get("avg_score") or 0.0, after.get("avg_score") or 0.0
    drop = b - a
    c.info(f"v1: traces={before['traces']:,} avg_score={b:.3f} failures={before['failures']}")
    c.info(f"v2: traces={after['traces']:,} avg_score={a:.3f} failures={after['failures']}")
    if drop < args.regression_threshold:
        c.warn(f"drop {drop:.3f} is under the {args.regression_threshold} threshold; nothing to chase")
        return
    print(f"    \033[31mREGRESSION\033[0m avg_score {b:.3f} -> {a:.3f} ({a - b:+.3f})")

    # --------------------------------------------- 3. dataset from failures
    c.step("Build a regression dataset from the failing traces")
    predicate = (
        f"created > now() - interval {args.window_minutes} minute "
        f"AND is_root AND metadata.run_id = '{c.RUN_ID}' "
        f"AND metadata.release = 'v2' AND scores.answered < 0.5"
    )
    failures = list(
        c.btql_all(
            f"""
            SELECT id, root_span_id, input, output, metadata
            FROM project_logs('{pid}')
            WHERE {predicate}
            ORDER BY _pagination_key
            LIMIT 500
            """,
            max_rows=args.harvest_limit,
        )
    )
    c.info(f"harvested {len(failures):,} failing traces")
    if not failures:
        c.die("no failing traces found -- widen --window-minutes")

    ds = braintrust.init_dataset(
        project=c.PROJECT_NAME,
        name=args.dataset,
        description=f"Failures from release v2, run {c.RUN_ID}",
        metadata={"source": "05_flywheel", "incident_release": "v2", "run_id": c.RUN_ID},
    )
    seen: set[str] = set()
    n_written = 0
    for f in failures:
        question = (f.get("input") or {}).get("question", "")
        if question in seen:
            continue
        seen.add(question)
        # Ground truth comes from the synthetic fixture here. In practice
        # this is the annotation step of workflow 04.
        idx = int(question.split("case ")[-1].rstrip(")")) if "case " in question else 0
        ds.insert(
            id=f["id"],
            input=f.get("input"),
            expected=c.synthetic_case(idx)["expected"],
            tags=["from-production", "incident-v2"],
            metadata={
                "source_root_span_id": f.get("root_span_id"),
                "source_permalink": c.log_permalink(pid, f["id"]),
                "observed_output": f.get("output"),
            },
        )
        n_written += 1
    ds.flush()
    c.info(f"wrote {n_written:,} unique cases to dataset {ds.id}")

    c.wait_until_queryable(
        f"SELECT count(1) AS n FROM dataset('{ds.id}')",
        expect_at_least=n_written,
        label="regression_dataset_queryable",
    )
    xact_id = c.dataset_version(ds)
    snap = c.snapshot_dataset(ds.id, f"incident-{c.RUN_ID}", xact_id, description=f"{n_written} cases from v2")
    c.info(f"pinned as snapshot {snap['name']} @ {xact_id}")

    # ----------------------------------------------------------- 4. the fix
    c.step("Fix the prompt")
    v3 = c.api_put(
        "/v1/prompt",
        {
            "project_id": pid,
            "name": "Flywheel answerer",
            "slug": PROMPT_SLUG,
            "prompt_data": {
                "prompt": {"type": "chat", "messages": [
                    {"role": "system", "content": GOOD_PROMPT + " Never reply 'I don't know'; give your best answer."},
                    {"role": "user", "content": "{{input.question}}"},
                ]},
                "options": {"model": args.model, "params": {"temperature": 0}},
            },
        },
    )
    v3_version = str(v3["_xact_id"])
    c.info(f"prompt {PROMPT_SLUG} v3 (fixed) _xact_id={v3_version}")

    # -------------------------------------------- 5. offline regression run
    c.step("Run the offline regression against the pinned dataset")
    pinned = braintrust.init_dataset(project=c.PROJECT_NAME, name=args.dataset, version=xact_id)

    def make_task(fixed: bool):
        """Stand-in for the system under test at each prompt version.

        Swap this for `braintrust.invoke(project_id=pid, slug=PROMPT_SLUG,
        version=...)` to exercise the real prompts. The stub keeps the loop
        free and deterministic so it can be run at any size.
        """

        def task(input_):
            question = (input_ or {}).get("question", "")
            idx = int(question.split("case ")[-1].rstrip(")")) if "case " in question else 0
            case = c.synthetic_case(idx)
            if not fixed and case["metadata"]["difficulty"] == "hard":
                return {"answer": "I don't know."}
            return {"answer": case["expected"]["answer"]}

        return task

    def answered(output, expected) -> float:
        text = str((output or {}).get("answer", "")).lower()
        return 0.0 if (not text or "i don't know" in text) else 1.0

    baseline_name = f"w05-broken-{c.RUN_ID}"
    base = braintrust.Eval(
        c.PROJECT_NAME,
        data=pinned,
        task=make_task(fixed=False),
        scores=[answered],
        experiment_name=baseline_name,
        metadata={"prompt_version": str(v2_bad["_xact_id"]), "variant": "broken", "dataset_version": xact_id},
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
        metadata={"prompt_version": v3_version, "variant": "fixed", "dataset_version": xact_id},
        tags=["workflow-05", "candidate"],
    )
    c.info(f"candidate: {cand.summary.experiment_url}")

    # ------------------------------------------------------- 6. compare
    c.step("Compare candidate to baseline")
    cand_scores, _ = c.summary_scores(cand.summary)
    base_scores, _ = c.summary_scores(base.summary)
    gate = cand_scores.get("answered")
    base_gate = base_scores.get("answered")
    for name, s in cand_scores.items():
        delta = f"{s.diff:+.4f}" if s.diff is not None else "n/a"
        c.info(f"  {name:<12} {s.score:.4f}  delta={delta}  +{s.improvements or 0}/-{s.regressions or 0}")

    ship = bool(
        gate
        and (gate.diff is None or gate.diff >= -args.max_regression)
        and gate.score > (base_gate.score if base_gate else 0)
    )
    if not ship:
        print("\n\033[31mBLOCK\033[0m the fix does not beat the broken baseline on the harvested cases")
        raise SystemExit(1)
    print(f"\n\033[32mSHIP\033[0m  {PROMPT_SLUG}@{v3_version}")

    # -------------------------------------------------------- 7. deploy
    c.step("Deploy the fixed prompt")
    if args.environment:
        try:
            c.api_post("/environment", {"name": args.environment, "slug": args.environment})
        except c.BTError:
            c.info(f"environment '{args.environment}' already exists")
        c.api_put(
            "/v1/prompt",
            {
                "project_id": pid,
                "name": "Flywheel answerer",
                "slug": PROMPT_SLUG,
                "environment_slugs": [args.environment],
                "prompt_data": v3["prompt_data"],
            },
        )
        c.info(f"assigned {PROMPT_SLUG} to environment '{args.environment}'")
        c.info(f"consumers load it with: braintrust.load_prompt(project_id=pid, slug='{PROMPT_SLUG}',"
               f" environment='{args.environment}')")
    else:
        c.info(f"pin consumers to the validated version: "
               f"braintrust.load_prompt(project_id=pid, slug='{PROMPT_SLUG}', version='{v3_version}')")
        c.info("pass --environment production to promote by environment instead of by version")

    # -------------------------------------------------------- 8. monitor
    c.step("Monitor the fixed release")
    wave(logger, args.requests, args.concurrency, release="v3", broken=False, offset=args.requests * 2)
    c.wait_until_queryable(
        f"""SELECT count_distinct(root_span_id) AS n FROM project_logs('{pid}')
            WHERE created > now() - interval {args.window_minutes} minute
              AND is_root AND metadata.run_id = '{c.RUN_ID}' AND metadata.release = 'v3'""",
        expect_at_least=args.requests,
        label="post_deploy_monitor_lag",
        timeout_s=300,
    )
    final = window_stats(pid, "v3", args.window_minutes)
    f = final.get("avg_score") or 0.0
    c.info(f"v1={b:.3f}  v2={a:.3f}  v3={f:.3f}")
    if f >= b - args.max_regression:
        print(f"\n\033[32mRECOVERED\033[0m avg_score back to {f:.3f} (pre-incident {b:.3f})")
    else:
        print(f"\n\033[33mSTILL DEGRADED\033[0m {f:.3f} vs pre-incident {b:.3f} -- loop again")

    print(
        "\nThe loop is closed. In production the pieces are wired as:\n"
        "  detection  -> a project_automation alert on the score filter (workflow 03)\n"
        "  harvest    -> a scheduled job running workflow 04's query + insert\n"
        "  regression -> workflow 02 in CI, exit code as the required status check\n"
        "  deploy     -> prompt environment assignment, not a code change"
    )


if __name__ == "__main__":
    main()
