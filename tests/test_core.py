import math
from xml.etree import ElementTree as ET

import pytest

from app.elevation import elevation_stats
from app.friendliness import compute_friendliness
from app.geo import cumulative_distances, decode_polyline, haversine_m, sample_along
from app.geocoding import GeocodingError, parse_coordinates
from app.gpx import GPX_NS, build_gpx, slugify
from app.providers.graphhopper import classify_graphhopper_road_class
from app.providers.valhalla import classify_valhalla_edge
from tests.conftest import encode_polyline


def test_haversine_one_degree_latitude():
    assert haversine_m((0, 0), (1, 0)) == pytest.approx(111_195, rel=1e-3)


def test_decode_polyline_google_example():
    coords = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    assert coords == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


def test_decode_polyline6_roundtrip():
    pts = [(51.044301, -114.063102), (51.05, -114.07), (51.0539, -114.0695)]
    assert decode_polyline(encode_polyline(pts), precision=6) == pts


def test_sample_along_respects_spacing_and_limits():
    line = [(51.0, -114.0), (51.01, -114.0)]  # ~1112 m
    pts, dists = sample_along(line, 100, 1000)
    assert pts[0] == line[0] and pts[-1] == line[-1]
    assert len(pts) == 13
    assert dists[-1] == pytest.approx(cumulative_distances(line)[-1])
    pts, _ = sample_along(line, 1, 5)
    assert len(pts) == 5


@pytest.mark.parametrize(
    "text,expected",
    [("51.0447,-114.0719", (51.0447, -114.0719)), (" 51.0447, -114.0719 ", (51.0447, -114.0719)),
     ("geo:48.85,2.29", (48.85, 2.29)), ("-33.9 151.2", (-33.9, 151.2))],
)
def test_parse_coordinates(text, expected):
    assert parse_coordinates(text) == expected


def test_parse_coordinates_non_coordinate_and_out_of_range():
    assert parse_coordinates("221B Baker Street, London") is None
    assert parse_coordinates("Pier 39") is None
    with pytest.raises(GeocodingError):
        parse_coordinates("114.07,51.04")  # lon,lat swapped -> latitude out of range


def test_elevation_stats_up_and_down():
    # 0 -> 1000 m climbing 50 m, then 1000 -> 2000 m descending 30 m.
    d = [0, 500, 1000, 1500, 2000]
    e = [100, 125, 150, 135, 120]
    stats = elevation_stats(d, e)
    assert stats.gain_m == pytest.approx(50, abs=0.5)
    assert stats.loss_m == pytest.approx(30, abs=0.5)
    assert stats.min_m == 100 and stats.max_m == 150
    assert stats.max_grade_pct == pytest.approx(5.0, abs=0.2)
    assert stats.steep_distance_m == 0


def test_elevation_stats_ignores_small_noise_and_detects_steep():
    d = list(range(0, 1001, 10))
    noisy = [100 + (0.5 if i % 2 else 0) for i in range(len(d))]
    assert elevation_stats(d, noisy).gain_m < 1.5
    steep = [100 + x * 0.12 for x in d]
    stats = elevation_stats(d, steep)
    assert stats.steep_distance_m == pytest.approx(1000, abs=1)
    assert stats.max_grade_pct == pytest.approx(12, abs=0.1)


def test_friendliness_prefers_footways_over_busy_roads():
    good = compute_friendliness(1000, 900, {"pedestrian": 1000}, {}, None)
    bad = compute_friendliness(1000, 900, {"busy_road": 1000}, {}, None)
    assert good.score > bad.score
    assert good.rating in ("excellent", "good")
    assert any("busy roads" in n for n in bad.notes)


def test_friendliness_without_way_types_uses_directness():
    f = compute_friendliness(3000, 1000, {}, {}, None)
    assert f.way_type_score is None
    assert f.detour_factor == 3.0
    assert f.score == f.directness_score == 0.0
    assert compute_friendliness(10, 0, {}, {}, None).score is None


def test_way_type_classification():
    assert classify_valhalla_edge("sidewalk", "residential") == "pedestrian"
    assert classify_valhalla_edge("road", "primary") == "busy_road"
    assert classify_valhalla_edge("road", "residential") == "quiet_street"
    assert classify_valhalla_edge("steps", None) == "steps"
    assert classify_graphhopper_road_class("FOOTWAY") == "pedestrian"
    assert classify_graphhopper_road_class("trunk") == "busy_road"
    assert classify_graphhopper_road_class("weird") == "unknown"


def test_build_gpx_is_valid_gpx11():
    xml = build_gpx(
        "Pizza & beer <run>",
        "desc",
        [(51.0, -114.0), (51.001, -114.001)],
        [1000.0, 1001.5],
        [("Start", (51.0, -114.0), None), ("Liquor store", (51.001, -114.001), "123 Main St")],
    )
    root = ET.fromstring(xml.encode())
    ns = {"g": GPX_NS}
    assert root.tag == f"{{{GPX_NS}}}gpx" and root.get("version") == "1.1"
    assert root.find("g:metadata/g:name", ns).text == "Pizza & beer <run>"
    assert [w.find("g:name", ns).text for w in root.findall("g:wpt", ns)] == ["Start", "Liquor store"]
    pts = root.findall("g:trk/g:trkseg/g:trkpt", ns)
    assert len(pts) == 2
    assert pts[1].find("g:ele", ns).text == "1001.5"
    assert math.isclose(float(pts[1].get("lat")), 51.001)


def test_slugify():
    assert slugify("Café crawl: Montréal!") == "cafe-crawl-montreal"
    assert slugify("!!!") == "walking-route"
