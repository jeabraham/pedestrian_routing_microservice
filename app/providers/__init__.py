"""Registry of routing providers."""

from __future__ import annotations

import httpx

from app.config import Settings
from app.providers.base import LegRoute, RoutingError, RoutingProvider
from app.providers.graphhopper import GraphHopperProvider
from app.providers.openrouteservice import OpenRouteServiceProvider
from app.providers.osrm import OSRMProvider
from app.providers.valhalla import ValhallaProvider

__all__ = ["LegRoute", "RoutingError", "RoutingProvider", "build_providers"]

# Short aliases agents may use.
ALIASES = {"ors": "openrouteservice", "gh": "graphhopper", "osm": "osrm"}


def build_providers(settings: Settings, client: httpx.AsyncClient) -> dict[str, RoutingProvider]:
    providers: list[RoutingProvider] = [
        OSRMProvider(client, settings.osrm_base_url, settings.osrm_profile),
        ValhallaProvider(client, settings.valhalla_base_url),
        OpenRouteServiceProvider(client, settings.ors_base_url, settings.ors_api_key),
        GraphHopperProvider(client, settings.graphhopper_base_url, settings.graphhopper_api_key),
    ]
    return {p.id: p for p in providers if p.id not in settings.disabled_providers}
