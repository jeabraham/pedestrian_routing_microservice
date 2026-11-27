"""Valhalla provider (pedestrian costing).

Defaults to the free FOSSGIS Valhalla server.  After routing, the route shape
is matched with ``trace_attributes`` to obtain per-edge way types and surfaces,
which feed the pedestrian friendliness score.
"""

from __future__ import annotations

from collections import defaultdict

from app.geo import LatLon, decode_polyline
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
)

_TURN_MANEUVERS = set(range(9, 17))  # slight right ... slight left
_PEDESTRIAN_USES = {"footway", "sidewalk", "pedestrian", "pedestrian_crossing", "elevator", "crosswalk"}
_PATH_USES = {"path", "track", "bridleway", "mountain_bike"}
_STEP_USES = {"steps", "escalator"}
_FERRY_USES = {"ferry", "rail-ferry", "rail_ferry"}
_BUSY_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary"}


def classify_valhalla_edge(use: str | None, road_class: str | None) -> str:
    use = (use or "").lower()
    road_class = (road_class or "").lower()
    if use in _PEDESTRIAN_USES:
        return PEDESTRIAN
    if use in _STEP_USES:
        return STEPS
    if use in _PATH_USES:
        return PATH
    if use == "cycleway":
        return CYCLEWAY
    if use in _FERRY_USES:
        return FERRY
    if use == "living_street":
        return QUIET_STREET
    if road_class in _BUSY_CLASSES:
        return BUSY_ROAD
    if road_class or use:
        return QUIET_STREET
    return UNKNOWN


class ValhallaProvider(RoutingProvider):
    id = "valhalla"
    name = "Valhalla (OpenStreetMap, pedestrian costing)"
    description = (
        "Pedestrian routing that prefers walkways and sidewalks, supports avoid_hills and avoid_steps, "
        "and reports way types and surfaces. Free FOSSGIS server, no API key."
    )
    supports = ("walking", "avoid_hills", "avoid_steps", "way_types", "surfaces", "turn_count", "street_names")

    def __init__(self, client, base_url: str):
        super().__init__(client)
        self.base_url = base_url.rstrip("/")

    def _costing_options(self, options: RouteOptions) -> dict:
        pedestrian: dict = {"walking_speed": options.walking_speed_kmh}
        if options.avoid_hills:
            pedestrian["use_hills"] = 0.0
        if options.avoid_steps:
            pedestrian["step_penalty"] = 600
        return {"pedestrian": pedestrian}

    async def route_leg(self, start: LatLon, end: LatLon, options: RouteOptions) -> LegRoute:
        body = {
            "locations": [{"lat": start[0], "lon": start[1]}, {"lat": end[0], "lon": end[1]}],
            "costing": "pedestrian",
            "costing_options": self._costing_options(options),
            "directions_options": {"units": "kilometers"},
        }
        data = await self._request_json("POST", f"{self.base_url}/route", json=body)
        trip = data.get("trip") if isinstance(data, dict) else None
        if not trip or not trip.get("legs"):
            raise RoutingError(f"{self.name}: no route found")
        coords: list[LatLon] = []
        distance_km = 0.0
        duration_s = 0.0
        turn_count = 0
        streets: dict[str, float] = defaultdict(float)
        for leg in trip["legs"]:
            shape = decode_polyline(leg["shape"], precision=6)
            coords.extend(shape if not coords else shape[1:])
            summary = leg.get("summary", {})
            distance_km += float(summary.get("length", 0.0))
            duration_s += float(summary.get("time", 0.0))
            for m in leg.get("maneuvers", []):
                if m.get("type") in _TURN_MANEUVERS:
                    turn_count += 1
                for street in m.get("street_names") or []:
                    streets[street] += float(m.get("length", 0.0)) * 1000
                    break
        result = LegRoute(
            coords=coords,
            distance_m=distance_km * 1000,
            duration_s=duration_s,
            turn_count=turn_count,
            street_lengths_m=dict(streets),
        )
        await self._add_edge_attributes(trip["legs"], result)
        return result

    async def _add_edge_attributes(self, legs: list[dict], result: LegRoute) -> None:
        way_types: dict[str, float] = defaultdict(float)
        surfaces: dict[str, float] = defaultdict(float)
        try:
            for leg in legs:
                body = {
                    "encoded_polyline": leg["shape"],
                    "shape_match": "walk_or_snap",
                    "costing": "pedestrian",
                    "units": "kilometers",
                    "filters": {
                        "attributes": ["edge.use", "edge.road_class", "edge.surface", "edge.length"],
                        "action": "include",
                    },
                }
                data = await self._request_json("POST", f"{self.base_url}/trace_attributes", json=body)
                for edge in data.get("edges", []):
                    length_m = float(edge.get("length", 0.0)) * 1000
                    way_types[classify_valhalla_edge(edge.get("use"), edge.get("road_class"))] += length_m
                    if edge.get("surface"):
                        surfaces[str(edge["surface"])] += length_m
        except (RoutingError, KeyError, TypeError, ValueError) as exc:
            result.warnings.append(f"Valhalla way-type details unavailable: {exc}")
            return
        result.way_types_m = {k: round(v, 1) for k, v in way_types.items() if v > 0}
        result.surfaces_m = {k: round(v, 1) for k, v in surfaces.items() if v > 0}
