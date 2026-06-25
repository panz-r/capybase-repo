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
    ap.add_argument(
        "--conformal",
        action="store_true",
        help="fit a split-conformal predictor with a coverage guarantee",
    )
    ap.add_argument(
        "--alpha",
        type=float,
        default=0.1,
        help="conformal miscoverage rate (coverage = 1-alpha, default 0.1)",
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

    # Single source of truth: import the canonical key list from the runtime
    # module so a newly-fit model automatically picks up any key added there
    # (avoids the drift of maintaining two parallel lists).
    from capybase.calibration import _FEATURE_KEYS as feature_keys

    if args.conformal:
        out = _fit_conformal(X, y, feature_keys, dataset, store, args.alpha)
    else:
        out = _fit_logistic(X, y, feature_keys, dataset, store, args.threshold)

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = repo / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Wrote calibration model to {out_path}")
    return 0


def _fit_logistic(X, y, feature_keys, dataset, store, threshold_override):
    """Fit a logistic regression with F1-tuned threshold."""
    from sklearn.linear_model import LogisticRegression

    model = LogisticRegression(max_iter=1000, class_weight="balanced")
    model.fit(X, y)
    threshold = threshold_override
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
        "feature_keys": feature_keys,
        "n_samples": dataset.n,
        "n_positive": dataset.n_positive,
    }
    # TECP entropy threshold is computed from raw experience features, not the
    # feature matrix, so it needs the store. Emitted on both model kinds.
    out["tecp_entropy_threshold"] = _tecp_entropy_threshold(store, 0.1)
    return out


def _tecp_entropy_threshold(store, alpha):
    """Survey §4.1 (TECP): the (1-alpha) quantile of mean token-entropy over
    CORRECT (accepted) calibration candidates.

    A candidate whose ``mean_token_entropy`` exceeds this is treated as
    nonconforming with the confident majority → escalate. The runtime model
    stores this as ``tecp_entropy_threshold``; absent (or None) it is ignored.

    Only candidates that actually captured logprobs contribute; rows without an
    entropy value are skipped, so this stays safe even when entropy capture has
    only been enabled partway through a corpus.

    The quantile is computed in pure Python (the "higher" rounding convention
    matching ``numpy.quantile(..., method="higher")``): index
    ``ceil((1-alpha) * (n-1))`` on the sorted entropies. This keeps the TECP
    helper free of the numpy/sklearn dependency the rest of the fitter carries,
    so it can be unit-tested and reused without the offline stack installed.
    """
    import math

    entropies = []
    for exp in store:
        # Accepted = correct; rejected/escalated = failure (the label convention
        # used everywhere else in this file and in calibration.py).
        if exp.outcome != "accepted":
            continue
        feats = exp.validator_features or {}
        val = feats.get("mean_token_entropy")
        try:
            if val is not None:
                entropies.append(float(val))
        except (TypeError, ValueError):
            continue
    if not entropies:
        print("TECP: no entropy observations on accepted outcomes; skipping threshold.")
        return None
    entropies.sort()
    n = len(entropies)
    pos = math.ceil((1.0 - alpha) * (n - 1))
    q = float(entropies[max(0, min(pos, n - 1))])
    print(
        f"TECP entropy threshold (alpha={alpha}): {q:.4f} "
        f"over {n} accepted outcomes."
    )
    return q


def _fit_conformal(X, y, feature_keys, dataset, store, alpha):
    """Fit a split-conformal predictor with a coverage guarantee (1-alpha)."""
    from sklearn.linear_model import LogisticRegression

    n = len(y)
    # Split: first half for training, second half for calibration.
    split = max(1, n // 2)
    X_train, X_cal = X[:split], X[split:]
    y_train, y_cal = y[:split], y[split:]
    model = LogisticRegression(max_iter=1000, class_weight="balanced")
    model.fit(X_train, y_train)
    # Nonconformity score: 1 - P(correct label).
    # For label=1 (failure): score = 1 - P(fail). For label=0 (success): score = 1 - P(success).
    cal_probas = model.predict_proba(X_cal)[:, 1]  # P(fail)
    scores = []
    for proba, label in zip(cal_probas, y_cal):
        p_correct = (1.0 - proba) if label < 0.5 else proba
        scores.append(float(1.0 - p_correct))
    scores.sort()
    print(
        f"Conformal fit: {len(y_train)} train, {len(y_cal)} calibration. "
        f"Coverage target: {1-alpha:.0%}. "
        f"Calibration score range: [{min(scores):.3f}, {max(scores):.3f}]"
        if scores else "Conformal fit: no calibration data"
    )
    return {
        "type": "conformal",
        "coefficients": model.coef_[0].tolist(),
        "intercept": float(model.intercept_[0]),
        "alpha": float(alpha),
        "calibration_scores": scores,
        "feature_keys": feature_keys,
        "n_samples": dataset.n,
        "n_positive": dataset.n_positive,
        # TECP entropy threshold (survey §4.1): the (1-alpha) quantile of mean
        # token-entropy over accepted outcomes. Runtime ConformalRiskModel
        # escalates any candidate whose entropy exceeds this. None when no
        # entropy observations exist (e.g. entropy capture still off).
        "tecp_entropy_threshold": _tecp_entropy_threshold(store, alpha),
    }


if __name__ == "__main__":
    raise SystemExit(main())
