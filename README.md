# pedestrian_routing_microservice

Small service so agents (ChatGPT, Gemini, Claude, ...) can plan walking trips.

Give it an **ordered list of stops** (GPS coordinates and/or street addresses) and it

1. resolves addresses to coordinates (OpenStreetMap Nominatim),
2. builds a pedestrian path between each pair of consecutive stops using a
   **pluggable routing provider** (OSRM, Valhalla, OpenRouteService, GraphHopper),
3. returns a **GPX 1.1 track** (download link, or inline) that imports into the
   **Suunto app** (and Garmin Connect, Komoot, Strava, ...) for watch navigation,
4. returns **statistics for the whole route and for every leg**: distance, walking
   time (flat and with Naismith's hill correction), elevation gain/loss, min/max
   elevation and a **pedestrian friendliness** score with its components.

Agents can call it repeatedly to evaluate different stop orders or providers
before giving the user the final GPX link.

Two interfaces are served by the same process:

| Interface | URL | Best for |
|-----------|-----|----------|
| REST + OpenAPI 3.1 | `/route`, `/providers`, ... (`/docs`, `/openapi.json`) | ChatGPT custom GPT *Actions*, scripts, any HTTP client |
| GET-only links | `GET /route?stops=...`, `GET /route.gpx?stops=...` | agents that can only fetch URLs or produce links |
| MCP (streamable HTTP, stateless, JSON responses) | `/mcp` | Claude, Gemini CLI, ChatGPT connectors/apps, Cursor, ... |

---

## Quick start

```bash
python3 -m venv venv                # isolated environment (venv/ is git-ignored)
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt
python -m app                       # serves on http://localhost:8000 (PORT env var honoured)
pytest                              # tests use a mocked upstream, no network needed
```

```bash
curl -s localhost:8000/route -H 'content-type: application/json' -d '{
  "route_name": "Pizza and beer run",
  "stops": [
    {"location": "51.0447,-114.0719", "name": "Home"},
    {"location": "Co-op Midtown, Calgary, Canada", "name": "Groceries"},
    {"location": "Willow Park Wines & Spirits, Calgary, Canada"},
    {"location": "51.0375,-114.0849", "name": "Pizza"}
  ],
  "provider": "valhalla",
  "options": {"avoid_steps": true}
}'
```

Response (abridged):

```json
{
  "route_id": "6f1c...",
  "provider": "valhalla",
  "stops": [{"index": 0, "name": "Home", "lat": 51.0447, "lon": -114.0719, "source": "coordinates"}, "..."],
  "summary": {
    "distance_km": 4.12, "duration_min": 49.4, "duration_with_hills_min": 53.1,
    "elevation_gain_m": 37.0, "elevation_loss_m": 35.5,
    "friendliness": {"score": 78.4, "rating": "good",
                      "way_types_m": {"pedestrian": 2310.0, "quiet_street": 1400.0, "busy_road": 410.0},
                      "steep_distance_m": 0.0, "max_grade_pct": 4.1, "detour_factor": 1.21, "notes": ["..."]}
  },
  "legs": [{"index": 0, "from_stop": "Home", "to_stop": "Groceries", "stats": {"...": "..."},
            "turn_count": 4, "main_streets": ["Stephen Avenue", "..."]}],
  "gpx_url": "https://your-app.up.railway.app/gpx/6f1c....gpx",
  "gpx_filename": "pizza-and-beer-run.gpx",
  "warnings": [], "attribution": ["..."]
}
```

### Endpoints

| Method & path | operationId | Purpose |
|---------------|-------------|---------|
| `POST /route` | `planWalkingRoute` | Plan a route (JSON body, see `/docs`). `include_gpx`/`include_geometry` add inline data. |
| `GET /route?stops=a\|b\|c` | `planWalkingRouteGet` | Same with query parameters (`stops` repeatable or `\|`-separated, `names`, `provider`, `route_name`, `avoid_hills`, `avoid_steps`). |
| `POST /route/compare` | `compareProviders` | Route the same stops with several providers, compare summaries. |
| `GET /route.gpx?stops=a\|b` | `downloadRouteGpx` | Plan and immediately download the GPX - a stateless, shareable link. |
| `GET /gpx/{route_id}.gpx` | `getGpx` | Download the GPX of a planned route (`Content-Disposition: attachment`). |
| `GET /providers` | `listProviders` | Providers, availability and supported features. |
| `GET /geocode?q=...` | `geocodeLocation` | Check how an address resolves. |
| `GET /health` | `health` | Health check. |
| `POST /mcp` | - | MCP endpoint. |

Stops are strings: `"lat,lon"` in decimal degrees (e.g. `"48.8584,2.2945"`) or an
address/place name (include the city and country for reliable geocoding).

### MCP tools

`plan_walking_route`, `compare_walking_routes`, `list_routing_providers`,
`geocode_location`, `get_route_gpx`. Inputs are flat (lists of strings, booleans)
because some clients (e.g. Gemini) dislike nested `$ref` schemas.

Example client configuration (Claude Desktop / Gemini CLI / Cursor style):

```json
{ "mcpServers": { "walking-routes": { "url": "https://your-app.up.railway.app/mcp" } } }
```

If `API_KEY` is set, also send the header `Authorization: Bearer YOUR_API_KEY` (or `X-API-Key: YOUR_API_KEY`).

---

## Routing providers (investigation)

| Provider | Id | Key? | Pedestrian quality | Elevation | Friendliness data | Notes |
|----------|----|------|--------------------|-----------|-------------------|-------|
| **OSRM** on FOSSGIS `routing.openstreetmap.de/routed-foot` | `osrm` (default) | no | Good shortest walking paths on OSM | via Open-Meteo | turn count, street names, ferries | Very fast. The `router.project-osrm.org` demo only supports *car*, so the FOSSGIS foot instance is used. Fair-use; self-host for volume. |
| **Valhalla** on FOSSGIS `valhalla1.openstreetmap.de` | `valhalla` | no | Pedestrian costing prefers walkways/sidewalks; `use_hills`, `step_penalty` | via Open-Meteo | way types & surfaces (via `trace_attributes`) | Best free option for friendliness; supports `avoid_hills` and `avoid_steps`. Fair-use. |
| **OpenRouteService** (HeiGIT) | `openrouteservice` / `ors` | yes (free tier ~2000 req/day) | `foot-walking` profile | native | way types (footway/street/road/steps), surfaces | `avoid_steps` via `avoid_features`. |
| **GraphHopper** Directions API | `graphhopper` / `gh` | yes (free tier) or self-hosted | `foot` profile | native | road classes, surfaces | `avoid_steps` via custom model. Set `GRAPHHOPPER_BASE_URL` to a self-hosted server to run without a key. |

Other services considered: Mapbox / Google / HERE / TomTom walking directions
(commercial keys, restrictive terms for storing/exporting geometry to GPX - not
added), BRouter (great hiking profiles, self-host friendly - a good next
provider), Stadia Maps (hosted Valhalla, key needed - works by pointing
`VALHALLA_BASE_URL` at it if no extra auth is needed).

Adding a provider: subclass `RoutingProvider` in `app/providers/`, implement
`route_leg(start, end, options) -> LegRoute`, and register it in
`app/providers/__init__.py`. Agents can pick any available provider per request,
or call `/route/compare` to try them all.

### Elevation

Providers with native elevation (ORS, GraphHopper) are used as-is. Otherwise the
track is sampled every ~60 m (max 300 samples, i.e. at most 3 requests) and looked
up in the free **Open-Meteo elevation API** (Copernicus GLO-90 DEM). Gain/loss uses a
1.5 m hysteresis on a 25 m resampled profile to suppress DEM noise.

Open-Meteo is rate limited (HTTP 429). Rate-limited or failed requests are retried
with backoff (honouring `Retry-After`); if some batches still fail, the missing
samples are interpolated from the ones that succeeded and a warning says the stats
are approximate. Elevation is only reported as unavailable when no samples at all
could be fetched, and the route itself never fails because of elevation.

### Pedestrian friendliness (0-100)

Weighted mean of the available components (see `app/friendliness.py`):

* **way_type_score** (weight 0.5) - distance-weighted quality: pedestrian/footway 100,
  path 90, quiet street 75, cycleway/unknown 60, steps/ferry 50, busy road 30.
  Only when the provider reports way types (Valhalla, ORS, GraphHopper).
* **hill_score** (0.3) - penalises distance steeper than 8 %.
* **directness_score** (0.2) - penalises detours (walking / straight-line distance > 1.3).

Rating: excellent ≥ 80, good ≥ 65, fair ≥ 50, else poor. `notes` explain notable issues.

---

## Using it from agents

### ChatGPT (custom GPT with Actions)

1. Deploy (see Railway below) so the service has a public **https** URL and set
   `PUBLIC_BASE_URL` (Railway's `RAILWAY_PUBLIC_DOMAIN` is used automatically). ChatGPT
   requires the `servers` entry this adds to the OpenAPI document.
2. In the GPT editor -> *Actions* -> *Import from URL*: `https://<host>/openapi.json`.
   If `API_KEY` is set choose *Authentication: API Key*, *Custom header* `X-API-Key`
   (or *Bearer*).
3. Privacy policy URL: `https://<host>/privacy`.
4. Suggested GPT instructions:
   > When the user wants a walking route, call planWalkingRoute with the stops in order
   > ("lat,lon" or full addresses with city). Compare alternative stop orders or providers
   > if helpful. Report distance, time, elevation gain/loss and friendliness per leg and in
   > total, and give the user the gpx_url as a download link for the Suunto app.

Responses do not include the GPX inline by default (keeps them small for
ChatGPT); the user downloads it from `gpx_url`. As a fallback when Actions are not
available, an agent can simply construct a link like
`https://<host>/route.gpx?stops=51.0447,-114.0719|Calgary%20Tower,%20Calgary&route_name=My%20walk`.

ChatGPT can also use the MCP endpoint (`/mcp`) as a connector/app in developer mode.

### Gemini, Claude and other MCP clients

Point the client at `https://<host>/mcp` (streamable HTTP). For REST-only agents,
use `/openapi.json`.

### Importing the GPX into Suunto (iPhone)

1. Open the `gpx_url` on the iPhone (Safari downloads the `.gpx` file).
2. Tap the file -> *Share* -> **Suunto** (or *Open in Suunto*). The app imports it as a route.
3. Check the route in *Map -> My routes*, make sure **Use in watch** is on, and sync.
4. On the watch start an exercise -> *Navigation* -> *Routes* -> select the route.

The GPX contains one `<trk>` (single `<trkseg>` with elevation) and a `<wpt>` per stop.

---

## Deploying on Railway

The repository contains `railway.json` (Railpack builder, start command
`python -m app`, health check `/health`) and a `Procfile`.

1. *New Project* -> *Deploy from GitHub repo* -> select this repository.
2. *Settings -> Networking -> Generate Domain*.
3. Optional variables (below), e.g. `ORS_API_KEY`, `GRAPHHOPPER_API_KEY`, `API_KEY`.

### Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | Listen port (set by Railway). |
| `PUBLIC_BASE_URL` | `https://$RAILWAY_PUBLIC_DOMAIN` | Absolute base URL for GPX links and the OpenAPI `servers` entry. |
| `API_KEY` | - | If set, required as `X-API-Key` or `Authorization: Bearer` (except `/health`, `/docs`, `/openapi.json`, `/privacy`, `/gpx/*`). |
| `DEFAULT_PROVIDER` | `osrm` | Provider used when the request names none. |
| `DISABLED_PROVIDERS` | - | Comma-separated provider ids to hide. |
| `OSRM_BASE_URL` / `OSRM_PROFILE` | FOSSGIS foot / `foot` | OSRM server. |
| `VALHALLA_BASE_URL` | `https://valhalla1.openstreetmap.de` | Valhalla server. |
| `ORS_API_KEY` / `ORS_BASE_URL` | - / `https://api.openrouteservice.org` | OpenRouteService. |
| `GRAPHHOPPER_API_KEY` / `GRAPHHOPPER_BASE_URL` | - / `https://graphhopper.com/api/1` | GraphHopper. |
| `NOMINATIM_BASE_URL` / `NOMINATIM_USER_AGENT` | public Nominatim / project UA | Geocoder (max 1 request/s enforced, results cached). |
| `ELEVATION_PROVIDER` | `open-meteo` | `open-meteo` or `none`. |
| `ELEVATION_SAMPLE_SPACING_M` / `ELEVATION_MAX_SAMPLES` | `60` / `300` | Elevation sampling (the DEM is ~90 m resolution; fewer samples = fewer Open-Meteo requests). |
| `GPX_TTL_SECONDS` / `GPX_MAX_ENTRIES` | 7 days / `1000` | In-memory GPX link retention (lost on restart; `/route.gpx` links are stateless). |
| `MAX_STOPS` | `25` | Maximum stops per request. |
| `HTTP_TIMEOUT_S` / `MAX_CONCURRENT_LEGS` | `30` / `4` | Upstream timeouts and parallelism. |

The free public OSRM/Valhalla/Nominatim/Open-Meteo services are fair-use; for
production volume self-host them or use keyed providers.

## Attribution

Map data © OpenStreetMap contributors (ODbL). Routing by OSRM/Valhalla (FOSSGIS),
openrouteservice.org (HeiGIT) or GraphHopper; elevation by Open-Meteo (Copernicus
DEM). Responses include an `attribution` list - please show it to users.
