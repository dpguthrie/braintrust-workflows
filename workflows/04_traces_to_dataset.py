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

Each step below is a standalone function. `main()` at the bottom composes them
into the full workflow; lift any one of them on its own.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

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


# --------------------------------------------------------------------------
# 1. find
# --------------------------------------------------------------------------


def count_matching(project_id: str, window: str, predicate: str, wait_s: float = 30.0) -> int:
    """Count root spans matching the predicate, polling briefly before giving up.

    Polls rather than failing on the first zero: scores written moments ago are
    not necessarily visible to an aggregate yet.
    """
    sql = f"""
        SELECT count_distinct(root_span_id) AS n
        FROM project_logs('{project_id}')
        WHERE {window} AND is_root AND {predicate}
    """
    deadline = time.time() + wait_s
    while True:
        total = c.btql_scalar(sql) or 0
        if total or time.time() > deadline:
            return total
        time.sleep(2.0)


def failure_breakdown(project_id: str, window: str, predicate: str) -> list[dict[str, Any]]:
    """Where the failures concentrate, by release and surface.

    This is the step that decides whether the dataset is worth building at all:
    a failure mode spread evenly across every dimension is usually an eval
    problem, not a model problem.
    """
    return c.btql(
        f"""
        SELECT metadata.release             AS release,
               metadata.surface             AS surface,
               count_distinct(root_span_id) AS traces
        FROM project_logs('{project_id}')
        WHERE {window} AND is_root AND {predicate}
        GROUP BY metadata.release, metadata.surface
        ORDER BY traces DESC
        LIMIT 10
        """
    )


def find_problem_traces(project_id: str, window: str, predicate: str, wait_s: float, window_minutes: int) -> int:
    """Count matches and print where they concentrate. Returns the count."""
    c.step("Find problematic production traces")
    total = count_matching(project_id, window, predicate, wait_s)
    c.info(f"{total:,} matching root spans in the last {window_minutes} minutes")
    if not total:
        c.die(
            f"nothing matched after {wait_s:.0f}s. Run 03_online_evals.py first, "
            f"widen --window-minutes, or check --score-field matches the key your "
            f"online scoring rule writes."
        )
    for row in failure_breakdown(project_id, window, predicate):
        c.info(f"  release={str(row.get('release')):<20} surface={str(row.get('surface')):<8} {row['traces']:>6}")
    return total


# --------------------------------------------------------------------------
# 2. select
# --------------------------------------------------------------------------


def export_traces(
    project_id: str, window: str, predicate: str, limit: int, page_size: int = 500
) -> list[dict[str, Any]]:
    """Page out the matching root spans.

    `is_root` keeps this to one row per trace. `ORDER BY _pagination_key` is
    what makes the cursor advance -- without a cursor-compatible sort the
    export silently re-reads page one.
    """
    return list(
        c.btql_all(
            f"""
            SELECT id, root_span_id, created, input, output, expected, scores, metadata, tags, error
            FROM project_logs('{project_id}')
            WHERE {window} AND is_root AND {predicate}
            ORDER BY _pagination_key
            LIMIT {page_size}
            """,
            max_rows=limit,
        )
    )


