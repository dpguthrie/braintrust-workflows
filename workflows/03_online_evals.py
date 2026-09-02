#!/usr/bin/env python3
"""Workflow 03 -- Online evals.

    production request arrives
      -> trace captured
      -> evaluators score sampled/live traffic
      -> dashboards surface regressions
      -> engineer investigates bad traces

Run:
    python 03_online_evals.py --requests 200 --sampling-rate 0.25
    python 03_online_evals.py --requests 50000 --concurrency 32 --skip-setup

What to measure when running this at scale:
  * ingest ack latency (the `log_batch` metric) vs. ingest -> queryable lag
  * online-scoring lag: how long after a trace lands does `scores.*` appear,
    and does that lag stabilise or grow under sustained load
  * monitor/dashboard query latency at 100M and 1B rows -- the aggregate in
    step 4 is the same shape the Monitor page runs
"""

from __future__ import annotations

import argparse
import random
import time
from concurrent.futures import ThreadPoolExecutor

import braintrust

import _common as c

SCORER_SLUG = "answer-nonempty"
SCORER_NAME = "Answer non-empty"
RULE_NAME = "workflow-03 online scoring"
ALERT_NAME = "workflow-03 low-score alert"

# The key an online score lands under in `scores.*` is derived from the scorer,
# not from anything this script controls. Rather than hardcode it, the script
# discovers it from the first scored row (see `poll_for_scores`) and uses that
# everywhere downstream. This one is the expected default.
DEFAULT_SCORE_FIELD = "Answer non-empty"

# An inline code function: source text stored server-side and executed in the
# function sandbox. `def handler(input, output, expected, metadata)` is the
# contract; return a float in [0,1], or None to skip the row.
SCORER_CODE = '''
from typing import Any

def handler(input: Any, output: Any, expected: Any, metadata: dict[str, Any]) -> float | None:
    if output is None:
        return 0.0
    answer = output.get("answer") if isinstance(output, dict) else str(output)
    if not answer:
        return 0.0
    # Penalise the two failure modes this service actually has.
    text = str(answer).lower()
    if "i don't know" in text or "unable to" in text:
        return 0.0
    if len(text) < 20:
        return 0.5
    return 1.0
'''


# --------------------------------------------------------------------------
# 1+2. setup: scorer function and online scoring rule
# --------------------------------------------------------------------------


def upsert_scorer(pid: str) -> str:
    fn = c.api_put(
        "/v1/function",
        {
            "project_id": pid,
            "name": SCORER_NAME,
            "slug": SCORER_SLUG,
            "description": "Structural quality check on production answers",
            "function_type": "scorer",
            "function_data": {
                "type": "code",
                "data": {
                    "type": "inline",
                    "runtime_context": {"runtime": "python", "version": "3.12"},
                    "code": SCORER_CODE,
                },
            },
        },
    )
    return fn["id"]


def upsert_online_rule(pid: str, scorer_id: str, sampling_rate: float, btql_filter: str | None) -> dict:
    """Create-or-replace the online scoring rule.

    Rules live on the project as a `project_score` of type "online".
    PUT is create-or-replace by name; POST returns the existing rule unchanged
    if the name already exists, which silently ignores your new config.
    """
    online = {
        "sampling_rate": sampling_rate,
        "scorers": [
            {"type": "function", "id": scorer_id},
            # A built-in autoevals scorer needs no push step:
            # {"type": "global", "name": "Factuality", "function_type": "scorer"},
        ],
        "apply_to_root_span": True,
        "scope": {"type": "span"},
    }
    if btql_filter:
        online["btql_filter"] = btql_filter
    return c.api_put(
        "/v1/project_score",
        {
            "project_id": pid,
            "name": RULE_NAME,
            "description": "Scores sampled production traffic from workflow 03",
            "score_type": "online",
            "config": {"online": online},
        },
    )


def upsert_alert(pid: str, score_field: str, webhook_url: str, threshold: float, interval_s: int) -> dict:
    """A logs automation: evaluate a filter on new rows, fire at most once per interval."""
    return c.api_put(
        "/v1/project_automation",
        {
            "project_id": pid,
            "name": ALERT_NAME,
            "description": "Notify when a production trace scores below threshold",
            "config": {
                "event_type": "logs",
                "btql_filter": f"{score_ref(score_field)} < {threshold}",
                "interval_seconds": interval_s,
                "action": {"type": "webhook", "url": webhook_url},
            },
        },
    )


def score_ref(field: str) -> str:
    """Reference a score key in BTQL.

    Subscript syntax, not a double-quoted identifier: a score key containing a
    space (which is normal -- the key is the scorer's display name) crashes the
    BTQL parser when written as `scores."Answer non-empty"`, returning
    HTTP 400 "RuntimeError: memory access out of bounds".
    `scores['Answer non-empty']` parses correctly.
    """
    return f"scores[{field!r}]" if not field.replace("_", "").isalnum() else f"scores.{field}"


