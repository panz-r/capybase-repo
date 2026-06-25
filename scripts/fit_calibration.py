#!/usr/bin/env python3
"""Fit a calibrated risk model from the experience store.

Reads labeled outcomes from the experience store, builds a (features, label)
dataset, fits a logistic regression with scikit-learn (an optional dep), and
writes a tiny JSON model (coefficients + intercept + threshold) that
``CalibratedRiskEngine`` loads at runtime for pure-Python inference.

The label is 1.0 (will fail) for rejected/escalated merges, 0.0 for accepted.
The threshold is chosen to maximize F1 on the training data (or can be set
manually via --threshold). Requires ``scikit-learn`` (pip install scikit-learn).

Usage:
    python scripts/fit_calibration.py [--repo REPO] \\
        [--store PATH] [--out PATH] [--threshold 0.7]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".", help="repo root")
    ap.add_argument(
        "--store",
        default=".rebase-agent/memory/experiences.jsonl",
        help="experience store path (relative to --repo)",
    )
    ap.add_argument(
        "--out",
        default=".rebase-agent/memory/calibration.json",
        help="output model path (relative to --repo)",
    )
    ap.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="escalation threshold (default: auto-tuned for F1)",
    )
    ap.add_argument(
        "--min-samples",
        type=int,
        default=50,
        help="minimum labeled outcomes required to fit (default 50)",
    )
    args = ap.parse_args()

    try:
        import numpy as np  # noqa: F401
        from sklearn.linear_model import LogisticRegression  # noqa: F401
    except ImportError:
        print(
            "ERROR: scikit-learn and numpy are required for fitting.\n"
            "Install them: pip install scikit-learn",
            file=sys.stderr,
        )
        return 1

    repo = Path(args.repo).resolve()
    store_path = Path(args.store)
    if not store_path.is_absolute():
        store_path = repo / store_path
    if not store_path.is_file():
        print(f"ERROR: experience store not found at {store_path}", file=sys.stderr)
        return 1

    # Import after the sklearn check so the error is clear.
    from capybase.calibration import CalibrationDataset
    from capybase.memory.store import ExperienceStore

    store = ExperienceStore(store_path)
    dataset = CalibrationDataset.from_store(store)
    if dataset.n < args.min_samples:
        print(
            f"ERROR: only {dataset.n} labeled outcomes; need >= {args.min_samples}.",
            file=sys.stderr,
        )
        return 1
    print(f"Loaded {dataset.n} outcomes ({dataset.n_positive} positive/failure).")

    import numpy as np
    from sklearn.linear_model import LogisticRegression

    X = np.array([row[0] for row in dataset.rows], dtype=float)
    y = np.array([row[1] for row in dataset.rows], dtype=float)

    model = LogisticRegression(max_iter=1000, class_weight="balanced")
    model.fit(X, y)

    # Threshold tuning: if not specified, pick the threshold maximizing F1.
    threshold = args.threshold
    if threshold is None:
        probas = model.predict_proba(X)[:, 1]
        best_f1, best_t = 0.0, 0.5
        for t in [i / 100 for i in range(5, 96)]:
            preds = (probas >= t).astype(int)
            tp = int(((preds == 1) & (y == 1)).sum())
            fp = int(((preds == 1) & (y == 0)).sum())
            fn = int(((preds == 0) & (y == 1)).sum())
            prec = tp / (tp + fp) if (tp + fp) else 0.0
            rec = tp / (tp + fn) if (tp + fn) else 0.0
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
            if f1 > best_f1:
                best_f1, best_t = f1, t
        threshold = best_t
        print(f"Auto-tuned threshold: {threshold:.2f} (F1={best_f1:.3f})")

    out = {
        "coefficients": model.coef_[0].tolist(),
        "intercept": float(model.intercept_[0]),
        "threshold": float(threshold),
        "feature_keys": [
            "markers_remaining", "whole_file_markers_remaining", "splice_scope_ok",
            "copied_one_side", "copied_current_side", "copied_replayed_side",
            "model_needs_human", "syntax_passed", "ast_preserved",
            "lsp_error_count", "lsp_new_error_count",
            "hard_failure_count", "warning_count",
        ],
        "n_samples": dataset.n,
        "n_positive": dataset.n_positive,
    }
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = repo / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Wrote calibration model to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
