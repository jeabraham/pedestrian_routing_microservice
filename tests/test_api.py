import json
from xml.etree import ElementTree as ET

import pytest

from app.gpx import GPX_NS

NS = {"g": GPX_NS}
COORD_A = "51.0447,-114.0719"
COORD_B = "51.0375,-114.0849"


def _route(client, stops, **extra):
    body = {"stops": [{"location": s} if isinstance(s, str) else s for s in stops], **extra}
    return client.post("/route", json=body)


def test_health_and_providers(client):
    assert client.get("/health").json() == {"status": "ok"}
    providers = {p["id"]: p for p in client.get("/providers").json()}
    assert set(providers) == {"osrm", "valhalla", "openrouteservice", "graphhopper"}
    assert providers["osrm"]["is_default"] and all(p["available"] for p in providers.values())


def test_keyed_providers_unavailable_without_keys(make_client, settings_with):
    hosted_gh = settings_with(
        ors_api_key=None, graphhopper_api_key=None, graphhopper_base_url="https://graphhopper.com/api/1"
    )
    with make_client(hosted_gh) as c:
        providers = {p["id"]: p for p in c.get("/providers").json()}
        assert not providers["openrouteservice"]["available"]
        assert not providers["graphhopper"]["available"]
        r = _route(c, [COORD_A, COORD_B], provider="ors")
        assert r.status_code == 400 and "not configured" in r.json()["detail"]


