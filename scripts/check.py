#!/usr/bin/env python3
"""Sanity checks run on every PR: the site breaks if any of these fail."""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FIELDS = ("public_id", "track", "package", "level", "prompt", "online_runnable")
errors = []

try:
    tasks = json.loads((ROOT / "data" / "tasks.json").read_text(encoding="utf-8"))
except Exception as exc:  # noqa: BLE001
    sys.exit(f"data/tasks.json is not valid JSON: {exc}")

if not isinstance(tasks, list) or not tasks:
    sys.exit("data/tasks.json must be a non-empty list of tasks")

PACK = ROOT / "colab" / "practice_pack"
with (PACK / "TASK_INDEX.csv").open(newline="", encoding="utf-8") as handle:
    pack_tasks = {row["public_id"]: row for row in csv.DictReader(handle)}

seen = set()
for i, task in enumerate(tasks):
    missing = [f for f in REQUIRED_FIELDS if f not in task]
    if missing:
        errors.append(f"task #{i} ({task.get('public_id', '?')}) missing fields: {', '.join(missing)}")
    pid = task.get("public_id")
    if pid in seen:
        errors.append(f"duplicate public_id {pid}")
    seen.add(pid)
    if task.get("online_runnable") and not (ROOT / "notebooks" / f"RBioBench_{pid}.ipynb").exists():
        errors.append(f"{pid} is online_runnable but notebooks/RBioBench_{pid}.ipynb is missing")
    if task.get("online_runnable"):
        row = pack_tasks.get(pid)
        if not row:
            errors.append(f"{pid} is online_runnable but missing from colab/practice_pack/TASK_INDEX.csv")
        elif not (PACK / row["relative_path"] / "cases" / "seed_1000" / "inputs").is_dir():
            errors.append(f"{pid}: colab/practice_pack/{row['relative_path']}/cases/seed_1000/inputs is missing")

for path in sorted((ROOT / "notebooks").glob("*.ipynb")):
    try:
        nb = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(nb.get("cells"), list)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{path.relative_to(ROOT)} is not a valid notebook: {exc}")

if errors:
    print("\n".join(errors))
    sys.exit(1)
print(f"OK: {len(tasks)} tasks, {sum(1 for t in tasks if t.get('online_runnable'))} runnable in Colab")
