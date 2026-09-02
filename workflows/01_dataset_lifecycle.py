#!/usr/bin/env python3
"""Workflow 01 -- Dataset lifecycle.

    import a golden dataset
      -> inspect it
      -> filter/search
      -> edit examples
      -> version it
      -> reuse it across experiments

Run:
    python workflows/01_dataset_lifecycle.py --rows 1000
    python workflows/01_dataset_lifecycle.py --skip-experiments

Each step below is a standalone function. `main()` at the bottom composes them
into the full workflow; lift any one of them on its own. The `c.step` / `c.info`
calls are narration for the demo -- delete them when you copy a function out.

The point of the last two steps: a snapshot pins one `_xact_id`, and reading at
that version reproduces the dataset exactly as it was, edits and deletes
included. `compare_versions()` proves it by reading the same row twice.
"""

from __future__ import annotations

import argparse
import time
from typing import Any

import braintrust

import _common as c


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------


def import_dataset(name: str, rows: int, batch: int = 1000) -> braintrust.Dataset:
    """Create-or-open a dataset and upsert `rows` synthetic cases into it.

    Row ids are deterministic (`case-00000000`, ...), which makes the import an
    upsert: re-running with a larger `rows` extends the set instead of
    duplicating it. Give your own rows a stable id from your source system for
    the same property.
    """
    c.step(f"Import a golden dataset ({rows:,} rows)")
    dataset = braintrust.init_dataset(
        project=c.PROJECT_NAME,
        name=name,
        description="Golden support-QA set used by the workflow scripts",
        metadata={"owner": "quality-eng", "source": "01_dataset_lifecycle"},
    )
    c.info(f"dataset_id={dataset.id}")

    for i in range(rows):
        case = c.synthetic_case(i)
        dataset.insert(
            id=f"case-{i:08d}",
            input=case["input"],
            expected=case["expected"],
            metadata=case["metadata"],
            tags=case["tags"],
        )
        if (i + 1) % batch == 0:
            dataset.flush()
            c.info(f"flushed {i + 1:,}/{rows:,}")
    dataset.flush()
    return dataset


def wait_for_import(dataset_id: str, expect_rows: int, timeout_s: float = 300.0) -> None:
    """Block until every imported row is visible to a query.

    Writes are acknowledged before they are queryable. Anything that writes and
    then immediately reads has to wait for this.
    """
    c.step("Wait until every row is queryable")
    waited = c.wait_until_queryable(
        f"SELECT count(1) AS n FROM dataset('{dataset_id}')",
        expect_at_least=expect_rows,
        timeout_s=timeout_s,
    )
    c.info(f"all {expect_rows:,} rows queryable after {waited:.1f}s")


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------


def profile(dataset: braintrust.Dataset) -> list[dict[str, Any]]:
    """Summarise the dataset, and count rows per metadata dimension.

    `summarize()` is the SDK's own view. The GROUP BY underneath it is how you
    check a dataset is actually balanced across the dimensions you care about,
    which matters before you read anything into a per-dimension score.
    """
    c.step("Inspect the dataset")
    c.info(str(dataset.summarize()).strip().replace("\n", "\n    "))

    breakdown = c.btql(
        f"""
        SELECT metadata.surface     AS surface,
               metadata.difficulty  AS difficulty,
               count(1)             AS n
        FROM dataset('{dataset.id}')
        GROUP BY metadata.surface, metadata.difficulty
        ORDER BY n DESC
        """
    )
    for row in breakdown:
        c.info(f"surface={row['surface']:<8} difficulty={row['difficulty']:<8} n={row['n']:,}")
    return breakdown


# --------------------------------------------------------------------------
# filter / search
# --------------------------------------------------------------------------


def find_by_metadata(dataset_id: str, limit: int = 5, **fields: str) -> list[dict[str, Any]]:
    """Structured filter on metadata fields, e.g. `difficulty="hard"`."""
    where = " AND ".join(f"metadata.{k} = '{v}'" for k, v in fields.items())
    return c.btql(
        f"""
        SELECT id, input, expected, metadata
        FROM dataset('{dataset_id}')
        WHERE {where}
        ORDER BY _pagination_key
        LIMIT {limit}
        """
    )