def test_plan_route_osrm_with_address_and_coordinates(client, upstream):
    r = _route(
        client,
        [{"location": COORD_A, "name": "Home"}, "Calgary Tower", "Prince's Island Park"],
        route_name="Tower walk",
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["provider"] == "osrm"
    assert [s["name"] for s in data["stops"]] == ["Home", "Calgary Tower", "Prince's Island Park"]
    assert data["stops"][1]["source"] == "geocoded" and data["stops"][0]["source"] == "coordinates"
    assert len(data["legs"]) == 2
    leg = data["legs"][0]
    assert leg["from_stop"] == "Home" and leg["to_stop"] == "Calgary Tower"
    assert leg["stats"]["distance_m"] == 1234.5
    assert leg["stats"]["provider_duration_min"] == 15.0
    assert leg["stats"]["elevation_gain_m"] is not None
    assert leg["turn_count"] == 1
    assert leg["main_streets"][0] == "Stephen Avenue"
    summary = data["summary"]
    assert summary["distance_m"] == pytest.approx(2469.0)
    assert summary["elevation_gain_m"] == pytest.approx(
        sum(leg["stats"]["elevation_gain_m"] for leg in data["legs"])
    )
    assert summary["friendliness"]["score"] is not None
    assert summary["duration_with_hills_min"] >= summary["duration_min"]
    assert data["gpx_url"] == f"https://walk.example.com/gpx/{data['route_id']}.gpx"
    assert data["gpx_filename"] == "tower-walk.gpx"
    assert data["gpx"] is None
    assert any("Nominatim" in a for a in data["attribution"])
    assert any("Open-Meteo" in a for a in data["attribution"])
    # Nominatim requires a User-Agent.
    geo = [q for q in upstream.requests if q.url.host == "nominatim.test"]
    assert geo and all("pedestrian-routing" in q.headers["user-agent"] for q in geo)

    gpx = client.get(f"/gpx/{data['route_id']}.gpx")
    assert gpx.status_code == 200
    assert gpx.headers["content-type"].startswith("application/gpx+xml")
    assert 'filename="tower-walk.gpx"' in gpx.headers["content-disposition"]
    root = ET.fromstring(gpx.content)
    assert len(root.findall("g:wpt", NS)) == 3
    pts = root.findall("g:trk/g:trkseg/g:trkpt", NS)
    assert len(pts) == 21  # 2 legs x 11 points, shared junction de-duplicated
    assert all(p.find("g:ele", NS) is not None for p in pts)


def test_valhalla_way_types_and_options(client, upstream):
    r = _route(client, [COORD_A, COORD_B], provider="valhalla", options={"avoid_hills": True, "avoid_steps": True})
    assert r.status_code == 200, r.text
    data = r.json()
    f = data["legs"][0]["stats"]["friendliness"]
    assert f["way_types_m"] == {"pedestrian": 1000.0, "busy_road": 300.0, "steps": 200.0}
    assert f["surfaces_m"]["paved_smooth"] == 1000.0
    assert f["way_type_score"] is not None
    assert any("steps" in n for n in f["notes"])
    assert data["legs"][0]["turn_count"] == 1
    assert data["legs"][0]["stats"]["distance_m"] == 1500.0
    route_req = next(q for q in upstream.requests if q.url.path == "/route" and q.url.host == "valhalla.test")
    costing = json.loads(route_req.content)["costing_options"]["pedestrian"]
    assert costing["use_hills"] == 0.0 and costing["step_penalty"] > 0


def test_valhalla_trace_failure_is_a_warning(client, upstream):
    upstream.fail_paths.add("/trace_attributes")
    r = _route(client, [COORD_A, COORD_B], provider="valhalla")
    assert r.status_code == 200
    data = r.json()
    assert data["legs"][0]["stats"]["friendliness"]["way_types_m"] == {}
    assert any("way-type details unavailable" in w for w in data["warnings"])


def test_openrouteservice_native_elevation(client, upstream):
    r = _route(client, [COORD_A, COORD_B], provider="openrouteservice", options={"avoid_steps": True})
    assert r.status_code == 200, r.text
    data = r.json()
    leg = data["legs"][0]
    assert leg["stats"]["elevation_gain_m"] == pytest.approx(10.0)
    assert leg["stats"]["min_elevation_m"] == 1000.0
    assert set(leg["stats"]["friendliness"]["way_types_m"]) == {"pedestrian", "quiet_street"}
    assert leg["stats"]["friendliness"]["surfaces_m"].keys() == {"asphalt"}
    assert leg["main_streets"] == ["Bow Trail"]
    # Native elevation -> no Open-Meteo call.
    assert not any(q.url.host == "elevation.test" for q in upstream.requests)


def test_graphhopper(client):
    r = _route(client, [COORD_A, COORD_B], provider="gh")
    assert r.status_code == 200, r.text
    leg = r.json()["legs"][0]
    assert leg["stats"]["elevation_loss_m"] == pytest.approx(10.0)
    assert leg["stats"]["provider_duration_min"] == pytest.approx(16.7)
    assert set(leg["stats"]["friendliness"]["way_types_m"]) == {"pedestrian", "busy_road"}
    assert leg["turn_count"] == 1


def test_include_gpx_and_geometry(client):
    r = _route(client, [COORD_A, COORD_B], include_gpx=True, include_geometry=True)
    data = r.json()
    assert data["gpx"].startswith("<?xml")
    assert len(data["legs"][0]["coordinates"]) == 11


def test_errors(client, upstream):
    r = _route(client, [COORD_A, "Nowhere that exists"])
    assert r.status_code == 422 and "Could not find" in r.json()["detail"]
    r = _route(client, [COORD_A, COORD_B], provider="nope")
    assert r.status_code == 400
    r = _route(client, [COORD_A])
    assert r.status_code == 422
    upstream.fail_hosts.add("osrm.test")
    r = _route(client, [COORD_A, COORD_B])
    assert r.status_code == 502 and "Leg 1" in r.json()["detail"]
    assert client.get("/gpx/doesnotexist.gpx").status_code == 404


def test_elevation_failure_is_a_warning(client, upstream):
    upstream.fail_hosts.add("elevation.test")
    r = _route(client, [COORD_A, COORD_B])
    assert r.status_code == 200
    data = r.json()
    assert data["summary"]["elevation_gain_m"] is None
    assert any("Elevation unavailable" in w for w in data["warnings"])


def test_too_many_stops(make_client, settings_with):
    with make_client(settings_with(max_stops=2)) as c:
        r = _route(c, [COORD_A, COORD_B, COORD_A])
        assert r.status_code == 422 and "maximum" in r.json()["detail"]


def test_get_route_and_stateless_gpx(client):
    r = client.get("/route", params={"stops": f"{COORD_A}|Calgary Tower", "names": "Start|Tower"})
    assert r.status_code == 200, r.text
    assert [s["name"] for s in r.json()["stops"]] == ["Start", "Tower"]
    r = client.get("/route.gpx", params=[("stops", COORD_A), ("stops", COORD_B), ("route_name", "Quick one")])
    assert r.status_code == 200
    assert 'filename="quick-one.gpx"' in r.headers["content-disposition"]
    ET.fromstring(r.content)
    assert client.get("/route.gpx", params={"stops": COORD_A}).status_code == 422


def test_compare_providers(client, upstream):
    upstream.fail_hosts.add("gh.test")
    r = client.post("/route/compare", json={"stops": [{"location": COORD_A}, {"location": COORD_B}]})
    assert r.status_code == 200
    results = {x["provider"]: x for x in r.json()["results"]}
    assert set(results) == {"osrm", "valhalla", "openrouteservice", "graphhopper"}
    assert results["osrm"]["ok"] and results["osrm"]["gpx_url"]
    assert not results["graphhopper"]["ok"] and "503" in results["graphhopper"]["error"]


def test_openapi_has_servers_for_chatgpt(client):
    schema = client.get("/openapi.json").json()
    assert schema["servers"] == [{"url": "https://walk.example.com"}]
    ops = {op["operationId"] for path in schema["paths"].values() for op in path.values()}
    assert {"planWalkingRoute", "planWalkingRouteGet", "listProviders", "getGpx", "downloadRouteGpx"} <= ops


def test_api_key_auth(make_client, settings_with):
    with make_client(settings_with(api_key="s3cret")) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/openapi.json").status_code == 200
        assert c.get("/providers").status_code == 401
        assert c.get("/providers", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/providers", headers={"X-API-Key": "s3cret"}).status_code == 200
        r = c.post(
            "/route",
            json={"stops": [{"location": COORD_A}, {"location": COORD_B}]},
            headers={"Authorization": "Bearer " + "s3cret"},
        )
        assert r.status_code == 200
        # GPX downloads stay public (unguessable ids) so users can open them on their phone.
        assert c.get(f"/gpx/{r.json()['route_id']}.gpx").status_code == 200
        assert c.post("/mcp", json={}).status_code == 401
