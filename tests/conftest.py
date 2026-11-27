"""Shared fixtures: a fake upstream (routing, geocoding, elevation) served
through ``httpx.MockTransport`` so tests never touch the network."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.elevation import OpenMeteoElevation
from app.main import create_app
from app.planner import RoutePlanner

PLACES = {
    "calgary tower": (51.0443, -114.0631, "Calgary Tower, 101 9 Ave SW, Calgary, Alberta, Canada"),
    "prince's island park": (51.0539, -114.0695, "Prince's Island Park, Calgary, Alberta, Canada"),
}


def encode_polyline(coords, precision=6) -> str:
    factor = 10**precision
    out = []
    prev_lat = prev_lon = 0
    for lat, lon in coords:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for delta in (ilat - prev_lat, ilon - prev_lon):
            v = ~(delta << 1) if delta < 0 else delta << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def path_between(a, b, n=10):
    """A slightly bent path between two (lat, lon) points."""
    pts = []
    for i in range(n + 1):
        t = i / n
        bend = 0.0005 * (1 - abs(2 * t - 1))
        pts.append((a[0] + (b[0] - a[0]) * t + bend, a[1] + (b[1] - a[1]) * t))
    return pts


def fake_elevation(lat: float, lon: float) -> float:
    return 1000.0 + (lat - 51.0) * 5000.0  # ~0.55 m per 1e-4 deg of latitude


class FakeUpstream:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.fail_hosts: set[str] = set()
        self.fail_paths: set[str] = set()
        # Status codes for successive elevation requests (200 = normal answer).
        self.elevation_statuses: list[int] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        path = request.url.path
        if host in self.fail_hosts or path in self.fail_paths:
            return httpx.Response(503, json={"message": "unavailable"})
        if host == "nominatim.test":
            q = request.url.params["q"].lower()
            if q in PLACES:
                lat, lon, name = PLACES[q]
                return httpx.Response(200, json=[{"lat": str(lat), "lon": str(lon), "display_name": name}])
            return httpx.Response(200, json=[])
        if host == "elevation.test":
            status = self.elevation_statuses.pop(0) if self.elevation_statuses else 200
            if status != 200:
                return httpx.Response(status, headers={"Retry-After": "0"}, json={"reason": "Too many requests"})
            lats = [float(x) for x in request.url.params["latitude"].split(",")]
            lons = [float(x) for x in request.url.params["longitude"].split(",")]
            assert len(lats) <= 100
            return httpx.Response(200, json={"elevation": [fake_elevation(a, b) for a, b in zip(lats, lons)]})
        if host == "osrm.test":
            raw = path.split("/foot/")[1]
            (lon1, lat1), (lon2, lat2) = [tuple(map(float, p.split(","))) for p in raw.split(";")]
            pts = path_between((lat1, lon1), (lat2, lon2))
            return httpx.Response(
                200,
                json={
                    "code": "Ok",
                    "routes": [
                        {
                            "distance": 1234.5,
                            "duration": 900.0,
                            "geometry": {"type": "LineString", "coordinates": [[lon, lat] for lat, lon in pts]},
                            "legs": [
                                {
                                    "steps": [
                                        {"name": "Stephen Avenue", "distance": 800, "mode": "walking",
                                         "maneuver": {"type": "depart"}},
                                        {"name": "1 Street SW", "distance": 434.5, "mode": "walking",
                                         "maneuver": {"type": "turn", "modifier": "left"}},
                                        {"name": "", "distance": 0, "mode": "walking",
                                         "maneuver": {"type": "arrive"}},
                                    ]
                                }
                            ],
                        }
                    ],
                },
            )
        if host == "valhalla.test":
            body = json.loads(request.content)
            if path == "/route":
                a, b = body["locations"]
                pts = path_between((a["lat"], a["lon"]), (b["lat"], b["lon"]))
                return httpx.Response(
                    200,
                    json={
                        "trip": {
                            "legs": [
                                {
                                    "shape": encode_polyline(pts),
                                    "summary": {"length": 1.5, "time": 1080},
                                    "maneuvers": [
                                        {"type": 1, "street_names": ["Bow River Pathway"], "length": 1.0},
                                        {"type": 15, "street_names": ["Centre Street"], "length": 0.5},
                                        {"type": 4, "length": 0.0},
                                    ],
                                }
                            ]
                        }
                    },
                )
            if path == "/trace_attributes":
                return httpx.Response(
                    200,
                    json={
                        "edges": [
                            {"use": "footway", "road_class": "service_other", "surface": "paved_smooth", "length": 1.0},
                            {"use": "road", "road_class": "primary", "surface": "paved", "length": 0.3},
                            {"use": "steps", "road_class": "service_other", "surface": "paved", "length": 0.2},
                        ]
                    },
                )
        if host == "ors.test":
            if request.headers.get("authorization") != "ors-key":
                return httpx.Response(403, json={"error": "Access to this API has been disallowed"})
            body = json.loads(request.content)
            (lon1, lat1), (lon2, lat2) = body["coordinates"]
            pts = path_between((lat1, lon1), (lat2, lon2))
            coords = [[lon, lat, 1000.0 + i] for i, (lat, lon) in enumerate(pts)]
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {"type": "LineString", "coordinates": coords},
                            "properties": {
                                "summary": {"distance": 1500.0, "duration": 1080.0},
                                "segments": [
                                    {"steps": [
                                        {"type": 11, "name": "Bow Trail", "distance": 1000},
                                        {"type": 0, "name": "-", "distance": 500},
                                        {"type": 10, "name": "-", "distance": 0},
                                    ]}
                                ],
                                "extras": {
                                    "waytypes": {"values": [[0, 5, 7], [5, 10, 3]]},
                                    "surface": {"values": [[0, 10, 3]]},
                                },
                            },
                        }
                    ],
                },
            )
        if host == "gh.test":
            body = json.loads(request.content)
            (lon1, lat1), (lon2, lat2) = body["points"]
            pts = path_between((lat1, lon1), (lat2, lon2))
            coords = [[lon, lat, 1000.0 - i] for i, (lat, lon) in enumerate(pts)]
            return httpx.Response(
                200,
                json={
                    "paths": [
                        {
                            "distance": 1400.0,
                            "time": 1_000_000,
                            "points": {"type": "LineString", "coordinates": coords},
                            "instructions": [
                                {"sign": 0, "street_name": "Memorial Drive", "distance": 1000},
                                {"sign": 2, "street_name": "", "distance": 400},
                                {"sign": 4, "street_name": "", "distance": 0},
                            ],
                            "details": {
                                "road_class": [[0, 8, "footway"], [8, 10, "primary"]],
                                "surface": [[0, 10, "asphalt"]],
                            },
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"message": f"unexpected request {request.url}"})


@pytest.fixture(autouse=True)
def _no_elevation_retry_delay(monkeypatch):
    monkeypatch.setattr(OpenMeteoElevation, "retry_delay_s", 0.0)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        osrm_base_url="https://osrm.test/routed-foot",
        valhalla_base_url="https://valhalla.test",
        ors_base_url="https://ors.test",
        ors_api_key="ors-key",
        graphhopper_base_url="https://gh.test/api/1",
        graphhopper_api_key="gh-key",
        nominatim_base_url="https://nominatim.test",
        nominatim_min_interval_s=0.0,
        open_meteo_base_url="https://elevation.test",
        public_base_url="https://walk.example.com",
    )


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def make_client(upstream):
    def _make(settings: Settings) -> TestClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        planner = RoutePlanner(settings, http)
        return TestClient(create_app(settings, planner))

    return _make


@pytest.fixture
def client(make_client, settings):
    with make_client(settings) as c:
        yield c


@pytest.fixture
def settings_with(settings):
    def _with(**kwargs) -> Settings:
        return replace(settings, **kwargs)

    return _with
