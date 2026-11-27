"""Route planning: geocode stops, route each leg, add elevation, compute
statistics and produce the GPX file."""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from typing import Sequence

import httpx

from app.config import Settings
from app.elevation import ElevationError, ElevationStats, OpenMeteoElevation, elevation_stats
from app.friendliness import compute_friendliness
from app.geo import LatLon, cumulative_distances, haversine_m, interpolate_values, sample_along
from app.geocoding import Geocoder, GeocodingError
from app.gpx import build_gpx, slugify
from app.models import (
    CompareItem,
    CompareRequest,
    CompareResponse,
    GeocodeResult,
    Leg,
    ProviderInfo,
    ResolvedStop,
    RouteOptions,
    RouteRequest,
    RouteResponse,
    SegmentStats,
)
from app.providers import ALIASES, build_providers
from app.providers.base import LegRoute, RoutingError, RoutingProvider
from app.store import GpxStore

NAISMITH_S_PER_M_ASCENT = 6.0  # 1 hour per 600 m of ascent


class PlanError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class RoutePlanner:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        providers: dict[str, RoutingProvider] | None = None,
        geocoder: Geocoder | None = None,
        elevation: OpenMeteoElevation | None = None,
        store: GpxStore | None = None,
    ):
        self.settings = settings
        self.client = client
        self.providers = providers if providers is not None else build_providers(settings, client)
        self.geocoder = geocoder or Geocoder(settings, client)
        if elevation is None and settings.elevation_provider == "open-meteo":
            elevation = OpenMeteoElevation(settings, client)
        self.elevation = elevation
        self.store = store or GpxStore(settings.gpx_ttl_seconds, settings.gpx_max_entries)

    # ------------------------------------------------------------------ providers
    @property
    def default_provider_id(self) -> str:
        default = ALIASES.get(self.settings.default_provider, self.settings.default_provider)
        provider = self.providers.get(default)
        if provider and provider.available():
            return default
        for pid, p in self.providers.items():
            if p.available():
                return pid
        raise PlanError(503, "No routing provider is available")

    def list_providers(self) -> list[ProviderInfo]:
        default = self.default_provider_id
        return [
            ProviderInfo(
                id=p.id,
                name=p.name,
                available=p.available(),
                is_default=p.id == default,
                requires_api_key=p.requires_api_key,
                description=p.description,
                supports=list(p.supports),
            )
            for p in self.providers.values()
        ]

    def get_provider(self, provider_id: str | None) -> RoutingProvider:
        pid = (provider_id or "").strip().lower() or self.default_provider_id
        pid = ALIASES.get(pid, pid)
        provider = self.providers.get(pid)
        if provider is None:
            raise PlanError(400, f"Unknown provider {provider_id!r}. Available: {', '.join(self.providers)}")
        if not provider.available():
            raise PlanError(400, f"Provider {pid!r} is not configured on this server (missing API key).")
        return provider

    # ------------------------------------------------------------------ geocoding
    async def geocode(self, query: str) -> GeocodeResult:
        try:
            return await self.geocoder.resolve(query)
        except GeocodingError as exc:
            raise PlanError(422, str(exc)) from exc

    # ------------------------------------------------------------------ planning
    async def plan(self, request: RouteRequest, base_url: str) -> RouteResponse:
        provider = self.get_provider(request.provider)
        if len(request.stops) > self.settings.max_stops:
            raise PlanError(422, f"Too many stops ({len(request.stops)}); the maximum is {self.settings.max_stops}.")
        if len(request.stops) < 2:
            raise PlanError(422, "At least two stops are required.")

        stops = await self._resolve_stops(request)
        points = [(s.lat, s.lon) for s in stops]
        leg_routes = await self._route_legs(provider, points, request.options)

        warnings: list[str] = []
        for leg in leg_routes:
            for w in leg.warnings:
                if w not in warnings:
                    warnings.append(w)

        attribution = [provider.attribution]
        if any(s.source == "geocoded" for s in stops):
            attribution.append(self.geocoder.attribution)

        leg_elevations = await self._leg_elevations(leg_routes, warnings, attribution)

        legs: list[Leg] = []
        leg_stats_raw: list[tuple[LegRoute, ElevationStats | None, float]] = []
        for i, route in enumerate(leg_routes):
            elev = leg_elevations[i] if leg_elevations else None
            estats = elevation_stats(cumulative_distances(route.coords), elev) if elev else None
            straight = haversine_m(points[i], points[i + 1])
            stats = self._segment_stats(
                _leg_distance(route), route.duration_s, straight, route.way_types_m, route.surfaces_m, estats,
                request.options,
            )
            leg_stats_raw.append((route, estats, straight))
            legs.append(
                Leg(
                    index=i,
                    from_stop=stops[i].name,
                    to_stop=stops[i + 1].name,
                    stats=stats,
                    turn_count=route.turn_count,
                    main_streets=[
                        name for name, _ in sorted(route.street_lengths_m.items(), key=lambda kv: -kv[1])[:5]
                    ],
                    coordinates=[[round(a, 6), round(b, 6)] for a, b in route.coords]
                    if request.include_geometry
                    else None,
                )
            )

        summary = self._summary(leg_stats_raw, request.options)

        track, track_ele = _merge_track(leg_routes, leg_elevations)
        route_name = (request.route_name or "").strip() or f"Walk: {stops[0].name} to {stops[-1].name}"
        description = _describe(summary, len(stops), provider.id)
        gpx = build_gpx(
            route_name,
            description,
            track,
            track_ele,
            [(s.name, (s.lat, s.lon), s.display_name) for s in stops],
        )
        route_id = uuid.uuid4().hex
        filename = f"{slugify(route_name)}.gpx"
        self.store.put(route_id, filename, gpx)

        return RouteResponse(
            route_id=route_id,
            route_name=route_name,
            provider=provider.id,
            stops=stops,
            summary=summary,
            legs=legs,
            gpx_url=f"{base_url.rstrip('/')}/gpx/{route_id}.gpx",
            gpx_filename=filename,
            warnings=warnings,
            attribution=attribution,
            gpx=gpx if request.include_gpx else None,
        )

    async def compare(self, request: CompareRequest, base_url: str) -> CompareResponse:
        ids = request.providers or [p.id for p in self.providers.values() if p.available()]
        results: list[CompareItem] = []
        for pid in ids:
            try:
                resp = await self.plan(
                    RouteRequest(
                        stops=request.stops, provider=pid, options=request.options, route_name=request.route_name
                    ),
                    base_url,
                )
                results.append(
                    CompareItem(
                        provider=resp.provider, ok=True, route_id=resp.route_id, gpx_url=resp.gpx_url,
                        summary=resp.summary,
                    )
                )
            except PlanError as exc:
                results.append(CompareItem(provider=pid, ok=False, error=exc.message))
        return CompareResponse(results=results)

    # ------------------------------------------------------------------ helpers
    async def _resolve_stops(self, request: RouteRequest) -> list[ResolvedStop]:
        resolved = await asyncio.gather(*(self.geocode(s.location) for s in request.stops))
        stops = []
        for i, (stop, geo) in enumerate(zip(request.stops, resolved)):
            name = (stop.name or "").strip()
            if not name and geo.source == "geocoded":
                name = stop.location.strip()
            elif not name:
                name = "Start" if i == 0 else "Finish" if i == len(request.stops) - 1 else f"Stop {i + 1}"
            stops.append(
                ResolvedStop(
                    index=i,
                    name=name[:100],
                    input=stop.location,
                    lat=geo.lat,
                    lon=geo.lon,
                    source=geo.source,
                    display_name=geo.display_name,
                )
            )
        return stops

    async def _route_legs(
        self, provider: RoutingProvider, points: Sequence[LatLon], options: RouteOptions
    ) -> list[LegRoute]:
        sem = asyncio.Semaphore(max(1, self.settings.max_concurrent_legs))

        async def one(i: int) -> LegRoute:
            a, b = points[i], points[i + 1]
            if haversine_m(a, b) < 1.0:
                return LegRoute(coords=[a, b], distance_m=0.0, duration_s=0.0, turn_count=0)
            async with sem:
                try:
                    leg = await provider.route_leg(a, b, options)
                except RoutingError as exc:
                    raise PlanError(502, f"Leg {i + 1} ({i + 1}->{i + 2}): {exc}") from exc
            if len(leg.coords) < 2:
                raise PlanError(502, f"Leg {i + 1}: provider returned an empty path")
            return leg

        return list(await asyncio.gather(*(one(i) for i in range(len(points) - 1))))

    async def _leg_elevations(
        self, legs: list[LegRoute], warnings: list[str], attribution: list[str]
    ) -> list[list[float]] | None:
        if all(leg.elevations and len(leg.elevations) == len(leg.coords) for leg in legs):
            return [list(leg.elevations or []) for leg in legs]
        if self.elevation is None:
            return None
        # Sample the whole walk once, then interpolate back onto every vertex.
        track: list[LatLon] = []
        offsets: list[int] = []
        for leg in legs:
            offsets.append(len(track))
            track.extend(leg.coords)
        cum = cumulative_distances(track)
        samples, sample_d = sample_along(
            track, self.settings.elevation_sample_spacing_m, self.settings.elevation_max_samples
        )
        try:
            values = await self.elevation.lookup(samples)
        except ElevationError as exc:
            warnings.append(f"Elevation unavailable: {exc}")
            return None
        known = [(d, v) for d, v in zip(sample_d, values) if v is not None]
        if not known:
            warnings.append("Elevation unavailable: no elevation data for this route")
            return None
        if len(known) < len(values):
            pct = 100 * len(known) / len(values)
            warnings.append(
                f"Elevation partially available ({pct:.0f}% of samples); gaps were interpolated, "
                "so elevation stats may be approximate."
            )
        attribution.append(self.elevation.attribution)
        all_ele = interpolate_values([d for d, _ in known], [v for _, v in known], cum)
        return [all_ele[offsets[i] : offsets[i] + len(leg.coords)] for i, leg in enumerate(legs)]

    def _segment_stats(
        self,
        distance_m: float,
        provider_duration_s: float | None,
        straight_m: float,
        way_types: dict[str, float],
        surfaces: dict[str, float],
        estats: ElevationStats | None,
        options: RouteOptions,
    ) -> SegmentStats:
        flat_s = distance_m / (options.walking_speed_kmh / 3.6)
        hills_s = flat_s + (estats.gain_m * NAISMITH_S_PER_M_ASCENT if estats else 0.0)
        return SegmentStats(
            distance_m=round(distance_m, 1),
            distance_km=round(distance_m / 1000, 3),
            duration_min=round(flat_s / 60, 1),
            duration_with_hills_min=round(hills_s / 60, 1),
            provider_duration_min=round(provider_duration_s / 60, 1) if provider_duration_s is not None else None,
            elevation_gain_m=estats.gain_m if estats else None,
            elevation_loss_m=estats.loss_m if estats else None,
            min_elevation_m=estats.min_m if estats else None,
            max_elevation_m=estats.max_m if estats else None,
            friendliness=compute_friendliness(distance_m, straight_m, way_types, surfaces, estats),
        )

    def _summary(
        self, legs: list[tuple[LegRoute, ElevationStats | None, float]], options: RouteOptions
    ) -> SegmentStats:
        distance = sum(_leg_distance(r) for r, _, _ in legs)
        durations = [r.duration_s for r, _, _ in legs]
        duration = sum(d for d in durations if d is not None) if all(d is not None for d in durations) else None
        straight = sum(s for _, _, s in legs)
        way_types: dict[str, float] = defaultdict(float)
        surfaces: dict[str, float] = defaultdict(float)
        for r, _, _ in legs:
            for k, v in r.way_types_m.items():
                way_types[k] += v
            for k, v in r.surfaces_m.items():
                surfaces[k] += v
        estats_list = [e for _, e, _ in legs]
        estats = None
        if estats_list and all(e is not None for e in estats_list):
            es = [e for e in estats_list if e is not None]
            estats = ElevationStats(
                gain_m=round(sum(e.gain_m for e in es), 1),
                loss_m=round(sum(e.loss_m for e in es), 1),
                min_m=min(e.min_m for e in es),
                max_m=max(e.max_m for e in es),
                steep_distance_m=round(sum(e.steep_distance_m for e in es), 1),
                max_grade_pct=max(e.max_grade_pct for e in es),
            )
        return self._segment_stats(distance, duration, straight, dict(way_types), dict(surfaces), estats, options)


def _leg_distance(route: LegRoute) -> float:
    if route.distance_m > 0:
        return route.distance_m
    return cumulative_distances(route.coords)[-1] if len(route.coords) > 1 else 0.0


def _merge_track(
    legs: list[LegRoute], elevations: list[list[float]] | None
) -> tuple[list[LatLon], list[float] | None]:
    track: list[LatLon] = []
    ele: list[float] | None = [] if elevations else None
    for i, leg in enumerate(legs):
        for j, pt in enumerate(leg.coords):
            if track and track[-1] == pt:
                continue
            track.append(pt)
            if ele is not None and elevations is not None:
                ele.append(elevations[i][j])
    return track, ele


def _describe(summary: SegmentStats, n_stops: int, provider: str) -> str:
    parts = [f"{summary.distance_km:.2f} km walk", f"{n_stops} stops", f"~{summary.duration_with_hills_min:.0f} min"]
    if summary.elevation_gain_m is not None:
        parts.append(f"+{summary.elevation_gain_m:.0f} m / -{summary.elevation_loss_m:.0f} m")
    if summary.friendliness.score is not None:
        parts.append(f"friendliness {summary.friendliness.score:.0f}/100")
    parts.append(f"routed with {provider}")
    return ", ".join(parts)
