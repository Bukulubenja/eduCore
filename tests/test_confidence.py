"""The scoring rules of ADR-0002, in isolation from the database."""

from __future__ import annotations

import pytest

from educore.presence.confidence import score
from educore.presence.evaluators import SignalResult
from educore.presence.models import Disposition, SignalType, Verdict, default_weights


class FakePolicy:
    def __init__(self, **overrides):
        self.weights = default_weights()
        self.accept_threshold = 75
        self.review_threshold = 45
        self.min_evidence_weight = 40
        for key, value in overrides.items():
            setattr(self, key, value)

    def weight_for(self, signal_type):
        return int(self.weights.get(signal_type, 0))


def signal(signal_type, verdict, hard_fail=False):
    return SignalResult(signal_type, verdict, hard_fail=hard_fail)


def test_full_evidence_verifies():
    results = [
        signal(SignalType.QR, Verdict.PASS),
        signal(SignalType.DEVICE, Verdict.PASS),
        signal(SignalType.GEOFENCE, Verdict.PASS),
        signal(SignalType.TIME_WINDOW, Verdict.PASS),
    ]
    assessment = score(results, FakePolicy())
    assert assessment.confidence == 100
    assert assessment.disposition == Disposition.VERIFIED


def test_unavailable_signals_are_excluded_not_counted_as_failures():
    """The central correction of ADR-0002.

    Wi-Fi that is absent must not score like Wi-Fi that failed, or a phone
    with no Wi-Fi chip is permanently penalised for its hardware.
    """
    present = [
        signal(SignalType.QR, Verdict.PASS),
        signal(SignalType.DEVICE, Verdict.PASS),
        signal(SignalType.WIFI, Verdict.UNAVAILABLE),
        signal(SignalType.GEOFENCE, Verdict.UNAVAILABLE),
        signal(SignalType.TIME_WINDOW, Verdict.PASS),
    ]
    assessment = score(present, FakePolicy())

    assert assessment.available_weight == 35 + 25 + 10
    assert assessment.confidence == 100
    assert assessment.disposition == Disposition.VERIFIED


def test_six_layer_and_gate_would_have_blocked_this_check_in():
    """The scenario that motivated the redesign.

    Two sensors unavailable and one weak signal failing. Under an AND-gate
    this is a locked-out teacher; here it is a recorded arrival that a human
    glances at.
    """
    results = [
        signal(SignalType.QR, Verdict.PASS),
        signal(SignalType.DEVICE, Verdict.PASS),
        signal(SignalType.GEOFENCE, Verdict.UNAVAILABLE),
        signal(SignalType.WIFI, Verdict.UNAVAILABLE),
        signal(SignalType.TIME_WINDOW, Verdict.FAIL),
    ]
    assessment = score(results, FakePolicy())

    assert assessment.confidence == 86           # 60 of 70 available weight
    assert assessment.disposition == Disposition.VERIFIED
    assert assessment.accepted


def test_thin_evidence_cannot_reach_verified():
    """A perfect score over one weak signal is not strong evidence."""
    results = [
        signal(SignalType.TIME_WINDOW, Verdict.PASS),
        signal(SignalType.QR, Verdict.UNAVAILABLE),
        signal(SignalType.DEVICE, Verdict.UNAVAILABLE),
    ]
    assessment = score(results, FakePolicy())

    assert assessment.confidence == 100
    assert assessment.available_weight == 10
    assert assessment.disposition == Disposition.PROVISIONAL


def test_no_evidence_at_all_is_provisional_not_rejected():
    assessment = score([signal(SignalType.QR, Verdict.UNAVAILABLE)], FakePolicy())
    assert assessment.disposition == Disposition.PROVISIONAL
    assert assessment.accepted


@pytest.mark.parametrize("failing", [SignalType.QR, SignalType.DEVICE,
                                     SignalType.GEOFENCE])
def test_hard_fail_rejects_regardless_of_other_evidence(failing):
    results = [
        signal(SignalType.QR, Verdict.PASS),
        signal(SignalType.DEVICE, Verdict.PASS),
        signal(SignalType.GEOFENCE, Verdict.PASS),
        signal(SignalType.TIME_WINDOW, Verdict.PASS),
        signal(failing, Verdict.FAIL, hard_fail=True),
    ]
    assessment = score(results, FakePolicy())

    assert assessment.disposition == Disposition.REJECTED
    assert assessment.confidence == 0
    assert failing in assessment.rejection_reason


def test_middling_evidence_is_provisional():
    results = [
        signal(SignalType.QR, Verdict.UNAVAILABLE),
        signal(SignalType.DEVICE, Verdict.PASS),
        signal(SignalType.GEOFENCE, Verdict.FAIL),
        signal(SignalType.TIME_WINDOW, Verdict.PASS),
    ]
    assessment = score(results, FakePolicy())

    assert assessment.confidence == 70           # 35 of 50
    assert assessment.disposition == Disposition.PROVISIONAL


def test_weights_are_school_policy_not_code():
    """A school with no Wi-Fi and no beacons must still reach verified."""
    weights = default_weights()
    weights[SignalType.WIFI] = 0
    weights[SignalType.BEACON] = 0
    policy = FakePolicy(weights=weights)

    results = [
        signal(SignalType.QR, Verdict.PASS),
        signal(SignalType.DEVICE, Verdict.PASS),
        signal(SignalType.TIME_WINDOW, Verdict.PASS),
        signal(SignalType.WIFI, Verdict.FAIL),      # zero-weighted: ignored
    ]
    assessment = score(results, policy)

    assert assessment.disposition == Disposition.VERIFIED
    assert assessment.available_weight == 70
