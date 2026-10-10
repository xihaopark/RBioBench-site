"""
Runtime helpers for release-manifest-based evaluation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np


class GradingTier(Enum):
    """Tiered grading for fine-grained failure attribution.

    Hierarchy (worst -> best):
        EXEC_FAIL -> NO_OUTPUT -> OUTPUT_BAD -> SCHEMA_OK -> CONTENT_OK -> PASS
    """

    EXEC_FAIL = "exec_fail"  # R runtime error (stderr has "Error:")
    NO_OUTPUT = "no_output"  # R exit 0 but no expected output files
    OUTPUT_BAD = "output_bad"  # Output files exist but unreadable / schema mismatch
    SCHEMA_OK = "schema_ok"  # Column names match reference
    CONTENT_OK = "content_ok"  # Values match within tolerance (row/col order may differ)
    PASS = "pass"  # Exact match (original definition)

    def __lt__(self, other: GradingTier) -> bool:
        """Allow tier comparisons: EXEC_FAIL < NO_OUTPUT < ... < PASS"""
        order = [
            GradingTier.EXEC_FAIL,
            GradingTier.NO_OUTPUT,
            GradingTier.OUTPUT_BAD,
            GradingTier.SCHEMA_OK,
            GradingTier.CONTENT_OK,
            GradingTier.PASS,
        ]
        return order.index(self) < order.index(other)

    def __le__(self, other: GradingTier) -> bool:
        return self == other or self < other

    def __gt__(self, other: GradingTier) -> bool:
        return other < self

    def __ge__(self, other: GradingTier) -> bool:
        return self == other or self > other


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.resolve()
PYTHON_PKG_DIR = REPO_ROOT / "python_pkg"
TASKS_DIR = REPO_ROOT / "tasks"

# Manifests historically used `public/test_cases_generated/<task_id>`; canonical checkout
# keeps generated cases under `python_pkg/test_cases_generated/<task_id>`.
_LEGACY_PUBLIC_FIXTURES_PREFIX = "public/test_cases_generated/"
_CANONICAL_FIXTURES_PREFIX = "python_pkg/test_cases_generated/"


def resolve_fixtures_path(
    task_meta: dict[str, Any],
    repo_root: Path | None = None,
) -> Path | None:
    """
    Resolve task_meta['fixtures_path'] to an on-disk directory (embedded bundle root or
    external case_* parent). Returns None if nothing exists.

    If the manifest path is missing but matches the legacy ``public/test_cases_generated/``
    prefix, falls back to ``python_pkg/test_cases_generated/`` with the same suffix.
    """
    base = repo_root if repo_root is not None else REPO_ROOT
    fp = task_meta.get("fixtures_path")
    if not fp:
        return None
    rel = str(fp).strip().replace("\\", "/")
    if not rel:
        return None
    primary = (base / rel).resolve()
    if primary.is_dir():
        return primary
    if rel.startswith(_LEGACY_PUBLIC_FIXTURES_PREFIX):
        suffix = rel[len(_LEGACY_PUBLIC_FIXTURES_PREFIX) :]
        alt = (base / _CANONICAL_FIXTURES_PREFIX.rstrip("/") / suffix).resolve()
        if alt.is_dir():
            return alt
    return None


def describe_release_fixture_issue(task_meta: dict[str, Any], repo_root: Path | None = None) -> str | None:
    """
    If the task cannot be graded under validate_release_task (missing tree / cases), return
    a one-line explanation; otherwise None.

    Used to fail fast before long agent runs when fixtures are misconfigured.
    """
    tid = str(task_meta.get("task_id", "?"))
    mode = task_meta.get("fixture_mode")
    if mode == "embedded_case":
        root = resolve_fixtures_path(task_meta, repo_root)
        if root is None:
            return f"{tid}: fixtures_path not found ({task_meta.get('fixtures_path')!r})"
        if not (root / "inputs").is_dir():
            return f"{tid}: embedded inputs/ missing under {root}"
        if not (root / "outputs").is_dir():
            return f"{tid}: embedded outputs/ missing under {root}"
        return None
    root = resolve_fixtures_path(task_meta, repo_root)
    if root is None:
        return f"{tid}: fixtures_path not found ({task_meta.get('fixtures_path')!r})"
    cases = _discover_external_cases(task_meta, repo_root)
    if not cases:
        return f"{tid}: no case_* directories under {root}"
    if not (cases[0] / "inputs").is_dir():
        return f"{tid}: inputs/ missing in {cases[0].name} under {root}"
    return None


DEFAULT_RELEASE_MANIFEST = (
    TASKS_DIR / "releases" / "rbiobench_paper_v1" / "release_manifest.json"
)

_environment_file = REPO_ROOT / "environment" / "environment.json"
if _environment_file.is_file():
    _frozen_image = json.loads(_environment_file.read_text(encoding="utf-8"))["image_reference"]
else:
    _frozen_image = "xihaopark/r_code:latest"
DOCKER_IMAGE = os.environ.get("RBIO_IMAGE", _frozen_image)
DOCKER_WORKDIR = os.environ.get("RBIO_DOCKER_WORKDIR", "/workspace")
TIMEOUT_SECONDS = int(os.environ.get("RBIO_TIMEOUT_SECONDS", "120"))


def repo_rel(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        # Formal experiment artifacts intentionally live outside the Git checkout.
        # Preserve the existing repo-relative representation for in-repo paths,
        # while recording external artifacts as explicit absolute paths.
        return str(resolved)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_release_manifest(manifest_path: str | Path = DEFAULT_RELEASE_MANIFEST) -> dict[str, Any]:
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Release manifest not found: {manifest_path}")
    return read_json(manifest_path)


def select_release_tasks(
    manifest_path: str | Path = DEFAULT_RELEASE_MANIFEST,
    track: str = "all",
    package: str | None = None,
    sample: int | None = None,
    include_deferred: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = Path(manifest_path)
    manifest = load_release_manifest(manifest_path)
    tasks = list(manifest.get("tasks", []))

    if track != "all":
        tasks = [task for task in tasks if task.get("track_id") == track]
    if package:
        tasks = [task for task in tasks if task.get("package") == package]
    if not include_deferred:
        tasks = [task for task in tasks if task.get("status_id") == "include"]
    if sample:
        tasks = tasks[:sample]
    return manifest, tasks


def normalize_task_dirname(task_id: str) -> str:
    return task_id.replace("/", "__")


def extract_r_code(response_text: str) -> str:
    """Strip markdown fences; join all ```r / ``` blocks if multiple (Direct-LLM + agent outputs)."""
    text = response_text or ""
    blocks_r = re.findall(r"```[rR]\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if blocks_r:
        return "\n\n".join(blocks_r)
    blocks_plain = re.findall(r"```\s*\n(.*?)```", text, re.DOTALL)
    if blocks_plain:
        return "\n\n".join(blocks_plain)
    return text


def compare_files(
    ref_file: Path,
    llm_file: Path,
    task_meta: dict[str, Any] | None = None,
    reference_dir: Path | None = None,
) -> dict[str, Any]:
    """Compare files with tiered grading.

    For tabular files, respects task_meta.get("strict_order", False) for row/col order tolerance.
    """
    suffix = ref_file.suffix.lower()
    if suffix in (".csv", ".tsv"):
        strict_order = (task_meta or {}).get("strict_order", False)
        return _compare_tabular(ref_file, llm_file, suffix, strict_order=strict_order)
    if suffix == ".rds":
        scoring_mode = (task_meta or {}).get("scoring_mode_id", "strict")
        if scoring_mode == "comparator":
            # Companion CSV/TSV artifacts do not substitute for validating the
            # published RDS object itself. Compare its scientific content too.
            return _compare_rds_relaxed(
                ref_file,
                llm_file,
                require_structural_match=True,
            )
        return _compare_rds(ref_file, llm_file)
    return _compare_binary(ref_file, llm_file)


def _compare_tabular(
    ref_file: Path, llm_file: Path, suffix: str, strict_order: bool = False
) -> dict[str, Any]:
    """Compare tabular files with tiered grading and optional order tolerance.

    Args:
        ref_file: Reference file path
        llm_file: LLM output file path
        suffix: ".tsv" or ".csv"
        strict_order: If False (default), ignore row/column order differences

    Returns:
        Dict with keys: match (bool), tier (GradingTier), reason (str, optional)
    """
    try:
        import pandas as pd

        sep = "\t" if suffix == ".tsv" else ","
        ref_df = pd.read_csv(ref_file, sep=sep)
        llm_df = pd.read_csv(llm_file, sep=sep)

        # Check shape (basic schema check)
        if ref_df.shape != llm_df.shape:
            return {
                "match": False,
                "tier": GradingTier.OUTPUT_BAD,
                "reason": f"Shape mismatch: ref={ref_df.shape} vs llm={llm_df.shape}",
            }

        # Check column names (SCHEMA_OK level)
        ref_cols = set(ref_df.columns)
        llm_cols = set(llm_df.columns)
        if ref_cols != llm_cols:
            missing = ref_cols - llm_cols
            extra = llm_cols - ref_cols
            return {
                "match": False,
                "tier": GradingTier.OUTPUT_BAD,
                "reason": f"Column mismatch: missing={missing}, extra={extra}",
            }

        # If not strict_order, sort both dataframes by all columns for comparison
        if not strict_order and len(ref_df.columns) > 0:
            # Sort by all columns to ignore row order
            sort_cols = list(ref_df.columns)
            try:
                ref_df = ref_df.sort_values(by=sort_cols).reset_index(drop=True)
                llm_df = llm_df.sort_values(by=sort_cols).reset_index(drop=True)
            except Exception:
                # If sorting fails (e.g., uncomparable types), fall back to original
                pass

        # Check values (CONTENT_OK/PASS level)
        for col in ref_df.columns:
            if ref_df[col].dtype in ("float64", "float32", "int64", "int32"):
                ref_missing = ref_df[col].isna().to_numpy()
                llm_missing = llm_df[col].isna().to_numpy()
                if not np.array_equal(ref_missing, llm_missing):
                    return {
                        "match": False,
                        "tier": GradingTier.SCHEMA_OK,
                        "reason": f"Missing-value mismatch in column: {col}",
                    }
                if not np.allclose(
                    ref_df[col].to_numpy(),
                    llm_df[col].to_numpy(),
                    rtol=1e-3,
                    atol=1e-6,
                    equal_nan=True,
                ):
                    return {
                        "match": False,
                        "tier": GradingTier.SCHEMA_OK,
                        "reason": f"Numeric mismatch in column: {col}",
                    }
            else:
                ref_vals = ref_df[col].astype(str).str.strip()
                llm_vals = llm_df[col].astype(str).str.strip()
                if not ref_vals.equals(llm_vals):
                    return {
                        "match": False,
                        "tier": GradingTier.SCHEMA_OK,
                        "reason": f"Value mismatch in column: {col}",
                    }

        # All checks pass
        return {"match": True, "tier": GradingTier.PASS}
    except Exception as exc:
        return {
            "match": False,
            "tier": GradingTier.OUTPUT_BAD,
            "reason": f"Comparison error: {str(exc)[:200]}",
        }


def _compare_rds(ref_file: Path, llm_file: Path) -> dict[str, Any]:
    """Compare RDS files with tiered grading.

    Returns dict with: match (bool), tier (GradingTier), reason (str, optional)
    """
    compare_script = """
