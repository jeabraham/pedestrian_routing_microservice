"""Pedestrian friendliness scoring.

The score (0-100) combines up to three transparent components:

* ``way_type_score`` - distance-weighted quality of the ways used
  (footways/pedestrian zones best, busy roads worst).  Only available when the
  provider reports way types (Valhalla, OpenRouteService, GraphHopper).
* ``hill_score`` - penalises distance on grades steeper than 8 %.
* ``directness_score`` - penalises large detours compared to the straight line.
"""

from __future__ import annotations

from app.elevation import ElevationStats
from app.models import Friendliness
from app.providers.base import BUSY_ROAD, CYCLEWAY, FERRY, PATH, PEDESTRIAN, QUIET_STREET, STEPS, UNKNOWN

WAY_TYPE_QUALITY = {
    PEDESTRIAN: 100.0,
    PATH: 90.0,
    QUIET_STREET: 75.0,
    CYCLEWAY: 60.0,
    UNKNOWN: 60.0,
    STEPS: 50.0,
    FERRY: 50.0,
    BUSY_ROAD: 30.0,
}

_WEIGHTS = {"way_type_score": 0.5, "hill_score": 0.3, "directness_score": 0.2}


def rating_for(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 80:
        return "excellent"
    if score >= 65:
        return "good"
    if score >= 50:
        return "fair"
    return "poor"


def compute_friendliness(
    distance_m: float,
    straight_line_m: float,
    way_types_m: dict[str, float],
    surfaces_m: dict[str, float],
    elevation: ElevationStats | None,
) -> Friendliness:
    notes: list[str] = []
    components: dict[str, float] = {}

    typed_total = sum(way_types_m.values())
    if typed_total > 0:
        components["way_type_score"] = sum(
            WAY_TYPE_QUALITY.get(k, 60.0) * v for k, v in way_types_m.items()
        ) / typed_total
        busy = way_types_m.get(BUSY_ROAD, 0.0) / typed_total
        ped = (way_types_m.get(PEDESTRIAN, 0.0) + way_types_m.get(PATH, 0.0)) / typed_total
        if busy >= 0.3:
            notes.append(f"{busy:.0%} of the distance is along busy roads.")
        if ped >= 0.5:
            notes.append(f"{ped:.0%} of the distance is on footways, pedestrian zones or paths.")
        if way_types_m.get(STEPS, 0.0) > 0:
            notes.append("Route includes steps/stairs.")
        if way_types_m.get(FERRY, 0.0) > 0:
            notes.append("Route includes a ferry crossing.")
    else:
        notes.append("Way types not reported by this provider; score based on hills and directness only.")

    steep = None
    max_grade = None
    if elevation is not None and distance_m > 0:
        steep = elevation.steep_distance_m
        max_grade = elevation.max_grade_pct
        steep_frac = min(1.0, steep / distance_m)
        components["hill_score"] = max(0.0, 100.0 - steep_frac * 200.0)
        if steep_frac >= 0.1:
            notes.append(f"{steep_frac:.0%} of the distance is steeper than 8%.")

    detour = None
    if straight_line_m > 50 and distance_m > 0:
        detour = distance_m / straight_line_m
        components["directness_score"] = max(0.0, min(100.0, 100.0 - (detour - 1.3) * 100.0))
        if detour > 2:
            notes.append(f"Walking distance is {detour:.1f}x the straight-line distance (barriers or detours).")

    score = None
    if components:
        weight = sum(_WEIGHTS[k] for k in components)
        score = round(sum(_WEIGHTS[k] * v for k, v in components.items()) / weight, 1)

    return Friendliness(
        score=score,
        rating=rating_for(score),
        way_type_score=_round(components.get("way_type_score")),
        hill_score=_round(components.get("hill_score")),
        directness_score=_round(components.get("directness_score")),
        way_types_m={k: round(v, 1) for k, v in sorted(way_types_m.items(), key=lambda kv: -kv[1])},
        surfaces_m={k: round(v, 1) for k, v in sorted(surfaces_m.items(), key=lambda kv: -kv[1])},
        steep_distance_m=steep,
        max_grade_pct=max_grade,
        detour_factor=round(detour, 2) if detour is not None else None,
        notes=notes,
    )


def _round(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None