def search_text(dataset_id: str, phrase: str, limit: int = 5) -> list[dict[str, Any]]:
    """Full-text search over input and expected.

    `MATCH` is an ordered-phrase operator, not a substring match, and it only
    prunes at the index level when the field is inverted-indexed. Matching more
    than one column needs an explicit OR.
    """
    return c.btql(
        f"""
        SELECT id, input
        FROM dataset('{dataset_id}')
        WHERE input MATCH '{phrase}' OR expected MATCH '{phrase}'
        ORDER BY _pagination_key
        LIMIT {limit}
        """
    )


def export_ids(dataset_id: str, max_rows: int, page_size: int = 1000) -> list[str]:
    """Paginate the whole dataset. This is the export path.

    `ORDER BY _pagination_key` is what makes the cursor advance -- without a
    cursor-compatible sort the export silently re-reads page one forever.
    """
    return [
        row["id"]
        for row in c.btql_all(
            f"SELECT id FROM dataset('{dataset_id}') ORDER BY _pagination_key LIMIT {page_size}",
            max_rows=max_rows,
        )
    ]


def search_demo(dataset: braintrust.Dataset, total_rows: int) -> list[dict[str, Any]]:
    """Run the three read patterns above and report. Returns the filter hits."""
    c.step("Filter and full-text search")

    hits = find_by_metadata(dataset.id, difficulty="hard", surface="web")
    c.info(f"structured filter -> {len(hits)} rows (showing up to 5)")

    phrase = "rotate an API key"
    c.info(f"MATCH {phrase!r} -> {len(search_text(dataset.id, phrase))} rows")

    c.info(f"paginated scan returned {len(export_ids(dataset.id, total_rows)):,} rows")
    return hits


# --------------------------------------------------------------------------
# edit
# --------------------------------------------------------------------------


def correct_examples(
    dataset: braintrust.Dataset,
    row_ids: list[str],
    corrected_answer: str = "CORRECTED by quality-eng review",
) -> list[str]:
    """Apply a review correction to specific rows.

    `update()` merges -- only the fields you pass change, everything else on the
    row is left alone. Use `insert()` with the same id to replace a row wholesale.
    """
    c.step("Edit examples")
    for row_id in row_ids:
        dataset.update(
            id=row_id,
            expected={"answer": corrected_answer},
            tags=["golden", "reviewed"],
            metadata={"reviewed_by": "01_dataset_lifecycle", "reviewed_at": time.time()},
        )
    dataset.flush()
    c.info(f"corrected {len(row_ids)} rows: {', '.join(row_ids)}")
    return row_ids


# --------------------------------------------------------------------------
# version
# --------------------------------------------------------------------------


def snapshot_version(dataset: braintrust.Dataset, label: str, description: str = "") -> tuple[dict[str, Any], str]:
    """Name the dataset's current state. Returns (snapshot, xact_id).

    A snapshot binds a human-readable name to one `_xact_id`. The xact_id is the
    version -- pass it to `init_dataset(version=...)` or a BTQL `version` to read
    the dataset exactly as it is right now, however it changes later.
    """
    c.step("Version the dataset")
    xact_id = c.dataset_version(dataset)
    c.info(f"current _xact_id = {xact_id}")

    snapshot = c.snapshot_dataset(dataset.id, label, xact_id, description=description)
    c.info(f"snapshot {snapshot['id']} name={snapshot['name']} xact_id={snapshot['xact_id']}")
    c.info(f"snapshots on this dataset: {[s['name'] for s in c.list_snapshots(dataset.id)]}")
    return snapshot, xact_id


def read_at_version(dataset_id: str, row_id: str, xact_id: str | None = None) -> Any:
    """Read one row's `expected`, either at a pinned version or at latest."""
    opts = {"version": xact_id} if xact_id else {}
    rows = c.btql(f"SELECT id, expected FROM dataset('{dataset_id}') WHERE id = '{row_id}'", **opts)
    return rows[0]["expected"] if rows else None


