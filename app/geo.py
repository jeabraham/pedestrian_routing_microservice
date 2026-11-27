"""Small geometry helpers (no heavy GIS dependencies)."""

from __future__ import annotations

import math
from bisect import bisect_right
from typing import Sequence

EARTH_RADIUS_M = 6_371_008.8

LatLon = tuple[float, float]


def haversine_m(a: LatLon, b: LatLon) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def cumulative_distances(coords: Sequence[LatLon]) -> list[float]:
    """Distance (m) from the first point to every point along the polyline."""
    out = [0.0] * len(coords)
    for i in range(1, len(coords)):
        out[i] = out[i - 1] + haversine_m(coords[i - 1], coords[i])
    return out


def decode_polyline(encoded: str, precision: int = 5) -> list[LatLon]:
    """Decode a Google encoded polyline (precision 5) or Valhalla polyline6."""
    coords: list[LatLon] = []
    index = lat = lon = 0
    factor = 10**precision
    length = len(encoded)
    while index < length:
        for is_lat in (True, False):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lat:
                lat += delta
            else:
                lon += delta
        coords.append((lat / factor, lon / factor))
    return coords


def sample_along(coords: Sequence[LatLon], spacing_m: float, max_samples: int) -> tuple[list[LatLon], list[float]]:
    """Return points spaced roughly ``spacing_m`` apart along the polyline.

    Returns the sample points and their distance along the line.  The first
    and last vertices are always included.  Spacing is widened when needed to
    respect ``max_samples``.
    """
    if not coords:
        return [], []
    cum = cumulative_distances(coords)
    total = cum[-1]
    if total <= 0 or len(coords) == 1:
        return [coords[0]], [0.0]
    max_samples = max(2, max_samples)
    spacing = max(spacing_m, total / (max_samples - 1))
    n = max(1, math.ceil(total / spacing))
    targets = [min(total, i * total / n) for i in range(n + 1)]
    points = [interpolate_at(coords, cum, d) for d in targets]
    return points, targets


def interpolate_at(coords: Sequence[LatLon], cum: Sequence[float], d: float) -> LatLon:
    if d <= 0:
        return coords[0]
    if d >= cum[-1]:
        return coords[-1]
    i = bisect_right(cum, d) - 1
    seg = cum[i + 1] - cum[i]
    t = 0.0 if seg == 0 else (d - cum[i]) / seg
    (lat1, lon1), (lat2, lon2) = coords[i], coords[i + 1]
    return (lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t)


def interpolate_values(xs: Sequence[float], ys: Sequence[float], targets: Sequence[float]) -> list[float]:
    """Piecewise-linear interpolation of ``ys`` (defined at sorted ``xs``)."""
    if not xs:
        return []
    out = []
    for t in targets:
        if t <= xs[0]:
            out.append(ys[0])
        elif t >= xs[-1]:
            out.append(ys[-1])
        else:
            i = bisect_right(xs, t) - 1
            span = xs[i + 1] - xs[i]
            f = 0.0 if span == 0 else (t - xs[i]) / span
            out.append(ys[i] + (ys[i + 1] - ys[i]) * f)
    return out
