"""
OSRM driving-route client.
Port of osrm.js — identical request / response shape.
"""

from __future__ import annotations

import urllib.parse

import requests
from django.conf import settings


def _format_coordinate(coord: list) -> str:
    """[lng, lat] → 'lng,lat'"""
    return f"{coord[0]},{coord[1]}"


def _to_lat_lng(coord: list) -> list:
    """[lng, lat] → [lat, lng]"""
    return [coord[1], coord[0]]


def fetch_driving_route(locations: list[list]) -> dict:
    """
    Fetch a driving route from OSRM for the given [lng, lat] waypoints.

    Returns a dict matching the shape produced by the original osrm.js:
        {
            geometry, polyline, distance_km, distance_miles,
            duration_minutes, waypoints, legs
        }
    """
    osrm_base = getattr(settings, "OSRM_BASE_URL", "https://router.project-osrm.org")
    coordinate_path = ";".join(_format_coordinate(loc) for loc in locations)
    url = (
        f"{osrm_base}/route/v1/driving/{coordinate_path}"
        "?overview=full&geometries=geojson&steps=true"
    )

    try:
        response = requests.get(
            url,
            headers={"User-Agent": "RoutePilotDemo/1.0"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"OSRM request failed: {exc}") from exc

    if not response.ok:
        raise RuntimeError(f"OSRM request failed with status {response.status_code}")

    data = response.json()

    if data.get("code") != "Ok" or not data.get("routes", [{}])[0].get("geometry", {}).get("coordinates"):
        raise RuntimeError(data.get("message") or "OSRM did not return a usable route")

    route = data["routes"][0]

    return {
        "geometry": route["geometry"],
        "polyline": [_to_lat_lng(c) for c in route["geometry"]["coordinates"]],
        "distance_km": round(route["distance"] / 1000, 2),
        "distance_miles": round(route["distance"] / 1609.344, 2),
        "duration_minutes": round(route["duration"] / 60, 1),
        "waypoints": [
            {
                "name": wp["name"],
                "location": wp["location"],
                "lat_lng": _to_lat_lng(wp["location"]),
            }
            for wp in data["waypoints"]
        ],
        "legs": [
            {
                "distance_km": round(leg["distance"] / 1000, 2),
                "duration_minutes": round(leg["duration"] / 60, 1),
                "summary": leg.get("summary", ""),
            }
            for leg in route["legs"]
        ],
    }
