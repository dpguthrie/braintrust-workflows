#!/usr/bin/env python3
"""Workflow 04 -- Traces to dataset.

    find problematic production traces
      -> filter/select examples
      -> convert them into an eval dataset
      -> annotate expected behavior
      -> version the dataset

Run:
    python workflows/04_traces_to_dataset.py
    python workflows/04_traces_to_dataset.py --select error
    python workflows/04_traces_to_dataset.py --select low-score --score-field "Answer non-empty"

Prerequisite: 03_online_evals.py has put traces in the project.
"""

from __future__ import annotations

import argparse
import json
import time

import braintrust

import _common as c


def score_ref(field: str) -> str:
    """Reference a score key in BTQL.

    Subscript syntax, not a double-quoted identifier: a score key containing a
    space (which is normal -- the key is the scorer's display name) crashes the
    BTQL parser when written as `scores."Answer non-empty"`, returning
    HTTP 400 "RuntimeError: memory access out of bounds".
    `scores['Answer non-empty']` parses correctly.
    """
    return f"scores[{field!r}]" if not field.replace("_", "").isalnum() else f"scores.{field}"


def selection_predicate(mode: str, score_field: str, threshold: float) -> str:
    """The one clause that defines 'problematic' for this dataset.

    Keep it as a single string: it is the provenance of the dataset, and it is
    also directly reusable as an online-scoring or automation `btql_filter`.
    """
    if mode == "error":
        return "error IS NOT NULL"
    if mode == "low-score":
        return f"{score_ref(score_field)} < {threshold}"
    if mode == "slow":
        # metrics.duration is a `summary`-shape field. On the default `spans`
        # shape, derive wall time from the root span's own start/end.
        return "(metrics.end - metrics.start) > 5"
    if mode == "tagged":
        return "tags IN ('triage')"  # SQL mode. BTQL syntax would be: tags INCLUDES 'triage'
    raise ValueError(mode)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--select", default="low-score", choices=["low-score", "error", "slow", "tagged"])
    ap.add_argument("--score-field", default="Answer non-empty", help="score key written by online scoring")
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--window-minutes", type=int, default=1440)
    ap.add_argument("--limit", type=int, default=200, help="max traces to harvest")
    ap.add_argument("--page-size", type=int, default=500)
    ap.add_argument("--dataset", default="prod-regressions")
    ap.add_argument("--annotate", type=int, default=10, help="how many rows to fill in an expected value for")
    ap.add_argument("--wait-seconds", type=float, default=30.0, help="how long to wait for matches to appear")
    args = ap.parse_args()

    c.banner(f"Traces to dataset (select={args.select})")
    pid = c.project_id()

    window = f"created > now() - interval {args.window_minutes} minute"
    predicate = selection_predicate(args.select, args.score_field, args.threshold)
    c.info(f"predicate: {predicate}")

    # ------------------------------------------------------- 1. find traces
    c.step("Find problematic production traces")
    count_sql = f"""
        SELECT count_distinct(root_span_id) AS n
        FROM project_logs('{pid}')
        WHERE {window} AND is_root AND {predicate}
    """
    # Poll rather than fail on the first zero. Scores written moments ago are
    # not necessarily visible to an aggregate yet, and a reference script that
    # dies on a transient empty result is just annoying.
    deadline = time.time() + args.wait_seconds
    while True:
        total = c.btql_scalar(count_sql) or 0
        if total or time.time() > deadline:
            break
        time.sleep(2.0)
    c.info(f"{total:,} matching root spans in the last {args.window_minutes} minutes")
    if not total:
        c.die(
            f"nothing matched after {args.wait_seconds:.0f}s. Run 03_online_evals.py "
            f"first, widen --window-minutes, or check --score-field matches the key "
            f"your online scoring rule writes."
        )

    # Where the failures concentrate. This is the step that decides whether the
    # dataset is worth building: a failure mode spread evenly across every
    # dimension is usually an eval problem, not a model problem.
    breakdown = c.btql(
        f"""
        SELECT metadata.release          AS release,
               metadata.surface          AS surface,
               count_distinct(root_span_id) AS traces
        FROM project_logs('{pid}')
        WHERE {window} AND is_root AND {predicate}
        GROUP BY metadata.release, metadata.surface
        ORDER BY traces DESC
        LIMIT 10
        """
    )
    for row in breakdown:
        c.info(f"  release={str(row.get('release')):<20} surface={str(row.get('surface')):<8} {row['traces']:>6}")

    # --------------------------------------------------- 2. select examples
    c.step("Select and export the examples")
    # `is_root` keeps this to one row per trace. `ORDER BY _pagination_key`
    # is what makes the cursor advance -- without a cursor-compatible sort the
    # export silently re-reads page one.
    export_sql = f"""
        SELECT id, root_span_id, created, input, output, expected, scores, metadata, tags, error
        FROM project_logs('{pid}')
        WHERE {window} AND is_root AND {predicate}
        ORDER BY _pagination_key
        LIMIT {args.page_size}
    """
    t0 = time.time()
    rows = list(c.btql_all(export_sql, max_rows=args.limit))
    dur = time.time() - t0
    c.info(f"exported {len(rows):,} traces in {dur:.1f}s")

    # Deduplicate on the actual input. Production repeats itself; a dataset
    # with 400 copies of the same question measures nothing 400 times.
    seen: set[str] = set()
    unique = []
    for r in rows:
        key = json.dumps(r.get("input"), sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    c.info(f"{len(unique):,} unique inputs after dedupe ({len(rows) - len(unique):,} duplicates dropped)")

    # ------------------------------------------------- 3. build the dataset
    c.step("Convert them into an eval dataset")
    ds = braintrust.init_dataset(
        project=c.PROJECT_NAME,
        name=args.dataset,
        description=f"Harvested from production traces where {predicate}",
        metadata={"source": "04_traces_to_dataset", "predicate": predicate, "harvested_at": time.time()},
    )
    c.info(f"dataset_id={ds.id}")

    t0 = time.time()
    row_ids: list[str] = []
    for r in unique:
        # Reusing the source span id as the dataset row id makes the harvest
        # idempotent: re-running never duplicates a case, and it keeps a
        # back-pointer from every dataset row to the trace it came from.
        rid = r["id"]
        row_ids.append(rid)
        ds.insert(
            id=rid,
            input=r.get("input"),
            # `expected` stays empty on purpose. What production produced is
            # the observed output, not the ground truth; step 4 is where a
            # human supplies the truth.
            expected=None,
            tags=["from-production", "needs-review", args.select],
            metadata={
                "source_root_span_id": r.get("root_span_id"),
                "source_span_id": rid,
                "source_permalink": c.log_permalink(pid, rid),
                "observed_output": r.get("output"),
                "observed_scores": r.get("scores"),
                "observed_error": r.get("error"),
                "release": (r.get("metadata") or {}).get("release"),
                "surface": (r.get("metadata") or {}).get("surface"),
            },
        )
    ds.flush()
    dur = time.time() - t0
    c.info(f"wrote {len(row_ids):,} rows in {dur:.1f}s")

    c.wait_until_queryable(
        f"SELECT count(1) AS n FROM dataset('{ds.id}')",
        expect_at_least=len(row_ids),
        label="harvest_ingest_to_queryable",
    )

    # --------------------------------------------------------- 4. annotate
    c.step("Annotate the expected behavior")
    to_review = c.btql(
        f"""
        SELECT id, input, metadata
        FROM dataset('{ds.id}')
        WHERE tags IN ('needs-review')
        ORDER BY _pagination_key
        LIMIT {args.annotate}
        """
    )
    c.info(f"{len(to_review)} rows queued for review")

    for r in to_review:
        question = (r.get("input") or {}).get("question", "")
        # Stand-in for a subject-matter expert. In practice this is either
        # the Braintrust human-review UI or an SME-facing custom view; both
        # write to the same `expected` field.
        ground_truth = {"answer": f"[SME] correct answer for: {question[:60]}"}
        ds.update(
            id=r["id"],
            expected=ground_truth,
            tags=["from-production", "reviewed", args.select],
            metadata={"reviewed_by": "04_traces_to_dataset", "reviewed_at": time.time()},
        )
    ds.flush()

    # Reviewer commentary that is not ground truth goes on the audit log
    # instead of the row, so it never leaks into the eval input.
    if to_review:
        c.api_post(
            f"/v1/dataset/{ds.id}/feedback",
            {
                "feedback": [
                    {
                        "id": to_review[0]["id"],
                        "comment": "Retrieval returned the wrong KB article; expected answer added by review.",
                        "source": "api",
                    }
                ]
            },
        )
        c.info("attached a review comment to the first row")

    # ---------------------------------------------------------- 5. version
    c.step("Version the dataset")
    c.wait_until_queryable(
        f"SELECT count(1) AS n FROM dataset('{ds.id}') WHERE tags IN ('reviewed')",
        expect_at_least=len(to_review),
        label="annotation_to_queryable",
    )
    xact_id = c.dataset_version(ds)
    snap_name = f"{args.select}-{time.strftime('%Y%m%d-%H%M%S')}"
    snap = c.snapshot_dataset(
        ds.id,
        snap_name,
        xact_id,
        description=f"{len(row_ids)} harvested, {len(to_review)} reviewed. Predicate: {predicate}",
    )
    c.info(f"snapshot {snap['id']} name={snap['name']} xact_id={snap['xact_id']}")

    reviewed = c.btql_scalar(
        f"SELECT count(1) AS n FROM dataset('{ds.id}') WHERE tags IN ('reviewed')", version=xact_id
    )
    c.info(f"pinned read at {xact_id}: {reviewed} reviewed rows of {len(row_ids)} total")

    print(
        f"\nRun the regression against this exact set:\n"
        f"  python workflows/02_offline_evals.py --dataset {args.dataset} --dataset-version {xact_id}\n"
        f"\nManaged alternative: `braintrust.DatasetPipeline` declares the same\n"
        f"source/transform/target and runs under `bt datasets pipeline run`.\n"
        f"It is experimental -- the explicit BTQL + insert path above is stable."
    )


if __name__ == "__main__":
    main()
