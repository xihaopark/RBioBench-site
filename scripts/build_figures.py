#!/usr/bin/env python3
"""Inline figures/*.png into site/*Figure.html so Apps Script can include them."""
from base64 import b64encode
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIGURES = {
    "ResultFigure.html": (
        "result_overview.png",
        "RBioBench performance and error analysis across Clinical Study and Bioinformatics tasks",
    ),
    "SampleFigure.html": (
        "sample_tasks.png",
        "Four representative RBioBench tasks and their execution-based pass conditions",
    ),
    "ClinicalCostFigure.html": (
        "clinical_cost.png",
        "Clinical Study task pass rate versus mean model cost per task",
    ),
    "OmicsCostFigure.html": (
        "omics_cost.png",
        "Bioinformatics task pass rate versus mean model cost per task",
    ),
}

for output_name, (image_name, alt_text) in FIGURES.items():
    encoded = b64encode((ROOT / "figures" / image_name).read_bytes()).decode("ascii")
    html = f'<img loading="lazy" src="data:image/png;base64,{encoded}" alt="{alt_text}">\n'
    (ROOT / "site" / output_name).write_text(html, encoding="utf-8")
    print(output_name, len(html))
