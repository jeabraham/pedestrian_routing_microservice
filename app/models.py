"""Public request/response models shared by the REST API and the MCP tools.

The schemas are deliberately flat and simple (plain strings, numbers, lists)
because some agents - notably ChatGPT Actions - struggle with deeply nested or
polymorphic JSON schemas.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Stop(BaseModel):
    location: str = Field(
        ...,
        description=(
            'Either GPS coordinates as "latitude,longitude" in decimal degrees '
            '(e.g. "48.8584,2.2945") or a street address / place name '
            '(e.g. "Eiffel Tower, Paris"). Include the city for addresses.'
        ),
        examples=["48.8584,2.2945", "Louvre Museum, Paris, France"],
    )
    name: str | None = Field(None, description="Optional label for this stop, used as the GPX waypoint name.")


class RouteOptions(BaseModel):
    avoid_hills: bool = Field(False, description="Prefer flatter paths where the provider supports it.")
    avoid_steps: bool = Field(False, description="Avoid stairs/steps where the provider supports it.")
    walking_speed_kmh: float = Field(5.0, gt=0.5, lt=15, description="Walking speed used for time estimates.")


class RouteRequest(BaseModel):
    stops: list[Stop] = Field(..., min_length=2, description="Ordered list of stops (start, intermediate stops, end).")
    provider: str | None = Field(
        None, description="Routing provider id (see GET /providers). Defaults to the server default (usually 'osrm')."
    )
    route_name: str | None = Field(None, description="Name for the route, used in the GPX file and the file name.")
    options: RouteOptions = Field(default_factory=RouteOptions)
    include_gpx: bool = Field(
        False, description="If true, the GPX XML is also returned inline in the 'gpx' field (can be large)."
    )
    include_geometry: bool = Field(
        False, description="If true, each leg includes its [lat, lon] coordinate list (can be large)."
    )


class ResolvedStop(BaseModel):
    index: int
    name: str
    input: str
    lat: float
    lon: float
    source: str = Field(..., description="'coordinates' if given as lat,lon, otherwise 'geocoded'.")
    display_name: str | None = Field(None, description="Full address returned by the geocoder.")


class Friendliness(BaseModel):
    score: float | None = Field(
        None, description="Overall pedestrian friendliness 0 (hostile) - 100 (excellent). Null if unknown."
    )
    rating: str | None = Field(None, description="Human-readable rating: excellent, good, fair or poor.")
    way_type_score: float | None = None
    hill_score: float | None = None
    directness_score: float | None = None
    way_types_m: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Metres per way category: pedestrian (footways, pedestrian zones), path (trails, tracks), "
            "quiet_street (residential/living streets), busy_road, steps, cycleway, ferry, unknown."
        ),
    )
    surfaces_m: dict[str, float] = Field(default_factory=dict, description="Metres per surface type (if known).")
    steep_distance_m: float | None = Field(None, description="Distance on grades steeper than 8%.")
    max_grade_pct: float | None = Field(None, description="Steepest sustained grade (%), smoothed over ~50 m.")
    detour_factor: float | None = Field(None, description="Walking distance divided by straight-line distance.")
    notes: list[str] = Field(default_factory=list)


class SegmentStats(BaseModel):
    distance_m: float
    distance_km: float
    duration_min: float = Field(..., description="Walking time from distance and speed (flat ground).")
    duration_with_hills_min: float = Field(..., description="Walking time adding Naismith's rule for ascent.")
    provider_duration_min: float | None = Field(None, description="Walking time estimated by the routing provider.")
    elevation_gain_m: float | None = None
    elevation_loss_m: float | None = None
    min_elevation_m: float | None = None
    max_elevation_m: float | None = None
    friendliness: Friendliness = Field(default_factory=Friendliness)


class Leg(BaseModel):
    index: int
    from_stop: str
    to_stop: str
    stats: SegmentStats
    turn_count: int | None = Field(None, description="Number of turn instructions on this leg.")
    main_streets: list[str] = Field(default_factory=list, description="Longest named streets/paths used.")
    coordinates: list[list[float]] | None = None


class RouteResponse(BaseModel):
    route_id: str
    route_name: str
    provider: str
    stops: list[ResolvedStop]
    summary: SegmentStats
    legs: list[Leg]
    gpx_url: str = Field(..., description="Download URL of the GPX file (importable into Suunto, Garmin, etc.).")
    gpx_filename: str
    warnings: list[str] = Field(default_factory=list)
    attribution: list[str] = Field(default_factory=list)
    gpx: str | None = None


class ProviderInfo(BaseModel):
    id: str
    name: str
    available: bool
    is_default: bool
    requires_api_key: bool
    description: str
    supports: list[str]


class GeocodeResult(BaseModel):
    query: str
    lat: float
    lon: float
    display_name: str | None = None
    source: str


class CompareRequest(BaseModel):
    stops: list[Stop] = Field(..., min_length=2)
    providers: list[str] | None = Field(None, description="Providers to compare. Defaults to all available.")
    options: RouteOptions = Field(default_factory=RouteOptions)
    route_name: str | None = None


class CompareItem(BaseModel):
    provider: str
    ok: bool
    error: str | None = None
    route_id: str | None = None
    gpx_url: str | None = None
    summary: SegmentStats | None = None


class CompareResponse(BaseModel):
    results: list[CompareItem]
