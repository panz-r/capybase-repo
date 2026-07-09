"""Session-level drift detection (behavioral-regression, mechanism-gated).

This module is the second-generation drift detector. The first generation
compared a **prose intent anchor** to **merged source code** via cosine
embedding distance. An external review (see ``docs/drift-detector-review.md``)
established that cross-modal comparison has no operating point: the
prose-vs-code distance floor (~0.29–0.45) is the *baseline for a correct
merge*, not noise around a signal, so no threshold separates correct from
drifted output. That design was scrapped.

The replacement follows the review's three immediate-action recommendations:

1. **Gate on resolution mechanism.** A resolution produced by a deterministic
   path (exact-history reuse, structural union, brace repair, test-gated side
   pick, combination search, block capture) is a verbatim or provably-safe
   replay of a previously-validated state — drift is impossible by
   construction, so NO drift signal is emitted. Only LLM-produced resolutions
   (the actual drift-risk case) can fire the advisory.

2. **No embeddings, no anchor, no threshold.** The module holds no model, no
   vector space, and no tuned number. There is nothing to calibrate.

3. **Test regression is the primary signal.** The behavioral signal — "a test
   that passed pre-rebase now fails" — has a documented 0% false-positive rate
   in the semantic-conflict literature (SAM). The orchestrator already computes
   this set via the test-continuity invariant; this module merely accumulates
   it across the session as the drift trajectory.

The monitor is advisory (never blocks a merge, never escalates, never mutates
state) and best-effort (never raises; a missing test baseline makes it a silent
no-op). It journals a ``DriftReport`` per observed step and a one-line
``summary()`` at session end.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DriftReport:
    """One per-step behavioral-drift observation.

    ``mechanism`` is the coarse resolution class of the step's accepted
    outcomes: ``"deterministic"`` (verbatim/structural replay — drift
    impossible by construction), ``"llm"`` (model-produced), or ``"mixed"``
    (some of each).

    ``regressed_tests`` are the test node-IDs that passed on the pre-rebase
    baseline but fail after this step's merge — the 0%-FPR behavioral signal.
    ``coverage_note`` records whether the behavioral signal was active for this
    step (a baseline was captured) or inactive (no baseline → the primary
    signal cannot fire, and the note says so).

    ``is_drift`` is True only when the step was model-produced AND introduced a
    regression. A deterministic step never drifts regardless of regressions
    (the resolution is a replay; a pre-existing test failure is not drift it
    caused).
    """

    commit_index: int
    mechanism: str
    regressed_tests: tuple[str, ...]
    coverage_note: str

    @property
    def is_drift(self) -> bool:
        return bool(self.regressed_tests) and self.mechanism != "deterministic"

    def render(self) -> str:
        n = len(self.regressed_tests)
        if self.is_drift:
            names = ", ".join(self.regressed_tests[:5])
            tail = " ..." if n > 5 else ""
            return (
                f"behavioral drift @ commit {self.commit_index}: "
                f"{n} regression(s) via {self.mechanism} resolution "
                f"[{names}{tail}] — {self.coverage_note}"
            )
        if self.mechanism == "deterministic":
            return (
                f"no drift @ commit {self.commit_index}: deterministic "
                f"resolution (drift impossible by construction) — "
                f"{self.coverage_note}"
            )
        # LLM/mixed but no regression — the healthy case.
        return (
            f"no drift @ commit {self.commit_index}: {self.mechanism} "
            f"resolution, 0 regressions — {self.coverage_note}"
        )


@dataclass
class DriftMonitor:
    """Advisory session-level behavioral-drift accumulator.

    Construct once per session; call :meth:`observe` after each step's test
    gate has run (the observation needs the step's test-regression set and the
    accepted resolutions' provenance). Never blocks a merge — the advisory is
    journaled by the caller. Never raises; an inactive monitor (disabled by
    config) observes nothing and reports an empty summary.

    Unlike the scrapped embedding monitor, this one takes no embedder and no
    threshold: the signal is behavioral (binary pass/fail) and the gate is
    structural (the resolution mechanism). There is nothing to calibrate.
    """

    _history: list[DriftReport] = field(default_factory=list)
    _active: bool = True

    def observe(
        self,
        *,
        commit_index: int,
        mechanism: str,
        regressed_tests: list[str] | tuple[str, ...] = (),
        coverage_note: str = "",
    ) -> DriftReport | None:
        """Record one step's behavioral outcome.

        Returns the :class:`DriftReport` (appended to history), or None when the
        monitor is inactive. The caller journals the report when
        ``report.is_drift`` is True. ``mechanism`` must be one of
        ``"deterministic"``, ``"llm"``, ``"mixed"``.
        """
        if not self._active:
            return None
        report = DriftReport(
            commit_index=commit_index,
            mechanism=mechanism,
            regressed_tests=tuple(regressed_tests),
            coverage_note=coverage_note or "no test baseline captured",
        )
        self._history.append(report)
        return report

    @property
    def history(self) -> list[DriftReport]:
        return list(self._history)

    @property
    def total_regressions(self) -> int:
        """Total regressions across model-produced steps this session.

        Deterministic steps are excluded: a regression observed after a
        verbatim replay was not caused by the replay (it pre-existed on the
        base), so it does not count toward LLM-induced drift.
        """
        return sum(
            len(r.regressed_tests)
            for r in self._history
            if r.mechanism != "deterministic"
        )

    @property
    def drift_steps(self) -> list[DriftReport]:
        """Only the steps where drift actually fired (model-produced + regressions)."""
        return [r for r in self._history if r.is_drift]

    def summary(self) -> str:
        """A one-line post-session drift summary for the report/logs.

        Reports total regressions across the session, broken out by mechanism,
        so the operator can see whether drift was LLM-induced or only ever
        appeared under deterministic replays (where it is not actionable drift).
        Empty when the monitor observed nothing.
        """
        if not self._history:
            return ""
        n = len(self._history)
        det_regressions = sum(
            len(r.regressed_tests)
            for r in self._history
            if r.mechanism == "deterministic"
        )
        llm_regressions = self.total_regressions
        drift_n = len(self.drift_steps)
        if drift_n == 0 and llm_regressions == 0:
            if det_regressions:
                return (
                    f"behavioral drift over the {n}-commit window: "
                    f"0 LLM-induced regression(s) "
                    f"({det_regressions} pre-existing under deterministic replays) "
                    f"— no model-induced drift"
                )
            return (
                f"behavioral drift over the {n}-commit window: "
                f"0 regression(s) — no drift"
            )
        return (
            f"behavioral drift over the {n}-commit window: "
            f"{llm_regressions} LLM-induced regression(s) across {drift_n} step(s)"
        )
