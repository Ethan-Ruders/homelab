"""Geographic utilities: IGN Geoplateforme car isochrones & ray-casting."""
from __future__ import annotations

import json
import math
from typing import Any

import httpx

ZONES: dict[str, dict[str, Any]] = {
    "loperhet": {
        "label": "Loperhet / Brest Métropole",
        "lat": 48.3746,
        "lon": -4.3053,
    },
    "lannion": {
        "label": "Lannion / Trégor",
        "lat": 48.7321,
        "lon": -3.4591,
    },
    "caen": {
        "label": "Caen / Caen la Mer",
        "lat": 49.1829,
        "lon": -0.3707,
    },
}

IGN_ISOCHRONE_URL = "https://data.geopf.fr/navigation/isochrone"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in kilometers."""
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 2)


def fetch_ign_isochrone(lat: float, lon: float, seconds: int) -> dict[str, Any]:
    """Fetch driving isochrone GeoJSON geometry from IGN Geoplateforme."""
    params = {
        "resource": "bdtopo-valhalla",
        "point": f"{lon},{lat}",
        "profile": "car",
        "costType": "time",
        "costValue": str(seconds),
        "timeUnit": "second",
        "direction": "departure",
        "crs": "EPSG:4326",
    }
    with httpx.Client(timeout=25.0, headers={"User-Agent": "Homelab-Career-Radar/1.0"}) as client:
        resp = client.get(IGN_ISOCHRONE_URL, params=params)
        resp.raise_for_status()
        payload = resp.json()

    geom = payload.get("geometry")
    if not geom or not geom.get("type") or not geom.get("coordinates"):
        raise RuntimeError(f"IGN isochrone empty geometry for ({lat},{lon}) {seconds}s")
    return geom


def _point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    """Ray-casting point-in-ring check in (lon, lat) planar coords."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def _point_in_polygon_coords(lon: float, lat: float, rings: list[list[list[float]]]) -> bool:
    if not rings or not _point_in_ring(lon, lat, rings[0]):
        return False
    for hole in rings[1:]:
        if _point_in_ring(lon, lat, hole):
            return False
    return True


def point_in_geojson(lon: float | None, lat: float | None, geom: dict[str, Any] | None) -> bool:
    """Check whether (lon, lat) lies inside a Polygon or MultiPolygon GeoJSON geometry."""
    if lon is None or lat is None or not geom:
        return False
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if not coords:
        return False
    if gtype == "Polygon":
        return _point_in_polygon_coords(lon, lat, coords)
    if gtype == "MultiPolygon":
        return any(_point_in_polygon_coords(lon, lat, poly) for poly in coords)
    return False


def ensure_zones_isochrones(cur, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Load or fetch & cache IGN 20-min and 30-min car isochrones for all zones."""
    cur.execute("SELECT code, label, lat, lon, iso20_geojson, iso30_geojson FROM zones")
    existing = {row["code"]: dict(row) for row in cur.fetchall()}

    result: dict[str, dict[str, Any]] = {}
    for code, cfg in ZONES.items():
        cached = existing.get(code)
        if not force_refresh and cached and cached.get("iso20_geojson") and cached.get("iso30_geojson"):
            result[code] = cached
            continue

        iso20 = fetch_ign_isochrone(cfg["lat"], cfg["lon"], 1200)
        iso30 = fetch_ign_isochrone(cfg["lat"], cfg["lon"], 1800)
        cur.execute(
            """
            INSERT INTO zones (code, label, lat, lon, iso20_geojson, iso30_geojson, updated_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, NOW())
            ON CONFLICT (code) DO UPDATE SET
                label = EXCLUDED.label,
                lat = EXCLUDED.lat,
                lon = EXCLUDED.lon,
                iso20_geojson = EXCLUDED.iso20_geojson,
                iso30_geojson = EXCLUDED.iso30_geojson,
                updated_at = NOW()
            """,
            (code, cfg["label"], cfg["lat"], cfg["lon"], json.dumps(iso20), json.dumps(iso30)),
        )
        result[code] = {
            "code": code,
            "label": cfg["label"],
            "lat": cfg["lat"],
            "lon": cfg["lon"],
            "iso20_geojson": iso20,
            "iso30_geojson": iso30,
        }
    return result


def classify_travel_time(lon: float | None, lat: float | None, iso20: dict[str, Any], iso30: dict[str, Any]) -> str | None:
    """Return '<=20min', '20-30min', or None if outside the 30-min car isochrone."""
    if point_in_geojson(lon, lat, iso20):
        return "<=20min"
    if point_in_geojson(lon, lat, iso30):
        return "20-30min"
    return None
