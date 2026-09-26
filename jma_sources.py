"""
JMA-only data handling for Shindo Screener.

Every Shindo value this app shows comes from JMA itself. The app never
computes its own intensity forecast or arrival time for a location:
under Japan's Meteorological Service Act (気象業務法 Art. 17), issuing
your own seismic-motion forecast for specific places requires a JMA
forecast-business license. Relaying JMA's own values does not.

What this module relays:
  1. During an Earthquake Early Warning (via Wolfx):
     - JMA's forecast for the user's forecast area, but only when JMA
       itself lists that area in a warning (WarnArea). JMA publishes
       per-area forecasts only for warnings.
     - JMA's predicted maximum intensity for the quake overall.
  2. After shaking (via P2P地震情報, which relays JMA's reports):
     - The intensity JMA measured at the seismometer station nearest
       the user (震源・震度情報, a few minutes after the quake).
     - The intensity JMA measured for the user's forecast area
       (震度速報, about 1.5 minutes after quakes of Shindo 3 or more).

"Nearest station" and "your forecast area" are lookups, not forecasts:
the app picks which of JMA's published numbers to show, it never makes
a new one.
"""

import json
import math
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
STEP_ORDER = ["0", "1", "2", "3", "4", "5-", "5+", "6-", "6+", "7"]
STEP_RANK = {s: i for i, s in enumerate(STEP_ORDER)}

# P2P地震情報 scale codes -> JMA steps. 46 means "5- or more, estimated"
# (JMA had not received the station's data yet); shown as 5-.
P2P_SCALE = {10: "1", 20: "2", 30: "3", 40: "4", 45: "5-", 46: "5-", 50: "5+", 55: "6-", 60: "6+", 70: "7"}

_SHINDO_TEXT = {"5弱": "5-", "5強": "5+", "6弱": "6-", "6強": "6+",
                "5-": "5-", "5+": "5+", "6-": "6-", "6+": "6+",
                "0": "0", "1": "1", "2": "2", "3": "3", "4": "4", "7": "7"}

NEARBY_RADIUS_KM = 25     # stations further than this don't count as "near you"
NEARBY_LIMIT = 12


def normalize_shindo(value):
    """'5弱' / '5-' / 4 / '4' -> '5-' / '5-' / '4' / '4'; anything else (e.g. 'over', '不明') -> None."""
    if value is None:
        return None
    return _SHINDO_TEXT.get(str(value).strip())


def rank(step):
    return STEP_RANK.get(step, -1)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------- stations

