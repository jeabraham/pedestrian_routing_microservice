"""Routing provider abstraction.

Every provider turns a pair of coordinates into a :class:`LegRoute` - a
normalised description of a walking path.  Adding a new provider only requires
subclassing :class:`RoutingProvider` and registering it in
:mod:`app.providers`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import httpx

from app.geo import LatLon, cumulative_distances
from app.models import RouteOptions

# Normalised way categories used for the pedestrian friendliness score.
PEDESTRIAN = "pedestrian"
PATH = "path"
QUIET_STREET = "quiet_street"
BUSY_ROAD = "busy_road"
STEPS = "steps"
CYCLEWAY = "cycleway"
FERRY = "ferry"
UNKNOWN = "unknown"


class RoutingError(Exception):
    """Raised when a provider cannot produce a route."""


@dataclass
class LegRoute:
    coords: list[LatLon]
    distance_m: float
    duration_s: float | None = None
    elevations: list[float] | None = None
    way_types_m: dict[str, float] = field(default_factory=dict)
    surfaces_m: dict[str, float] = field(default_factory=dict)
    turn_count: int | None = None
    street_lengths_m: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class RoutingProvider(ABC):
    id: str = ""
    name: str = ""
    description: str = ""
    requires_api_key: bool = False
    supports: tuple[str, ...] = ()
    attribution: str = "Routing data © OpenStreetMap contributors (ODbL)"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    def available(self) -> bool:
        return True

    @abstractmethod
    async def route_leg(self, start: LatLon, end: LatLon, options: RouteOptions) -> LegRoute: ...

    async def _request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            resp = await self.client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise RoutingError(f"{self.name}: request failed ({exc.__class__.__name__}: {exc})") from exc
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400:
            detail = _error_detail(data) or resp.text[:300]
            raise RoutingError(f"{self.name}: HTTP {resp.status_code}: {detail}")
        if data is None:
            raise RoutingError(f"{self.name}: invalid JSON response")
        return data


def _error_detail(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in ("message", "error", "error_message"):
        value = data.get(key)
        if isinstance(value, dict):
            value = value.get("message")
        if value:
            return str(value)
    return None


def distances_by_ranges(
    coords: Sequence[LatLon],
    ranges: Iterable[Sequence[Any]],
    classify: Any,
) -> dict[str, float]:
    """Sum distance per category for ``[from_index, to_index, value]`` ranges.

    ``classify`` maps the raw provider value to a category name.
    """
    cum = cumulative_distances(coords)
    out: dict[str, float] = defaultdict(float)
    n = len(coords)
    for item in ranges:
        if len(item) < 3:
            continue
        start, stop, value = int(item[0]), int(item[1]), item[2]
        start = max(0, min(start, n - 1))
        stop = max(0, min(stop, n - 1))
        if stop <= start:
            continue
        out[classify(value)] += cum[stop] - cum[start]
    return {k: round(v, 1) for k, v in out.items() if v > 0}
