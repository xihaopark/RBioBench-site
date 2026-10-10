#!/usr/bin/env python3
"""Score each runnable task's reference outputs with the Colab evaluator; all must pass."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "colab" / "practice_pack"
tasks = json.loads((ROOT / "data" / "tasks.json").read_text(encoding="utf-8"))
failed = []

for task in (t for t in tasks if t.get("online_runnable")):
    pid = task["public_id"]
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "ws"
        subprocess.run([sys.executable, PACK / "scripts" / "materialize_case.py", "--release", PACK,
                        "--task", pid, "--workspace", workspace], check=True, capture_output=True)
        gt = next(PACK.glob(f"tracks/*/tasks/{pid}/cases/seed_1000/gt_outputs"))
        shutil.copytree(gt, workspace / "outputs", dirs_exist_ok=True)
        result = subprocess.run([sys.executable, PACK / "evaluator" / "score_submission.py", "--release", PACK,
                                 "--task", pid, "--outputs", workspace / "outputs"], capture_output=True, text=True)
        try:
            passed = json.loads(result.stdout).get("pass") is True
        except json.JSONDecodeError:
            passed = False
        print(f"{pid}: {'PASS' if passed else 'FAIL'}")
        if not passed:
            print(result.stdout + result.stderr)
            failed.append(pid)

sys.exit(1 if failed else 0)