def load_stations(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [{"name": n, "lat": la, "lon": lo, "area": ar} for n, la, lo, ar in data["stations"]]


def locate_home(home, stations, radius_km=NEARBY_RADIUS_KM, limit=NEARBY_LIMIT):
    """
    Returns {"area": JMA forecast-area name for the user, "stations":
    nearby stations sorted by distance (each with distance_km)}.
    The area is the one the nearest station belongs to: JMA's station
    table is itself keyed to these areas, so this needs no map polygons.
    """
    ranked = sorted(
        ({**s, "distance_km": haversine_km(home["lat"], home["lon"], s["lat"], s["lon"])} for s in stations),
        key=lambda s: s["distance_km"],
    )
    if not ranked:
        return {"area": None, "stations": []}
    near = [s for s in ranked if s["distance_km"] <= radius_km][:limit] or ranked[:1]
    return {"area": ranked[0]["area"], "stations": near}


def _area_key(name):
    """Area names appear with and without 地方 (熊本県熊本地方 vs 熊本県熊本); compare without it."""
    return (name or "").replace("地方", "").strip()


# ---------------------------------------------------------------- EEW (Wolfx)

def parse_eew(payload, home_area):
    """
    Wolfx JMA EEW message -> relay dict, or None to ignore.
    Never computes a location forecast: area_forecast is present only
    if JMA's own warning lists the user's area.
    """
    if payload.get("type") not in (None, "jma_eew") or payload.get("isTraining"):
        return None
    event_id = str(payload.get("EventID") or "")
    if not event_id:
        return None
    if payload.get("isCancel"):
        return {"kind": "eew_cancel", "event_id": event_id}

    area_forecast = None
    for w in payload.get("WarnArea") or []:
        if _area_key(w.get("Chiiki")) == _area_key(home_area):
            # JMA gives a range (e.g. 5弱 to 5強) or an open end ("over": this level or more).
            # Field order isn't documented, so sort rather than assume.
            raw = [w.get("Shindo1"), w.get("Shindo2")]
            steps = sorted({s for s in map(normalize_shindo, raw) if s}, key=rank)
            if steps:
                area_forecast = {
                    "from": steps[0],
                    "to": steps[-1] if len(steps) > 1 else None,
                    "or_more": len(steps) == 1 and any(str(v).strip() in ("over", "以上", "") for v in raw),
                    "arrived": bool(w.get("Arrive")),
                }
            break

    try:
        mag = float(payload.get("Magnitude", payload.get("Magunitude")))
    except (TypeError, ValueError):
        mag = None
    try:
        lat, lon = float(payload.get("Latitude")), float(payload.get("Longitude"))
    except (TypeError, ValueError):
        lat = lon = None
    try:
        depth = float(payload.get("Depth"))
    except (TypeError, ValueError):
        depth = None

    return {
        "kind": "eew",
        "event_id": event_id,
        "serial": payload.get("Serial"),
        "origin_time": _parse_jst(payload.get("OriginTime")),
        "hypocenter": payload.get("Hypocenter") if isinstance(payload.get("Hypocenter"), str) else None,
        "magnitude": mag, "depth_km": depth, "lat": lat, "lon": lon,
        "jma_max_forecast": normalize_shindo(payload.get("MaxIntensity")),
        "is_warning": bool(payload.get("isWarn")),
        "is_final": bool(payload.get("isFinal")),
        "area_forecast": area_forecast,
        "accuracy": (payload.get("Accuracy") or {}).get("Epicenter") if isinstance(payload.get("Accuracy"), dict) else None,
    }


# ---------------------------------------------------------------- measured (P2P地震情報, code 551)

def parse_p2p_quake(msg, home):
    """
    P2P地震情報 JMAQuake (code 551) -> relay dict, or None.
    home = locate_home(...) result. Picks JMA's measured value at the
    nearest station near the user, else JMA's measured value for the
    user's area. Never interpolates or estimates.
    """
    if msg.get("code") != 551:
        return None
    issue_type = (msg.get("issue") or {}).get("type")
    if issue_type not in ("ScalePrompt", "ScaleAndDestination", "DetailScale"):
        return None
    eq = msg.get("earthquake") or {}
    hyp = eq.get("hypocenter") or {}
    points = msg.get("points") or []

    reading = None
    by_name = {}
    for p in points:
        step = P2P_SCALE.get(p.get("scale"))
        if step and not p.get("isArea"):
            by_name[p.get("addr")] = max(by_name.get(p.get("addr"), step), step, key=rank)
    # Only the station nearest the user speaks for the user's location. JMA's
    # detailed report lists every station that measured Shindo 1 or more, so
    # if the nearest station is absent there, it measured below 1. A further
    # station's reading is kept only as context, never as "your" reading.
    nearest = home["stations"][0] if home["stations"] else None
    nearby_max = None
    for s in home["stations"]:
        if s["name"] in by_name and (nearby_max is None or rank(by_name[s["name"]]) > rank(nearby_max["step"])):
            nearby_max = {"station": s["name"], "station_km": round(s["distance_km"], 1), "step": by_name[s["name"]]}
    if nearest and nearest["name"] in by_name:
        reading = {"source": "station", "step": by_name[nearest["name"]], "station": nearest["name"],
                   "station_km": round(nearest["distance_km"], 1)}
    elif nearest and issue_type == "DetailScale" and by_name:
        reading = {"source": "station_below_1", "step": "0", "station": nearest["name"],
                   "station_km": round(nearest["distance_km"], 1)}
    if reading is None and home.get("area"):
        for p in points:
            if p.get("isArea") and _area_key(p.get("addr")) == _area_key(home["area"]):
                step = P2P_SCALE.get(p.get("scale"))
                if step:
                    reading = {"source": "area", "step": step, "area": home["area"]}
                break

    def num(v):
        try:
            v = float(v)
            return None if v < 0 else v       # P2P uses -1 for "unknown"
        except (TypeError, ValueError):
            return None

    return {
        "kind": "measured",
        "report_type": issue_type,
        "origin_time": _parse_jst(eq.get("time")),
        "hypocenter": hyp.get("name") or None,
        "magnitude": num(hyp.get("magnitude")),
        "depth_km": num(hyp.get("depth")),
        "lat": num(hyp.get("latitude")) if hyp.get("latitude") not in (None, -200) else None,
        "lon": num(hyp.get("longitude")) if hyp.get("longitude") not in (None, -200) else None,
        "jma_max_measured": P2P_SCALE.get(eq.get("maxScale")),
        "tsunami": eq.get("domesticTsunami") if eq.get("domesticTsunami") not in (None, "None", "Unknown", "Checking") else None,
        "reading": reading,
        "nearby_max": nearby_max,
        "felt_near_user": reading is not None and reading["source"] != "station_below_1",
    }


def _parse_jst(text):
    """'2026/09/26 11:21:01' (JST) -> aware datetime, or None."""
    if not text:
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(str(text), fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    return None


def same_quake(t1, t2, tolerance_s=90):
    """EEW and measured reports use different IDs; match them by origin time."""
    return bool(t1 and t2 and abs((t1 - t2).total_seconds()) <= tolerance_s)