args <- commandArgs(trailingOnly=TRUE)
ref <- readRDS(args[1])
llm <- readRDS(args[2])

canonicalize <- function(x) {
    if (inherits(x, "DESeqDataSet")) {
        attr(x, "dispersionFunction") <- NULL
        row_data <- SummarizedExperiment::rowData(x)
        for (nm in intersect(c("dispGeneIter", "dispIter"), names(row_data))) {
            row_data[[nm]][] <- 0
        }
        SummarizedExperiment::rowData(x) <- row_data
    }
    if (inherits(x, "GeneSetCollection")) {
        for (i in seq_along(x)) {
            methods::slot(x[[i]], "setIdentifier") <- GSEABase::setName(x[[i]])
        }
    }
    if (inherits(x, "Seurat")) {
        x@version <- package_version("0.0.0")
        for (nm in names(x@commands)) {
            if ("time.stamp" %in% methods::slotNames(x@commands[[nm]])) {
                x@commands[[nm]]@time.stamp <- as.POSIXct("2000-01-01", tz = "UTC")
            }
        }
    }
    if (isS4(x) && "runtime" %in% methods::slotNames(x)) {
        runtime <- methods::slot(x, "runtime")
        runtime[] <- 0
        methods::slot(x, "runtime") <- runtime
    }
    if (is.list(x) && !isS4(x)) {
        x[] <- lapply(x, canonicalize)
    }
    x
}

