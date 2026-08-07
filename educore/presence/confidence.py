"""Scoring: evidence in, confidence and disposition out (ADR-0002).

The whole point of this module is that it does not gate. It produces a number
and a disposition; the caller records attendance either way. A sensor failure
raises a review, it never stops someone teaching.
"""

from __future__ import annotations

from dataclasses import dataclass

from .evaluators import SignalResult
from .models import Disposition, Verdict


@dataclass
class Assessment:
    confidence: int
    disposition: str
    available_weight: int
    passed_weight: int
    rejection_reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.disposition != Disposition.REJECTED


def score(results: list[SignalResult], policy) -> Assessment:
    """Weighted proportion of the evidence that actually turned up.

        confidence = 100 * passed_weight / available_weight

    Unavailable signals are excluded from *both* sides. A phone with no Wi-Fi
    hardware must not be scored as though it failed a Wi-Fi check -- that
    would punish equipment, not conduct.
    """
    hard_fails = [r for r in results if r.hard_fail]
    if hard_fails:
        # Independent of score: these indicate deception rather than thin
        # evidence, and no amount of other signals should outvote them.
        first = hard_fails[0]
        return Assessment(
            confidence=0,
            disposition=Disposition.REJECTED,
            available_weight=0,
            passed_weight=0,
            rejection_reason=f"{first.signal_type}: {first.detail}",
        )

    available = 0
    passed = 0
    for result in results:
        weight = policy.weight_for(result.signal_type)
        if weight <= 0 or result.verdict == Verdict.UNAVAILABLE:
            continue
        available += weight
        if result.verdict == Verdict.PASS:
            passed += weight

    if available == 0:
        return Assessment(
            confidence=0,
            disposition=Disposition.PROVISIONAL,
            available_weight=0,
            passed_weight=0,
            rejection_reason="",
        )

    confidence = round(100 * passed / available)

    if available < policy.min_evidence_weight:
        # A perfect score from one weak signal is not strong evidence. Without
        # this floor, a lone passing time-window check reports 100%.
        disposition = (Disposition.PROVISIONAL if confidence >= policy.review_threshold
                       else Disposition.REJECTED)
        return Assessment(confidence, disposition, available, passed,
                          "insufficient evidence"
                          if disposition == Disposition.REJECTED else "")

    if confidence >= policy.accept_threshold:
        disposition = Disposition.VERIFIED
    elif confidence >= policy.review_threshold:
        disposition = Disposition.PROVISIONAL
    else:
        disposition = Disposition.REJECTED

    return Assessment(
        confidence=confidence,
        disposition=disposition,
        available_weight=available,
        passed_weight=passed,
        rejection_reason=("confidence below the review threshold"
                          if disposition == Disposition.REJECTED else ""),
    )
