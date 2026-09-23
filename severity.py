"""
Deterministic severity calculation.

This module is intentionally the ONLY place that turns
(magnitude, depth, location) into an estimated felt intensity at home.
The VLM never touches this logic: it only ever hands this module
plain extracted text (city name, magnitude, JMA-reported Shindo).

Formula source (subducting-plate model, appropriate for offshore
Tohoku/Japan Trench events, the study zone this was originally built for):

    I = -8.33 + 2.19*M - 0.00550*Delta_h - 1.14*log10(Delta_h)
    Delta_h = sqrt(Delta^2 + h^2)

    M      = JMA magnitude
    Delta  = epicentral distance, km
    h      = focal depth, km
    I      = estimated JMA seismic intensity (continuous, roughly 0-7)

Source: intensity attenuation relations derived from earthquakes near
the Japan Trench, subducting-plate model (cited via Science.gov
"jma seismic intensity" topic index, referencing the underlying
attenuation study). This was calibrated on a small number of events
(reportedly four, near the Japan Trench), so treat the output as a
rough estimate, not a survey-grade figure. Worth sanity-checking
against JQuake's own reported Shindo for a few real events before
trusting it, and worth eventually re-calibrating against a real
seismic-study log of real felt/reported events for this specific
study zone, since that dataset already exists and is more locally
relevant than a four-event fit.

FLAG: this formula has NOT been independently re-derived or verified
here beyond the single search result it came from. Re-verify against
the original paper (Japan Trench subducting-plate intensity
attenuation model) before leaning on this for anything beyond a
personal ambient display.
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


def estimated_intensity(magnitude, depth_km, epicentral_distance_km):
    """
    Subducting-plate JMA intensity attenuation estimate.
    Returns a continuous intensity value (not yet snapped to a
    real JMA step like 5- / 5+ / 6- / 6+).
    """
    delta_h = math.sqrt(epicentral_distance_km ** 2 + depth_km ** 2)
    delta_h = max(delta_h, 1e-6)  # guard against log(0)
    intensity = (-8.33
                 + 2.19 * magnitude
                 - 0.00550 * delta_h
                 - 1.14 * math.log10(delta_h))
    return intensity


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
