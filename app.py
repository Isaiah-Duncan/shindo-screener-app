"""
Shindo Screener: desktop app version.

One process, one window. Replaces the three-piece PowerShell setup
(WebSocket client + local HTTP server + browser tab) with a single
double-click executable.

Architecture:
- pywebview shows the display as a native window, no browser needed.
- The WebSocket listener runs in a background thread in the same
  process and pushes updates directly into the page via
  window.evaluate_js() (real push, not the old fetch-every-5-seconds
  polling), since there's no longer a separate server to poll.
- First run: if no saved location exists, the window shows a small
  setup screen (type a city name) instead of the main display. The
  name is geocoded via OpenStreetMap's free Nominatim service, saved
  locally, and the app switches to the real screener from then on.

Settings are saved to the OS's proper per-user app-data folder, not
next to the executable, so they survive app updates and work
correctly even when PyInstaller unpacks to a temp directory at runtime.
"""

import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import webview
import websockets
import websockets.sync.client as ws_client

import alert
import severity

WOLFX_JMA_EEW_URL = "wss://ws-api.wolfx.jp/jma_eew"
CITIES_JSON_RELATIVE_PATH = "cities_jp.json"
IDLE_STATE = {"status": "idle", "updated_at": None}
IDLE_TIMEOUT_MINUTES = 1
SANITY_MAX_DISTANCE_KM = 3000
CHECK_INTERVAL_SECONDS = 15
ACTIVE_POLL_INTERVAL_SECONDS = 3
IDLE_POLL_INTERVAL_SECONDS = 8


def resource_path(relative_path):
    """
    Works both when running normally (python app.py) and when frozen
    into a single PyInstaller executable, where bundled files get
    unpacked to a temp folder referenced by sys._MEIPASS.
    """
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


# severity.py resolves its own CITIES_PATH from its module __file__,
# which is a real path when running via `python app.py` but is NOT
# reliably a real on-disk path once PyInstaller freezes this into a
# single .exe (pure-Python modules get bundled into an in-memory
# archive, not extracted as individual .py files). Overriding it here,
# through the same resource_path() helper already proven correct for
# screener_app.html and icon.ico, removes that ambiguity entirely
# rather than relying on frozen-module __file__ behavior lining up by
# coincidence.
severity.CITIES_PATH = resource_path(CITIES_JSON_RELATIVE_PATH)


