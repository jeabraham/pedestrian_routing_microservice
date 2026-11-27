import json

COORD_A = "51.0447,-114.0719"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(client, method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    r = client.post("/mcp", json=body, headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _call(client, name, arguments):
    return _rpc(client, "tools/call", {"name": name, "arguments": arguments})["result"]


def test_initialize_and_list_tools(client):
    init = _rpc(
        client,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    )
    assert "plan_walking_route" in init["result"]["instructions"]
    tools = {t["name"]: t for t in _rpc(client, "tools/list")["result"]["tools"]}
    assert set(tools) == {
        "plan_walking_route",
        "compare_walking_routes",
        "list_routing_providers",
        "geocode_location",
        "get_route_gpx",
    }
    # Inputs are flat (no $ref) for maximum client compatibility.
    assert "$defs" not in json.dumps(tools["plan_walking_route"]["inputSchema"])


def test_plan_walking_route_tool_and_gpx(client):
    result = _call(
        client,
        "plan_walking_route",
        {"stops": [COORD_A, "Calgary Tower"], "stop_names": ["Home"], "provider": "valhalla", "route_name": "Beer run"},
    )
    assert not result.get("isError"), result
    data = result["structuredContent"]
    assert data["provider"] == "valhalla"
    assert [s["name"] for s in data["stops"]] == ["Home", "Calgary Tower"]
    assert data["gpx_url"].startswith("https://walk.example.com/gpx/")
    assert data["summary"]["friendliness"]["score"] is not None

    gpx = _call(client, "get_route_gpx", {"route_id": data["route_id"]})
    assert "<gpx" in gpx["content"][0]["text"]


def test_tool_errors_are_reported(client):
    result = _call(client, "plan_walking_route", {"stops": [COORD_A, "Nowhere that exists"]})
    assert result["isError"]
    assert "Could not find" in result["content"][0]["text"]
    result = _call(client, "get_route_gpx", {"route_id": "missing"})
    assert result["isError"]


def test_other_tools(client):
    providers = _call(client, "list_routing_providers", {})["structuredContent"]["result"]
    assert {p["id"] for p in providers} == {"osrm", "valhalla", "openrouteservice", "graphhopper"}
    geo = _call(client, "geocode_location", {"query": "Calgary Tower"})["structuredContent"]
    assert geo["source"] == "geocoded" and round(geo["lat"], 2) == 51.04
    cmp = _call(client, "compare_walking_routes", {"stops": [COORD_A, "Calgary Tower"], "providers": ["osrm", "ors"]})
    assert [r["ok"] for r in cmp["structuredContent"]["results"]] == [True, True]
