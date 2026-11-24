from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from metadata_agent.extract.normalizer import normalize_enum_value
from metadata_agent.store.export import METADATA_SHEET

logger = logging.getLogger(__name__)

DEFAULT_GOLDEN_PATH = "人类数据_数据库_20240721_1.xlsx"
DEFAULT_THRESHOLD = 0.80

DEFAULT_COMPARE_COLUMNS = (
    "Class", "OrganismSimple", "Gender", "Tumor/Cancer",
    "Tissue", "CellLine",
)


def load_golden(path: str | Path = DEFAULT_GOLDEN_PATH) -> pd.DataFrame:
    return pd.read_excel(path, sheet_name="Sheet1")


def _merged_name(merged: pd.DataFrame, column: str, suffix: str) -> str:
    """Merge suffixes only colliding names; merge keys and one-sided columns keep their bare name."""
    suffixed = f"{column}{suffix}"
    return suffixed if suffixed in merged.columns else column


def _labeled_mask(values: pd.Series) -> pd.Series:
    return values.notna() & values.astype(str).str.strip().ne("")


def _evaluate_column(
    merged: pd.DataFrame,
    col: str,
    *,
    in_predictions: bool,
    in_golden: bool,
    is_merge_key: bool,
    joined_rows: int,
    threshold: float,
) -> tuple[dict[str, Any], float | None, list[str]]:
    """Score one column. Returns (metrics, accuracy if genuinely scored else None, errors)."""
    errors: list[str] = []
    accuracy = 0.0
    labeled_rows = 0
    scored: float | None = None

    if not in_predictions:
        errors.append(f"Column '{col}' is missing from predictions.")
    if not in_golden:
        errors.append(f"Column '{col}' is missing from golden data.")
    if is_merge_key:
        errors.append(f"Column '{col}' is a merge key; comparing it against itself measures nothing.")

    # The denominator belongs to the golden data, so report it even when predictions lack the column.
    if in_golden:
        gold_values = merged[_merged_name(merged, col, "_gold")]
        mask = _labeled_mask(gold_values)
        labeled_rows = int(mask.sum())

        if in_predictions and not is_merge_key:
            if labeled_rows == 0:
                errors.append(f"Column '{col}' has no labeled rows and was not evaluated.")
            else:
                pred_values = merged[_merged_name(merged, col, "_pred")]
                pred_normed = pred_values[mask].astype(str).map(lambda v: normalize_enum_value(col, v))
                gold_normed = gold_values[mask].astype(str).map(lambda v: normalize_enum_value(col, v))
                accuracy = float((pred_normed == gold_normed).mean())
                scored = accuracy
                if accuracy < threshold:
                    errors.append(
                        f"Column '{col}' accuracy {accuracy:.3f} is below threshold {threshold:.3f}."
                    )

    metrics = {
        "accuracy": accuracy,
        "labeled_rows": labeled_rows,
        "joined_rows": joined_rows,
        "missing_in_predictions": not in_predictions,
        "missing_in_golden": not in_golden,
    }
    return metrics, scored, errors


def evaluate(
    predictions: pd.DataFrame,
    golden: pd.DataFrame,
    id_column: str = "SourceID",
    source_column: str = "Source",
    threshold: float = DEFAULT_THRESHOLD,
    compare_columns: Sequence[str] | None = None,
) -> dict[str, Any]:
    compared = tuple(DEFAULT_COMPARE_COLUMNS if compare_columns is None else compare_columns)
    merged = predictions.merge(
        golden,
        on=[id_column, source_column],
        how="inner",
        suffixes=("_pred", "_gold"),
    )

    joined_rows = len(merged)
    errors: list[str] = []
    if joined_rows == 0:
        logger.warning("No overlapping samples between predictions and golden set")
        errors.append("No overlapping samples between predictions and golden set.")

    merge_keys = {id_column, source_column}
    results: dict[str, dict[str, Any]] = {}
    accuracies: list[float] = []

    for col in compared:
        metrics, scored, column_errors = _evaluate_column(
            merged,
            col,
            in_predictions=col in predictions.columns,
            in_golden=col in golden.columns,
            is_merge_key=col in merge_keys,
            joined_rows=joined_rows,
            threshold=threshold,
        )
        results[col] = metrics
        errors.extend(column_errors)
        if scored is not None:
            accuracies.append(scored)

    if not accuracies:
        errors.append("No columns were evaluated.")

    return {
        "covered": joined_rows,
        "columns": results,
        "overall": sum(accuracies) / len(accuracies) if accuracies else 0.0,
        "threshold": threshold,
        "pass": not errors,
        "reasons": errors,
        "reason": "; ".join(errors) if errors else None,
    }


def run_eval(
    predictions_path: str | Path | None = None,
    canonical_db: str | Path = "data/canonical.sqlite",
    golden_path: str | Path = DEFAULT_GOLDEN_PATH,
    threshold: float = DEFAULT_THRESHOLD,
    compare_columns: Sequence[str] | None = None,
) -> dict[str, Any]:
    golden = load_golden(golden_path)

    if predictions_path is not None:
        predictions = pd.read_excel(str(predictions_path), sheet_name=METADATA_SHEET)
    else:
        import sqlite3
        conn = sqlite3.connect(str(canonical_db))
        predictions = pd.read_sql("SELECT * FROM canonical", conn)
        conn.close()

    result = evaluate(predictions, golden, threshold=threshold, compare_columns=compare_columns)

    logger.info("Golden-set evaluation: %d covered samples", result["covered"])
    for col, metrics in result["columns"].items():
        comparable = not metrics["missing_in_predictions"] and not metrics["missing_in_golden"]
        scored = comparable and metrics["labeled_rows"] > 0
        status = "PASS" if scored and metrics["accuracy"] >= threshold else "FAIL"
        logger.info(
            "  %s: %.1f%% (%d labeled / %d joined) [%s]",
            col,
            metrics["accuracy"] * 100,
            metrics["labeled_rows"],
            metrics["joined_rows"],
            status,
        )
    if result["reason"]:
        logger.warning("Golden-set evaluation failed: %s", result["reason"])

    return result
