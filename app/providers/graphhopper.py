"""GraphHopper provider (``foot`` profile).

Requires an API key from https://graphhopper.com (``GRAPHHOPPER_API_KEY``);
``GRAPHHOPPER_BASE_URL`` can point to a self-hosted instance instead.
Returns native elevation, road classes and surfaces.
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

_ROAD_CLASSES = {
    "motorway": BUSY_ROAD,
    "trunk": BUSY_ROAD,
    "primary": BUSY_ROAD,
    "secondary": BUSY_ROAD,
    "tertiary": BUSY_ROAD,
    "residential": QUIET_STREET,
    "living_street": QUIET_STREET,
    "service": QUIET_STREET,
    "unclassified": QUIET_STREET,
    "road": QUIET_STREET,
    "footway": PEDESTRIAN,
    "pedestrian": PEDESTRIAN,
    "corridor": PEDESTRIAN,
    "path": PATH,
    "track": PATH,
    "bridleway": PATH,
    "steps": STEPS,
    "cycleway": CYCLEWAY,
    "ferry": FERRY,
}

_TURN_SIGNS = {-98, -8, -7, -3, -2, -1, 1, 2, 3, 6, 7, 8}


def classify_graphhopper_road_class(value: object) -> str:
    return _ROAD_CLASSES.get(str(value).lower(), UNKNOWN)


class GraphHopperProvider(RoutingProvider):
    id = "graphhopper"
    name = "GraphHopper (foot)"
    description = (
        "GraphHopper Directions API walking profile with native elevation, road classes and surfaces. "
        "Supports avoid_steps. Requires GRAPHHOPPER_API_KEY (free tier available) or a self-hosted server."
    )
    requires_api_key = True
    supports = ("walking", "avoid_steps", "elevation", "way_types", "surfaces", "turn_count", "street_names")
    attribution = "Routing © GraphHopper | Map data © OpenStreetMap contributors"

    def __init__(self, client, base_url: str, api_key: str | None):
        super().__init__(client)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def available(self) -> bool:
        # A self-hosted GraphHopper does not need a key.
        return bool(self.api_key) or "graphhopper.com" not in self.base_url

    async def route_leg(self, start: LatLon, end: LatLon, options: RouteOptions) -> LegRoute:
        if not self.available():
            raise RoutingError(f"{self.name}: GRAPHHOPPER_API_KEY is not configured")
        body: dict = {
            "points": [[start[1], start[0]], [end[1], end[0]]],
            "profile": "foot",
            "points_encoded": False,
            "elevation": True,
            "instructions": True,
            "locale": "en",
            "details": ["road_class", "surface"],
        }
        warnings: list[str] = []
        if options.avoid_steps:
            body["ch.disable"] = True
            body["custom_model"] = {"priority": [{"if": "road_class == STEPS", "multiply_by": "0.05"}]}
        if options.avoid_hills:
            warnings.append("GraphHopper provider does not implement avoid_hills; try 'valhalla'.")
        params = {"key": self.api_key} if self.api_key else None
        data = await self._request_json("POST", f"{self.base_url}/route", json=body, params=params)
        paths = data.get("paths") if isinstance(data, dict) else None
        if not paths:
            raise RoutingError(f"{self.name}: no route found")
        path = paths[0]
        raw = path["points"]["coordinates"]
        coords = [(float(c[1]), float(c[0])) for c in raw]
        elevations = [float(c[2]) for c in raw] if raw and all(len(c) > 2 for c in raw) else None

        turn_count = 0
        streets: dict[str, float] = defaultdict(float)
        for instr in path.get("instructions", []):
            if instr.get("sign") in _TURN_SIGNS:
                turn_count += 1
            name = (instr.get("street_name") or "").strip()
            if name:
                streets[name] += float(instr.get("distance", 0.0))

        details = path.get("details", {})
        way_types = distances_by_ranges(coords, details.get("road_class", []), classify_graphhopper_road_class)
        surfaces = distances_by_ranges(coords, details.get("surface", []), lambda v: str(v).lower())
        return LegRoute(
            coords=coords,
            distance_m=float(path.get("distance", 0.0)),
            duration_s=float(path["time"]) / 1000 if path.get("time") is not None else None,
            elevations=elevations,
            way_types_m=way_types,
            surfaces_m=surfaces,
            turn_count=turn_count,
            street_lengths_m=dict(streets),
            warnings=warnings,
        )
