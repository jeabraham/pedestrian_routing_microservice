"""GPX 1.1 writer.

The output is a single ``<trk>`` with one ``<trkseg>`` (the whole walk, with
elevations) plus a ``<wpt>`` per stop.  This is the layout the Suunto app (and
Garmin Connect, Komoot, Strava, ...) import cleanly as a route that can be
synced to a watch for navigation.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Sequence
from xml.etree import ElementTree as ET

from app.geo import LatLon

GPX_NS = "http://www.topografix.com/GPX/1/1"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
CREATOR = "pedestrian-routing-microservice"


def slugify(text: str, default: str = "walking-route") -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug[:80] or default


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrib: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrib)
    if text is not None:
        el.text = text
    return el


def build_gpx(
    name: str,
    description: str,
    track: Sequence[LatLon],
    elevations: Sequence[float] | None,
    waypoints: Sequence[tuple[str, LatLon, str | None]],
) -> str:
    """Build a GPX document.

    ``waypoints`` are ``(name, (lat, lon), description)`` tuples.
    """
    root = ET.Element(
        "gpx",
        {
            "version": "1.1",
            "creator": CREATOR,
            "xmlns": GPX_NS,
            "xmlns:xsi": XSI_NS,
            "xsi:schemaLocation": f"{GPX_NS} http://www.topografix.com/GPX/1/1/gpx.xsd",
        },
    )
    metadata = _sub(root, "metadata")
    _sub(metadata, "name", name)
    _sub(metadata, "desc", description)
    _sub(metadata, "time", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    for wp_name, (lat, lon), wp_desc in waypoints:
        wpt = _sub(root, "wpt", lat=f"{lat:.6f}", lon=f"{lon:.6f}")
        _sub(wpt, "name", wp_name)
        if wp_desc:
            _sub(wpt, "desc", wp_desc)
        _sub(wpt, "sym", "Flag, Blue")

    trk = _sub(root, "trk")
    _sub(trk, "name", name)
    _sub(trk, "desc", description)
    _sub(trk, "type", "walking")
    seg = _sub(trk, "trkseg")
    for i, (lat, lon) in enumerate(track):
        pt = _sub(seg, "trkpt", lat=f"{lat:.6f}", lon=f"{lon:.6f}")
        if elevations is not None:
            _sub(pt, "ele", f"{elevations[i]:.1f}")

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