ref_cmp <- canonicalize(ref)
llm_cmp <- canonicalize(llm)

# Helper to map R comparison results to tier codes
# TIER_PASS -> identical
# TIER_CONTENT_OK -> data frame with matching values (but not identical row order etc)
# TIER_SCHEMA_OK -> data frame with matching shape/columns but different values
# TIER_OUTPUT_BAD -> structure differs

tryCatch({
    if (identical(ref, llm)) {
        cat("TIER_PASS")
    } else if (isTRUE(all.equal(ref_cmp, llm_cmp, tolerance = 1e-6, check.attributes = TRUE))) {
        cat("TIER_CONTENT_OK")
    } else if (is.data.frame(ref) && is.data.frame(llm)) {
        # Both are data frames - check schema first
        ref_cols <- names(ref)
        llm_cols <- names(llm)
        if (length(ref_cols) != length(llm_cols) || !all(ref_cols == llm_cols)) {
            cat("TIER_OUTPUT_BAD:column_names_differ")
        } else if (nrow(ref) != nrow(llm)) {
            cat("TIER_OUTPUT_BAD:row_count_differ")
        } else {
            # Schema matches, check values
            all_close <- all(sapply(seq_along(ref), function(i) {
                if (is.numeric(ref[[i]]) && is.numeric(llm[[i]])) {
                    isTRUE(all.equal(ref[[i]], llm[[i]], tolerance=1e-3))
                } else {
                    identical(as.character(ref[[i]]), as.character(llm[[i]]))
                }
            }))
            if (all_close) {
                # Values match but not identical (e.g., row order, attributes)
                cat("TIER_CONTENT_OK")
            } else {
                # Schema OK but values differ
                cat("TIER_SCHEMA_OK:values_differ")
            }
        }
    } else {
        cat("TIER_OUTPUT_BAD:structure_differs")
    }
}, error=function(e) cat(paste0("TIER_OUTPUT_BAD:ERROR:", e$message)))
"""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / "compare.R"
            script_path.write_text(compare_script, encoding="utf-8")
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--platform",
                    "linux/amd64",
                    "-v",
                    f"{ref_file.parent}:/ref:ro",
                    "-v",
                    f"{llm_file.parent}:/llm:ro",
                    "-v",
                    f"{tmpdir}:/scripts:ro",
                    DOCKER_IMAGE,
                    "Rscript",
                    "/scripts/compare.R",
                    f"/ref/{ref_file.name}",
                    f"/llm/{llm_file.name}",
                ],
                capture_output=True,
                timeout=60,
                text=True,
            )
            output = result.stdout.strip()

            # Map tier codes to GradingTier enum
            tier_map = {
                "TIER_PASS": GradingTier.PASS,
                "TIER_CONTENT_OK": GradingTier.CONTENT_OK,
                "TIER_SCHEMA_OK": GradingTier.SCHEMA_OK,
                "TIER_OUTPUT_BAD": GradingTier.OUTPUT_BAD,
            }

            if output == "TIER_PASS":
                return {"match": True, "tier": GradingTier.PASS}
            if output == "TIER_CONTENT_OK":
                return {"match": True, "tier": GradingTier.CONTENT_OK}
            if output.startswith("TIER_SCHEMA_OK:"):
                return {"match": False, "tier": GradingTier.SCHEMA_OK, "reason": output}
            if output.startswith("TIER_OUTPUT_BAD:"):
                return {"match": False, "tier": GradingTier.OUTPUT_BAD, "reason": output}
            if output.startswith("ERROR:"):
                return {"match": False, "tier": GradingTier.OUTPUT_BAD, "reason": output}
            return {"match": False, "tier": GradingTier.OUTPUT_BAD, "reason": f"Unexpected: {output[:200]}"}
    except Exception as exc:
        return {"match": False, "tier": GradingTier.OUTPUT_BAD, "reason": f"RDS comparison error: {str(exc)[:200]}"}


def _compare_rds_relaxed(
    ref_file: Path,
    llm_file: Path,
    require_structural_match: bool,
) -> dict[str, Any]:
    compare_script = """
