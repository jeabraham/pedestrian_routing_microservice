"""OSRM provider.

Defaults to the free FOSSGIS-hosted OSRM foot profile
(https://routing.openstreetmap.de), which unlike the project-osrm.org demo
server actually provides pedestrian routing.  Fair-use only: self-host OSRM
and set ``OSRM_BASE_URL`` for heavy use.
"""

from __future__ import annotations

from collections import defaultdict

from app.geo import LatLon
from app.models import RouteOptions
from app.providers.base import LegRoute, RoutingError, RoutingProvider

_TURN_TYPES = {"turn", "end of road", "fork", "roundabout", "rotary", "roundabout turn", "new name", "continue"}


class OSRMProvider(RoutingProvider):
    id = "osrm"
    name = "OSRM (OpenStreetMap, foot profile)"
    description = (
        "Fast shortest-path walking routes on OpenStreetMap data via the free FOSSGIS OSRM server. "
        "No API key. Does not report way types, so friendliness relies on hills and directness."
    )
    supports = ("walking", "turn_count", "street_names")

    def __init__(self, client, base_url: str, profile: str = "foot"):
        super().__init__(client)
        self.base_url = base_url.rstrip("/")
        self.profile = profile

    async def route_leg(self, start: LatLon, end: LatLon, options: RouteOptions) -> LegRoute:
        coords = f"{start[1]},{start[0]};{end[1]},{end[0]}"
        url = f"{self.base_url}/route/v1/{self.profile}/{coords}"
        data = await self._request_json(
            "GET", url, params={"overview": "full", "geometries": "geojson", "steps": "true"}
        )
        if data.get("code") != "Ok" or not data.get("routes"):
            raise RoutingError(f"{self.name}: {data.get('message') or data.get('code') or 'no route found'}")
        route = data["routes"][0]
        geometry = [(float(lat), float(lon)) for lon, lat, *_ in route["geometry"]["coordinates"]]
        warnings: list[str] = []
        if options.avoid_hills or options.avoid_steps:
            warnings.append("OSRM ignores avoid_hills/avoid_steps; try provider 'valhalla' or 'openrouteservice'.")

        turn_count = 0
        streets: dict[str, float] = defaultdict(float)
        ferry_m = 0.0
        for leg in route.get("legs", []):
            for step in leg.get("steps", []):
                maneuver = step.get("maneuver", {})
                if maneuver.get("type") in _TURN_TYPES and maneuver.get("modifier") not in (None, "straight"):
                    turn_count += 1
                name = (step.get("name") or "").strip()
                if name:
                    streets[name] += float(step.get("distance", 0.0))
                if step.get("mode") == "ferry":
                    ferry_m += float(step.get("distance", 0.0))
        # OSRM has no way types, so a ferry is reported as a warning rather than as the only
        # typed distance (which would skew the way-type score).
        if ferry_m > 0:
            warnings.append(f"Route includes a ferry crossing ({ferry_m:.0f} m).")
        return LegRoute(
            coords=geometry,
            distance_m=float(route.get("distance", 0.0)),
            duration_s=float(route["duration"]) if route.get("duration") is not None else None,
            turn_count=turn_count,
            street_lengths_m=dict(streets),
            warnings=warnings,
        )