def poll_for_scores(pid: str, window: str, want: int, timeout_s: float, interval_s: float = 3.0):
    """Poll until online scores appear, and report which key they landed under.

    Returns (score_field, n_scored, elapsed_seconds).
    """
    sql = f"""
        SELECT id, scores
        FROM project_logs('{pid}')
        WHERE {window} AND is_root AND metadata.workflow = 'online-evals'
        ORDER BY created DESC
        LIMIT 200
    """
    t0 = time.time()
    field = DEFAULT_SCORE_FIELD
    while True:
        rows = c.btql(sql)
        scored = [r for r in rows if isinstance(r.get("scores"), dict) and r["scores"]]
        if scored:
            keys = sorted(scored[0]["scores"].keys())
            field = keys[0]
            if len(keys) > 1:
                c.info(f"score keys present: {keys}")
        if len(scored) >= want or time.time() - t0 > timeout_s:
            elapsed = time.time() - t0
            c.metric(
                "online_scoring_lag",
                elapsed * 1000,
                scored=len(scored),
                sampled=len(rows),
                expected=want,
                score_field=field,
                timed_out=len(scored) < want,
            )
            return field, len(scored), elapsed
        time.sleep(interval_s)


# --------------------------------------------------------------------------
# 1. production traffic
# --------------------------------------------------------------------------

FAILURE_ANSWERS = ["", "I don't know.", "Unable to help."]


def serve_one(logger, i: int, failure_rate: float) -> None:
    """One 'production request', traced the way the real service would be."""
    case = c.synthetic_case(i)
    question = case["input"]["question"]
    degraded = random.random() < failure_rate

    with logger.start_span(name="handle_request", type="function") as root:
        root.log(
            input={"question": question},
            metadata={
                "workflow": "online-evals",
                "surface": case["metadata"]["surface"],
                "release": "v2.3.1-degraded" if degraded else "v2.3.1",
                "request_id": f"{c._RUN_ID}-{i}",
            },
        )

        with root.start_span(name="retrieve", type="tool") as retrieval:
            retrieval.log(
                input={"query": question},
                output={"docs": [f"kb-{i % 40}", f"kb-{(i + 7) % 40}"]},
                metrics={"docs_returned": 2},
            )

        with root.start_span(name="generate", type="llm") as llm:
            answer = random.choice(FAILURE_ANSWERS) if degraded else case["expected"]["answer"]
            llm.log(
                input=[{"role": "user", "content": question}],
                output={"answer": answer},
                metadata={"model": "gpt-4o-mini"},
                # Normalised metric names -- these are what the cost rollup
                # and the Monitor page read.
                metrics={
                    "prompt_tokens": 180 + (i % 40),
                    "completion_tokens": 45 + (i % 20),
                    "tokens": 225 + (i % 60),
                },
            )

        root.log(output={"answer": answer})


# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--failure-rate", type=float, default=0.15)
    ap.add_argument("--sampling-rate", type=float, default=1.0, help="fraction of traffic to score online")
    ap.add_argument("--score-threshold", type=float, default=0.5)
    ap.add_argument("--window-minutes", type=int, default=60)
    ap.add_argument("--skip-setup", action="store_true", help="reuse the existing scorer + rule")
    ap.add_argument("--alert-webhook", default=None, help="create a regression alert pointing at this URL")
    ap.add_argument("--score-wait", type=float, default=180.0, help="seconds to wait for online scores")
    args = ap.parse_args()

    c.banner(f"Online evals ({args.requests:,} requests)")
    c.install_sdk_tls()
    pid = c.project_id()

    # -------------------------------------------------- scorer + online rule
    if args.skip_setup:
        c.step("Reusing the existing scorer and online scoring rule")
    else:
        c.step("Push the scorer and configure the online scoring rule")
        with c.timed("push_scorer"):
            scorer_id = upsert_scorer(pid)
        c.info(f"scorer function id={scorer_id} slug={SCORER_SLUG}")

        with c.timed("upsert_online_rule"):
            rule = upsert_online_rule(
                pid,
                scorer_id,
                args.sampling_rate,
                # `!=` is not supported in online-scoring filters -- use IS NOT.
                btql_filter="metadata.workflow = 'online-evals'",
            )
        c.info(f"rule id={rule['id']} sampling_rate={args.sampling_rate} scope=span apply_to_root_span=true")

    # ------------------------------------------------- 1. traffic and traces
    c.step("Serve production traffic and capture traces")
    logger = braintrust.init_logger(project=c.PROJECT_NAME, project_id=pid)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(lambda i: serve_one(logger, i, args.failure_rate), range(args.requests)))
    ack = time.time() - t0
    c.metric("trace_generation", ack * 1000, requests=args.requests, rps=round(args.requests / max(ack, 1e-6), 1))

    with c.timed("logger_flush"):
        logger.flush()
    c.info(f"{args.requests:,} requests, 3 spans each = {args.requests * 3:,} spans in {ack:.1f}s")

    window = f"created > now() - interval {args.window_minutes} minute"
    trace_count_sql = f"""
        SELECT count_distinct(root_span_id) AS n
        FROM project_logs('{pid}')
        WHERE {window} AND metadata.request_id LIKE '{c._RUN_ID}-%'
    """
    c.wait_until_queryable(
        trace_count_sql, expect_at_least=args.requests, label="log_ingest_to_queryable", timeout_s=300
    )

    # --------------------------------------------- 2. wait for online scores
    c.step("Wait for online scoring to catch up")
    # Cap the expectation at the 200-row sample the poller reads back.
    want = max(1, int(min(args.requests, 200) * args.sampling_rate * 0.8))
    score_field, got, scored_lag = poll_for_scores(pid, window, want, timeout_s=args.score_wait)
    c.info(
        f"{got} of the last 200 root spans scored after {scored_lag:.0f}s "
        f"(sampling_rate={args.sampling_rate}, score key = {score_field!r})"
    )
    if got == 0:
        c.warn(
            "No online scores yet. Check that the rule is enabled, that the "
            "service token backing it can read this project's logs, and that "
            "the btql_filter matches (metadata.workflow = 'online-evals')."
        )

    sf = score_ref(score_field)
    if args.alert_webhook and not args.skip_setup:
        alert = upsert_alert(pid, score_field, args.alert_webhook, args.score_threshold, interval_s=300)
        c.info(f"alert automation id={alert['id']} -> {args.alert_webhook}")

    # ---------------------------------------- 3. dashboards / regression view
    c.step("Dashboard view: score and latency over time")
    with c.timed("monitor_timeseries_query"):
        series = c.btql(
            f"""
            SELECT hour(created)                    AS bucket,
                   metadata.release                 AS release,
                   count_distinct(root_span_id)     AS traces,
                   avg({sf})        AS avg_score,
                   -- metrics.duration only exists on the `summary` shape.
                   -- On spans, derive it from the root span's own start/end.
                   percentile(metrics.end - metrics.start, 0.95) AS p95_duration,
                   sum(metrics.tokens)              AS tokens
            FROM project_logs('{pid}')
            WHERE {window} AND is_root AND metadata.workflow = 'online-evals'
            GROUP BY hour(created), metadata.release
            ORDER BY bucket DESC, release
            LIMIT 20
            """
        )
    for row in series:
        avg = row.get("avg_score")
        c.info(
            f"  {str(row.get('bucket'))[:16]}  release={str(row.get('release')):<18} "
            f"traces={row.get('traces'):>6}  avg_score={avg if avg is None else round(avg, 3)}  "
            f"p95={row.get('p95_duration')}"
        )

    with c.timed("regression_by_dimension_query"):
        by_surface = c.btql(
            f"""
            SELECT metadata.surface            AS surface,
                   count_distinct(root_span_id) AS traces,
                   avg({sf})    AS avg_score,
                   count_if({sf} < {args.score_threshold}) AS bad
            FROM project_logs('{pid}')
            WHERE {window} AND is_root AND metadata.workflow = 'online-evals'
            GROUP BY metadata.surface
            HAVING count(1) > 0
            ORDER BY avg_score ASC
            """
        )
    for row in by_surface:
        avg = row.get("avg_score")
        c.info(f"  surface={str(row.get('surface')):<8} traces={row.get('traces'):>6} "
               f"avg_score={avg if avg is None else round(avg, 3)} below_threshold={row.get('bad')}")

    # ------------------------------------------- 4. investigate bad traces
    c.step("Investigate the worst traces")
    with c.timed("bad_trace_query"):
        bad = c.btql(
            f"""
            SELECT id, root_span_id, input, output, scores, metadata
            FROM project_logs('{pid}')
            WHERE {window}
              AND is_root
              AND metadata.workflow = 'online-evals'
              AND {sf} < {args.score_threshold}
            ORDER BY created DESC
            LIMIT 10
            """
        )
    c.info(f"{len(bad)} traces below {args.score_threshold}")
    for row in bad[:5]:
        question = (row.get("input") or {}).get("question", "")
        c.info(f"  score={(row.get('scores') or {}).get(score_field)}  {question[:50]!r}")
        c.info(f"    {c.log_permalink(pid, row['id'])}")

    if bad:
        # Pull the full trace for one of them -- the retrieve + generate spans
        # are where the actual cause lives.
        rsid = bad[0]["root_span_id"]
        with c.timed("full_trace_fetch"):
            spans = c.btql(
                f"""
                SELECT span_id, span_attributes, input, output, metrics, error
                FROM project_logs('{pid}', shape => 'traces')
                WHERE root_span_id = '{rsid}'
                """
            )
        c.info(f"trace {rsid} has {len(spans)} spans:")
        for s in spans:
            name = (s.get("span_attributes") or {}).get("name")
            stype = (s.get("span_attributes") or {}).get("type")
            c.info(f"    {str(name):<16} type={stype} error={s.get('error')}")

    print(
        "\nNext: 04_traces_to_dataset.py turns these bad traces into a "
        "regression dataset."
    )


if __name__ == "__main__":
    main()
