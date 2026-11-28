"""Runtime configuration, read from environment variables.

Every setting has a sensible default so the service runs out-of-the-box with
free, key-less OpenStreetMap based services.  Keyed providers
(OpenRouteService, GraphHopper) are enabled automatically when their API key
is present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value is not None else default


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value is not None else default


@dataclass(frozen=True)
class Settings:
    default_provider: str = "osrm"

    osrm_base_url: str = "https://routing.openstreetmap.de/routed-foot"
    osrm_profile: str = "foot"
    valhalla_base_url: str = "https://valhalla1.openstreetmap.de"
    ors_base_url: str = "https://api.openrouteservice.org"
    ors_api_key: str | None = None
    graphhopper_base_url: str = "https://graphhopper.com/api/1"
    graphhopper_api_key: str | None = None

    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    nominatim_user_agent: str = "pedestrian-routing-microservice/1.0 (+https://github.com/jeabraham/pedestrian_routing_microservice)"
    nominatim_min_interval_s: float = 1.0

    # "open-meteo" or "none"
    elevation_provider: str = "open-meteo"
    open_meteo_base_url: str = "https://api.open-meteo.com"
    elevation_sample_spacing_m: float = 60.0
    elevation_max_samples: int = 300

    public_base_url: str | None = None
    api_key: str | None = None
    mcp_oauth_enabled: bool = False
    mcp_oauth_login_secret: str | None = None
    mcp_oauth_db_path: str | None = None
    gpx_ttl_seconds: int = 7 * 24 * 3600
    gpx_max_entries: int = 1000
    max_stops: int = 25
    http_timeout_s: float = 30.0
    max_concurrent_legs: int = 4
    disabled_providers: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls) -> "Settings":
        oauth_enabled = (_env("MCP_OAUTH_ENABLED", "false") or "").lower()
        if oauth_enabled not in ("true", "false", "1", "0"):
            raise ValueError("MCP_OAUTH_ENABLED must be true or false")
        public_base_url = _env("PUBLIC_BASE_URL")
        if public_base_url is None and _env("RAILWAY_PUBLIC_DOMAIN"):
            public_base_url = f"https://{_env('RAILWAY_PUBLIC_DOMAIN')}"
        disabled = _env("DISABLED_PROVIDERS", "") or ""
        return cls(
            default_provider=(_env("DEFAULT_PROVIDER", cls.default_provider) or "").lower(),
            osrm_base_url=_env("OSRM_BASE_URL", cls.osrm_base_url),
            osrm_profile=_env("OSRM_PROFILE", cls.osrm_profile),
            valhalla_base_url=_env("VALHALLA_BASE_URL", cls.valhalla_base_url),
            ors_base_url=_env("ORS_BASE_URL", cls.ors_base_url),
            ors_api_key=_env("ORS_API_KEY"),
            graphhopper_base_url=_env("GRAPHHOPPER_BASE_URL", cls.graphhopper_base_url),
            graphhopper_api_key=_env("GRAPHHOPPER_API_KEY"),
            nominatim_base_url=_env("NOMINATIM_BASE_URL", cls.nominatim_base_url),
            nominatim_user_agent=_env("NOMINATIM_USER_AGENT", cls.nominatim_user_agent),
            nominatim_min_interval_s=_env_float("NOMINATIM_MIN_INTERVAL_S", cls.nominatim_min_interval_s),
            elevation_provider=(_env("ELEVATION_PROVIDER", cls.elevation_provider) or "").lower(),
            open_meteo_base_url=_env("OPEN_METEO_BASE_URL", cls.open_meteo_base_url),
            elevation_sample_spacing_m=_env_float("ELEVATION_SAMPLE_SPACING_M", cls.elevation_sample_spacing_m),
            elevation_max_samples=_env_int("ELEVATION_MAX_SAMPLES", cls.elevation_max_samples),
            public_base_url=public_base_url.rstrip("/") if public_base_url else None,
            api_key=_env("API_KEY"),
            mcp_oauth_enabled=oauth_enabled in ("true", "1"),
            mcp_oauth_login_secret=_env("MCP_OAUTH_LOGIN_SECRET"),
            mcp_oauth_db_path=_env("MCP_OAUTH_DB_PATH"),
            gpx_ttl_seconds=_env_int("GPX_TTL_SECONDS", cls.gpx_ttl_seconds),
            gpx_max_entries=_env_int("GPX_MAX_ENTRIES", cls.gpx_max_entries),
            max_stops=_env_int("MAX_STOPS", cls.max_stops),
            http_timeout_s=_env_float("HTTP_TIMEOUT_S", cls.http_timeout_s),
            max_concurrent_legs=_env_int("MAX_CONCURRENT_LEGS", cls.max_concurrent_legs),
            disabled_providers=frozenset(p.strip().lower() for p in disabled.split(",") if p.strip()),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
