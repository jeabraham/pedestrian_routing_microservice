"""OpenRouteService provider (``foot-walking`` profile).

Requires a free API key from https://openrouteservice.org (``ORS_API_KEY``).
Returns native elevation plus way-type and surface details.
"""

from __future__ import annotations

from collections import defaultdict

from app.geo import LatLon
from app.models import RouteOptions
from app.providers.base import (
    BUSY_ROAD,
    CYCLEWAY,
    FERRY,
    PATH,
    PEDESTRIAN,
    QUIET_STREET,
    STEPS,
    UNKNOWN,
    LegRoute,
    RoutingError,
    RoutingProvider,
    distances_by_ranges,
)

ORS_WAYTYPES = {
    1: BUSY_ROAD,  # state road
    2: BUSY_ROAD,  # road
    3: QUIET_STREET,  # street
    4: PATH,
    5: PATH,  # track
    6: CYCLEWAY,
    7: PEDESTRIAN,  # footway
    8: STEPS,
    9: FERRY,
}

ORS_SURFACES = {
    0: "unknown",
    1: "paved",
    2: "unpaved",
    3: "asphalt",
    4: "concrete",
    5: "cobblestone",
    6: "metal",
    7: "wood",
    8: "compacted_gravel",
    9: "fine_gravel",
    10: "gravel",
    11: "dirt",
    12: "ground",
    13: "ice",
    14: "paving_stones",
    15: "sand",
    16: "woodchips",
    17: "grass",
    18: "grass_paver",
}

_TURN_TYPES = {0, 1, 2, 3, 4, 5, 7, 9, 12, 13}


class OpenRouteServiceProvider(RoutingProvider):
    id = "openrouteservice"
    name = "OpenRouteService (foot-walking)"
    description = (
        "HeiGIT OpenRouteService walking profile with native elevation, way types (footway, street, road, steps) "
        "and surfaces. Supports avoid_steps. Requires ORS_API_KEY (free tier available)."
    )
    requires_api_key = True
    supports = ("walking", "avoid_steps", "elevation", "way_types", "surfaces", "turn_count", "street_names")
    attribution = "Routing © openrouteservice.org by HeiGIT | Map data © OpenStreetMap contributors"

    def __init__(self, client, base_url: str, api_key: str | None):
        super().__init__(client)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def available(self) -> bool:
        return bool(self.api_key)

    async def route_leg(self, start: LatLon, end: LatLon, options: RouteOptions) -> LegRoute:
        if not self.api_key:
            raise RoutingError(f"{self.name}: ORS_API_KEY is not configured")
        body: dict = {
            "coordinates": [[start[1], start[0]], [end[1], end[0]]],
            "elevation": True,
            "instructions": True,
            "extra_info": ["waytype", "surface"],
            "units": "m",
        }
        warnings: list[str] = []
        if options.avoid_steps:
            body["options"] = {"avoid_features": ["steps"]}
        if options.avoid_hills:
            warnings.append("OpenRouteService foot-walking does not support avoid_hills; try 'valhalla'.")
        data = await self._request_json(
            "POST",
            f"{self.base_url}/v2/directions/foot-walking/geojson",
            json=body,
            headers={"Authorization": self.api_key, "Accept": "application/geo+json, application/json"},
        )
        features = data.get("features") if isinstance(data, dict) else None
        if not features:
            raise RoutingError(f"{self.name}: no route found")
        feature = features[0]
        raw = feature["geometry"]["coordinates"]
        coords = [(float(c[1]), float(c[0])) for c in raw]
        elevations = [float(c[2]) for c in raw] if raw and all(len(c) > 2 for c in raw) else None
        props = feature.get("properties", {})
        summary = props.get("summary", {})

        turn_count = 0
        streets: dict[str, float] = defaultdict(float)
        for segment in props.get("segments", []):
            for step in segment.get("steps", []):
                if step.get("type") in _TURN_TYPES:
                    turn_count += 1
                name = (step.get("name") or "").strip()
                if name and name != "-":
                    streets[name] += float(step.get("distance", 0.0))

        extras = props.get("extras", {})
        way_types = distances_by_ranges(
            coords, extras.get("waytypes", {}).get("values", []), lambda v: ORS_WAYTYPES.get(int(v), UNKNOWN)
        )
        surfaces = distances_by_ranges(
            coords, extras.get("surface", {}).get("values", []), lambda v: ORS_SURFACES.get(int(v), "unknown")
        )
        return LegRoute(
            coords=coords,
            distance_m=float(summary.get("distance", 0.0)),
            duration_s=float(summary["duration"]) if summary.get("duration") is not None else None,
            elevations=elevations,
            way_types_m=way_types,
            surfaces_m=surfaces,
            turn_count=turn_count,
            street_lengths_m=dict(streets),
            warnings=warnings,
        )