def settings_dir():
    """
    Proper per-user app-data folder, not the exe's own folder: a
    PyInstaller --onefile exe runs from a temp extraction location,
    saving settings there would lose them on every restart.
    """
    if sys.platform == "win32":
        base = os.getenv("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.path.expanduser("~/.config")
    path = os.path.join(base, "ShindoScreener")
    os.makedirs(path, exist_ok=True)
    return path


SETTINGS_PATH = os.path.join(settings_dir(), "settings.json")


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# Every setting besides name/lat/lon, with its default for a settings.json
# saved before that setting existed (an older install upgrading in place,
# for example, never had alert_volume written yet).
SETTINGS_DEFAULTS = {
    "language": "en",
    "time_format": "24h",
    "text_scale": "normal",
    "motion_effects": "on",
    "alert_min_step": alert.DEFAULT_ALERT_MIN_STEP,
    "alert_wake_screen": "on",
    "alert_volume": alert.DEFAULT_ALERT_VOLUME,
}


def merged_settings(existing, **changes):
    """
    Builds the full settings dict to write: defaults, then whatever an
    existing settings.json already had, then this call's specific
    changes on top. Centralizing the merge here (rather than every Api
    setter method threading every field through as positional
    arguments) is what lets a new setting get added in one place
    instead of every setter needing to learn about it.
    """
    data = dict(SETTINGS_DEFAULTS)
    if existing:
        data.update({k: v for k, v in existing.items() if k in data or k in ("name", "lat", "lon")})
    data.update(changes)
    return data


def save_settings(data):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


def search_locations(city_name):
    """
    Free, no-API-key geocoding via OpenStreetMap's Nominatim, returning
    multiple candidates rather than blindly picking the first result.

    Two real bugs this fixes, found by testing against actual data
    during this build, not theoretical:
    - Without featureType=settlement, a misspelled search could match
      a business name (a real test: "Hokiado" matched a restaurant
      called "Hokiado Ramen House" in Omaha, Nebraska, silently
      becoming someone's "home location"). Restricting to settlement
      features excludes businesses/amenities entirely.
    - Japanese place names repeat across prefectures (a real test:
      "Fuchu" alone matches real, distinct places in Tokyo, two
      different spots in Hiroshima Prefecture, and Toyama). Blindly
      taking the first result silently picks one without telling the
      user there were others. Returning candidates lets the person
      actually choose.

    countrycodes=jp restricts results to Japan, matching this app's
    actual scope and removing irrelevant same-named places elsewhere.

    Usage policy requires a real User-Agent identifying the app and
    caps requests at 1/second; fine here, this runs only when someone
    is actively setting up or changing their location, not repeatedly.
    """
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "q": city_name, "format": "json", "limit": 5, "accept-language": "en",
        "featureType": "settlement", "addressdetails": 1, "countrycodes": "jp",
    })
    req = urllib.request.Request(url, headers={"User-Agent": "ShindoScreener/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        results = json.loads(resp.read().decode("utf-8"))

    candidates = []
    for item in results:
        # display_name already handles Japan's address hierarchy correctly
        # (Tokyo has no "province" field but still resolves right; a
        # suburb of a larger city shows both), simpler and more robust
        # than manually picking address fields apart.
        label = item["display_name"]
        if label.endswith(", Japan"):
            label = label[: -len(", Japan")]
        candidates.append({"name": label, "lat": float(item["lat"]), "lon": float(item["lon"])})
    return candidates


class Api:
    """Exposed to JavaScript as `pywebview.api.<method>()`."""

    def __init__(self, on_settings_saved, get_listener, get_window):
        self._on_settings_saved = on_settings_saved
        self._get_listener = get_listener  # callable returning the current Listener or None
        self._get_window = get_window

    def search_location(self, city_name):
        city_name = (city_name or "").strip()
        if not city_name:
            return {"ok": False, "error": "Type a city or town name first.", "candidates": []}
        try:
            candidates = search_locations(city_name)
        except Exception as e:
            return {"ok": False, "error": f"Lookup failed: {e}", "candidates": []}
        if not candidates:
            return {"ok": False, "error": f"Couldn't find \"{city_name}\" as a real town or city. Check the spelling, or try a nearby larger place.", "candidates": []}
        return {"ok": True, "candidates": candidates}

    def confirm_location(self, name, lat, lon):
        """
        Called once the user has picked a specific candidate from the
        search results: saves it directly, no re-geocoding needed
        since we already have exact coordinates from the search step.
        """
        existing = load_settings()
        save_settings(merged_settings(existing, name=name, lat=lat, lon=lon))
        listener = self._get_listener()
        if listener is not None:
            listener.home = {"name": name, "lat": lat, "lon": lon}
        else:
            self._on_settings_saved({"name": name, "lat": lat, "lon": lon})
        return {"ok": True, "name": name}

    def set_language(self, lang):
        existing = load_settings()
        if existing:
            save_settings(merged_settings(existing, language=lang))
        return {"ok": True}

    def set_time_format(self, fmt):
        existing = load_settings()
        if existing:
            save_settings(merged_settings(existing, time_format=fmt))
        return {"ok": True}

    def set_text_scale(self, scale):
        existing = load_settings()
        if existing:
            save_settings(merged_settings(existing, text_scale=scale))
        return {"ok": True}

    def set_motion_effects(self, value):
        existing = load_settings()
        if existing:
            save_settings(merged_settings(existing, motion_effects=value))
        return {"ok": True}

    def set_alert_min_step(self, step):
        if step not in alert.SELECTABLE_ALERT_STEPS:
            return {"ok": False, "error": f"Not a selectable alert threshold: {step}"}
        existing = load_settings()
        if existing:
            save_settings(merged_settings(existing, alert_min_step=step))
        listener = self._get_listener()
        if listener is not None:
            listener.alert_min_step = step
        return {"ok": True}

    def set_alert_wake_screen(self, value):
        existing = load_settings()
        if existing:
            save_settings(merged_settings(existing, alert_wake_screen=value))
        listener = self._get_listener()
        if listener is not None:
            listener.alert_wake_screen = value == "on"
        return {"ok": True}

    def set_alert_volume(self, value):
        if value not in alert.ALERT_SOUND_FILES:
            return {"ok": False, "error": f"Not a real volume preset: {value}"}
        existing = load_settings()
        if existing:
            save_settings(merged_settings(existing, alert_volume=value))
        listener = self._get_listener()
        if listener is not None:
            listener.alert_volume = value
        return {"ok": True}

    def preview_alert_sound(self, volume):
        """Plays the chosen volume preset once, sound only, so a person
        can hear how loud Quiet/Normal/Loud is before picking one. No
        screen wake or foreground jump, this is just an audio preview,
        not a real alert."""
        if volume not in alert.ALERT_SOUND_FILES:
            return {"ok": False, "error": f"Not a real volume preset: {volume}"}
        alert.preview(resource_path, volume)
        return {"ok": True}

    VALID_TEST_STEPS = ["0", "1", "2", "3", "4", "5-", "5+", "6-", "6+", "7"]

    # Per-step (magnitude, depth_km, distance_km) used by trigger_test_event
    # below. Each triple was solved against severity.py's own real
    # attenuation formula (estimated_intensity + snap_to_jma_step) so it
    # actually produces that exact step, rather than every test button
    # showing the same fixed M6.5/72km pairing regardless of which step
    # was picked. Magnitude escalates and depth shallows across the range,
    # matching how real Japan Trench-region events plausibly scale; if
    # severity.py's formula is ever recalibrated, these should be
    # re-solved to match (see severity.py's own FLAG comment on that
    # formula's uncalibrated status).
    TEST_EVENT_PARAMS = {
        "0":  {"magnitude": 4.8, "depth_km": 30.0, "distance_km": 29.4},
        "1":  {"magnitude": 5.2, "depth_km": 30.0, "distance_km": 27.4},
        "2":  {"magnitude": 5.6, "depth_km": 30.0, "distance_km": 16.1},
        "3":  {"magnitude": 6.0, "depth_km": 25.0, "distance_km": 13.2},
        "4":  {"magnitude": 6.5, "depth_km": 25.0, "distance_km": 21.0},
        "5-": {"magnitude": 6.8, "depth_km": 20.0, "distance_km": 20.1},
        "5+": {"magnitude": 7.1, "depth_km": 20.0, "distance_km": 29.7},
        "6-": {"magnitude": 7.4, "depth_km": 15.0, "distance_km": 42.0},
        "6+": {"magnitude": 7.7, "depth_km": 15.0, "distance_km": 52.7},
        "7":  {"magnitude": 8.0, "depth_km": 10.0, "distance_km": 47.6},
    }

    def trigger_test_event(self, step="5-"):
        """
        Pushes a fake reading pinned to an exact Shindo step, for
        checking the display's rendering at every real step directly.
        The magnitude/depth/distance shown alongside each step come from
        TEST_EVENT_PARAMS above, a real value that produces that exact
        step through the app's own formula, not an override that could
        mismatch what's on screen. The frontend is responsible for
        showing a professionalism warning before ever calling this;
        this method itself doesn't gate on anything, so it stays simple
        and testable.
        """
        if step not in self.VALID_TEST_STEPS:
            return {"ok": False, "error": f"Not a real Shindo step: {step}"}
        listener = self._get_listener()
        if listener is None:
            return {"ok": False, "error": "Set a location first."}
        params = self.TEST_EVENT_PARAMS[step]
        listener.push({
            "status": "active",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "magnitude": params["magnitude"],
            "depth_km": params["depth_km"],
            "shindo_reported_by_jquake": "TEST",
            "matched_city": "Test epicenter",
            "distance_km": params["distance_km"],
            "jma_step_estimated": step,
            "tsunami_warning": None,
            "serial": 1,
            "maturity": "preliminary",
            "accuracy": None,
        })
        return {"ok": True}


class Listener:
    """
    The WebSocket listener, adapted from jma_ws_client.py to push
    directly into the webview window instead of writing state.json
    for something else to poll.
    """

    def __init__(self, window, home, alert_min_step=alert.DEFAULT_ALERT_MIN_STEP,
                 alert_wake_screen=True, alert_volume=alert.DEFAULT_ALERT_VOLUME):
        self.window = window
        self.home = home  # {"name":..., "lat":..., "lon":...}
        self.current_state = dict(IDLE_STATE)
        # Mutated directly by the Api set_alert_* methods when the user
        # changes a setting, so a change takes effect on the very next
        # push without needing to restart the listener thread.
        self.alert_min_step = alert_min_step
        self.alert_wake_screen = alert_wake_screen
        self.alert_volume = alert_volume

    def push(self, state):
        self.current_state = state
        try:
            self.window.evaluate_js(f"window.applyState({json.dumps(state)})")
        except Exception:
            pass  # window may be closing, not fatal
        if alert.should_alert(state, min_step=self.alert_min_step):
            alert.trigger(
                self.window, resource_path,
                wake_screen=self.alert_wake_screen, volume=self.alert_volume,
            )

    def cities_data(self):
        data = severity.load_cities()
        data["home"] = self.home
        return data

    def now_iso(self):
        return datetime.now(timezone.utc).isoformat()

    def minutes_since(self, iso_timestamp):
        if not iso_timestamp:
            return float("inf")
        then = datetime.fromisoformat(iso_timestamp)
        return (datetime.now(timezone.utc) - then).total_seconds() / 60.0

    def extract_epicenter_coords(self, payload):
        lat, lon = payload.get("Latitude"), payload.get("Longitude")
        if lat is not None and lon is not None:
            try:
                return float(lat), float(lon)
            except (TypeError, ValueError):
                pass
        return None, None

    def extract_hypocenter_name(self, payload):
        h = payload.get("Hypocenter")
        return h if isinstance(h, str) else None

    def extract_magnitude(self, payload):
        for key in ("Magnitude", "Magunitude", "magnitude"):
            val = payload.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
        return None

    def extract_tsunami_warning(self, payload):
        for key in ("Tsunami", "isTsunami", "TsunamiWarning", "TsunamiComment"):
            val = payload.get(key)
            if val:
                return str(val)
        return None

    def check_idle_timeout(self):
        if self.current_state.get("status") not in (None, "idle"):
            if self.minutes_since(self.current_state.get("updated_at")) > IDLE_TIMEOUT_MINUTES:
                self.push(dict(IDLE_STATE, updated_at=self.now_iso()))

    def classify_maturity(self, serial, is_final):
        """
        Report-maturity wording driven by real feed fields (Serial,
        isFinal), not an invented confidence scale. This is the
        distinction between "the app made up a confidence level" and
        "the app is just describing what JMA's own report metadata
        already says."
        """
        if is_final:
            return "final"
        if serial is not None and serial > 1:
            return "updated"
        return "preliminary"

    def extract_accuracy(self, payload):
        """
        JMA's own accuracy/method field (e.g. "IPF法（5点以上）", IPF
        method, 5+ station points), confirmed present in real payloads
        tested during this build. Passed through as-is rather than
        translated into an invented Low/Medium/High scale; only the
        one specific value we've actually verified gets a plain-English
        rendering, everything else is left for the frontend to show
        untranslated (correct as-is in Japanese mode, omitted rather
        than guessed at in English mode) rather than mistranslating an
        unfamiliar value with false confidence.
        """
        accuracy = payload.get("Accuracy")
        if isinstance(accuracy, dict):
            return accuracy.get("Epicenter")
        return None

    def process_payload(self, payload):
        if payload.get("isCancel"):
            self.push(dict(IDLE_STATE, updated_at=self.now_iso()))
            return

        magnitude = self.extract_magnitude(payload)
        depth = payload.get("Depth")
        hypocenter_name = self.extract_hypocenter_name(payload)
        lat, lon = self.extract_epicenter_coords(payload)

        if magnitude is None:
            return  # nothing usable, ignore rather than show a broken state

        depth_km = float(depth) if depth is not None else 30.0
        cities = self.cities_data()

        if lat is not None and lon is not None:
            result = severity.evaluate_event_from_epicenter(magnitude, depth_km, lat, lon, cities)
            if hypocenter_name:
                result["matched_city"] = hypocenter_name
        elif hypocenter_name:
            result = severity.evaluate_event(magnitude, depth_km, [hypocenter_name], cities)
        else:
            result = {"matched_city": None, "distance_km": None, "jma_step": None}

        if result.get("distance_km") is not None and result["distance_km"] > SANITY_MAX_DISTANCE_KM:
            self.push(dict(IDLE_STATE, updated_at=self.now_iso()))
            return

        if result.get("jma_step") == "0":
            self.push(dict(IDLE_STATE, updated_at=self.now_iso()))
            return

        state = {
            "status": "active",
            "updated_at": self.now_iso(),
            "magnitude": magnitude,
            "depth_km": depth_km,
            "shindo_reported_by_jquake": payload.get("MaxIntensity"),
            "matched_city": result.get("matched_city"),
            "distance_km": result.get("distance_km"),
            "jma_step_estimated": result.get("jma_step"),
            "tsunami_warning": self.extract_tsunami_warning(payload),
            "serial": payload.get("Serial"),
            "maturity": self.classify_maturity(payload.get("Serial"), payload.get("isFinal")),
            "accuracy": self.extract_accuracy(payload),
        }
        self.push(state)

    def run(self):
        while True:
            try:
                with ws_client.connect(WOLFX_JMA_EEW_URL, open_timeout=10) as ws:
                    while True:
                        self.check_idle_timeout()
                        try:
                            message = ws.recv(timeout=CHECK_INTERVAL_SECONDS)
                        except TimeoutError:
                            continue
                        try:
                            payload = json.loads(message)
                        except json.JSONDecodeError:
                            continue
                        if payload.get("type") == "heartbeat":
                            continue
                        self.process_payload(payload)
            except (websockets.exceptions.ConnectionClosed, OSError):
                time.sleep(5)
            except Exception:
                traceback.print_exc()
                time.sleep(5)


def main():
    html_path = resource_path("screener_app.html")
    icon_path = resource_path("icon.ico")

    window_holder = {}
    listener_holder = {}

    def start_listener(settings):
        # Accepts either the full saved-settings dict (from on_loaded)
        # or just {"name","lat","lon"} (from a brand-new first-time
        # setup, before any other setting has been chosen); .get()
        # below falls back to the same defaults either way.
        home = {"name": settings["name"], "lat": settings["lat"], "lon": settings["lon"]}
        listener = Listener(
            window_holder["window"], home,
            alert_min_step=settings.get("alert_min_step", alert.DEFAULT_ALERT_MIN_STEP),
            alert_wake_screen=settings.get("alert_wake_screen", "on") == "on",
            alert_volume=settings.get("alert_volume", alert.DEFAULT_ALERT_VOLUME),
        )
        listener_holder["instance"] = listener
        threading.Thread(target=listener.run, daemon=True).start()

    def on_settings_saved(home):
        start_listener(home)

    def get_listener():
        return listener_holder.get("instance")

    api = Api(on_settings_saved, get_listener, lambda: window_holder.get("window"))

    window = webview.create_window(
        "Shindo Screener", html_path, width=1000, height=600,
        background_color="#000000", js_api=api,
    )
    window_holder["window"] = window

    def on_loaded():
        settings = load_settings()
        if settings:
            lang = settings.get("language", "en")
            time_fmt = settings.get("time_format", "24h")
            text_scale = settings.get("text_scale", "normal")
            motion = settings.get("motion_effects", "on")
            alert_min_step = settings.get("alert_min_step", alert.DEFAULT_ALERT_MIN_STEP)
            alert_wake_screen = settings.get("alert_wake_screen", "on")
            alert_volume = settings.get("alert_volume", alert.DEFAULT_ALERT_VOLUME)
            window.evaluate_js(f"window.applyLanguageOnly({json.dumps(lang)})")
            window.evaluate_js(f"window.applyTimeFormatOnly({json.dumps(time_fmt)})")
            window.evaluate_js(f"window.applyTextScaleOnly({json.dumps(text_scale)})")
            window.evaluate_js(f"window.applyMotionEffectsOnly({json.dumps(motion)})")
            window.evaluate_js(f"window.applyAlertMinStepOnly({json.dumps(alert_min_step)})")
            window.evaluate_js(f"window.applyAlertWakeScreenOnly({json.dumps(alert_wake_screen)})")
            window.evaluate_js(f"window.applyAlertVolumeOnly({json.dumps(alert_volume)})")
            window.evaluate_js(f"window.showMainScreen({json.dumps(settings['name'])})")
            start_listener(settings)
        else:
            window.evaluate_js("window.showSetupScreen()")

    window.events.loaded += on_loaded
    def on_shown():
        # Documented pywebview/Windows quirk (github.com/r0x0r/pywebview
        # issue #866): clicks can silently not register until something
        # forces a repaint/hit-test refresh, dragging the mouse to
        # another corner is exactly that trigger. A tiny resize nudge
        # right after the window appears forces that refresh
        # automatically instead of requiring the user to do it by hand.
        try:
            w, h = window.width, window.height
            window.resize(w, h + 1)
            window.resize(w, h)
        except Exception:
            pass

    window.events.shown += on_shown
    # gui='edgechromium' explicitly selects the modern WebView2 backend
    # rather than letting pywebview auto-detect: auto-detection can
    # land on an older, less reliable backend on some Windows setups,
    # which is the more common root cause behind this whole bug class.
    webview.start(icon=icon_path if os.path.exists(icon_path) else None, gui="edgechromium")


if __name__ == "__main__":
    main()
