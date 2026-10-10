#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", type=Path, default=Path("."))
    parser.add_argument("--task", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()

    with (args.release / "TASK_INDEX.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        rows = {row["public_id"]: row for row in csv.DictReader(handle)}
    if args.task not in rows:
        raise SystemExit("unknown task: " + args.task)

    task_dir = args.release / rows[args.task]["relative_path"]
    source_inputs = task_dir / "cases" / "seed_1000" / "inputs"
    if not source_inputs.is_dir():
        raise SystemExit("seed_1000 inputs not found")
    if args.workspace.exists() and any(args.workspace.iterdir()):
        raise SystemExit("workspace must be empty")

    (args.workspace / "inputs").mkdir(parents=True, exist_ok=True)
    (args.workspace / "outputs").mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source_inputs, args.workspace / "inputs", dirs_exist_ok=True
    )
    shutil.copy2(task_dir / "task.json", args.workspace / "task.json")
    shutil.copy2(task_dir / "schema.json", args.workspace / "schema.json")
    print(args.workspace.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
