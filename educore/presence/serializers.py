"""Presence API serialisers."""

from __future__ import annotations

from rest_framework import serializers

from .models import (
    AttendanceEvent,
    AttendanceException,
    AttendanceRecord,
    AttendanceSignal,
    SignalType,
)


class SignalSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttendanceSignal
        fields = ["signal_type", "verdict", "weight_applied", "hard_fail",
                  "detail", "payload"]


class EventSerializer(serializers.ModelSerializer):
    signals = SignalSerializer(many=True, read_only=True)

    class Meta:
        model = AttendanceEvent
        fields = ["id", "kind", "captured_at", "received_at",
                  "clock_skew_seconds", "disposition", "confidence",
                  "rejection_reason", "policy_version", "signals"]


class RecordSerializer(serializers.ModelSerializer):
    needs_review = serializers.BooleanField(read_only=True)
    membership_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = AttendanceRecord
        fields = ["id", "membership_id", "date", "status", "disposition",
                  "confidence", "first_in_at", "last_out_at",
                  "minutes_on_site", "resolution", "policy_version",
                  "needs_review"]


class ExceptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttendanceException
        fields = ["id", "record", "raised_by", "reason_code", "narrative",
                  "requested_status", "decision", "decided_by", "decided_at",
                  "decision_note", "created_at"]
        read_only_fields = ["decision", "decided_by", "decided_at",
                            "decision_note", "created_at", "raised_by"]


class RawSignalsField(serializers.DictField):
    """Device-reported evidence: {signal_type: {...}}.

    Validated for shape only. Whether a signal is *true* is the evaluators'
    business, and nothing the client sends is trusted here.
    """

    def to_internal_value(self, data):
        data = super().to_internal_value(data)
        allowed = set(SignalType.values)
        unknown = set(data) - allowed
        if unknown:
            raise serializers.ValidationError(
                f"Unknown signal types: {', '.join(sorted(unknown))}."
            )
        for key, value in data.items():
            if not isinstance(value, dict):
                raise serializers.ValidationError(
                    f"Signal '{key}' must be an object."
                )
        return data


class CheckInSerializer(serializers.Serializer):
    client_event_id = serializers.UUIDField()
    campus_id = serializers.UUIDField()
    captured_at = serializers.DateTimeField()
    signals = RawSignalsField(child=serializers.DictField(), required=False,
                              default=dict)


class AppealSerializer(serializers.Serializer):
    reason_code = serializers.ChoiceField(choices=AttendanceException.Reason.choices)
    narrative = serializers.CharField(required=False, allow_blank=True,
                                      max_length=2000)
    requested_status = serializers.CharField(required=False, allow_blank=True)


class DecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=[
        AttendanceException.Decision.UPHELD,
        AttendanceException.Decision.DECLINED,
    ])
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class SyncOperationSerializer(serializers.Serializer):
    client_event_id = serializers.UUIDField()
    type = serializers.ChoiceField(choices=["attendance.check_in",
                                            "attendance.check_out"])
    captured_at = serializers.DateTimeField()
    payload = serializers.DictField()


class SyncSerializer(serializers.Serializer):
    device_id = serializers.CharField(required=False, allow_blank=True)
    device_time = serializers.DateTimeField(required=False)
    operations = SyncOperationSerializer(many=True)