def dedupe_by_input(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one row per distinct input.

    Production repeats itself. A dataset with 400 copies of the same question
    measures nothing 400 times, and skews every aggregate you compute over it.
    """
    seen: set[str] = set()
    unique = []
    for row in rows:
        key = json.dumps(row.get("input"), sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def select_examples(project_id: str, window: str, predicate: str, limit: int, page_size: int):
    """Export and dedupe. Returns the rows worth turning into cases."""
    c.step("Select and export the examples")
    t0 = time.time()
    rows = export_traces(project_id, window, predicate, limit, page_size)
    c.info(f"exported {len(rows):,} traces in {time.time() - t0:.1f}s")
    unique = dedupe_by_input(rows)
    c.info(f"{len(unique):,} unique inputs after dedupe ({len(rows) - len(unique):,} duplicates dropped)")
    return unique


# --------------------------------------------------------------------------
# 3. build
# --------------------------------------------------------------------------


def build_dataset(
    project_id: str, name: str, predicate: str, rows: list[dict[str, Any]], select_mode: str
) -> tuple[braintrust.Dataset, list[str]]:
    """Write the harvested traces into a dataset. Returns (dataset, row_ids).

    Two choices worth copying:

    - the source span id becomes the dataset row id, so re-harvesting is
      idempotent and every row keeps a back-pointer to the trace it came from
    - `expected` stays empty. What production produced is the observed output,
      not ground truth -- that is what the annotate step is for. Putting the
      observed output in `expected` bakes the bug into the dataset.
    """
    c.step("Convert them into an eval dataset")
    dataset = braintrust.init_dataset(
        project=c.PROJECT_NAME,
        name=name,
        description=f"Harvested from production traces where {predicate}",
        metadata={"source": "04_traces_to_dataset", "predicate": predicate, "harvested_at": time.time()},
    )
    c.info(f"dataset_id={dataset.id}")

    t0 = time.time()
    row_ids = []
    for row in rows:
        row_id = row["id"]
        row_ids.append(row_id)
        dataset.insert(
            id=row_id,
            input=row.get("input"),
            expected=None,
            tags=["from-production", "needs-review", select_mode],
            metadata={
                "source_root_span_id": row.get("root_span_id"),
                "source_span_id": row_id,
                "source_permalink": c.log_permalink(project_id, row_id),
                "observed_output": row.get("output"),
                "observed_scores": row.get("scores"),
                "observed_error": row.get("error"),
                "release": (row.get("metadata") or {}).get("release"),
                "surface": (row.get("metadata") or {}).get("surface"),
            },
        )
    dataset.flush()
    c.info(f"wrote {len(row_ids):,} rows in {time.time() - t0:.1f}s")

    c.wait_until_queryable(
        f"SELECT count(1) AS n FROM dataset('{dataset.id}')", expect_at_least=len(row_ids)
    )
    return dataset, row_ids


# --------------------------------------------------------------------------
# 4. annotate
# --------------------------------------------------------------------------


def rows_needing_review(dataset_id: str, limit: int) -> list[dict[str, Any]]:
    """Rows still tagged `needs-review`. `INCLUDES` is BTQL-only; SQL uses IN."""
    return c.btql(
        f"""
        SELECT id, input, metadata
        FROM dataset('{dataset_id}')
        WHERE tags IN ('needs-review')
        ORDER BY _pagination_key
        LIMIT {limit}
        """
    )


def add_review_comment(dataset_id: str, row_id: str, comment: str) -> None:
    """Attach reviewer commentary to a row's audit log, not to the row.

    Notes that are not ground truth belong here, where they never leak into
    the eval input.
    """
    c.api_post(
        f"/v1/dataset/{dataset_id}/feedback",
        {"feedback": [{"id": row_id, "comment": comment, "source": "api"}]},
    )


def annotate(dataset: braintrust.Dataset, limit: int, select_mode: str) -> list[dict[str, Any]]:
    """Fill in `expected` for rows awaiting review.

    The ground truth here is a stand-in for a subject-matter expert. In practice
    that is the Braintrust human-review UI or an SME-facing custom view; both
    write to the same `expected` field.
    """
    c.step("Annotate the expected behavior")
    to_review = rows_needing_review(dataset.id, limit)
    c.info(f"{len(to_review)} rows queued for review")

    for row in to_review:
        question = (row.get("input") or {}).get("question", "")
        dataset.update(
            id=row["id"],
            expected={"answer": f"[SME] correct answer for: {question[:60]}"},
            tags=["from-production", "reviewed", select_mode],
            metadata={"reviewed_by": "04_traces_to_dataset", "reviewed_at": time.time()},
        )
    dataset.flush()

    if to_review:
        add_review_comment(
            dataset.id,
            to_review[0]["id"],
            "Retrieval returned the wrong KB article; expected answer added by review.",
        )
        c.info("attached a review comment to the first row")
    return to_review


# --------------------------------------------------------------------------
# 5. version
# --------------------------------------------------------------------------


def version_dataset(
    dataset: braintrust.Dataset, label: str, reviewed: int, total: int, predicate: str
) -> str:
    """Snapshot the annotated dataset. Returns the pinned xact_id."""
    c.step("Version the dataset")
    c.wait_until_queryable(
        f"SELECT count(1) AS n FROM dataset('{dataset.id}') WHERE tags IN ('reviewed')",
        expect_at_least=reviewed,
    )
    xact_id = c.dataset_version(dataset)
    snapshot = c.snapshot_dataset(
        dataset.id,
        label,
        xact_id,
        description=f"{total} harvested, {reviewed} reviewed. Predicate: {predicate}",
    )
    c.info(f"snapshot {snapshot['id']} name={snapshot['name']} xact_id={snapshot['xact_id']}")

    pinned = c.btql_scalar(
        f"SELECT count(1) AS n FROM dataset('{dataset.id}') WHERE tags IN ('reviewed')", version=xact_id
    )
    c.info(f"pinned read at {xact_id}: {pinned} reviewed rows of {total} total")
    return xact_id


# --------------------------------------------------------------------------
# the workflow
# --------------------------------------------------------------------------


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

    find_problem_traces(pid, window, predicate, args.wait_seconds, args.window_minutes)
    rows = select_examples(pid, window, predicate, args.limit, args.page_size)

    dataset, row_ids = build_dataset(pid, args.dataset, predicate, rows, args.select)
    reviewed = annotate(dataset, args.annotate, args.select)

    xact_id = version_dataset(
        dataset,
        label=f"{args.select}-{time.strftime('%Y%m%d-%H%M%S')}",
        reviewed=len(reviewed),
        total=len(row_ids),
        predicate=predicate,
    )

    print(
        f"\nRun the regression against this exact set:\n"
        f"  python workflows/02_offline_evals.py --dataset {args.dataset} --dataset-version {xact_id}\n"
        f"\nManaged alternative: `braintrust.DatasetPipeline` declares the same\n"
        f"source/transform/target and runs under `bt datasets pipeline run`.\n"
        f"It is experimental -- the explicit BTQL + insert path above is stable."
    )


if __name__ == "__main__":
    main()
