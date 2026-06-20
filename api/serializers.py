"""
Request validation serializers.
Replaces the zod schemas in index.js.
"""

from rest_framework import serializers


class CoordinateField(serializers.ListField):
    """Validates a [lng, lat] pair."""

    child = serializers.FloatField()

    def __init__(self, **kwargs):
        kwargs.setdefault("min_length", 2)
        kwargs.setdefault("max_length", 2)
        super().__init__(**kwargs)

    def validate(self, value):
        value = super().validate(value)
        lng, lat = value
        if not (-180 <= lng <= 180):
            raise serializers.ValidationError("Longitude must be between -180 and 180.")
        if not (-90 <= lat <= 90):
            raise serializers.ValidationError("Latitude must be between -90 and 90.")
        return value


class RouteRequestSerializer(serializers.Serializer):
    current_location = CoordinateField()
    pickup_location = CoordinateField()
    dropoff_location = CoordinateField()
    current_cycle_used = serializers.FloatField(min_value=0, max_value=70, required=False, default=0)
    start_time = serializers.DateTimeField(required=False, allow_null=True, default=None)
