"""FastAPI application: REST API + MCP endpoint (``/mcp``).

Run locally with ``uvicorn app.main:app --reload``.
"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
from mcp.server.transport_security import TransportSecuritySettings

from app.config import Settings, get_settings
from app.mcp_server import build_mcp_server
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

API_DESCRIPTION = """\
Plan pedestrian (walking) routes through an ordered list of stops and get a GPX
track plus statistics for the whole route and each leg: distance, walking time,
elevation gain/loss and a pedestrian friendliness score.

* Stops are `"latitude,longitude"` strings or street addresses / place names.
* Several routing back-ends are available (`GET /providers`); try more than one
  or use `POST /route/compare`.
* `gpx_url` in the response is a direct download link. The GPX imports into the
  Suunto app (and Garmin, Komoot, Strava...) for watch navigation.
* `GET /route.gpx?stops=...` builds and downloads a GPX in one request - handy
  for agents that can only produce links.
* MCP clients can connect to the streamable HTTP endpoint at `/mcp`.
"""

PUBLIC_PATHS = ("/health", "/openapi.json", "/docs", "/redoc", "/gpx/", "/privacy")


def create_app(settings: Settings | None = None, planner: RoutePlanner | None = None) -> FastAPI:
    settings = settings or get_settings()
    state: dict[str, RoutePlanner] = {}
    if planner is not None:
        state["planner"] = planner

    def get_planner() -> RoutePlanner:
        if "planner" not in state:
            client = httpx.AsyncClient(
                timeout=settings.http_timeout_s,
                headers={"User-Agent": settings.nominatim_user_agent},
                follow_redirects=True,
            )
            state["planner"] = RoutePlanner(settings, client)
        return state["planner"]

    def mcp_base_url() -> str:
        return settings.public_base_url or f"http://localhost:{os.getenv('PORT', '8000')}"

    mcp = build_mcp_server(get_planner, mcp_base_url)
    # Stateless JSON responses work best behind Railway's proxy and with simple clients.  DNS-rebinding
    # protection is meant for servers bound to localhost; this is a public web service.
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with mcp.session_manager.run():
            yield
        if planner is None and "planner" in state:
            await state["planner"].client.aclose()

    app = FastAPI(
        title="Pedestrian Routing Microservice",
        version="1.0.0",
        description=API_DESCRIPTION,
        lifespan=lifespan,
    )

    def base_url(request: Request) -> str:
        return settings.public_base_url or str(request.base_url).rstrip("/")

    @app.middleware("http")
    async def api_key_auth(request: Request, call_next):
        path = request.url.path
        if settings.api_key and path != "/" and not path.startswith(PUBLIC_PATHS) and request.method != "OPTIONS":
            supplied = request.headers.get("x-api-key") or ""
            auth = request.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                supplied = supplied or auth[7:].strip()
            if not hmac.compare_digest(supplied.encode(), settings.api_key.encode()):
                return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(PlanError)
    async def plan_error_handler(_: Request, exc: PlanError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    @app.get("/", include_in_schema=False)
    async def root(request: Request):
        base = base_url(request)
        return {
            "service": "pedestrian-routing-microservice",
            "rest_docs": f"{base}/docs",
            "openapi": f"{base}/openapi.json",
            "mcp_endpoint": f"{base}/mcp",
            "providers": [p.id for p in get_planner().list_providers() if p.available],
        }

    @app.get("/health", operation_id="health", tags=["meta"])
    async def health():
        return {"status": "ok"}

    @app.get("/privacy", include_in_schema=False)
    async def privacy():
        return Response(
            "This service does not store personal data. Stops are sent to the configured routing, geocoding "
            "and elevation providers to compute routes. Generated GPX files are kept in memory temporarily "
            "so they can be downloaded, then discarded.",
            media_type="text/plain",
        )

    @app.get("/providers", operation_id="listProviders", response_model=list[ProviderInfo], tags=["routing"])
    async def list_providers():
        """List routing providers, whether each is available and which features it supports."""
        return get_planner().list_providers()

    @app.get("/geocode", operation_id="geocodeLocation", response_model=GeocodeResult, tags=["routing"])
    async def geocode(q: Annotated[str, Query(description="Address, place name or 'lat,lon'")]):
        """Resolve an address or place name to coordinates (to check a stop before routing)."""
        return await get_planner().geocode(q)

    @app.post("/route", operation_id="planWalkingRoute", response_model=RouteResponse, tags=["routing"])
    async def plan_route(body: RouteRequest, request: Request):
        """Plan a walking route through the stops in order. Returns stats per leg and in total, and a GPX link."""
        return await get_planner().plan(body, base_url(request))

    @app.get(
        "/route",
        operation_id="planWalkingRouteGet",
        response_model=RouteResponse,
        response_model_exclude_none=True,
        tags=["routing"],
    )
    async def plan_route_get(
        request: Request,
        stops: Annotated[list[str], Query(description=_STOPS_QUERY_DOC)],
        names: Annotated[list[str] | None, Query(description="Optional stop names (repeat or '|' separated).")] = None,
        provider: str | None = None,
        route_name: str | None = None,
        avoid_hills: bool = False,
        avoid_steps: bool = False,
        walking_speed_kmh: float = 5.0,
    ):
        """Same as POST /route but with query parameters, for agents that can only issue GET requests."""
        body = _query_request(stops, names, provider, route_name, avoid_hills, avoid_steps, walking_speed_kmh)
        return await get_planner().plan(body, base_url(request))

    @app.post("/route/compare", operation_id="compareProviders", response_model=CompareResponse, tags=["routing"])
    async def compare(body: CompareRequest, request: Request):
        """Route the same stops with several providers and compare summaries (each with its own GPX link)."""
        return await get_planner().compare(body, base_url(request))

    @app.get(
        "/route.gpx",
        operation_id="downloadRouteGpx",
        tags=["gpx"],
        response_class=Response,
        responses={200: {"content": {"application/gpx+xml": {}}, "description": "GPX file"}},
    )
    async def route_gpx(
        request: Request,
        stops: Annotated[list[str], Query(description=_STOPS_QUERY_DOC)],
        names: Annotated[list[str] | None, Query(description="Optional stop names (repeat or '|' separated).")] = None,
        provider: str | None = None,
        route_name: str | None = None,
        avoid_hills: bool = False,
        avoid_steps: bool = False,
    ):
        """Plan a route and download its GPX directly. A shareable link, e.g.
        `/route.gpx?stops=51.0447,-114.0719|51.0375,-114.0849&route_name=My%20walk`."""
        body = _query_request(stops, names, provider, route_name, avoid_hills, avoid_steps, 5.0)
        body.include_gpx = True
        result = await get_planner().plan(body, base_url(request))
        return _gpx_response(result.gpx or "", result.gpx_filename)

    @app.get(
        "/gpx/{route_id}.gpx",
        operation_id="getGpx",
        tags=["gpx"],
        response_class=Response,
        responses={200: {"content": {"application/gpx+xml": {}}, "description": "GPX file"}},
    )
    async def get_gpx(route_id: str):
        """Download the GPX file of a planned route."""
        stored = get_planner().store.get(route_id)
        if stored is None:
            raise HTTPException(404, "Unknown or expired route id; plan the route again.")
        return _gpx_response(stored.content, stored.filename)

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes
        )
        if settings.public_base_url:
            # ChatGPT Actions require an absolute server URL.
            schema["servers"] = [{"url": settings.public_base_url}]
        if settings.api_key:
            schema.setdefault("components", {})["securitySchemes"] = {
                "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
                "BearerAuth": {"type": "http", "scheme": "bearer"},
            }
            schema["security"] = [{"ApiKeyAuth": []}, {"BearerAuth": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    app.state.get_planner = get_planner
    # The MCP app is mounted last so the REST routes above take precedence.
    app.mount("/", mcp_app)
    return app


_STOPS_QUERY_DOC = (
    "Stops in order: repeat the parameter (stops=a&stops=b) or separate with '|'. "
    "Each stop is 'lat,lon' or an address."
)


def _split(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out.extend(part.strip() for part in value.split("|"))
    return out


def _query_request(
    stops: list[str],
    names: list[str] | None,
    provider: str | None,
    route_name: str | None,
    avoid_hills: bool,
    avoid_steps: bool,
    walking_speed_kmh: float,
) -> RouteRequest:
    locations = [s for s in _split(stops) if s]
    name_list = _split(names)
    if len(locations) < 2:
        raise PlanError(422, "At least two stops are required.")
    try:
        options = RouteOptions(avoid_hills=avoid_hills, avoid_steps=avoid_steps, walking_speed_kmh=walking_speed_kmh)
    except ValueError as exc:
        raise PlanError(422, str(exc)) from exc
    return RouteRequest(
        stops=[
            Stop(location=loc, name=name_list[i] if i < len(name_list) and name_list[i] else None)
            for i, loc in enumerate(locations)
        ],
        provider=provider,
        route_name=route_name,
        options=options,
    )


def _gpx_response(content: str, filename: str) -> Response:
    return Response(
        content,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


app = create_app()
