#!/usr/bin/env python3
"""Workflow 01 -- Dataset lifecycle.

    create/import a golden dataset
      -> inspect it
      -> filter/search
      -> edit examples
      -> version it
      -> reuse it across experiments

Run:
    python workflows/01_dataset_lifecycle.py --rows 1000
    python workflows/01_dataset_lifecycle.py --skip-experiments

The point of the last two steps: a snapshot pins one `_xact_id`, and reading
at that version reproduces the dataset exactly as it was, edits and deletes
included. The script proves it by editing again *after* the snapshot and
reading both.
"""

from __future__ import annotations

import argparse
import time

import braintrust

import _common as c


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=1000, help="golden dataset size")
    ap.add_argument("--batch", type=int, default=1000, help="rows per flush")
    ap.add_argument("--dataset", default="golden-support-qa")
    ap.add_argument("--skip-experiments", action="store_true")
    ap.add_argument("--freshness-timeout", type=float, default=300.0)
    args = ap.parse_args()

    c.banner(f"Dataset lifecycle ({args.rows:,} rows)")
    pid = c.project_id()

    # ---------------------------------------------------------------- create
    c.step("Create / import the golden dataset")
    ds = braintrust.init_dataset(
        project=c.PROJECT_NAME,
        name=args.dataset,
        description="Golden support-QA set used by the workflow scripts",
        metadata={"owner": "quality-eng", "source": "01_dataset_lifecycle"},
    )
    c.info(f"dataset_id={ds.id}")

    # Deterministic row ids make the import an upsert: rerunning at a larger
    # --rows extends the set instead of duplicating it.
    t0 = time.time()
    written = 0
    for i in range(args.rows):
        case = c.synthetic_case(i)
        ds.insert(
            id=f"case-{i:08d}",
            input=case["input"],
            expected=case["expected"],
            metadata=case["metadata"],
            tags=case["tags"],
        )
        written += 1
        if written % args.batch == 0:
            ds.flush()
            c.info(f"flushed {written:,}/{args.rows:,}")
    ds.flush()
    elapsed = time.time() - t0
    c.info(f"wrote {args.rows:,} rows in {elapsed:.1f}s ({args.rows / max(elapsed, 1e-6):,.0f} rows/s)")

    # ------------------------------------------------------------- freshness
    c.step("Wait until every row is queryable (ingest -> queryable)")
    count_sql = f"SELECT count(1) AS n FROM dataset('{ds.id}')"
    lag = c.wait_until_queryable(
        count_sql,
        expect_at_least=args.rows,
        timeout_s=args.freshness_timeout,
        label="dataset_ingest_to_queryable",
    )
    c.info(f"all {args.rows:,} rows queryable after {lag:.1f}s")

    # --------------------------------------------------------------- inspect
    c.step("Inspect the dataset")
    summary = ds.summarize()
    c.info(str(summary).strip().replace("\n", "\n    "))

    profile = c.btql(
        f"""
        SELECT metadata.surface     AS surface,
               metadata.difficulty  AS difficulty,
               count(1)             AS n
        FROM dataset('{ds.id}')
        GROUP BY metadata.surface, metadata.difficulty
        ORDER BY n DESC
        """
    )
    for row in profile:
        c.info(f"surface={row['surface']:<8} difficulty={row['difficulty']:<8} n={row['n']:,}")

    # --------------------------------------------------------- filter/search
    c.step("Filter and full-text search")

    hard_web = c.btql(
        f"""
        SELECT id, input, expected, metadata
        FROM dataset('{ds.id}')
        WHERE metadata.difficulty = 'hard' AND metadata.surface = 'web'
        ORDER BY _pagination_key
        LIMIT 5
        """
    )
    c.info(f"structured filter -> {len(hard_web)} rows (showing up to 5)")

    # MATCH is an ordered-phrase full-text operator, not a substring match.
    # It prunes at the index level only when the field is inverted-indexed.
    matches = c.btql(
        f"""
        SELECT id, input
        FROM dataset('{ds.id}')
        WHERE input MATCH 'rotate an API key' OR expected MATCH 'rotate an API key'
        ORDER BY _pagination_key
        LIMIT 5
        """
    )
    c.info(f"MATCH 'rotate an API key' -> {len(matches)} rows")

    # Paginating the whole set is the export path; cursor advance needs a
    # cursor-compatible sort (_pagination_key or _xact_id).
    scanned = sum(
        1
        for _ in c.btql_all(
            f"SELECT id FROM dataset('{ds.id}') ORDER BY _pagination_key LIMIT 1000",
            max_rows=args.rows,
        )
    )
    c.info(f"paginated scan returned {scanned:,} rows")

    # ------------------------------------------------------------------ edit
    c.step("Edit examples")
    edit_targets = [r["id"] for r in hard_web[:3]] or [f"case-{i:08d}" for i in range(3)]
    for rid in edit_targets:
        # update() merges: only the fields you pass change.
        ds.update(
            id=rid,
            expected={"answer": "CORRECTED by quality-eng review"},
            tags=["golden", "reviewed"],
            metadata={"reviewed_by": "01_dataset_lifecycle", "reviewed_at": time.time()},
        )
    ds.flush()
    c.info(f"corrected {len(edit_targets)} rows: {', '.join(edit_targets)}")

    # ------------------------------------------------------------- versioning
    c.step("Version the dataset")

    # Reading `ds.version` from the SDK fetches every row to compute the max
    # _xact_id. At scale, ask the query engine instead.
    xact_id = c.dataset_version(ds)
    c.info(f"current _xact_id = {xact_id}")

    snap_name = f"reviewed-{time.strftime('%Y%m%d-%H%M%S')}"
    snap = c.snapshot_dataset(
        ds.id,
        snap_name,
        xact_id,
        description=f"{args.rows} rows, {len(edit_targets)} corrections applied",
    )
    c.info(f"snapshot {snap['id']} name={snap['name']} xact_id={snap['xact_id']}")
    c.info(f"snapshots on this dataset: {[s['name'] for s in c.list_snapshots(ds.id)]}")

    # Prove the pin is real: mutate again, then read at the pinned xact_id.
    ds.update(id=edit_targets[0], expected={"answer": "POST-SNAPSHOT EDIT"})
    ds.flush()
    c.wait_until_queryable(
        f"SELECT count(1) AS n FROM dataset('{ds.id}') WHERE expected.answer = 'POST-SNAPSHOT EDIT'",
        expect_at_least=1,
        timeout_s=args.freshness_timeout,
        label="dataset_edit_to_queryable",
    )
    pinned = c.btql(
        f"SELECT id, expected FROM dataset('{ds.id}') WHERE id = '{edit_targets[0]}'",
        version=xact_id,
    )
    latest = c.btql(f"SELECT id, expected FROM dataset('{ds.id}') WHERE id = '{edit_targets[0]}'")
    c.info(f"at snapshot  -> {pinned[0]['expected'] if pinned else None}")
    c.info(f"at latest    -> {latest[0]['expected'] if latest else None}")

    # ------------------------------------------------------------- reuse
    if args.skip_experiments:
        c.step("Reuse across experiments (skipped)")
        c.info(f"pin later runs with: braintrust.init_dataset(..., version='{xact_id}')")
        return

    c.step("Reuse the pinned dataset across experiments")
    pinned_ds = braintrust.init_dataset(project=c.PROJECT_NAME, name=args.dataset, version=xact_id)

    def task(input_: dict) -> dict:
        # Stand-in for the system under test. Deterministic so this workflow
        # costs nothing and can run at any size.
        return {"answer": f"stub answer for {input_.get('question', '')[:40]}"}

    def answered(output: dict, expected: dict | None) -> float:
        if not expected:
            return 0.0
        return 1.0 if output.get("answer") else 0.0

    for variant in ("baseline", "candidate"):
        result = braintrust.Eval(
            c.PROJECT_NAME,
            data=pinned_ds,
            task=task,
            scores=[answered],
            experiment_name=f"w01-{variant}-{c.RUN_ID}",
            metadata={"dataset_snapshot": snap_name, "dataset_xact_id": xact_id, "variant": variant},
        )
        c.info(f"{variant}: {result.summary.experiment_url}")

    c.info(
        "Both experiments read the same pinned xact_id, so any score delta "
        "between them is attributable to the variant, not to dataset drift."
    )


if __name__ == "__main__":
    main()
