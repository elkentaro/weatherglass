#!/usr/bin/env python3
"""
WeatherGlass — Standalone weather dashboard
Standalone weather dashboard with configurable location.
"""

import logging
import math
import time
import threading
import requests
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("weatherglass")

# ─── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_LAT = 35.5627
DEFAULT_LNG = 139.6899
DEFAULT_LABEL = "Tokyo, Japan"
DEFAULT_TZ = "Asia/Tokyo"

# ─── Caches ──────────────────────────────────────────────────────────────────

_weather_cache = {}  # keyed by "lat,lng" → {"data", "ts"}
_weather_cache_lock = threading.Lock()
WEATHER_CACHE_TTL = 600  # 10 minutes

_iss_cache = {"data": None, "ts": 0}
ISS_CACHE_TTL = 3

# ─── AMeDAS (JMA Japan-only station observations) ───────────────────────────

_amedas_cache = {"data": None, "ts": 0, "station": None, "lat": None, "lng": None}
_amedas_cache_lock = threading.Lock()
AMEDAS_CACHE_TTL = 300

_amedas_table_cache = {"data": None, "ts": 0}

JMA_AMEDAS_BASE = "https://www.jma.go.jp/bosai/amedas/data"
JMA_AMEDAS_TABLE = "https://www.jma.go.jp/bosai/amedas/const/amedastable.json"
JMA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0",
    "Accept": "application/json,*/*",
}


def _get_amedas_table():
    now = time.time()
    if _amedas_table_cache["data"] and (now - _amedas_table_cache["ts"]) < 86400:
        return _amedas_table_cache["data"]
    try:
        r = requests.get(JMA_AMEDAS_TABLE, headers=JMA_HEADERS, timeout=10)
        r.raise_for_status()
        _amedas_table_cache["data"] = r.json()
        _amedas_table_cache["ts"] = now
        return _amedas_table_cache["data"]
    except Exception as e:
        log.warning(f"AMeDAS table fetch failed: {e}")
        return _amedas_table_cache["data"]


def _find_nearest_station(lat, lon, table, obs_data=None):
    best_id, best_dist = None, float("inf")
    for sid, info in table.items():
        if obs_data:
            sobs = obs_data.get(sid)
            if not sobs or "temp" not in sobs:
                continue
            if isinstance(sobs["temp"], list) and len(sobs["temp"]) >= 2 and sobs["temp"][1] != 0:
                continue
        slat = info["lat"][0] + info["lat"][1] / 60.0
        slon = info["lon"][0] + info["lon"][1] / 60.0
        d = math.sqrt((slat - lat) ** 2 + (slon - lon) ** 2)
        if d < best_dist:
            best_dist = d
            best_id = sid
    return best_id, best_dist


def _is_japan(lat, lng):
    """Check if coordinates are roughly within Japan's bounding box."""
    return 24.0 <= lat <= 46.0 and 122.0 <= lng <= 154.0


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("weather.html")


@app.route("/api/weather")
def api_weather():
    """Return cached Open-Meteo forecast. Accepts ?lat=...&lng=... query params."""
    lat = request.args.get("lat", DEFAULT_LAT, type=float)
    lng = request.args.get("lng", DEFAULT_LNG, type=float)
    tz = request.args.get("tz", "auto")

    cache_key = f"{lat:.4f},{lng:.4f}"
    now = time.time()

    with _weather_cache_lock:
        cached = _weather_cache.get(cache_key)
        if cached and (now - cached["ts"]) < WEATHER_CACHE_TTL:
            return jsonify({
                "weather": cached["data"],
                "cached": True,
                "age": int(now - cached["ts"]),
            })

    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lng}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
        "weather_code,wind_speed_10m,wind_direction_10m,precipitation"
        "&hourly=temperature_2m,relative_humidity_2m,precipitation_probability,precipitation,"
        "weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_sum,precipitation_probability_max,wind_speed_10m_max,"
        "sunrise,sunset"
        f"&timezone={tz}&forecast_days=4"
    )

    try:
        log.info(f"Weather fetch: lat={lat} lng={lng} tz={tz}")
        log.info(f"Weather URL: {url}")
        r = requests.get(url, timeout=15)
        log.info(f"Weather response: status={r.status_code} size={len(r.content)}")
        if not r.ok:
            log.error(f"Weather API error response: {r.text[:500]}")
        r.raise_for_status()
        data = r.json()
        # Check if Open-Meteo returned an error in JSON
        if "error" in data and "reason" in data:
            log.error(f"Open-Meteo error: {data}")
            return jsonify({"error": data.get("reason", "Unknown API error")}), 502
        log.info(f"Weather OK: keys={list(data.keys())}")
        with _weather_cache_lock:
            _weather_cache[cache_key] = {"data": data, "ts": now}
        return jsonify({"weather": data, "cached": False, "age": 0})
    except Exception as e:
        with _weather_cache_lock:
            cached = _weather_cache.get(cache_key)
            if cached:
                return jsonify({
                    "weather": cached["data"],
                    "cached": True,
                    "age": int(now - cached["ts"]),
                    "error": str(e),
                })
        return jsonify({"error": str(e)}), 502