args <- commandArgs(trailingOnly=TRUE)
mode <- args[3]

summarize_obj <- function(x) {
    sig <- list(
        class = sort(class(x)),
        is_s4 = isS4(x),
        length = tryCatch(length(x), error = function(e) NA_integer_),
        dim = tryCatch({
            d <- dim(x)
            if (is.null(d)) NULL else as.integer(d)
        }, error = function(e) NULL),
        names_head = tryCatch({
            n <- names(x)
            if (is.null(n)) NULL else head(as.character(n), 20)
        }, error = function(e) NULL),
        rownames_head = tryCatch({
            n <- rownames(x)
            if (is.null(n)) NULL else head(as.character(n), 20)
        }, error = function(e) NULL),
        colnames_head = tryCatch({
            n <- colnames(x)
            if (is.null(n)) NULL else head(as.character(n), 20)
        }, error = function(e) NULL)
    )

    if (isS4(x)) {
        sig$slots <- sort(methods::slotNames(x))
    }

    if ("SummarizedExperiment" %in% class(x)) {
        sig$dim <- as.integer(dim(x))
        sig$assays <- sort(as.character(SummarizedExperiment::assayNames(x)))
        sig$coldata_cols <- sort(as.character(colnames(SummarizedExperiment::colData(x))))
    }

    if ("ExpressionSet" %in% class(x)) {
        sig$dim <- as.integer(dim(Biobase::exprs(x)))
        sig$pdata_cols <- sort(as.character(colnames(Biobase::pData(x))))
        sig$fdata_cols <- sort(as.character(colnames(Biobase::fData(x))))
        sig$sample_names_head <- head(as.character(Biobase::sampleNames(x)), 20)
        sig$feature_names_head <- head(as.character(Biobase::featureNames(x)), 20)
    }

    if (inherits(x, "VCF")) {
        sig$dim <- as.integer(dim(x))
        sig$sample_names_head <- head(as.character(colnames(x)), 20)
        sig$seqlevels_head <- head(as.character(GenomeInfoDb::seqlevels(x)), 20)
    }

    if ("GeneSetCollection" %in% class(x)) {
        sig$set_count <- length(x)
        sig$set_names_head <- head(as.character(names(x)), 20)
    }

    if ("phyloseq" %in% class(x)) {
        sig$nsamples <- phyloseq::nsamples(x)
        sig$ntaxa <- phyloseq::ntaxa(x)
    }

    sig
}

