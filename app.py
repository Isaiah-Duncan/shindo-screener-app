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
from datetime import datetime, timedelta, timezone

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
QUIET_IDLE_MINUTES = 1        # Shindo 0-2 readings (see QUIET_STEPS) clear after this instead,
                               # since they're the dark/dim display tier, not worth holding for 10
                               # minutes (e.g. overnight: see no reason to keep a room lit for a
                               # "barely felt" reading long after the shaking itself is over).
                               # Shindo 0 ("below Shindo 1 here") belongs here too, not in the
                               # 10-minute bucket below: it's the least significant reading the
                               # app shows, so it shouldn't sit on screen the longest.
QUIET_STEPS = {"0", "1", "2"}
# Personalization for the quiet tier (Settings > Alerts > "Sensitivity").
# Replaces the old binary "Don't light up for" opt-out with a single
# dial: how long the brief bright flash is held before the 30s(-ish)
# fade to dark starts, and how long that fade itself takes, as well as
# which Shindo steps get this quieter treatment at all (how calmly the
# app reacts, not just for how long) and whether a muted step's
# description reads as the normal detailed phrase or a calmer one.
# "off" skips the flash entirely for its quiet steps. The actual
# per-level values live in screener_app.html's SENSITIVITY_PROFILES,
# since this is purely a display choice with no JMA data behind it.
SENSITIVITY_LEVELS = ("high", "normal", "low", "off")
# How long a personal "felt this" log entry is kept before being pruned,
# per Isaiah's explicit request: this is a local convenience record, not
# an archive, so it's bounded rather than growing forever on disk.
FELT_LOG_RETENTION_DAYS = 30
EEW_NOTICE_RADIUS_KM = 300    # show early warnings for quakes this close even when JMA hasn't warned your area
HOME_CHANGE_REPLAY_MINUTES = 10  # re-show a recent quake against a newly chosen location, see recheck_last_events
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
FELT_LOG_PATH = os.path.join(settings_dir(), "felt_log.json")


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def prune_felt_log(entries):
    """Drops any logged "felt this" entry older than the retention
    window, so the file this app accumulates on disk is self-cleaning
    rather than growing forever. An entry with a missing/unparseable
    timestamp is dropped too, rather than kept forever by accident."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=FELT_LOG_RETENTION_DAYS)
    kept = []
    for e in entries:
        try:
            felt_at = datetime.fromisoformat(e["felt_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if felt_at >= cutoff:
            kept.append(e)
    return kept


def load_felt_log():
    try:
        with open(FELT_LOG_PATH, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        entries = []
    return prune_felt_log(entries)


def save_felt_log(entries):
    with open(FELT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f)


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
    "sensitivity": "normal",
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

    def set_sensitivity(self, level):
        if level not in SENSITIVITY_LEVELS:
            return {"ok": False, "error": f"Not a selectable sensitivity level: {level}"}
        existing = load_settings()
        if existing:
            save_settings(merged_settings(existing, sensitivity=level))
        listener = self._get_listener()
        if listener is not None:
            listener.sensitivity = level
        return {"ok": True}

    def dismiss_active(self):
        """Manually clears whatever reading is on screen back to idle,
        instead of waiting out the automatic idle timeout
        (EEW_IDLE_MINUTES / MEASURED_IDLE_MINUTES / QUIET_IDLE_MINUTES).
        Just clears Listener.event; nothing JMA-related is discarded,
        a later message for a *different* quake is handled normally,
        and a repeat message for the same quake is free to start a new
        event rather than silently reviving the dismissed one."""
        listener = self._get_listener()
        if listener is not None:
            with listener.lock:
                listener.go_idle()
        return {"ok": True}

    def get_felt_log(self):
        """The personal "earthquakes felt here" log (Settings > History),
        for the frontend to list and export. Entries older than
        FELT_LOG_RETENTION_DAYS are already dropped by load_felt_log."""
        return {"ok": True, "entries": load_felt_log()}

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
                 alert_wake_screen=True, alert_volume=alert.DEFAULT_ALERT_VOLUME,
                 sensitivity="normal"):
        self.window = window
        self.stations = jma_sources.load_stations(resource_path(STATIONS_JSON_RELATIVE_PATH))
        self._home = None
        self.home_info = {"area": None, "stations": []}
        # The most recent raw message of each kind this session has seen,
        # with when it arrived, so a location change can re-apply it
        # against the new home instead of waiting for the next live
        # message; see recheck_last_events.
        self._last_eew_payload = None
        self._last_eew_received_at = None
        self._last_measured_msg = None
        self._last_measured_received_at = None
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
        self.sensitivity = sensitivity

    @property
    def home(self):
        return self._home

    @home.setter
    def home(self, value):
        self._home = value
        self.home_info = jma_sources.locate_home(value, self.stations)
        self.recheck_last_events()

    def recheck_last_events(self):
        """
        Re-applies the last EEW and measured messages this session
        received, now that home/home_info point at a new location.
        Without this, switching location mid-quake depends on catching
        the *next* live message by luck: JMA's early warnings for a
        small quake are often a single burst that never repeats, so a
        location change made a few seconds late would otherwise show
        nothing even though the app received a relevant message
        moments earlier. Only replays messages still fresh enough to
        matter (HOME_CHANGE_REPLAY_MINUTES); each call re-parses the
        raw message against the current home, so it applies exactly
        the same relevance and "felt near you" rules as a live message
        would, using the newly selected nearest station and area.
        """
        now = datetime.now(timezone.utc)
        if (self._last_eew_payload is not None and self._last_eew_received_at is not None
                and (now - self._last_eew_received_at).total_seconds() <= HOME_CHANGE_REPLAY_MINUTES * 60):
            self.handle_eew(self._last_eew_payload)
        if (self._last_measured_msg is not None and self._last_measured_received_at is not None
                and (now - self._last_measured_received_at).total_seconds() <= HOME_CHANGE_REPLAY_MINUTES * 60):
            self.handle_measured(self._last_measured_msg)

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
            if st.get("phase") == "eew":
                limit = EEW_IDLE_MINUTES
            elif st.get("display_step") in QUIET_STEPS:
                limit = QUIET_IDLE_MINUTES
            else:
                limit = MEASURED_IDLE_MINUTES
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
        # Passed straight through for the frontend to apply (Settings >
        # Alerts > "Sensitivity"): which steps get the quieter treatment,
        # how long that takes, and how the reading is described. Pure
        # display behavior, not JMA data, so the actual profiles live in
        # screener_app.html.
        s["sensitivity"] = self.sensitivity
        return s

    # ------------------------------------------------------------ EEW (Wolfx)

    def handle_eew(self, payload):
        info = jma_sources.parse_eew(payload, self.home_info.get("area"))
        if not info:
            return
        if info["kind"] == "eew":
            # Cached before the relevance check below, on purpose: a
            # location change later needs to re-run this exact payload
            # against a *different* home, so it must be kept even when
            # it wasn't relevant to today's home.
            self._last_eew_payload = payload
            self._last_eew_received_at = datetime.now(timezone.utc)
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
        # Cached before the "felt near you" check below, for the same
        # reason as handle_eew: a location change needs the raw message
        # to re-parse against the new nearest station, even when today's
        # nearest station felt nothing.
        self._last_measured_msg = msg
        self._last_measured_received_at = datetime.now(timezone.utc)
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
            # Keep the tsunami banner in sync with JMA, including clearing
            # it: a real level always updates it, an explicit "no tsunami"
            # from JMA clears a stale warning, and "unknown/checking" (or
            # no tsunami field at all in this message) leaves whatever is
            # already shown untouched rather than guessing.
            if info.get("tsunami"):
                ev["tsunami"] = info["tsunami"]
            elif info.get("tsunami_cleared"):
                ev["tsunami"] = None
            # Personal log (Settings > History): only earthquakes actually
            # felt at the user's nearest station/area, never every nearby
            # quake JMA reported. info["felt_near_user"] reflects *this*
            # message's reading; record_felt_event is itself safe to call
            # repeatedly for the same event (it updates in place rather
            # than duplicating), so a later, more detailed report for an
            # already-logged quake just refines that one entry.
            if info.get("felt_near_user"):
                self.record_felt_event(ev)
            self.push(self.build_state())

    def record_felt_event(self, ev):
        """Adds or updates this event's entry in the local "felt this"
        log (see FELT_LOG_PATH), for the Settings > History export. Not
        JMA data in itself, just a personal record of when the app's own
        relayed JMA readings actually showed something felt here."""
        reading = ev.get("reading") or {}
        step = reading.get("step")
        if not step:
            return
        now = self.now_iso()
        entries = load_felt_log()
        for e in entries:
            if e.get("event_key") == ev["key"]:
                e.update(display_step=step, magnitude=ev.get("magnitude"), hypocenter=ev.get("hypocenter"),
                         station=reading.get("station"), station_km=reading.get("station_km"), updated_at=now)
                break
        else:
            entries.append({
                "event_key": ev["key"], "felt_at": now, "updated_at": now,
                "display_step": step, "magnitude": ev.get("magnitude"), "hypocenter": ev.get("hypocenter"),
                "station": reading.get("station"), "station_km": reading.get("station_km"),
            })
        save_felt_log(prune_felt_log(entries))

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
            sensitivity=settings.get("sensitivity", "normal"),
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
            sensitivity = settings.get("sensitivity", "normal")
            window.evaluate_js(f"window.applyLanguageOnly({json.dumps(lang)})")
            window.evaluate_js(f"window.applyTimeFormatOnly({json.dumps(time_fmt)})")
            window.evaluate_js(f"window.applyTextScaleOnly({json.dumps(text_scale)})")
            window.evaluate_js(f"window.applyMotionEffectsOnly({json.dumps(motion)})")
            window.evaluate_js(f"window.applyAlertMinStepOnly({json.dumps(alert_min_step)})")
            window.evaluate_js(f"window.applyAlertWakeScreenOnly({json.dumps(alert_wake_screen)})")
            window.evaluate_js(f"window.applyAlertVolumeOnly({json.dumps(alert_volume)})")
            window.evaluate_js(f"window.applySensitivityOnly({json.dumps(sensitivity)})")
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
