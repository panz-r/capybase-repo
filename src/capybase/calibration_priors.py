"""Calibrated confidence — historical pass rates per conflict class.

The candidate-ref design's last acceptance piece: "Confidence should be
calibrated from observed historical outcomes for the relevant conflict
class. It should not come from the proposing model saying that it is
confident."

This module derives the calibration table from EXISTING measurement
(the eval results files — the merged latest-wins dataset sprint-27
assembled is exactly this) and exposes the per-class prior. The prior
INFORMS the review decision (a tier-B candidate in a class with a 95%
historical pass reads differently from one in a 40% class); it never
flips a tier on its own — evidence decides, priors annotate. That
boundary is deliberate: promoting on a prior would be gaming the
acceptance policy, the exact failure the design's "resolver never
decides safety" rule exists to prevent.

Class granularity: the honest key available in results data is
``language`` ( finer classes — conflict type, size bucket — need unit
metadata the results don't carry; when they do, widen ``_class_key``).
"""

from __future__ import annotations

import json
from pathlib import Path


def _class_key(record: dict) -> str | None:
    """The conflict-class key for one result record (language today)."""
    lang = record.get("language") or "?"
    return str(lang) or None


def derive_priors(records: list[dict]) -> dict[str, dict]:
    """Historical pass rates per class from result records.

    SAFE_SKIP / SETUP_FAILED records are excluded (they carry no
    resolution outcome). Returns ``{class: {"n": int, "pass_rate":
    float}}`` — pass_rate counts PASS only (WORKING is a graded
    success, not a PASS; conflating them would inflate confidence).
    """
    agg: dict[str, dict[str, int]] = {}
    for r in records:
        if r.get("terminal_reason") in ("SAFE_SKIP", "SETUP_FAILED"):
            continue
        verdict = r.get("verdict")
        if not verdict:
            continue
        key = _class_key(r)
        if key is None:
            continue
        cell = agg.setdefault(key, {"n": 0, "pass": 0})
        cell["n"] += 1
        if verdict == "PASS":
            cell["pass"] += 1
    return {
        k: {"n": v["n"], "pass_rate": round(v["pass"] / v["n"], 4)}
        for k, v in agg.items() if v["n"] > 0
    }


def save_priors(priors: dict, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(priors, indent=2), encoding="utf-8")


def load_priors(path: str | Path) -> dict | None:
    """Load a priors table; None (priors disabled) when absent/unreadable."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001 — a malformed table disables priors
        return None


def prior_for(priors: dict | None, language: str | None) -> dict | None:
    """The prior for one conflict class, when the sample is meaningful.

    Returns None below a minimum sample (default 20) — a 2-case 100%
    is not calibration, it's an anecdote.
    """
    if not priors:
        return None
    entry = priors.get(str(language or "?"))
    if not entry or entry.get("n", 0) < 20:
        return None
    return {"n": entry["n"], "pass_rate": entry.get("pass_rate", 0.0)}


def prior_reason(prior: dict | None) -> str:
    """The human-readable annotation the acceptance reasons carry."""
    if prior is None:
        return ""
    return (f"class calibration: {prior['pass_rate']*100:.0f}% "
            f"historical pass (n={prior['n']})")