result <- tryCatch({
    ref <- readRDS(args[1])
    llm <- readRDS(args[2])

    if (mode == "class_only") {
        if (identical(sort(class(ref)), sort(class(llm)))) {
            cat("MATCH")
        } else {
            cat("MISMATCH:class_differs")
        }
    } else {
        canonicalize <- function(x) {
            if (inherits(x, "DESeqDataSet")) {
                attr(x, "dispersionFunction") <- NULL
                row_data <- SummarizedExperiment::rowData(x)
                for (nm in intersect(c("dispGeneIter", "dispIter"), names(row_data))) {
                    row_data[[nm]][] <- 0
                }
                SummarizedExperiment::rowData(x) <- row_data
            }
            if (inherits(x, "GeneSetCollection")) {
                for (i in seq_along(x)) {
                    methods::slot(x[[i]], "setIdentifier") <- GSEABase::setName(x[[i]])
                }
            }
            if (inherits(x, "Seurat")) {
                x@version <- package_version("0.0.0")
                for (nm in names(x@commands)) {
                    if ("time.stamp" %in% methods::slotNames(x@commands[[nm]])) {
                        x@commands[[nm]]@time.stamp <- as.POSIXct("2000-01-01", tz = "UTC")
                    }
                }
            }
            if (isS4(x) && "runtime" %in% methods::slotNames(x)) {
                runtime <- methods::slot(x, "runtime")
                runtime[] <- 0
                methods::slot(x, "runtime") <- runtime
            }
            if (is.list(x) && !isS4(x)) {
                x[] <- lapply(x, canonicalize)
            }
            x
        }
        ref_cmp <- canonicalize(ref)
        llm_cmp <- canonicalize(llm)
        if (isTRUE(all.equal(ref_cmp, llm_cmp, tolerance = 1e-6, check.attributes = TRUE))) {
            cat("MATCH")
        } else {
            ref_sig <- summarize_obj(ref)
            llm_sig <- summarize_obj(llm)
            if (identical(ref_sig, llm_sig)) {
                cat("MISMATCH:content_differs")
            } else {
                cat("MISMATCH:signature_differs")
            }
        }
    }
}, error = function(e) cat(paste0("ERROR:", e$message)))
"""
    mode = "structure" if require_structural_match else "class_only"
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / "compare.R"
            script_path.write_text(compare_script, encoding="utf-8")
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--platform",
                    "linux/amd64",
                    "-v",
                    f"{ref_file.parent}:/ref:ro",
                    "-v",
                    f"{llm_file.parent}:/llm:ro",
                    "-v",
                    f"{tmpdir}:/scripts:ro",
                    DOCKER_IMAGE,
                    "Rscript",
                    "/scripts/compare.R",
                    f"/ref/{ref_file.name}",
                    f"/llm/{llm_file.name}",
                    mode,
                ],
                capture_output=True,
                timeout=60,
                text=True,
            )
            output = result.stdout.strip()
            if output == "MATCH":
                return {"match": True}
            if output.startswith("MISMATCH:") or output.startswith("ERROR:"):
                return {"match": False, "reason": output}
            return {"match": False, "reason": f"Unexpected: {output[:200]}"}
    except Exception as exc:
        return {"match": False, "reason": f"RDS relaxed comparison error: {str(exc)[:200]}"}


def _compare_binary(ref_file: Path, llm_file: Path) -> dict[str, Any]:
    """Compare binary files with tiered grading.

    Binary comparison is strict - no partial credit for binary files.
    """
    try:
        ref_bytes = ref_file.read_bytes()
        llm_bytes = llm_file.read_bytes()
        if ref_bytes == llm_bytes:
            return {"match": True, "tier": GradingTier.PASS}
        return {
            "match": False,
            "tier": GradingTier.OUTPUT_BAD,
            "reason": f"Binary mismatch (ref={len(ref_bytes)}B, llm={len(llm_bytes)}B)",
        }
    except Exception as exc:
        return {
            "match": False,
            "tier": GradingTier.OUTPUT_BAD,
            "reason": f"Binary read error: {str(exc)[:200]}",
        }


def _discover_external_cases(task_meta: dict[str, Any], repo_root: Path | None = None) -> list[Path]:
    task_cases_dir = resolve_fixtures_path(task_meta, repo_root)
    if task_cases_dir is None:
        return []
    candidates = []
    for path in task_cases_dir.iterdir():
        if path.is_dir() and path.name.startswith("case_"):
            parts = path.name.split("_")
            if len(parts) >= 2 and parts[1].isdigit():
                candidates.append(path)
    candidates.sort(key=lambda item: item.name)

    def has_local_reference(case_dir: Path) -> bool:
        outputs_dir = case_dir / "outputs"
        expected_dir = case_dir / "expected"
        return (outputs_dir.exists() and any(outputs_dir.iterdir())) or (
            expected_dir.exists() and any(expected_dir.iterdir())
        )

    with_reference = [case for case in candidates if has_local_reference(case)]
    if with_reference:
        return with_reference[:5]
    return candidates[:1]


def _resolve_reference_outputs(
    task_meta: dict[str, Any], case_dir: Path, repo_root: Path | None = None
) -> tuple[Path | None, tempfile.TemporaryDirectory | None]:
    outputs_dir = case_dir / "outputs"
    if outputs_dir.exists() and any(outputs_dir.iterdir()):
        return outputs_dir, None

    expected_dir = case_dir / "expected"
    if expected_dir.exists() and any(expected_dir.iterdir()):
        return expected_dir, None

    task_id = task_meta["task_id"]
    parts = task_id.split("/", 1)
    if len(parts) != 2:
        return None, None

    package = task_meta["package"]
    task_slug = parts[1]
    pkg_root = (repo_root or REPO_ROOT).resolve() / "python_pkg"
    gold_dir = pkg_root / "bigcodebench" / "data" / "rbench_data" / package / "gold"
    if not gold_dir.exists():
        return None, None

    task_code = task_slug.split("_")[0]
    artifacts = [Path(path).name for path in task_meta.get("expected_artifacts", []) if path]
    tmp_dir = tempfile.TemporaryDirectory(prefix=f"rbio_gold_{task_code}_")
    staged_dir = Path(tmp_dir.name)
    copied = False

    if artifacts:
        for artifact_name in artifacts:
            gold_path = gold_dir / f"{task_code}_{artifact_name}"
            if gold_path.exists():
                shutil.copy2(gold_path, staged_dir / artifact_name)
                copied = True

    if not copied:
        for gold_file in gold_dir.glob(f"{task_code}_*"):
            dest_name = (
                gold_file.name[len(task_code) + 1 :]
                if gold_file.name.startswith(f"{task_code}_")
                else gold_file.name
            )
            shutil.copy2(gold_file, staged_dir / dest_name)
            copied = True

    if not copied:
        tmp_dir.cleanup()
        return None, None
    return staged_dir, tmp_dir


def write_candidate_archive(
    sample_dir: Path,
    task_meta: dict[str, Any],
    generated_code: str,
    candidate_meta: dict[str, Any] | None = None,
) -> Path:
    archive_dir = sample_dir / "candidate"
    archive_dir.mkdir(parents=True, exist_ok=True)

    code_hash = hashlib.sha256(generated_code.encode("utf-8")).hexdigest()
    (archive_dir / "generated_solution.R").write_text(generated_code, encoding="utf-8")
    (archive_dir / "prompt.txt").write_text(task_meta.get("prompt", ""), encoding="utf-8")
    meta_extra = dict(candidate_meta or {})
    diagnostic_report = meta_extra.pop("diagnostic_report", None)
    if diagnostic_report is not None:
        diagnostics_path = archive_dir / "diagnostics.json"
        diagnostics_path.write_text(
            json.dumps(diagnostic_report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        meta_extra["diagnostics_path"] = repo_rel(diagnostics_path)
    (archive_dir / "raw_response.txt").write_text(meta_extra.get("raw_response", ""), encoding="utf-8")

    metadata = {
        "task_id": task_meta.get("task_id"),
        "package": task_meta.get("package"),
        "track_id": task_meta.get("track_id"),
        "content_tag_id": task_meta.get("content_tag_id"),
        "flow_tag_id": task_meta.get("flow_tag_id"),
        "scoring_mode_id": task_meta.get("scoring_mode_id"),
        "code_sha256": code_hash,
    }
    metadata.update(meta_extra)
    (archive_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return archive_dir


def _write_tabular_summary_csv(source_file: Path, summary_file: Path) -> bool:
    try:
        import pandas as pd

        suffix = source_file.suffix.lower()
        if suffix not in {".csv", ".tsv"}:
            return False

        sep = "\t" if suffix == ".tsv" else ","
        df = pd.read_csv(source_file, sep=sep)
        summary = pd.DataFrame(
            [
                {
                    "n_rows": int(df.shape[0]),
                    "n_cols": int(df.shape[1]),
                    "col_names": ", ".join(str(col) for col in df.columns),
                }
            ]
        )
        summary.to_csv(summary_file, index=False)
        return True
    except Exception:
        return False


def _populate_llm_outputs_dir(
    *,
    output_files: list[Path],
    reference_dir: Path,
    llm_outputs_dir: Path,
    normalize_outputs: bool,
) -> list[str]:
    normalizations: list[str] = []
    copied_files: dict[str, Path] = {}

    for output_file in output_files:
        destination = llm_outputs_dir / output_file.name
        shutil.copy2(output_file, destination)
        copied_files[output_file.name] = destination

    if not normalize_outputs:
        return normalizations

    reference_files = [path for path in reference_dir.iterdir() if path.is_file()]
    reference_names = {path.name for path in reference_files}
    non_summary_refs = [path for path in reference_files if path.name != "summary.csv"]
    produced_non_summary = [path for path in output_files if path.name != "summary.csv"]

    if len(non_summary_refs) == 1:
        ref_file = non_summary_refs[0]
        if ref_file.name not in copied_files and len(produced_non_summary) == 1:
            produced_file = produced_non_summary[0]
            if produced_file.suffix.lower() == ref_file.suffix.lower():
                destination = llm_outputs_dir / ref_file.name
                shutil.copy2(produced_file, destination)
                copied_files[ref_file.name] = destination
                normalizations.append(f"aliased {produced_file.name} -> {ref_file.name}")

    if "summary.csv" in reference_names and "summary.csv" not in copied_files:
        summary_source: Path | None = None
        if len(non_summary_refs) == 1 and non_summary_refs[0].name in copied_files:
            summary_source = copied_files[non_summary_refs[0].name]
        elif len(produced_non_summary) == 1:
            summary_source = copied_files.get(produced_non_summary[0].name)

        if summary_source is not None:
            summary_path = llm_outputs_dir / "summary.csv"
            if _write_tabular_summary_csv(summary_source, summary_path):
                copied_files["summary.csv"] = summary_path
                normalizations.append(f"generated summary.csv from {summary_source.name}")

    return normalizations


def validate_release_task(
    task_meta: dict[str, Any],
    generated_code: str,
    output_root: Path,
    sample_idx: int = 0,
    candidate_meta: dict[str, Any] | None = None,
    normalize_outputs: bool = False,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Validate a task with tiered grading.

    Returns dict with:
        - status: Original status string (PASS/FAIL/NO_OUTPUT/etc)
        - tier: GradingTier enum value (for fine-grained failure attribution)
        - message: Human-readable message
        - candidate_archive: Path to saved candidate code
        - test_cases: List of per-case results with their tiers
    """
    base = (repo_root or REPO_ROOT).resolve()
    task_id = task_meta["task_id"]
    task_output_dir = output_root / normalize_task_dirname(task_id)
    sample_dir = task_output_dir / f"sample_{sample_idx:02d}"
    sample_dir.mkdir(parents=True, exist_ok=True)

    archive_dir = write_candidate_archive(sample_dir, task_meta, generated_code, candidate_meta)
    code_file = archive_dir / "generated_solution.R"

    cases: list[dict[str, Any]] = []
    reference_tmp_handles = []
    all_passed = True

    # Track worst tier seen (for overall_tier calculation)
    worst_tier = GradingTier.PASS

    if task_meta.get("fixture_mode") == "embedded_case":
        embedded_root = resolve_fixtures_path(task_meta, base)
        if embedded_root is None:
            fp = task_meta.get("fixtures_path")
            return {
                "status": "NO_TEST_DATA",
                "tier": GradingTier.NO_OUTPUT,  # Benchmark issue, not agent failure
                "message": f"No test fixtures found: embedded fixtures_path={fp!r} not on disk under repo {base}",
                "candidate_archive": repo_rel(archive_dir),
                "test_cases": [],
            }
        case_inputs = embedded_root / "inputs"
        case_reference = embedded_root / "outputs"
        case_specs = [("case_embedded", case_inputs, case_reference, None)]
    else:
        case_specs = []
        for case_dir in _discover_external_cases(task_meta, base):
            reference_dir, reference_tmp = _resolve_reference_outputs(task_meta, case_dir, base)
            if reference_tmp:
                reference_tmp_handles.append(reference_tmp)
            case_specs.append((case_dir.name, case_dir / "inputs", reference_dir, case_dir))

    if not case_specs:
        fp = task_meta.get("fixtures_path")
        root = resolve_fixtures_path(task_meta, base)
        if root is None:
            detail = f"fixtures_path={fp!r} did not resolve under repo {base}"
        else:
            detail = f"resolved {root} but no usable case_* dirs with inputs/references or gold fallback"
        return {
            "status": "NO_TEST_DATA",
            "tier": GradingTier.NO_OUTPUT,  # Benchmark issue
            "message": f"No test fixtures found: {detail}",
            "candidate_archive": repo_rel(archive_dir),
            "test_cases": [],
        }

    for case_index, (case_name, inputs_dir, reference_dir, _case_dir) in enumerate(case_specs, 1):
        if not inputs_dir.exists():
            cases.append({
                "case": case_name,
                "status": "NO_INPUT",
                "tier": GradingTier.NO_OUTPUT,
                "message": "Inputs directory not found"
            })
            worst_tier = min(worst_tier, GradingTier.NO_OUTPUT)
            all_passed = False
            break
        if reference_dir is None or not reference_dir.exists():
            cases.append({
                "case": case_name,
                "status": "NO_REFERENCE",
                "tier": GradingTier.NO_OUTPUT,
                "message": "Reference outputs not found"
            })
            worst_tier = min(worst_tier, GradingTier.NO_OUTPUT)
            all_passed = False
            break

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                # Prepare stub layer mount if available
                stub_layer_dir = SCRIPT_DIR / "stub_layer"
                stub_mount_args = []
                env_args = []
                if stub_layer_dir.exists() and (stub_layer_dir / "admiral_stubs.R").exists():
                    stub_mount_args = [
                        "-v",
                        f"{stub_layer_dir}:{DOCKER_WORKDIR}/stub_layer:ro",
                    ]
                    env_args = [
                        "-e",
                        f"R_PROFILE_USER={DOCKER_WORKDIR}/stub_layer/.Rprofile_stub_inject",
                    ]

                try:
                    result = subprocess.run(
                        [
                            "docker",
                            "run",
                            "--rm",
                            "--platform",
                            "linux/amd64",
                            "-v",
                            f"{inputs_dir}:{DOCKER_WORKDIR}/inputs:ro",
                            "-v",
                            f"{tmpdir}:{DOCKER_WORKDIR}/outputs",
                            "-v",
                            f"{code_file.absolute()}:{DOCKER_WORKDIR}/solution.R:ro",
                        ]
                        + stub_mount_args
                        + env_args
                        + [
                            "-w",
                            DOCKER_WORKDIR,
                            DOCKER_IMAGE,
                            "Rscript",
                            "solution.R",
                        ],
                        capture_output=True,
                        timeout=TIMEOUT_SECONDS,
                        text=True,
                    )
                except subprocess.TimeoutExpired:
                    cases.append(
                        {
                            "case": case_name,
                            "status": "TIMEOUT",
                            "tier": GradingTier.EXEC_FAIL,
                            "message": f"Timeout after {TIMEOUT_SECONDS}s",
                        }
                    )
                    worst_tier = min(worst_tier, GradingTier.EXEC_FAIL)
                    all_passed = False
                    break

                case_output_dir = sample_dir / f"case_{case_index:02d}"
                case_output_dir.mkdir(exist_ok=True)
                (case_output_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8")
                (case_output_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8")

                tmpdir_path = Path(tmpdir)
                outputs_subdir = tmpdir_path / "outputs"
                if outputs_subdir.exists() and any(outputs_subdir.iterdir()):
                    output_files = [path for path in outputs_subdir.iterdir() if path.is_file()]
                else:
                    output_files = [path for path in tmpdir_path.iterdir() if path.is_file()]

                # Check for execution failure (non-zero exit code with Error in stderr)
                exec_failed = result.returncode != 0 or ("Error:" in (result.stderr or ""))

                if not output_files:
                    # No output produced - could be EXEC_FAIL or NO_OUTPUT
                    case_tier = GradingTier.EXEC_FAIL if exec_failed else GradingTier.NO_OUTPUT
                    cases.append(
                        {
                            "case": case_name,
                            "status": "NO_OUTPUT",
                            "tier": case_tier,
                            "message": "No output files created",
                            "stderr": result.stderr[:1000] if result.stderr else "",
                            "returncode": result.returncode,
                        }
                    )
                    worst_tier = min(worst_tier, case_tier)
                    all_passed = False
                    break

                llm_outputs_dir = case_output_dir / "llm_outputs"
                llm_outputs_dir.mkdir(exist_ok=True)
                normalizations = _populate_llm_outputs_dir(
                    output_files=output_files,
                    reference_dir=reference_dir,
                    llm_outputs_dir=llm_outputs_dir,
                    normalize_outputs=normalize_outputs,
                )

                comparison = {}
                case_match = True
                case_tier = GradingTier.PASS  # Best case for this case

                for ref_file in reference_dir.glob("*"):
                    if not ref_file.is_file():
                        continue
                    llm_name = ref_file.name
                    if "_" in ref_file.name:
                        prefix = ref_file.name.split("_", 1)[0]
                        task_suffix = task_id.split("/", 1)[-1].split("_")[0]
                        if prefix == task_suffix:
                            llm_name = ref_file.name[len(prefix) + 1 :]
                    llm_file = llm_outputs_dir / llm_name
                    if not llm_file.exists():
                        comp = {"match": False, "tier": GradingTier.OUTPUT_BAD, "reason": "File not generated"}
                        comparison[ref_file.name] = comp
                        case_match = False
                        case_tier = min(case_tier, GradingTier.OUTPUT_BAD)
                        continue
                    comp = compare_files(
                        ref_file,
                        llm_file,
                        task_meta=task_meta,
                        reference_dir=reference_dir,
                    )
                    comparison[ref_file.name] = comp
                    if not comp.get("match", False):
                        case_match = False
                        # Update case_tier based on comparison tier
                        comp_tier = comp.get("tier", GradingTier.OUTPUT_BAD)
                        case_tier = min(case_tier, comp_tier)

                # If execution had errors but we got output, treat as EXEC_FAIL
                if exec_failed:
                    case_tier = min(case_tier, GradingTier.EXEC_FAIL)
                    case_status = "EXEC_FAIL"
                else:
                    case_status = "PASS" if case_match else "FAIL"

                cases.append(
                    {
                        "case": case_name,
                        "status": case_status,
                        "tier": case_tier,
                        "comparison": comparison,
                        "returncode": result.returncode,
                        "normalizations": normalizations,
                    }
                )
                worst_tier = min(worst_tier, case_tier)
                if not case_match:
                    all_passed = False
                    break
        except Exception as exc:
            cases.append({
                "case": case_name,
                "status": "ERROR",
                "tier": GradingTier.EXEC_FAIL,
                "message": str(exc)[:500]
            })
            worst_tier = min(worst_tier, GradingTier.EXEC_FAIL)
            all_passed = False
            break

    for handle in reference_tmp_handles:
        try:
            handle.cleanup()
        except Exception:
            pass

    # Determine overall status and tier
    overall_status = "PASS" if all_passed else "FAIL"
    if cases and cases[-1]["status"] in {"NO_TEST_DATA", "NO_REFERENCE", "TIMEOUT", "NO_OUTPUT", "ERROR", "NO_INPUT", "EXEC_FAIL"}:
        overall_status = cases[-1]["status"]

    overall_tier = worst_tier

    return {
        "status": overall_status,
        "tier": overall_tier,
        "message": "" if all_passed else f"Failed at {cases[-1].get('case', 'unknown')}",
        "candidate_archive": repo_rel(archive_dir),
        "test_cases": cases,
    }
