"""
API views — port of the Express route handlers in index.js.
"""

from __future__ import annotations

import base64
import time

from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from .osrm import fetch_driving_route
from .planner import build_trip_plan
from .serializers import RouteRequestSerializer


@api_view(["GET"])
def health(request: Request) -> Response:
    """GET /api/health"""
    return Response({"status": "ok", "service": "route-pilot-api"})


@api_view(["POST"])
def login(request: Request) -> Response:
    """POST /api/auth/login"""
    email = request.data.get("email", "").strip()
    password = request.data.get("password", "").strip()

    if not email or not password:
        return Response(
            {"error": "Email and password are required."},
            status=400,
        )

    token = base64.urlsafe_b64encode(f"{email}:{int(time.time() * 1000)}".encode()).decode()
    name = email.split("@")[0] or "Route Planner"

    return Response({"token": token, "user": {"email": email, "name": name}})


@api_view(["POST"])
def route(request: Request) -> Response:
    """POST /api/route"""
    serializer = RouteRequestSerializer(data=request.data)

    if not serializer.is_valid():
        return Response(
            {
                "error": "Invalid route request. Coordinates must be [lng, lat].",
                "details": serializer.errors,
            },
            status=400,
        )

    data = serializer.validated_data

    start_time = data.get("start_time")
    start_time_str = start_time.isoformat() if start_time else None

    try:
        driving_route = fetch_driving_route([
            data["current_location"],
            data["pickup_location"],
            data["dropoff_location"],
        ])
        trip_plan = build_trip_plan(
            driving_route,
            {
                "currentCycleUsed": data.get("current_cycle_used", 0),
                "startTime": start_time_str,
            },
        )
        return Response(trip_plan)

    except (ValueError, RuntimeError) as exc:
        message = str(exc)
        status = 409 if "Insufficient 70-hour cycle" in message else 502
        return Response({"error": message}, status=status)
