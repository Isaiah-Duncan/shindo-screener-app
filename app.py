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
import jma_sources

WOLFX_JMA_EEW_URL = "wss://ws-api.wolfx.jp/jma_eew"
P2P_WS_URL = "wss://api.p2pquake.net/v2/ws"
STATIONS_JSON_RELATIVE_PATH = "stations_jp.json"
IDLE_STATE = {"status": "idle", "updated_at": None}
EEW_IDLE_MINUTES = 5          # an early warning stays up this long if no measured report follows
MEASURED_IDLE_MINUTES = 10    # a measured reading stays up this long, then the screen goes quiet
EEW_NOTICE_RADIUS_KM = 300    # show early warnings for quakes this close even when JMA hasn't warned your area
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


def search_locations(city_name, lang="en"):
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

    `lang` is the app's current UI language ("en" or "ja"), passed
    straight to Nominatim's accept-language so a search made while the
    app is in Japanese comes back as 宮古市, 岩手県 rather than a
    romanized "Miyako, Iwate Prefecture" (this used to be hardcoded to
    "en" regardless of the app's language, so every saved location was
    English-only even for someone using the Japanese UI end to end).
    This is a one-time choice made at search time: the name is saved as
    plain text, so a location searched in one language keeps that
    language's name even if the UI is switched afterward. Anything not
    "ja" falls back to "en", so an unexpected value never sends a blank
    or malformed accept-language.

    Usage policy requires a real User-Agent identifying the app and
    caps requests at 1/second; fine here, this runs only when someone
    is actively setting up or changing their location, not repeatedly.
    """
    accept_language = "ja" if lang == "ja" else "en"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "q": city_name, "format": "json", "limit": 5, "accept-language": accept_language,
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
        # Nominatim appends the country name in whichever language was
        # requested ("Japan" for en, "日本" for ja); strip either so the
        # trailing country name doesn't repeat what's already implied by
        # this being a Japan-only search (countrycodes=jp above).
        for suffix in (", Japan", ", 日本"):
            if label.endswith(suffix):
                label = label[: -len(suffix)]
                break
        candidates.append({"name": label, "lat": float(item["lat"]), "lon": float(item["lon"])})
    return candidates


class Api:
    """Exposed to JavaScript as `pywebview.api.<method>()`."""

    def __init__(self, on_settings_saved, get_listener, get_window):
        self._on_settings_saved = on_settings_saved
        self._get_listener = get_listener  # callable returning the current Listener or None
        self._get_window = get_window

    def search_location(self, city_name, lang="en"):
        city_name = (city_name or "").strip()
        if not city_name:
            return {"ok": False, "error": "Type a city or town name first.", "candidates": []}
        try:
            candidates = search_locations(city_name, lang)
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

    # Per-step (magnitude, depth_km, distance_km) shown by the test
    # buttons. These are only there so the test screen looks like a real
    # quake of that strength; the Shindo itself is pinned to the button.
    # Values are plausible for that level measured near the epicenter.
    TEST_EVENT_PARAMS = {
        "0":  {"magnitude": 2.0, "depth_km": 10.0, "distance_km": 30.0},
        "1":  {"magnitude": 2.8, "depth_km": 10.0, "distance_km": 27.0},
        "2":  {"magnitude": 3.4, "depth_km": 10.0, "distance_km": 16.0},
        "3":  {"magnitude": 4.3, "depth_km": 10.0, "distance_km": 13.0},
        "4":  {"magnitude": 5.5, "depth_km": 10.0, "distance_km": 21.0},
        "5-": {"magnitude": 6.1, "depth_km": 10.0, "distance_km": 20.0},
        "5+": {"magnitude": 6.7, "depth_km": 10.0, "distance_km": 30.0},
        "6-": {"magnitude": 6.8, "depth_km": 10.0, "distance_km": 20.0},
        "6+": {"magnitude": 7.6, "depth_km": 10.0, "distance_km": 10.0},
        "7":  {"magnitude": 8.0, "depth_km": 10.0, "distance_km": 5.0},
    }

    def trigger_test_event(self, step="5-", lang="en"):
        """
        Pushes a fake measured reading pinned to an exact Shindo step, so
        every step's screen can be checked directly. It uses the user's
        real nearest JMA station, so the test looks exactly like a real
        reading would. The frontend shows a professionalism warning
        before calling this. Each test gets its own event key, so the
        alert chime fires every time (useful for checking volume).
        """
        if step not in self.VALID_TEST_STEPS:
            return {"ok": False, "error": f"Not a real Shindo step: {step}"}
        listener = self._get_listener()
        if listener is None:
            return {"ok": False, "error": "Set a location first."}
        params = self.TEST_EVENT_PARAMS[step]
        nearest = (listener.home_info.get("stations") or [{}])[0]
        now = datetime.now(timezone.utc).isoformat()
        listener.push({
            "status": "active",
            "updated_at": now,
            "event_key": "test-" + now,
            "phase": "measured",
            "magnitude": params["magnitude"],
            "depth_km": params["depth_km"],
            "hypocenter": "テスト震源地" if lang == "ja" else "Test epicenter",
            "epicenter_km": params["distance_km"],
            "jma_max": step,
            "jma_max_kind": "test",
            "tsunami_warning": None,
            "serial": None,
            "maturity": "test",
            "accuracy": None,
            "home_area": listener.home_info.get("area"),
            "display_step": step,
            "display_source": "test",
            "alert_step": step,
            "station": nearest.get("name"),
            "station_km": round(nearest["distance_km"], 1) if nearest.get("distance_km") is not None else None,
        })
        return {"ok": True}


class Listener:
    """
    Two background connections, both relaying JMA's own values:
      - Wolfx JMA EEW (WebSocket): early warnings while shaking is coming.
      - P2P地震情報 (WebSocket): JMA's measured intensities afterward.
    See jma_sources.py for why the app never computes its own forecast.
    """

    def __init__(self, window, home, alert_min_step=alert.DEFAULT_ALERT_MIN_STEP,
                 alert_wake_screen=True, alert_volume=alert.DEFAULT_ALERT_VOLUME):
        self.window = window
        self.stations = jma_sources.load_stations(resource_path(STATIONS_JSON_RELATIVE_PATH))
        self._home = None
        self.home_info = {"area": None, "stations": []}
        self.home = home  # property: also finds the nearest JMA station and area
        self.current_state = dict(IDLE_STATE)
        self.event = None        # the quake currently on screen
        self.alerted = set()     # event keys that already chimed
        self.lock = threading.Lock()
        # Mutated directly by the Api set_alert_* methods when the user
        # changes a setting, so a change takes effect on the very next
        # push without needing to restart the listener threads.
        self.alert_min_step = alert_min_step
        self.alert_wake_screen = alert_wake_screen
        self.alert_volume = alert_volume

    @property
    def home(self):
        return self._home

    @home.setter
    def home(self, value):
        self._home = value
        self.home_info = jma_sources.locate_home(value, self.stations)

    # ------------------------------------------------------------ output

    def push(self, state):
        self.current_state = state
        try:
            self.window.evaluate_js(f"window.applyState({json.dumps(state, ensure_ascii=False)})")
        except Exception:
            pass  # window may be closing, not fatal
        key = state.get("event_key")
        if key and key not in self.alerted and alert.should_alert(state, min_step=self.alert_min_step):
            self.alerted.add(key)
            alert.trigger(
                self.window, resource_path,
                wake_screen=self.alert_wake_screen, volume=self.alert_volume,
            )

    def go_idle(self):
        self.event = None
        self.push(dict(IDLE_STATE, updated_at=self.now_iso()))

    def now_iso(self):
        return datetime.now(timezone.utc).isoformat()

    def minutes_since(self, iso_timestamp):
        if not iso_timestamp:
            return float("inf")
        then = datetime.fromisoformat(iso_timestamp)
        return (datetime.now(timezone.utc) - then).total_seconds() / 60.0

    def check_idle_timeout(self):
        with self.lock:
            st = self.current_state
            if st.get("status") != "active":
                return
            limit = EEW_IDLE_MINUTES if st.get("phase") == "eew" else MEASURED_IDLE_MINUTES
            if self.minutes_since(st.get("updated_at")) > limit:
                self.go_idle()

    def epicenter_km(self, lat, lon):
        if lat is None or lon is None:
            return None
        return round(jma_sources.haversine_km(self.home["lat"], self.home["lon"], lat, lon))

    # ------------------------------------------------------------ state building

    def build_state(self):
        """Turn self.event (merged EEW + measured info) into the display state."""
        ev = self.event
        s = {
            "status": "active",
            "updated_at": self.now_iso(),
            "event_key": ev["key"],
            "phase": ev.get("phase"),
            "magnitude": ev.get("magnitude"),
            "depth_km": ev.get("depth_km"),
            "hypocenter": ev.get("hypocenter"),
            "epicenter_km": self.epicenter_km(ev.get("lat"), ev.get("lon")),
            "jma_max": ev.get("jma_max"),
            "jma_max_kind": ev.get("jma_max_kind"),
            "tsunami_warning": ev.get("tsunami"),
            "serial": ev.get("serial"),
            "maturity": ev.get("maturity"),
            "accuracy": ev.get("accuracy"),
            "home_area": self.home_info.get("area"),
            "display_step": None,
            "display_source": "none",
            "alert_step": None,
        }
        r = ev.get("reading")
        if r:
            s.update(display_step=r["step"], display_source=r["source"], alert_step=r["step"],
                     station=r.get("station"), station_km=r.get("station_km"), area=r.get("area"))
            nm = ev.get("nearby_max")
            if nm and jma_sources.rank(nm["step"]) > jma_sources.rank(r["step"]):
                s["nearby_max"] = nm
        elif ev.get("area_forecast"):
            af = ev["area_forecast"]
            s.update(display_step=af["from"], display_source="area_warning", area_forecast=af,
                     alert_step=af.get("to") or af["from"])
        return s

    # ------------------------------------------------------------ EEW (Wolfx)

    def handle_eew(self, payload):
        info = jma_sources.parse_eew(payload, self.home_info.get("area"))
        if not info:
            return
        with self.lock:
            if info["kind"] == "eew_cancel":
                if self.event and self.event.get("eew_id") == info["event_id"]:
                    self.go_idle()
                return
            dist = self.epicenter_km(info["lat"], info["lon"])
            relevant = info["area_forecast"] is not None or (dist is not None and dist <= EEW_NOTICE_RADIUS_KM)
            same = self.event and (self.event.get("eew_id") == info["event_id"]
                                   or jma_sources.same_quake(self.event.get("origin_time"), info["origin_time"]))
            if not same:
                if not relevant:
                    return
                self.event = {"key": info["event_id"], "origin_time": info["origin_time"]}
            if self.event.get("phase") == "measured":
                return  # measured values already on screen; a late warning update must not replace them
            self.event.update(
                eew_id=info["event_id"], phase="eew", serial=info["serial"],
                maturity="final" if info["is_final"] else ("updated" if (info["serial"] or 1) > 1 else "preliminary"),
                hypocenter=info["hypocenter"], magnitude=info["magnitude"], depth_km=info["depth_km"],
                lat=info["lat"], lon=info["lon"], accuracy=info["accuracy"],
                jma_max=info["jma_max_forecast"], jma_max_kind="forecast",
                area_forecast=info["area_forecast"] or self.event.get("area_forecast"),
            )
            self.push(self.build_state())

    # ------------------------------------------------------------ measured (P2P地震情報)

    def handle_measured(self, msg):
        info = jma_sources.parse_p2p_quake(msg, self.home_info)
        if not info:
            return
        with self.lock:
            same = self.event and jma_sources.same_quake(self.event.get("origin_time"), info["origin_time"])
            if not same:
                if not info["felt_near_user"]:
                    return  # not felt at the user's nearest station: stay quiet
                key = info["origin_time"].isoformat() if info["origin_time"] else self.now_iso()
                self.event = {"key": key, "origin_time": info["origin_time"]}
            ev = self.event
            # a station reading (detailed report) outranks an area reading (quick report)
            if info["reading"] and not (ev.get("reading", {}).get("source") in ("station", "station_below_1")
                                        and info["reading"]["source"] == "area"):
                ev["reading"] = info["reading"]
                ev["nearby_max"] = info["nearby_max"]
            if ev.get("reading") is None:
                return  # e.g. a destination-only report with nothing for this location yet
            ev.update(phase="measured", serial=None,
                      maturity="measured_detail" if info["report_type"] == "DetailScale" else "measured_quick",
                      jma_max=info["jma_max_measured"] or ev.get("jma_max"), jma_max_kind="measured")
            for k in ("hypocenter", "magnitude", "depth_km", "lat", "lon"):
                if info.get(k) is not None:
                    ev[k] = info[k]
            if info.get("tsunami"):
                ev["tsunami"] = info["tsunami"]
            self.push(self.build_state())

    # ------------------------------------------------------------ connections

    def _ws_loop(self, url, handler, accept):
        while True:
            try:
                with ws_client.connect(url, open_timeout=10) as ws:
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
                        if isinstance(payload, dict) and accept(payload):
                            handler(payload)
            except (websockets.exceptions.ConnectionClosed, OSError):
                time.sleep(5)
            except Exception:
                traceback.print_exc()
                time.sleep(5)

    def run(self):
        threading.Thread(
            target=self._ws_loop, daemon=True,
            args=(P2P_WS_URL, self.handle_measured, lambda p: p.get("code") == 551),
        ).start()
        self._ws_loop(WOLFX_JMA_EEW_URL, self.handle_eew, lambda p: p.get("type") != "heartbeat")


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
