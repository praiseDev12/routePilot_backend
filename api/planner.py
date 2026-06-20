from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOS = {
    "cycleLimitHours": 70,
    "cycleWindowDays": 8,
    "maxShiftDrivingHours": 11,
    "dutyWindowHours": 14,
    "breakAfterDrivingHours": 8,
    "breakDurationHours": 0.5,
    "dailyResetHours": 10,
    "pickupLoadingHours": 1,
    "dropoffUnloadingHours": 1,
    "fuelIntervalMiles": 1000,
    "fuelDurationHours": 0.25,
}

STATUS = {
    "driving": "DRIVING",
    "offDuty": "OFF_DUTY",
    "onDuty": "ON_DUTY_NOT_DRIVING",
    "sleeper": "SLEEPER_BERTH",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _round(value: float, precision: int = 2) -> float:
    return round(value, precision)


def _add_hours(dt: datetime, hours: float) -> datetime:
    from datetime import timedelta
    return dt + timedelta(hours=hours)


def _format_event(status: str, start_time: datetime, duration_hours: float, note: str) -> dict:
    end_time = _add_hours(start_time, duration_hours)
    return {
        "status": status,
        "start_time": start_time.isoformat().replace("+00:00", "Z"),
        "end_time": end_time.isoformat().replace("+00:00", "Z"),
        "duration_hours": _round(duration_hours, 2),
        "note": note,
    }


def _haversine_miles(coord1: list, coord2: list) -> float:
    """Return distance in miles between two [lng, lat] coordinates."""
    lng1, lat1 = coord1
    lng2, lat2 = coord2
    radius_miles = 3958.7613

    def to_rad(deg):
        return deg * math.pi / 180

    d_lat = to_rad(lat2 - lat1)
    d_lng = to_rad(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(to_rad(lat1)) * math.cos(to_rad(lat2)) * math.sin(d_lng / 2) ** 2
    )
    return 2 * radius_miles * math.asin(math.sqrt(a))


def _coordinate_at_distance(coordinates: list, target_miles: float):
    if not coordinates:
        return None
    travelled = 0.0
    for i in range(1, len(coordinates)):
        segment = _haversine_miles(coordinates[i - 1], coordinates[i])
        travelled += segment
        if travelled >= target_miles:
            return coordinates[i]
    return coordinates[-1]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class TripState:
    clock: datetime
    events: list = field(default_factory=list)
    cycle_used: float = 0.0
    shift_driving: float = 0.0
    driving_since_break: float = 0.0
    duty_window_elapsed: float = 0.0
    route_driving_hours: float = 0.0
    route_miles_driven: float = 0.0
    next_fuel_at_miles: float = HOS["fuelIntervalMiles"]


# ---------------------------------------------------------------------------
# Core event emitter
# ---------------------------------------------------------------------------


def _push_event(
    state: TripState,
    status: str,
    duration_hours: float,
    note: str,
    stop_collection: list | None = None,
) -> None:
    if duration_hours <= 0:
        return

    event = _format_event(status, state.clock, duration_hours, note)
    state.events.append(event)

    if stop_collection is not None:
        stop_collection.append({
            "type": "daily_reset" if status == STATUS["sleeper"] else "break",
            "start_time": event["start_time"],
            "end_time": event["end_time"],
            "duration_hours": event["duration_hours"],
            "note": note,
        })

    state.clock = _add_hours(state.clock, duration_hours)

    if status == STATUS["driving"]:
        state.shift_driving += duration_hours
        state.driving_since_break += duration_hours
        state.duty_window_elapsed += duration_hours
        state.cycle_used += duration_hours
        state.route_driving_hours += duration_hours
    elif status == STATUS["onDuty"]:
        state.duty_window_elapsed += duration_hours
        state.cycle_used += duration_hours
    elif status == STATUS["offDuty"]:
        state.duty_window_elapsed += duration_hours
        state.driving_since_break = 0.0
    elif status == STATUS["sleeper"]:
        state.shift_driving = 0.0
        state.driving_since_break = 0.0
        state.duty_window_elapsed = 0.0


# ---------------------------------------------------------------------------
# Guard helpers
# ---------------------------------------------------------------------------


def _ensure_cycle_hours(state: TripState, hours: float, activity: str) -> None:
    remaining = HOS["cycleLimitHours"] - state.cycle_used
    if remaining + 0.0001 < hours:
        raise ValueError(
            f"Insufficient 70-hour cycle time for {activity}. "
            f"Remaining cycle hours: {_round(remaining)}."
        )


def _ensure_duty_capacity(state: TripState, hours: float, rest_stops: list) -> None:
    if (
        state.shift_driving >= HOS["maxShiftDrivingHours"]
        or state.duty_window_elapsed + hours > HOS["dutyWindowHours"]
    ):
        _push_event(
            state,
            STATUS["sleeper"],
            HOS["dailyResetHours"],
            "10-hour sleeper berth reset after daily duty limit",
            rest_stops,
        )


def _add_on_duty_event(
    state: TripState, hours: float, note: str, rest_stops: list
) -> None:
    _ensure_cycle_hours(state, hours, note)
    _ensure_duty_capacity(state, hours, rest_stops)
    _ensure_cycle_hours(state, hours, note)
    _push_event(state, STATUS["onDuty"], hours, note)


def _add_fuel_stop(
    state: TripState,
    fuel_stops: list,
    rest_stops: list,
    route_coordinates: list,
) -> None:
    _ensure_cycle_hours(state, HOS["fuelDurationHours"], "fuel stop")
    _ensure_duty_capacity(state, HOS["fuelDurationHours"], rest_stops)

    coordinate = _coordinate_at_distance(route_coordinates, state.next_fuel_at_miles)
    event_start = state.clock.isoformat().replace("+00:00", "Z")
    _push_event(
        state,
        STATUS["onDuty"],
        HOS["fuelDurationHours"],
        "Fuel stop required every 1,000 miles",
    )
    fuel_stops.append({
        "mile_marker": _round(state.next_fuel_at_miles, 1),
        "location": coordinate,
        "start_time": event_start,
        "duration_hours": HOS["fuelDurationHours"],
        "note": "Fueling interval compliance stop",
    })
    state.next_fuel_at_miles += HOS["fuelIntervalMiles"]


# ---------------------------------------------------------------------------
# Driving leg planner
# ---------------------------------------------------------------------------


def _plan_driving_leg(
    state: TripState,
    leg: dict,
    note: str,
    rest_stops: list,
    fuel_stops: list,
    route_coordinates: list,
) -> None:
    remaining_drive_hours = leg["duration_minutes"] / 60
    leg_miles = leg["distance_km"] / 1.609344
    mph = leg_miles / remaining_drive_hours if remaining_drive_hours > 0 else 0

    while remaining_drive_hours > 0.001:
        if state.route_miles_driven + 0.001 >= state.next_fuel_at_miles:
            _add_fuel_stop(state, fuel_stops, rest_stops, route_coordinates)
            continue

        if state.driving_since_break >= HOS["breakAfterDrivingHours"]:
            _push_event(
                state,
                STATUS["offDuty"],
                HOS["breakDurationHours"],
                "30-minute break after 8 hours of driving",
                rest_stops,
            )

        _ensure_duty_capacity(state, 0.01, rest_stops)

        cycle_remaining = HOS["cycleLimitHours"] - state.cycle_used
        shift_remaining = HOS["maxShiftDrivingHours"] - state.shift_driving
        duty_remaining = HOS["dutyWindowHours"] - state.duty_window_elapsed
        break_remaining = HOS["breakAfterDrivingHours"] - state.driving_since_break
        fuel_remaining_miles = state.next_fuel_at_miles - state.route_miles_driven
        fuel_remaining_hours = fuel_remaining_miles / mph if mph > 0 else float("inf")

        driving_hours = min(
            remaining_drive_hours,
            cycle_remaining,
            shift_remaining,
            duty_remaining,
            break_remaining,
            fuel_remaining_hours,
        )

        if driving_hours <= 0.001:
            if cycle_remaining <= 0.001:
                _ensure_cycle_hours(state, 0.01, "remaining route driving")

            if shift_remaining <= 0.001 or duty_remaining <= 0.001:
                _push_event(
                    state,
                    STATUS["sleeper"],
                    HOS["dailyResetHours"],
                    "10-hour sleeper berth reset after daily driving limit",
                    rest_stops,
                )
                continue

            if break_remaining <= 0.001:
                _push_event(
                    state,
                    STATUS["offDuty"],
                    HOS["breakDurationHours"],
                    "30-minute break after 8 hours of driving",
                    rest_stops,
                )
                continue

            driving_hours = min(remaining_drive_hours, cycle_remaining)

        _ensure_cycle_hours(state, driving_hours, "route driving")
        _push_event(state, STATUS["driving"], driving_hours, note)
        remaining_drive_hours -= driving_hours
        miles_this_segment = driving_hours * mph
        state.route_miles_driven += miles_this_segment

        if state.route_miles_driven + 0.001 >= state.next_fuel_at_miles and remaining_drive_hours > 0.001:
            _add_fuel_stop(state, fuel_stops, rest_stops, route_coordinates)


# ---------------------------------------------------------------------------
# Daily log builder
# ---------------------------------------------------------------------------


def _split_event_by_date(event: dict, totals_by_date: dict) -> None:
    from datetime import timedelta

    cursor = datetime.fromisoformat(event["start_time"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(event["end_time"].replace("Z", "+00:00"))

    while cursor < end:
        next_midnight = cursor.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        segment_end = next_midnight if next_midnight < end else end
        date = cursor.strftime("%Y-%m-%d")
        hours = (segment_end - cursor).total_seconds() / 3600

        if date not in totals_by_date:
            totals_by_date[date] = {
                "DRIVING": 0.0,
                "OFF_DUTY": 0.0,
                "ON_DUTY_NOT_DRIVING": 0.0,
                "SLEEPER_BERTH": 0.0,
            }

        totals_by_date[date][event["status"]] += hours
        cursor = segment_end


def _build_daily_logs(events: list) -> list:
    totals_by_date: dict[str, dict] = {}
    for event in events:
        _split_event_by_date(event, totals_by_date)

    return [
        {
            "day": i + 1,
            "date": date,
            "totals": {
                "DRIVING": _round(totals["DRIVING"]),
                "OFF_DUTY": _round(totals["OFF_DUTY"]),
                "ON_DUTY_NOT_DRIVING": _round(totals["ON_DUTY_NOT_DRIVING"]),
                "SLEEPER_BERTH": _round(totals["SLEEPER_BERTH"]),
            },
        }
        for i, (date, totals) in enumerate(totals_by_date.items())
    ]


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def build_trip_plan(route: dict, options: dict | None = None) -> dict:
    options = options or {}
    current_cycle_used = float(options.get("currentCycleUsed", 0))

    if not (0 <= current_cycle_used <= HOS["cycleLimitHours"]):
        raise ValueError("Current cycle used must be between 0 and 70 hours.")

    start_time_raw = options.get("startTime")
    if start_time_raw:
        clock = datetime.fromisoformat(start_time_raw.replace("Z", "+00:00"))
    else:
        clock = datetime.now(tz=timezone.utc)

    state = TripState(
        clock=clock,
        cycle_used=current_cycle_used,
    )
    fuel_stops: list = []
    rest_stops: list = []

    to_pickup, to_dropoff = route["legs"][0], route["legs"][1]
    coordinates = route["geometry"]["coordinates"]

    _plan_driving_leg(state, to_pickup, "Drive to pickup", rest_stops, fuel_stops, coordinates)
    _add_on_duty_event(state, HOS["pickupLoadingHours"], "Pickup loading time", rest_stops)
    _plan_driving_leg(state, to_dropoff, "Drive to dropoff", rest_stops, fuel_stops, coordinates)
    _add_on_duty_event(state, HOS["dropoffUnloadingHours"], "Dropoff unloading time", rest_stops)

    daily_logs = _build_daily_logs(state.events)

    total_driving_hours = _round(
        sum(e["duration_hours"] for e in state.events if e["status"] == STATUS["driving"])
    )
    total_on_duty_hours = _round(
        sum(
            e["duration_hours"]
            for e in state.events
            if e["status"] in (STATUS["driving"], STATUS["onDuty"])
        )
    )
    total_off_duty_hours = _round(
        sum(
            e["duration_hours"]
            for e in state.events
            if e["status"] in (STATUS["offDuty"], STATUS["sleeper"])
        )
    )
    total_sleeper_hours = _round(
        sum(e["duration_hours"] for e in state.events if e["status"] == STATUS["sleeper"])
    )

    return {
        "summary": {
            "assumptions": {
                "driver_type": "Property-carrying driver",
                "cycle_rule": f"{HOS['cycleLimitHours']} hours / {HOS['cycleWindowDays']} day cycle",
                "adverse_driving_conditions": False,
                "fuel_interval_miles": HOS["fuelIntervalMiles"],
                "pickup_loading_hours": HOS["pickupLoadingHours"],
                "dropoff_unloading_hours": HOS["dropoffUnloadingHours"],
            },
            "current_cycle_used": _round(current_cycle_used),
            "remaining_cycle_hours": _round(HOS["cycleLimitHours"] - state.cycle_used),
            "cycle_limit_hours": HOS["cycleLimitHours"],
            "total_driving_hours": total_driving_hours,
            "total_on_duty_hours": total_on_duty_hours,
            "total_off_duty_hours": total_off_duty_hours,
            "total_sleeper_berth_hours": total_sleeper_hours,
            "fuel_stop_count": len(fuel_stops),
            "rest_stop_count": len(rest_stops),
        },
        "route": route,
        "fuel_stops": fuel_stops,
        "rest_stops": rest_stops,
        "eld_events": state.events,
        "daily_logs": daily_logs,
    }
