#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from enum import Enum
from pathlib import Path

from release_runtime import compare_files


def json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Score one RBioBench submission.")
    parser.add_argument("--release", type=Path, default=Path("."))
    parser.add_argument("--task", required=True, help="public ID, for example BIO_0001")
    parser.add_argument("--outputs", type=Path, required=True)
    args = parser.parse_args()

    release = args.release.resolve()
    with (release / "TASK_INDEX.csv").open(newline="", encoding="utf-8") as handle:
        rows = {row["public_id"]: row for row in csv.DictReader(handle)}
    if args.task not in rows:
        raise SystemExit(f"unknown public task ID: {args.task}")

    task_root = release / rows[args.task]["relative_path"]
    task_meta = json.loads((task_root / "task.json").read_text(encoding="utf-8"))
    gt_root = task_root / "cases" / "seed_1000" / "gt_outputs"
    outputs_root = args.outputs.resolve()
    comparisons = {}
    passed = True
    reference_paths = {
        path.relative_to(gt_root)
        for path in gt_root.rglob("*")
        if path.is_file()
    }
    candidate_paths = {
        path.relative_to(outputs_root)
        for path in outputs_root.rglob("*")
        if path.is_file()
    }
    for relative in sorted(candidate_paths - reference_paths):
        comparisons[relative.as_posix()] = {
            "match": False,
            "tier": "output_bad",
            "reason": "Unexpected artifact",
        }
        passed = False
    for relative in sorted(reference_paths):
        reference = gt_root / relative
        candidate = outputs_root / relative
        if not candidate.is_file():
            comparisons[relative.as_posix()] = {
                "match": False,
                "tier": "output_bad",
                "reason": "File not generated",
            }
            passed = False
            continue
        result = compare_files(
            reference,
            candidate,
            task_meta=task_meta,
            reference_dir=gt_root,
        )
        comparisons[relative.as_posix()] = result
        passed = passed and bool(result.get("match"))

    report = {"task": args.task, "pass": passed, "comparisons": comparisons}
    print(json.dumps(report, indent=2, default=json_default))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
