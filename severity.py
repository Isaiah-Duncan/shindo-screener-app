"""
Deterministic severity calculation.

This module is intentionally the ONLY place that turns
(magnitude, depth, location) into an estimated felt intensity at home.
The VLM never touches this logic: it only ever hands this module
plain extracted text (city name, magnitude, JMA-reported Shindo).

Formula (replaced 9/25/2026; the old 4-event subducting-plate fit
consistently mis-scaled magnitude, e.g. an M5.2 came out as Shindo 1):

    1. Fault distance X (km), approximated from the hypocenter:
         L   = 10^(0.5*M - 1.85)            rupture length, km
         X   = max(sqrt(Delta^2 + h^2) - L/2, 3)
       (hypocentral distance minus half the rupture length, the same
       approximation JMA's EEW method uses; the 3 km floor keeps
       near-source values finite)

    2. PGV on engineering bedrock, Vs = 600 m/s (Si & Midorikawa 1999):
         log PGV600 = 0.58*M + 0.0038*h + d - 1.29
                      - log(X + 0.0028*10^(0.5*M)) - 0.002*X
         d = 0.00 crustal, -0.02 interplate, +0.12 intraslab

    3. Site amplification from Vs30 (Midorikawa et al. 1994):
         log Amp = 1.83 - 0.66*log(Vs30)       (Amp = 1 at Vs30 = 600)

    4. JMA instrumental intensity (Midorikawa et al. 1999):
         I = 2.68 + 1.72*log(PGV600 * Amp)

    M = JMA magnitude (treated as Mw), Delta = epicentral distance (km),
    h = focal depth (km). Defaults: Vs30 = 300 m/s ("average ground",
    since the app doesn't know each user's soil) and d = 0 (crustal).

Known limitation: on average ground (Vs30 300) this model tops out
around Shindo 6+ for realistic magnitudes; real Shindo 7 events come
from near-fault shaking on soft ground (Vs30 ~150-200). A per-location
Vs30 lookup (e.g. J-SHIS) would fix that later.

FLAG: coefficients were reconstructed and cross-checked against
secondary sources, not the original paper. Re-verify against
Si & Midorikawa (1999) before treating output as more than an
estimate. Real events commonly differ from this by +/-0.5 intensity.
"""

import json
import math
import os

CITIES_PATH = os.path.join(os.path.dirname(__file__), "cities_jp.json")


