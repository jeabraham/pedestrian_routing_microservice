"""Elevation lookup and elevation statistics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import httpx

from app.config import Settings
from app.geo import LatLon, interpolate_values

STEEP_GRADE = 0.08  # 8 %
_GAIN_THRESHOLD_M = 1.5
_PROFILE_STEP_M = 25.0


class ElevationError(Exception):
    pass


class OpenMeteoElevation:
    """Copernicus DEM (90 m) elevations from the free Open-Meteo API."""

    attribution = "Elevation data: Copernicus DEM GLO-90 via Open-Meteo.com (CC BY 4.0)"
    batch_size = 100

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.base_url = settings.open_meteo_base_url.rstrip("/")
        self.client = client

    async def lookup(self, points: Sequence[LatLon]) -> list[float]:
        out: list[float] = []
        for i in range(0, len(points), self.batch_size):
            batch = points[i : i + self.batch_size]
            params = {
                "latitude": ",".join(f"{p[0]:.5f}" for p in batch),
                "longitude": ",".join(f"{p[1]:.5f}" for p in batch),
            }
            try:
                resp = await self.client.get(f"{self.base_url}/v1/elevation", params=params)
                resp.raise_for_status()
                values = resp.json()["elevation"]
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise ElevationError(f"Elevation lookup failed: {exc}") from exc
            if len(values) != len(batch):
                raise ElevationError("Elevation lookup returned an unexpected number of values")
            out.extend(values)
        return _fill_missing(out)


def _fill_missing(values: list[float | None]) -> list[float]:
    known = [v for v in values if v is not None]
    if not known:
        raise ElevationError("No elevation data for this area")
    last = known[0]
    filled = []
    for v in values:
        if v is None:
            v = last
        filled.append(float(v))
        last = v
    return filled


@dataclass
class ElevationStats:
    gain_m: float
    loss_m: float
    min_m: float
    max_m: float
    steep_distance_m: float
    max_grade_pct: float


def elevation_stats(distances: Sequence[float], elevations: Sequence[float]) -> ElevationStats | None:
    """Compute gain/loss and grade statistics for an elevation profile.

    ``distances`` is the cumulative distance (m) of each elevation sample.
    """
    if len(distances) < 2 or len(distances) != len(elevations):
        return None
    total = distances[-1] - distances[0]
    if total <= 0:
        e = elevations[0]
        return ElevationStats(0.0, 0.0, e, e, 0.0, 0.0)
    n = max(1, int(round(total / _PROFILE_STEP_M)))
    xs = [distances[0] + total * k / n for k in range(n + 1)]
    es = interpolate_values(distances, elevations, xs)

    gain = loss = 0.0
    ref = es[0]
    for e in es[1:]:
        d = e - ref
        if d >= _GAIN_THRESHOLD_M:
            gain += d
            ref = e
        elif d <= -_GAIN_THRESHOLD_M:
            loss -= d
            ref = e
    tail = es[-1] - ref
    if tail > 0:
        gain += tail
    else:
        loss -= tail

    steep = 0.0
    max_grade = 0.0
    for k in range(n):
        lo, hi = max(0, k - 1), min(n, k + 2)
        run = xs[hi] - xs[lo]
        grade = abs(es[hi] - es[lo]) / run if run > 0 else 0.0
        max_grade = max(max_grade, grade)
        if grade > STEEP_GRADE:
            steep += xs[k + 1] - xs[k]
    return ElevationStats(
        gain_m=round(gain, 1),
        loss_m=round(loss, 1),
        min_m=round(min(elevations), 1),
        max_m=round(max(elevations), 1),
        steep_distance_m=round(steep, 1),
        max_grade_pct=round(max_grade * 100, 1),
    )