@app.route("/api/amedas")
def api_amedas():
    """Return live AMeDAS observation for nearest station. Japan only."""
    lat = request.args.get("lat", DEFAULT_LAT, type=float)
    lng = request.args.get("lng", DEFAULT_LNG, type=float)

    if not _is_japan(lat, lng):
        return jsonify({"error": "AMeDAS only available in Japan", "unavailable": True}), 200

    now = time.time()
    with _amedas_cache_lock:
        if (_amedas_cache["data"]
                and (now - _amedas_cache["ts"]) < AMEDAS_CACHE_TTL
                and _amedas_cache["lat"] == round(lat, 4)
                and _amedas_cache["lng"] == round(lng, 4)):
            return jsonify({
                "observation": _amedas_cache["data"],
                "station": _amedas_cache["station"],
                "cached": True,
                "age": int(now - _amedas_cache["ts"]),
            })

    try:
        table = _get_amedas_table()
        if not table:
            return jsonify({"error": "Could not fetch station table"}), 502

        r = requests.get(f"{JMA_AMEDAS_BASE}/latest_time.txt", headers=JMA_HEADERS, timeout=5)
        r.raise_for_status()
        latest_str = r.text.strip()
        time_key = latest_str[:19].replace("-", "").replace("T", "").replace(":", "")

        map_url = f"{JMA_AMEDAS_BASE}/map/{time_key}.json"
        r = requests.get(map_url, headers=JMA_HEADERS, timeout=10)
        r.raise_for_status()
        map_data = r.json()

        if not map_data:
            return jsonify({"error": "Empty map data"}), 502

        station_id, station_dist = _find_nearest_station(lat, lng, table, map_data)
        if not station_id or station_id not in map_data:
            return jsonify({"error": "No nearby station with temperature data"}), 502

        # If nearest station is too far (>0.5 degrees ≈ 50km), skip
        if station_dist > 0.5:
            return jsonify({"error": "No nearby AMeDAS station", "unavailable": True}), 200

        station_info = table[station_id]
        station_meta = {
            "id": station_id,
            "name_jp": station_info.get("kjName", ""),
            "name_en": station_info.get("enName", ""),
            "lat": station_info["lat"][0] + station_info["lat"][1] / 60.0,
            "lon": station_info["lon"][0] + station_info["lon"][1] / 60.0,
            "alt": station_info.get("alt", 0),
        }

        obs = map_data[station_id]

        def val(key):
            v = obs.get(key)
            if v is None:
                return None
            if isinstance(v, list) and len(v) >= 2:
                return v[0] if v[1] == 0 else None
            return v

        observation = {
            "time": latest_str,
            "temp": val("temp"),
            "humidity": val("humidity"),
            "wind_speed": val("wind"),
            "wind_direction": val("windDirection"),
            "precipitation_1h": val("precipitation1h"),
            "precipitation_10m": val("precipitation10m"),
            "pressure": val("normalPressure"),
            "sun_1h": val("sun1h"),
            "snow": val("snow"),
        }

        with _amedas_cache_lock:
            _amedas_cache["data"] = observation
            _amedas_cache["station"] = station_meta
            _amedas_cache["ts"] = now
            _amedas_cache["lat"] = round(lat, 4)
            _amedas_cache["lng"] = round(lng, 4)

        return jsonify({
            "observation": observation,
            "station": station_meta,
            "cached": False,
            "age": 0,
        })

    except Exception as e:
        log.error(f"AMeDAS fetch error: {e}")
        with _amedas_cache_lock:
            if _amedas_cache["data"]:
                return jsonify({
                    "observation": _amedas_cache["data"],
                    "station": _amedas_cache["station"],
                    "cached": True,
                    "age": int(now - _amedas_cache["ts"]),
                    "error": str(e),
                })
        return jsonify({"error": str(e)}), 502


@app.route("/api/iss")
def api_iss():
    """Return current ISS position."""
    now = time.time()
    if _iss_cache["data"] and (now - _iss_cache["ts"]) < ISS_CACHE_TTL:
        return jsonify(_iss_cache["data"])
    try:
        r = requests.get("https://api.wheretheiss.at/v1/satellites/25544", timeout=5)
        r.raise_for_status()
        data = r.json()
        _iss_cache["data"] = data
        _iss_cache["ts"] = now
        return jsonify(data)
    except Exception as e:
        log.warning(f"ISS fetch failed: {e}")
        if _iss_cache["data"]:
            return jsonify(_iss_cache["data"])
        return jsonify({"error": str(e)}), 502


@app.route("/api/health")
def api_health():
    """Quick connectivity check — tests outbound HTTP to Open-Meteo."""
    results = {}
    for name, url in [
        ("open-meteo", "https://api.open-meteo.com/v1/forecast?latitude=35.56&longitude=139.69&current=temperature_2m&forecast_days=1"),
        ("rainviewer", "https://api.rainviewer.com/public/weather-maps.json"),
        ("iss", "https://api.wheretheiss.at/v1/satellites/25544"),
        ("jma-amedas", "https://www.jma.go.jp/bosai/amedas/data/latest_time.txt"),
    ]:
        try:
            t0 = time.time()
            r = requests.get(url, timeout=8)
            elapsed = round((time.time() - t0) * 1000)
            results[name] = {
                "status": r.status_code,
                "ok": r.ok,
                "ms": elapsed,
                "bytes": len(r.content),
            }
        except Exception as e:
            results[name] = {"ok": False, "error": str(e)}
    log.info(f"Health check: {results}")
    return jsonify(results)


@app.route("/api/timezone")
def api_timezone():
    """Reverse-geocode timezone from coordinates using Open-Meteo."""
    lat = request.args.get("lat", type=float)
    lng = request.args.get("lng", type=float)
    if lat is None or lng is None:
        return jsonify({"error": "lat and lng required"}), 400
    try:
        r = requests.get(
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lng}"
            "&current=temperature_2m&timezone=auto&forecast_days=1",
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        return jsonify({
            "timezone": data.get("timezone", "auto"),
            "timezone_abbreviation": data.get("timezone_abbreviation", ""),
            "utc_offset_seconds": data.get("utc_offset_seconds", 0),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5099, debug=False)
