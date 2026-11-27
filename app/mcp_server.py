"""MCP (Model Context Protocol) interface.

Exposes the same functionality as the REST API as MCP tools over the
streamable HTTP transport (``/mcp``), for Claude, Gemini CLI, ChatGPT
connectors and other MCP-capable agents.  Tool inputs are deliberately flat
(lists of strings, no nested objects) for maximum client compatibility.
"""

from __future__ import annotations

from typing import Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app.models import (
    CompareRequest,
    CompareResponse,
    GeocodeResult,
    ProviderInfo,
    RouteOptions,
    RouteRequest,
    RouteResponse,
    Stop,
)
from app.planner import PlanError, RoutePlanner

INSTRUCTIONS = """\
Plan walking (pedestrian) routes and produce GPX files.

Workflow:
1. Call plan_walking_route with the stops in visiting order. Each stop is either
   "latitude,longitude" or a street address/place name (include city & country).
2. Inspect the per-leg and total statistics (distance, time, elevation gain/loss,
   pedestrian friendliness 0-100) and try other stop orders or providers
   (list_routing_providers / compare_walking_routes) if useful.
3. Give the user the gpx_url: a downloadable .gpx file that imports into the
   Suunto app (iPhone: open the file -> share to Suunto, or Suunto app -> Maps ->
   Routes -> Import), Garmin, Komoot, etc.
"""

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _stops(stops: list[str], stop_names: list[str] | None) -> list[Stop]:
    names = stop_names or []
    return [Stop(location=loc, name=names[i] if i < len(names) and names[i] else None) for i, loc in enumerate(stops)]


def build_mcp_server(get_planner: Callable[[], RoutePlanner], get_base_url: Callable[[], str]) -> MCPServer:
    mcp = MCPServer(name="pedestrian-routing", instructions=INSTRUCTIONS, version="1.0.0")

    @mcp.tool(annotations=_READ_ONLY)
    async def plan_walking_route(
        stops: list[str],
        stop_names: list[str] | None = None,
        provider: str | None = None,
        route_name: str | None = None,
        avoid_hills: bool = False,
        avoid_steps: bool = False,
        walking_speed_kmh: float = 5.0,
        include_gpx: bool = False,
    ) -> RouteResponse:
        """Plan a walking route through stops in the given order and create a GPX file.

        Args:
            stops: Ordered stops (at least 2). Each is "latitude,longitude" (e.g. "51.0447,-114.0719")
                or an address/place name (e.g. "Calgary Tower, Calgary, Canada").
            stop_names: Optional labels for the stops (same order), used as GPX waypoint names.
            provider: Routing provider id: "osrm" (default), "valhalla", "openrouteservice", "graphhopper".
            route_name: Optional route name (used for the GPX file name).
            avoid_hills: Prefer flatter paths (supported by valhalla).
            avoid_steps: Avoid stairs (valhalla, openrouteservice, graphhopper).
            walking_speed_kmh: Walking speed for time estimates (default 5).
            include_gpx: Also return the GPX XML inline in the "gpx" field.

        Returns total and per-leg distance, walking time, elevation gain/loss and pedestrian
        friendliness, plus gpx_url - a download link for the GPX track (Suunto/Garmin compatible).
        """
        request = RouteRequest(
            stops=_stops(stops, stop_names),
            provider=provider,
            route_name=route_name,
            options=RouteOptions(
                avoid_hills=avoid_hills, avoid_steps=avoid_steps, walking_speed_kmh=walking_speed_kmh
            ),
            include_gpx=include_gpx,
        )
        try:
            return await get_planner().plan(request, get_base_url())
        except PlanError as exc:
            raise ToolError(exc.message) from exc

    @mcp.tool(annotations=_READ_ONLY)
    async def compare_walking_routes(
        stops: list[str],
        stop_names: list[str] | None = None,
        providers: list[str] | None = None,
        avoid_hills: bool = False,
        avoid_steps: bool = False,
    ) -> CompareResponse:
        """Route the same ordered stops with several providers and compare their summaries.

        Args:
            stops: Ordered stops, each "latitude,longitude" or an address/place name.
            stop_names: Optional labels for the stops.
            providers: Provider ids to compare (default: all available).
            avoid_hills: Prefer flatter paths where supported.
            avoid_steps: Avoid stairs where supported.

        Each successful result includes a gpx_url for that provider's route.
        """
        request = CompareRequest(
            stops=_stops(stops, stop_names),
            providers=providers,
            options=RouteOptions(avoid_hills=avoid_hills, avoid_steps=avoid_steps),
        )
        try:
            return await get_planner().compare(request, get_base_url())
        except PlanError as exc:
            raise ToolError(exc.message) from exc

    @mcp.tool(annotations=_READ_ONLY)
    def list_routing_providers() -> list[ProviderInfo]:
        """List the routing providers on this server, whether they are available and what they support."""
        return get_planner().list_providers()

    @mcp.tool(annotations=_READ_ONLY)
    async def geocode_location(query: str) -> GeocodeResult:
        """Resolve an address/place name (or "lat,lon" string) to coordinates, to check a stop before routing."""
        try:
            return await get_planner().geocode(query)
        except PlanError as exc:
            raise ToolError(exc.message) from exc

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_route_gpx(route_id: str) -> str:
        """Return the GPX XML of a previously planned route (by route_id)."""
        stored = get_planner().store.get(route_id)
        if stored is None:
            raise ToolError(f"Unknown or expired route_id {route_id!r}; plan the route again.")
        return stored.content

    return mcp
