"""Signal evaluators: turn what a device reported into evidence.

Each evaluator answers one question and returns pass / fail / unavailable.
None of them decides the outcome -- that is confidence.py's job. Keeping the
two apart is what allows a school to reweight signals without touching the
logic that reads a GPS fix.

The distinction that matters most here is fail versus unavailable:

    fail         the evidence says this person was not where they claim
    unavailable  there is no evidence either way

Treating the second as the first is how a system starts accusing people whose
phone simply has no Wi-Fi chip, and false accusations destroy the willingness
to use the tool at all (ADR-0002).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django.utils import timezone

from .models import SignalType, Verdict


@dataclass
class SignalResult:
    signal_type: str
    verdict: str
    detail: str = ""
    hard_fail: bool = False
    payload: dict = field(default_factory=dict)


@dataclass
class EvaluationContext:
    """Everything an evaluator may look at. Deliberately explicit."""

    school_id: object
    membership: object
    campus: object
    policy: object
    kind: str
    captured_at: datetime
    received_at: datetime
    raw_signals: dict                      # signal_type -> device-supplied payload
    device: object = None
    device_error: str = ""
    qr_error: str = ""
    qr_nonce: str = ""

    @property
    def clock_skew_seconds(self) -> int:
        return int((self.received_at - self.captured_at).total_seconds())


# -- Individual evaluators ---------------------------------------------------


def evaluate_qr(ctx: EvaluationContext) -> SignalResult:
    """A server-signed, 30-second, single-use-per-person code.

    The workhorse: it is the only cheap signal that defeats the dominant
    real-world attack, which is photographing a static code and reusing it.
    """
    if SignalType.QR not in ctx.raw_signals:
        return SignalResult(SignalType.QR, Verdict.UNAVAILABLE, "not scanned")

    if ctx.qr_error:
        # A presented token that does not verify is not weak evidence; it is
        # an attempt to use a code that is expired, forged, already spent by
        # this person, or issued for somewhere else.
        return SignalResult(SignalType.QR, Verdict.FAIL, ctx.qr_error,
                            hard_fail=True)

    return SignalResult(SignalType.QR, Verdict.PASS, "valid token",
                        payload={"nonce": ctx.qr_nonce})


def evaluate_device(ctx: EvaluationContext) -> SignalResult:
    if SignalType.DEVICE not in ctx.raw_signals:
        return SignalResult(SignalType.DEVICE, Verdict.UNAVAILABLE,
                            "no device fingerprint supplied")

    if ctx.device_error:
        return SignalResult(SignalType.DEVICE, Verdict.FAIL, ctx.device_error,
                            hard_fail=True)

    attested = bool(ctx.raw_signals[SignalType.DEVICE].get("attestation"))
    if not attested and not ctx.device.attested:
        # Not a hard fail: attestation is unavailable on some legitimate
        # devices and unreliable on rooted-but-honest ones. It is a
        # confidence question, not an accusation.
        return SignalResult(SignalType.DEVICE, Verdict.PASS,
                            "registered device, unattested")

    return SignalResult(SignalType.DEVICE, Verdict.PASS, "registered and attested")


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def evaluate_geofence(ctx: EvaluationContext) -> SignalResult:
    raw = ctx.raw_signals.get(SignalType.GEOFENCE)
    if not raw:
        return SignalResult(SignalType.GEOFENCE, Verdict.UNAVAILABLE, "no fix")

    campus = ctx.campus
    if campus.latitude is None or campus.longitude is None:
        return SignalResult(SignalType.GEOFENCE, Verdict.UNAVAILABLE,
                            "campus has no coordinates")

    try:
        lat = float(raw["lat"])
        lon = float(raw["lon"])
        accuracy = float(raw.get("accuracy_m", 9_999))
    except (KeyError, TypeError, ValueError):
        return SignalResult(SignalType.GEOFENCE, Verdict.UNAVAILABLE,
                            "malformed fix")

    if raw.get("mocked"):
        return SignalResult(SignalType.GEOFENCE, Verdict.FAIL,
                            "location reported as mocked", hard_fail=True,
                            payload={"mocked": True})

    distance = _haversine_m(lat, lon, float(campus.latitude), float(campus.longitude))
    inside = distance <= ctx.policy.geofence_radius_m
    payload = {"distance_m": round(distance, 1), "accuracy_m": accuracy}

    if inside:
        return SignalResult(SignalType.GEOFENCE, Verdict.PASS,
                            f"{distance:.0f}m from campus centre", payload=payload)

    if accuracy < ctx.policy.geofence_trusted_accuracy_m:
        # Confident fix, confidently outside.
        return SignalResult(SignalType.GEOFENCE, Verdict.FAIL,
                            f"{distance:.0f}m outside the fence "
                            f"(accuracy {accuracy:.0f}m)",
                            hard_fail=True, payload=payload)

    # Outside, but the fix is too vague to prove anything.
    return SignalResult(SignalType.GEOFENCE, Verdict.UNAVAILABLE,
                        f"fix too imprecise to rely on (accuracy {accuracy:.0f}m)",
                        payload=payload)


def evaluate_wifi(ctx: EvaluationContext) -> SignalResult:
    raw = ctx.raw_signals.get(SignalType.WIFI)
    if not raw:
        return SignalResult(SignalType.WIFI, Verdict.UNAVAILABLE, "not connected")

    known = {b.lower() for b in (ctx.campus.wifi_bssid_hashes or [])}
    if not known:
        return SignalResult(SignalType.WIFI, Verdict.UNAVAILABLE,
                            "campus has no registered access points")

    supplied = str(raw.get("bssid_hash", "")).lower()
    if supplied and supplied in known:
        return SignalResult(SignalType.WIFI, Verdict.PASS, "school access point")

    # Being on another network is not evidence of absence -- mobile data works
    # inside the staff room too.
    return SignalResult(SignalType.WIFI, Verdict.UNAVAILABLE,
                        "not on a registered school access point")


def _duty_window(ctx: EvaluationContext) -> tuple[time, time]:
    from .models import DutySchedule

    local_day = timezone.localtime(ctx.captured_at).weekday()
    schedule = (
        DutySchedule.objects.filter(membership=ctx.membership, weekday=local_day)
        .first()
    )
    if schedule is not None:
        return schedule.starts_at, schedule.ends_at
    return ctx.policy.day_starts_at, ctx.policy.day_ends_at


def evaluate_time_window(ctx: EvaluationContext) -> SignalResult:
    starts_at, ends_at = _duty_window(ctx)
    local = timezone.localtime(ctx.captured_at)
    at = local.time()

    earliest = (datetime.combine(local.date(), starts_at)
                - timedelta(minutes=ctx.policy.early_checkin_minutes)).time()

    if ctx.kind == "check_in":
        if at < earliest:
            return SignalResult(SignalType.TIME_WINDOW, Verdict.FAIL,
                                f"check-in at {at:%H:%M}, before the school opens")
        if at > ends_at:
            return SignalResult(SignalType.TIME_WINDOW, Verdict.FAIL,
                                f"check-in at {at:%H:%M}, after the duty day ends")
        return SignalResult(SignalType.TIME_WINDOW, Verdict.PASS,
                            f"within duty window ({starts_at:%H:%M}-{ends_at:%H:%M})")

    if at < starts_at:
        return SignalResult(SignalType.TIME_WINDOW, Verdict.FAIL,
                            f"check-out at {at:%H:%M}, before the day began")
    return SignalResult(SignalType.TIME_WINDOW, Verdict.PASS, "plausible check-out")


def evaluate_beacon(ctx: EvaluationContext) -> SignalResult:
    raw = ctx.raw_signals.get(SignalType.BEACON)
    if not raw:
        return SignalResult(SignalType.BEACON, Verdict.UNAVAILABLE,
                            "no beacon in range")

    known = {b.lower() for b in (ctx.campus.beacon_ids or [])}
    supplied = str(raw.get("beacon_id", "")).lower()
    if not known:
        return SignalResult(SignalType.BEACON, Verdict.UNAVAILABLE,
                            "campus has no registered beacons")
    if supplied in known:
        return SignalResult(SignalType.BEACON, Verdict.PASS,
                            f"in range of {supplied}")
    return SignalResult(SignalType.BEACON, Verdict.UNAVAILABLE,
                        "beacon not recognised")


def evaluate_face(ctx: EvaluationContext) -> SignalResult:
    """Present so the pipeline has a slot for it. Deliberately inert.

    Enabling this requires a completed DPIA, per-school and per-person opt-in,
    and a new ADR superseding ADR-0004. It is not a configuration change.
    """
    return SignalResult(SignalType.FACE, Verdict.UNAVAILABLE,
                        "biometric verification is not enabled")


EVALUATORS = {
    SignalType.QR: evaluate_qr,
    SignalType.DEVICE: evaluate_device,
    SignalType.GEOFENCE: evaluate_geofence,
    SignalType.WIFI: evaluate_wifi,
    SignalType.TIME_WINDOW: evaluate_time_window,
    SignalType.BEACON: evaluate_beacon,
    SignalType.FACE: evaluate_face,
}


def evaluate_all(ctx: EvaluationContext) -> list[SignalResult]:
    """Run every evaluator whose signal carries weight under this policy."""
    results = []
    for signal_type, evaluator in EVALUATORS.items():
        if ctx.policy.weight_for(signal_type) <= 0:
            continue
        results.append(evaluator(ctx))
    return results


def payload_digest(raw_signals: dict) -> str:
    """Stable digest of what the device claimed.

    Lets a replayed client_event_id carrying *different* evidence be told
    apart from an honest network retry carrying the same evidence.
    """
    import json

    canonical = json.dumps(raw_signals, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