def load_cities():
    """
    Loads the reference city lookup table (used as a fallback when a
    feed gives a place name instead of coordinates). The caller is
    responsible for setting data["home"] (this app's Listener class
    does that from the user's saved settings, not a hardcoded default).
    """
    with open(CITIES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two lat/lon points."""
    r = 6371.0088  # mean Earth radius, km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


DEFAULT_VS30 = 300.0          # m/s, "average ground"
DEFAULT_FAULT_TYPE_TERM = 0.0  # crustal; -0.02 interplate, +0.12 intraslab


def fault_distance_km(magnitude, depth_km, epicentral_distance_km):
    """Approximate closest distance to the fault plane, in km."""
    hypocentral = math.sqrt(epicentral_distance_km ** 2 + depth_km ** 2)
    half_rupture = 0.5 * 10 ** (0.5 * magnitude - 1.85)
    return max(hypocentral - half_rupture, 3.0)


def estimated_intensity(magnitude, depth_km, epicentral_distance_km,
                        vs30=DEFAULT_VS30, fault_type_term=DEFAULT_FAULT_TYPE_TERM):
    """
    Si & Midorikawa (1999) PGV + Vs30 amplification + Midorikawa (1999)
    intensity conversion. Returns a continuous JMA intensity value
    (not yet snapped to a real step like 5- / 5+ / 6- / 6+).
    """
    x = fault_distance_km(magnitude, depth_km, epicentral_distance_km)
    log_pgv600 = (0.58 * magnitude
                  + 0.0038 * depth_km
                  + fault_type_term
                  - 1.29
                  - math.log10(x + 0.0028 * 10 ** (0.5 * magnitude))
                  - 0.002 * x)
    log_amp = 1.83 - 0.66 * math.log10(vs30)
    return 2.68 + 1.72 * (log_pgv600 + log_amp)


def snap_to_jma_step(continuous_intensity):
    """
    Snap a continuous intensity estimate to the real discrete JMA
    Shindo steps: 0, 1, 2, 3, 4, 5-, 5+, 6-, 6+, 7.
    """
    steps = [
        (0.5, "0"), (1.5, "1"), (2.5, "2"), (3.5, "3"), (4.5, "4"),
        (5.0, "5-"), (5.5, "5+"), (6.0, "6-"), (6.5, "6+"), (99, "7"),
    ]
    for ceiling, label in steps:
        if continuous_intensity < ceiling:
            return label
    return "7"


def city_lookup(city_name, cities_data):
    """
    Case-insensitive lookup with a simple fallback: if the exact
    name isn't found, this returns None rather than guessing: a
    silent wrong-city guess is worse than a visible miss here.
    """
    cities = cities_data["cities"]
    if city_name in cities:
        return cities[city_name]
    for key in cities:
        if key.lower() == city_name.lower():
            return cities[key]
    return None


def evaluate_event_from_epicenter(magnitude, depth_km, epicenter_lat, epicenter_lon, cities_data):
    """
    Same output shape as evaluate_event, but takes a real epicenter
    lat/lon directly (e.g. from the Wolfx JMA EEW feed) instead of a
    city name to look up. Preferred over evaluate_event whenever the
    feed actually gives coordinates, since it skips the lookup-table
    approximation entirely.
    """
    home = cities_data["home"]
    distance_km = haversine_km(home["lat"], home["lon"], epicenter_lat, epicenter_lon)
    continuous = estimated_intensity(magnitude, depth_km, distance_km)
    return {
        "matched_city": None,  # no real place name available for this path unless
                                # the caller (jma_ws_client.py) supplies the real
                                # hypocenter name, left blank rather than a fake
                                # "epicenter (direct coordinates)" label, since the
                                # display already shows the monitored location
                                # separately (bottom-right caption)
        "distance_km": distance_km,
        "jma_step": snap_to_jma_step(continuous),
        "continuous_intensity": continuous,
        "unmatched": [],
    }


def evaluate_event(magnitude, depth_km, affected_cities, cities_data):
    """
    Given extracted event text, find the closest matched city to
    home and return the estimated felt severity there.

    affected_cities: list of city-name strings as extracted by the VLM.
    Returns a dict with distance_km, matched_city, jma_step, or an
    'unmatched' list if none of the extracted names were found in the
    lookup table (this should surface as a visible gap, not a silent
    fallback to some default location).
    """
    home = cities_data["home"]
    best = None
    unmatched = []

    for name in affected_cities:
        coords = city_lookup(name, cities_data)
        if coords is None:
            unmatched.append(name)
            continue
        dist = haversine_km(home["lat"], home["lon"], coords["lat"], coords["lon"])
        if best is None or dist < best["distance_km"]:
            best = {"matched_city": name, "distance_km": dist}

    if best is None:
        return {
            "matched_city": None,
            "distance_km": None,
            "jma_step": None,
            "unmatched": unmatched,
        }

    continuous = estimated_intensity(magnitude, depth_km, best["distance_km"])
    best["jma_step"] = snap_to_jma_step(continuous)
    best["continuous_intensity"] = continuous
    best["unmatched"] = unmatched
    return best


if __name__ == "__main__":
    # Quick sanity check using the June 25 2026 M6.9 you logged.
    cities = load_cities()
    result = evaluate_event(
        magnitude=6.9,
        depth_km=40,
        affected_cities=["Miyako", "Kamaishi"],
        cities_data=cities,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