def compare_versions(dataset: braintrust.Dataset, row_id: str, xact_id: str, timeout_s: float = 300.0) -> None:
    """Edit a row *after* the snapshot, then read it at both versions.

    This is the proof that a pinned read is real: the snapshot keeps returning
    the pre-edit value no matter what happens to the dataset afterwards.
    """
    dataset.update(id=row_id, expected={"answer": "POST-SNAPSHOT EDIT"})
    dataset.flush()
    c.wait_until_queryable(
        f"SELECT count(1) AS n FROM dataset('{dataset.id}') "
        f"WHERE expected.answer = 'POST-SNAPSHOT EDIT'",
        expect_at_least=1,
        timeout_s=timeout_s,
    )
    c.info(f"at snapshot  -> {read_at_version(dataset.id, row_id, xact_id)}")
    c.info(f"at latest    -> {read_at_version(dataset.id, row_id)}")


# --------------------------------------------------------------------------
# reuse
# --------------------------------------------------------------------------


def stub_task(input_: dict[str, Any]) -> dict[str, Any]:
    """Stand-in for the system under test. Deterministic, so it costs nothing."""
    return {"answer": f"stub answer for {input_.get('question', '')[:40]}"}


def answered(output: dict[str, Any], expected: dict[str, Any] | None) -> float:
    """Trivial scorer: did the task return anything at all."""
    if not expected:
        return 0.0
    return 1.0 if output.get("answer") else 0.0


def run_experiment(dataset_name: str, xact_id: str, variant: str, snapshot_name: str) -> Any:
    """Run one experiment against the dataset pinned at `xact_id`.

    Pinning is the point: two experiments over the same xact_id differ only by
    what you changed, so a score delta is attributable to the variant rather
    than to the dataset having moved underneath you.
    """
    pinned = braintrust.init_dataset(project=c.PROJECT_NAME, name=dataset_name, version=xact_id)
    result = braintrust.Eval(
        c.PROJECT_NAME,
        data=pinned,
        task=stub_task,
        scores=[answered],
        experiment_name=f"w01-{variant}-{c.RUN_ID}",
        metadata={"dataset_snapshot": snapshot_name, "dataset_xact_id": xact_id, "variant": variant},
    )
    c.info(f"{variant}: {result.summary.experiment_url}")
    return result


# --------------------------------------------------------------------------
# the workflow
# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", type=int, default=1000, help="golden dataset size")
    ap.add_argument("--batch", type=int, default=1000, help="rows per flush")
    ap.add_argument("--dataset", default="golden-support-qa")
    ap.add_argument("--skip-experiments", action="store_true")
    ap.add_argument("--freshness-timeout", type=float, default=300.0)
    args = ap.parse_args()

    c.banner(f"Dataset lifecycle ({args.rows:,} rows)")

    dataset = import_dataset(args.dataset, args.rows, args.batch)
    wait_for_import(dataset.id, args.rows, args.freshness_timeout)

    profile(dataset)
    hits = search_demo(dataset, args.rows)

    targets = [r["id"] for r in hits[:3]] or [f"case-{i:08d}" for i in range(3)]
    correct_examples(dataset, targets)

    snapshot, xact_id = snapshot_version(
        dataset,
        label=f"reviewed-{time.strftime('%Y%m%d-%H%M%S')}",
        description=f"{args.rows} rows, {len(targets)} corrections applied",
    )
    compare_versions(dataset, targets[0], xact_id, args.freshness_timeout)

    if args.skip_experiments:
        c.step("Reuse across experiments (skipped)")
        c.info(f"pin later runs with: braintrust.init_dataset(..., version='{xact_id}')")
        return

    c.step("Reuse the pinned dataset across experiments")
    for variant in ("baseline", "candidate"):
        run_experiment(args.dataset, xact_id, variant, snapshot["name"])
    c.info(
        "Both experiments read the same pinned xact_id, so any score delta "
        "between them is attributable to the variant, not to dataset drift."
    )


if __name__ == "__main__":
    main()
