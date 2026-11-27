"""Turn stop strings into coordinates.

``"lat,lon"`` strings are parsed directly; anything else is geocoded with
Nominatim (OpenStreetMap).  Nominatim's usage policy requires a descriptive
User-Agent and at most one request per second, both enforced here, and results
are cached in memory.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict

import httpx

from app.config import Settings
from app.models import GeocodeResult

_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


class GeocodingError(Exception):
    pass


def parse_coordinates(text: str) -> tuple[float, float] | None:
    """Parse ``"lat,lon"``; returns ``None`` if the text is not a coordinate pair."""
    text = text.strip()
    if len(text) > 100:
        return None
    body = text[4:] if text[:4].lower() == "geo:" else text
    body = body.replace(";", ",")
    parts = [p.strip() for p in body.split(",")] if "," in body else body.split()
    if len(parts) != 2 or not all(_NUMBER_RE.fullmatch(p) for p in parts):
        return None
    lat, lon = float(parts[0]), float(parts[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise GeocodingError(f"Coordinates out of range (expected 'latitude,longitude'): {text!r}")
    return lat, lon


class Geocoder:
    attribution = "Geocoding © OpenStreetMap contributors via Nominatim"

    def __init__(self, settings: Settings, client: httpx.AsyncClient, cache_size: int = 2000):
        self.base_url = settings.nominatim_base_url.rstrip("/")
        self.user_agent = settings.nominatim_user_agent
        self.min_interval = settings.nominatim_min_interval_s
        self.client = client
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._cache: OrderedDict[str, GeocodeResult] = OrderedDict()
        self._cache_size = cache_size

    async def resolve(self, text: str) -> GeocodeResult:
        text = (text or "").strip()
        if not text:
            raise GeocodingError("Empty stop location")
        coords = parse_coordinates(text)
        if coords is not None:
            return GeocodeResult(query=text, lat=coords[0], lon=coords[1], source="coordinates")
        return await self.geocode(text)

    async def geocode(self, query: str) -> GeocodeResult:
        key = query.lower()
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self.client.get(
                    f"{self.base_url}/search",
                    params={"q": query, "format": "jsonv2", "limit": 1},
                    headers={"User-Agent": self.user_agent, "Accept-Language": "en"},
                )
            except httpx.HTTPError as exc:
                raise GeocodingError(f"Geocoding request failed for {query!r}: {exc}") from exc
            finally:
                self._last_request = time.monotonic()
        if resp.status_code >= 400:
            raise GeocodingError(f"Geocoding failed for {query!r}: HTTP {resp.status_code}")
        try:
            results = resp.json()
        except ValueError as exc:
            raise GeocodingError(f"Geocoding failed for {query!r}: invalid response") from exc
        if not results:
            raise GeocodingError(
                f"Could not find {query!r}. Add the city/country or give coordinates as 'latitude,longitude'."
            )
        top = results[0]
        result = GeocodeResult(
            query=query,
            lat=float(top["lat"]),
            lon=float(top["lon"]),
            display_name=top.get("display_name"),
            source="geocoded",
        )
        self._cache[key] = result
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return result
