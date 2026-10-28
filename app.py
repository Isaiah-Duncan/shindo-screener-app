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
PLACES_JSON_RELATIVE_PATH = "places_jp.json"
MUNICIPALITIES_JSON_RELATIVE_PATH = "municipalities_jp.json"
IDLE_STATE = {"status": "idle", "updated_at": None}
EEW_IDLE_MINUTES = 5          # an early warning stays up this long if no measured report follows
MEASURED_IDLE_MINUTES = 10    # a measured reading stays up this long, then the screen goes quiet
CANCEL_NOTICE_MINUTES = 2      # the "JMA cancelled this warning" notice stays this long, then the screen goes idle
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


DUPLICATE_WINDOW_MINUTES = 15


def _same_logged_quake(a, b):
    """Two log rows describe one quake: same place name, same magnitude (when both known),
    logged within DUPLICATE_WINDOW_MINUTES of each other."""
    try:
        gap = abs((datetime.fromisoformat(a["felt_at"]) - datetime.fromisoformat(b["felt_at"])).total_seconds())
    except (KeyError, TypeError, ValueError):
        return False
    if gap > DUPLICATE_WINDOW_MINUTES * 60:
        return False
    if (a.get("hypocenter") or None) != (b.get("hypocenter") or None):
        return False
    ma, mb = a.get("magnitude"), b.get("magnitude")
    return ma is None or mb is None or ma == mb


def clean_felt_log(entries):
    """Rows made while the beta "follow the quake" mode showed another place are not readings
    for the saved place, so they are dropped. Rows that describe the same quake are merged
    into one (the earliest time, the newest values)."""
    rows = [e for e in entries if not e.get("beta_follow")]
    rows.sort(key=lambda e: e.get("felt_at") or "")
    merged = []
    for e in rows:
        for m in merged:
            if _same_logged_quake(m, e):
                newer = (e.get("updated_at") or "") >= (m.get("updated_at") or "")
                if newer:
                    first = m.get("felt_at")
                    m.update({k: v for k, v in e.items() if v is not None})
                    m["felt_at"] = first
                break
        else:
            merged.append(dict(e))
    return merged


def load_felt_log():
    try:
        with open(FELT_LOG_PATH, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        entries = []
    return clean_felt_log(prune_felt_log(entries))


# ---- Quake series (for the aftershock table) --------------------------------
# One small row per measured earthquake JMA reported, with the value at each nearby big city.
# Kept on this computer for SERIES_KEEP_DAYS days. Every value is JMA's own; nothing is estimated.
SERIES_PATH = os.path.join(settings_dir(), "quake_series.json")
SERIES_KEEP_DAYS = 14
AFTERSHOCK_RADIUS_KM = 80
AFTERSHOCK_WINDOW_HOURS = 72
MAINSHOCK_MIN_MAGNITUDE = 4.5
SERIES_CITY_RADIUS_KM = 200


def load_series():
    try:
        with open(SERIES_PATH, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        rows = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SERIES_KEEP_DAYS)).isoformat()
    return [r for r in rows if isinstance(r, dict) and (r.get("origin") or "") >= cutoff]


def save_series(rows):
    try:
        with open(SERIES_PATH, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
    except OSError:
        pass


def save_felt_log(entries):
    with open(FELT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f)


# Local diagnostic record of the early-warning messages this app received
# and what it did with each one. Stays on this PC (never sent anywhere), is
# trimmed to DIAG_RETENTION_DAYS and DIAG_MAX_BYTES, and holds no monitored
# location: only fields from JMA's own message plus the app's decision. It
# exists so a surprising alert can be reconstructed afterwards.
DIAG_LOG_PATH = os.path.join(settings_dir(), "eew_diagnostics.jsonl")
DIAG_RETENTION_DAYS = 30
DIAG_MAX_BYTES = 1_000_000
_diag_pruned = False


def _prune_diag_log():
    cutoff = datetime.now(timezone.utc) - timedelta(days=DIAG_RETENTION_DAYS)
    try:
        with open(DIAG_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    kept = []
    for line in lines:
        try:
            if datetime.fromisoformat(json.loads(line)["t"]) >= cutoff:
                kept.append(line)
        except (KeyError, TypeError, ValueError):
            continue
    while kept and sum(len(x) + 1 for x in kept) > DIAG_MAX_BYTES:
        kept = kept[len(kept) // 2:]
    with open(DIAG_LOG_PATH, "w", encoding="utf-8") as f:
        f.write("".join(x + "\n" for x in kept))


def diag_log(kind, **fields):
    """Appends one JSON line. Never raises: diagnostics must not be able
    to break the live display."""
    global _diag_pruned
    try:
        if not _diag_pruned or (os.path.exists(DIAG_LOG_PATH) and os.path.getsize(DIAG_LOG_PATH) > DIAG_MAX_BYTES):
            _prune_diag_log()
            _diag_pruned = True
        entry = {"t": datetime.now(timezone.utc).isoformat(), "kind": kind}
        entry.update(fields)
        with open(DIAG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


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
    "quiet_hours": alert.DEFAULT_QUIET_HOURS,
    "alert_voice": alert.DEFAULT_ALERT_VOICE,
    "sensitivity": "normal",
    "follow_quake": "off",   # beta: switch the monitored place to the one nearest each quake's epicenter
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


# ---- Offline place search -------------------------------------------------
# Typing in the location box used to wait for Nominatim (5 to 10 seconds, and
# it only matches whole words, so "kum" never found Kumamoto). Search now
# runs on a bundled list of every Japanese municipality (places_jp.json) and
# matches the start of the name as the person types, in English letters,
# kana or kanji. Nominatim stays as a fallback for anything not on the list.

_places_cache = None
_last_online_lookup = 0.0
_online_lock = threading.Lock()


# Major cities for the "Cities" panel (rough city-centre coordinates). Each one is matched to its
# nearest JMA station; the value shown is always JMA's own measured value for that station.
MAJOR_CITIES = [
    ('Sapporo', '札幌', 43.06, 141.35),
    ('Asahikawa', '旭川', 43.77, 142.36),
    ('Hakodate', '函館', 41.77, 140.73),
    ('Aomori', '青森', 40.82, 140.74),
    ('Hachinohe', '八戸', 40.51, 141.49),
    ('Misawa', '三沢', 40.68, 141.37),
    ('Morioka', '盛岡', 39.7, 141.15),
    ('Sendai', '仙台', 38.27, 140.87),
    ('Akita', '秋田', 39.72, 140.1),
    ('Yamagata', '山形', 38.24, 140.33),
    ('Fukushima', '福島', 37.76, 140.47),
    ('Koriyama', '郡山', 37.4, 140.36),
    ('Mito', '水戸', 36.34, 140.45),
    ('Utsunomiya', '宇都宮', 36.57, 139.88),
    ('Maebashi', '前橋', 36.39, 139.06),
    ('Saitama', 'さいたま', 35.86, 139.65),
    ('Chiba', '千葉', 35.61, 140.12),
    ('Funabashi', '船橋', 35.69, 139.98),
    ('Tokyo', '東京', 35.69, 139.69),
    ('Kawasaki', '川崎', 35.53, 139.7),
    ('Yokohama', '横浜', 35.44, 139.64),
    ('Kofu', '甲府', 35.66, 138.57),
    ('Niigata', '新潟', 37.92, 139.04),
    ('Nagano', '長野', 36.65, 138.18),
    ('Toyama', '富山', 36.7, 137.21),
    ('Kanazawa', '金沢', 36.56, 136.66),
    ('Fukui', '福井', 36.07, 136.22),
    ('Shizuoka', '静岡', 34.98, 138.38),
    ('Hamamatsu', '浜松', 34.71, 137.73),
    ('Nagoya', '名古屋', 35.18, 136.91),
    ('Gifu', '岐阜', 35.42, 136.76),
    ('Tsu', '津', 34.73, 136.51),
    ('Kyoto', '京都', 35.01, 135.77),
    ('Osaka', '大阪', 34.69, 135.5),
    ('Kobe', '神戸', 34.69, 135.2),
    ('Nara', '奈良', 34.69, 135.83),
    ('Wakayama', '和歌山', 34.23, 135.17),
    ('Okayama', '岡山', 34.66, 133.92),
    ('Hiroshima', '広島', 34.4, 132.46),
    ('Matsue', '松江', 35.47, 133.05),
    ('Takamatsu', '高松', 34.34, 134.04),
    ('Matsuyama', '松山', 33.84, 132.77),
    ('Kochi', '高知', 33.56, 133.53),
    ('Tokushima', '徳島', 34.07, 134.56),
    ('Fukuoka', '福岡', 33.59, 130.4),
    ('Kumamoto', '熊本', 32.8, 130.71),
    ('Nagasaki', '長崎', 32.75, 129.88),
    ('Oita', '大分', 33.24, 131.61),
    ('Kagoshima', '鹿児島', 31.6, 130.56),
    ('Naha', '那覇', 26.21, 127.68),
]
CITY_TABLE_SIZE = 8
CITY_TABLE_MAX_AGE_MINUTES = 90


def _load_places():
    global _places_cache
    if _places_cache is None:
        try:
            with open(resource_path(PLACES_JSON_RELATIVE_PATH), "r", encoding="utf-8") as f:
                _places_cache = json.load(f).get("places", [])
        except Exception:
            traceback.print_exc()
            _places_cache = []
    return _places_cache


# ---- Town outlines for the map -------------------------------------------
# municipalities_jp.json holds the outline of every town, grouped by prefecture
# (MLIT National Land Numerical Information, simplified). Only the saved
# prefecture is sent to the screen. A drawing aid only: it never changes a JMA value.

_geo_cache = None


def _load_geo():
    global _geo_cache
    if _geo_cache is None:
        try:
            with open(resource_path(MUNICIPALITIES_JSON_RELATIVE_PATH), "r", encoding="utf-8") as f:
                _geo_cache = json.load(f).get("prefs", {})
        except Exception:
            traceback.print_exc()
            _geo_cache = {}
    return _geo_cache


def home_town_codes(name):
    """Town codes for the saved location name (English or Japanese form), or []."""
    if not name:
        return []
    for p in _load_places():
        if name in (p.get("e"), p.get("n"), p["p"] + p["j"]):
            return list(p.get("c") or [])
    return []


def _norm_romaji(text):
    """Fold spelling variants so 'Osaka', 'Oosaka' and 'Ohsaka' (and the
    Kunrei-style 'si', 'ti', 'tu') all compare equal, and so a half-typed
    word such as 'kagosh' still matches the start of 'kagoshima'."""
    t = text.lower()
    for a, b in (("\u014d", "o"), ("\u016b", "u"), ("\u0101", "a"), ("\u012b", "i"), ("\u0113", "e")):
        t = t.replace(a, b)
    t = "".join(ch for ch in t if ch.isalnum())
    t = t.replace("oh", "o").replace("ou", "o").replace("oo", "o").replace("uu", "u")
    t = t.replace("sh", "s").replace("ch", "t").replace("ts", "t").replace("j", "z").replace("fu", "hu")
    return t


def _to_hiragana(text):
    return "".join(chr(ord(ch) - 0x60) if "\u30a1" <= ch <= "\u30f6" else ch for ch in text)


def search_local_places(query, lang="en", limit=8):
    q = query.strip()
    if not q:
        return []
    places = _load_places()
    ascii_query = all(ord(ch) < 128 for ch in q)
    scored = []
    if ascii_query:
        nq = _norm_romaji(q)
        if not nq:
            return []
        for p in places:
            key = _norm_romaji(p["k"])
            if key.startswith(nq):
                score = 0
            elif any(_norm_romaji(w).startswith(nq) for w in p["k"].split()[1:]):
                score = 1
            elif _norm_romaji(p["e"].split(", ")[-1].replace(" Prefecture", "")).startswith(nq):
                score = 2
            elif nq in key and len(nq) >= 3:
                score = 3
            else:
                continue
            scored.append((score, "cwm".index(p["t"]), len(key), p))
    else:
        qh = _to_hiragana(q)
        for p in places:
            full = p["p"] + p["j"]
            if p["j"].startswith(q) or (p["h"] and p["h"].startswith(qh)):
                score = 0
            elif full.startswith(q) or p["p"].startswith(q):
                score = 2
            elif q in p["j"]:
                score = 3
            else:
                continue
            scored.append((score, "cwm".index(p["t"]), len(p["j"]), p))
    scored.sort(key=lambda s: s[:3])
    out = []
    for _, _, _, p in scored[:limit]:
        name = (p.get("n") or (p["p"] + p["j"])) if lang == "ja" else p["e"]
        out.append({"name": name, "lat": p["a"], "lon": p["o"]})
    return out



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
        candidates = search_local_places(city_name, lang)
        if not candidates and len(city_name) >= 3:
            # Not on the bundled list: ask Nominatim, at most once per second.
            global _last_online_lookup
            try:
                with _online_lock:
                    wait = 1.1 - (time.time() - _last_online_lookup)
                    if wait > 0:
                        time.sleep(wait)
                    _last_online_lookup = time.time()
                    candidates = search_locations(city_name, lang)
            except Exception as e:
                return {"ok": False, "error": f"Lookup failed: {e}", "candidates": []}
        if not candidates:
            return {"ok": False, "error": f"Couldn't find \"{city_name}\" as a real town or city. Check the spelling, or try a nearby larger place.", "candidates": []}
        return {"ok": True, "candidates": candidates}

    def get_map(self):
        """Outlines of the towns in the saved prefecture, plus which ones are 'home'.
        Returns {"ok": False} when the saved place has no town code (the screen then
        simply shows no map)."""
        listener = self._get_listener()
        home = (listener.home if listener is not None else None) or {}
        codes = home_town_codes(home.get("name"))
        if not codes:
            return {"ok": False}
        towns = _load_geo().get(codes[0][:2])
        if not towns:
            return {"ok": False}
        return {"ok": True, "home": codes, "towns": towns}

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

    def set_quiet_hours(self, value):
        value = "on" if value == "on" else "off"
        existing = load_settings()
        if existing:
            save_settings(merged_settings(existing, quiet_hours=value))
        listener = self._get_listener()
        if listener is not None:
            listener.quiet_hours = value == "on"
        return {"ok": True}

    def set_alert_voice(self, value):
        if value not in alert.VALID_ALERT_VOICE:
            return {"ok": False, "error": f"Not a voice setting: {value}"}
        existing = load_settings()
        if existing:
            save_settings(merged_settings(existing, alert_voice=value))
        listener = self._get_listener()
        if listener is not None:
            listener.alert_voice = value
        return {"ok": True}

    def set_follow_quake(self, value):
        """Beta. "on": for each new quake the app shows the place nearest its epicenter instead of
        the saved place, then returns to the saved place when the quake leaves the screen. Never
        changes the saved location."""
        if value not in ("on", "off"):
            return {"ok": False, "error": f"Not a follow setting: {value}"}
        existing = load_settings()
        if existing:
            save_settings(merged_settings(existing, follow_quake=value))
        listener = self._get_listener()
        if listener is not None:
            listener.set_follow(value == "on")
        return {"ok": True}

    def get_cities(self, lang="en"):
        listener = self._get_listener()
        return listener.city_table(lang) if listener is not None else {"ok": False, "reason": "none"}

    def get_aftershocks(self, lang="en"):
        listener = self._get_listener()
        return listener.aftershocks(lang) if listener is not None else {"ok": False, "reason": "none"}

    def view_city(self, name_en):
        listener = self._get_listener()
        return listener.view_city(name_en) if listener is not None else {"ok": False}

    def view_back(self):
        listener = self._get_listener()
        if listener is not None:
            listener.view_back()
        return {"ok": True}

    def save_share_image(self, data_url, stem="shindo-screener"):
        """Saves the PNG the Cities panel drew to Pictures/Shindo Screener and returns its path."""
        import base64
        try:
            head, _, b64 = (data_url or "").partition(",")
            if "image/png" not in head or not b64:
                return {"ok": False, "error": "Not a PNG image."}
            folder = os.path.join(os.path.expanduser("~"), "Pictures", "Shindo Screener")
            os.makedirs(folder, exist_ok=True)
            safe = "".join(c for c in str(stem) if c.isalnum() or c in "-_")[:60] or "shindo-screener"
            path = os.path.join(folder, safe + ".png")
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
            return {"ok": True, "path": path}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

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
        before calling this. This is a visual sample only: Listener.push()
        recognizes display_source == "test" and never wakes the screen,
        jumps to the foreground, or plays the alarm chime for it, no matter
        what the alert threshold is set to. To hear how loud a level
        sounds, use the dedicated volume preview (preview_alert_sound)
        instead, which is sound-only and fires from an explicit choice.
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
                 sensitivity="normal", quiet_hours=True, alert_voice=alert.DEFAULT_ALERT_VOICE,
                 follow_quake=False):
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
        self.follow_quake = follow_quake   # beta mode, see set_follow
        self.base_home = None    # the saved place, while a followed place is shown
        self.beta_ticked = set() # event keys that already played the beta wood tick
        self.viewing = False     # True while the user has picked a city in the Cities panel
        self.lock = threading.Lock()
        # Mutated directly by the Api set_alert_* methods when the user
        # changes a setting, so a change takes effect on the very next
        # push without needing to restart the listener threads.
        self.alert_min_step = alert_min_step
        self.alert_wake_screen = alert_wake_screen
        self.alert_volume = alert_volume
        self.sensitivity = sensitivity
        self.quiet_hours = quiet_hours
        self.alert_voice = alert_voice

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

    # ------------------------------------------------------------ beta: follow the quake

    def set_follow(self, on):
        self.follow_quake = bool(on)
        if not on:
            self.restore_home()
            if self.current_state.get("status") == "active":
                self.push(self.build_state()) if self.event else self.go_idle()

    def nearest_place(self, lat, lon):
        """The place in places_jp.json closest to a point, as a home dict, or None."""
        best, best_d = None, None
        for p in _load_places():
            d = jma_sources.haversine_km(lat, lon, p["a"], p["o"])
            if best_d is None or d < best_d:
                best, best_d = p, d
        if best is None:
            return None
        lang = (load_settings() or {}).get("language", "en")
        name = best["e"] if lang != "ja" else best["p"] + best["j"]
        return {"name": name, "lat": best["a"], "lon": best["o"]}

    def follow_epicenter(self, lat, lon):
        """Show the place nearest the epicenter. Does not touch the saved location and does not
        replay old messages (the caller re-parses the message it is handling)."""
        if lat is None or lon is None:
            return False
        place = self.nearest_place(lat, lon)
        if place is None:
            return False
        if self.base_home is None:
            self.base_home = self._home
        self._home = place
        self.home_info = jma_sources.locate_home(place, self.stations)
        diag_log("follow_quake_switched")
        return True

    # ------------------------------------------------------------ Cities panel

    def _series_recent(self):
        """True when the last 72 hours hold a quake big enough to have aftershocks worth listing."""
        now = time.time()
        if now - getattr(self, "_series_checked", 0) < 60:
            return getattr(self, "_series_flag", False)
        self._series_checked = now
        try:
            self._series_flag = bool(self.aftershocks().get("ok"))
        except Exception:
            self._series_flag = False
        return self._series_flag

    def city_values(self, msg, lat, lon, limit_km=None):
        """JMA's measured value at the nearest station of each big city, nearest to the epicenter first.
        step is None when JMA listed no Shindo 1 or more there; source tells "below 1" from "no report"."""
        rows = []
        for en, ja, clat, clon in MAJOR_CITIES:
            dist = jma_sources.haversine_km(lat, lon, clat, clon)
            if limit_km is None or dist <= limit_km:
                rows.append((dist, en, ja, clat, clon))
        rows.sort()
        out = []
        for dist, en, ja, clat, clon in rows:
            info = jma_sources.parse_p2p_quake(msg, jma_sources.locate_home({"lat": clat, "lon": clon}, self.stations))
            r = (info or {}).get("reading")
            out.append({"en": en, "ja": ja, "step": r["step"] if r and r["source"] != "station_below_1" else None,
                        "source": (r or {}).get("source"), "station": (r or {}).get("station"),
                        "distance_km": round(dist), "viewable": r is not None})
        return out

    def record_series(self, msg, info):
        """Keeps one row per measured quake (several JMA messages for one quake merge into one row)."""
        try:
            origin = info.get("origin_time")
            if origin is None or info.get("lat") is None or info.get("lon") is None:
                return
            cities = [{"en": c["en"], "step": c["step"], "src": c["source"]}
                      for c in self.city_values(msg, info["lat"], info["lon"], SERIES_CITY_RADIUS_KM)]
            row = {"origin": origin.astimezone(timezone.utc).isoformat(), "hypocenter": info.get("hypocenter"),
                   "lat": info["lat"], "lon": info["lon"], "magnitude": info.get("magnitude"),
                   "max_step": info.get("jma_max_measured"), "cities": cities, "updated_at": self.now_iso()}
            rows = load_series()
            for r in rows:
                try:
                    other = datetime.fromisoformat(r["origin"])
                except (KeyError, ValueError):
                    continue
                if jma_sources.same_quake(origin, other):
                    keep = {k: v for k, v in row.items() if v is not None}
                    r.update(keep)
                    break
            else:
                rows.append(row)
            rows.sort(key=lambda r: r.get("origin") or "")
            save_series(rows)
        except Exception:
            traceback.print_exc()

    def aftershocks(self, lang="en"):
        """The biggest recent quake and the quakes JMA reported near it afterward, each with the big
        cities that felt it. Pure bookkeeping of JMA reports."""
        rows = load_series()
        now = datetime.now(timezone.utc)
        recent = []
        for r in rows:
            try:
                t = datetime.fromisoformat(r["origin"])
            except (KeyError, ValueError):
                continue
            if (now - t).total_seconds() <= AFTERSHOCK_WINDOW_HOURS * 3600:
                recent.append((t, r))
        mains = [(t, r) for t, r in recent if (r.get("magnitude") or 0) >= MAINSHOCK_MIN_MAGNITUDE
                 or jma_sources.rank(r.get("max_step") or "0") >= jma_sources.rank("4")]
        if not mains:
            return {"ok": False, "reason": "no_main"}
        mt, mr = max(mains, key=lambda x: ((x[1].get("magnitude") or 0), x[0]))
        jst = timezone(timedelta(hours=9))
        out = []
        for t, r in sorted(recent, key=lambda x: x[0], reverse=True):
            if r is mr or t <= mt + timedelta(seconds=60):
                continue
            if jma_sources.haversine_km(mr["lat"], mr["lon"], r["lat"], r["lon"]) > AFTERSHOCK_RADIUS_KM:
                continue
            felt = [c for c in r.get("cities") or [] if c.get("step")]
            felt.sort(key=lambda c: -jma_sources.rank(c["step"]))
            names = {c[0]: c[1] for c in MAJOR_CITIES}
            out.append({"time": t.astimezone(jst).strftime("%m-%d %H:%M"), "magnitude": r.get("magnitude"),
                        "hypocenter": r.get("hypocenter"), "max_step": r.get("max_step"),
                        "cities": [{"en": c["en"], "ja": names.get(c["en"], c["en"]), "step": c["step"]} for c in felt[:4]]})
        return {"ok": True, "main": {"hypocenter": mr.get("hypocenter"), "magnitude": mr.get("magnitude"),
                                     "max_step": mr.get("max_step"), "time": mt.astimezone(jst).strftime("%Y-%m-%d %H:%M")},
                "count": len(out), "rows": out[:8]}

    def city_table(self, lang="en"):
        """The big cities nearest the epicenter of the latest measured report, each with JMA's own
        measured value at that city's nearest station. A city with no value was below Shindo 1 or
        not listed by JMA. Nothing is estimated."""
        msg = self._last_measured_msg
        if msg is None or self._last_measured_received_at is None:
            return {"ok": False, "reason": "no_report"}
        if (datetime.now(timezone.utc) - self._last_measured_received_at).total_seconds() > CITY_TABLE_MAX_AGE_MINUTES * 60:
            return {"ok": False, "reason": "old"}
        base = jma_sources.parse_p2p_quake(msg, self.home_info)
        if not base or base.get("lat") is None or base.get("lon") is None:
            return {"ok": False, "reason": "no_epicenter"}
        rows = []
        for en, ja, lat, lon in MAJOR_CITIES:
            dist = jma_sources.haversine_km(base["lat"], base["lon"], lat, lon)
            rows.append((dist, en, ja, lat, lon))
        rows.sort()
        out = []
        for dist, en, ja, lat, lon in rows[:CITY_TABLE_SIZE]:
            info = jma_sources.parse_p2p_quake(msg, jma_sources.locate_home({"lat": lat, "lon": lon}, self.stations))
            r = (info or {}).get("reading")
            step = r["step"] if r and r["source"] != "station_below_1" else None
            out.append({"en": en, "ja": ja, "step": step, "source": (r or {}).get("source"),
                        "station": (r or {}).get("station"), "distance_km": round(dist),
                        "viewable": r is not None})
        # The station JMA lists with the highest measured value (first one when several tie).
        top = None
        for pt in msg.get("points") or []:
            step = jma_sources.P2P_SCALE.get(pt.get("scale"))
            if step and not pt.get("isArea") and (top is None or jma_sources.rank(step) > jma_sources.rank(top["step"])):
                top = {"step": step, "station": pt.get("addr"), "pref": pt.get("pref")}
        origin = base.get("origin_time")
        return {"ok": True, "viewing": self.viewing, "max_place": top, "hypocenter": base.get("hypocenter"),
                "magnitude": base.get("magnitude"), "jma_max": base.get("jma_max_measured"),
                "time": origin.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M") if origin else None,
                "cities": out}

    def view_city(self, name_en):
        """Show the latest measured quake for a chosen city until the user goes back or the quake leaves the screen."""
        city = next((c for c in MAJOR_CITIES if c[0] == name_en), None)
        msg = self._last_measured_msg
        if city is None or msg is None:
            return {"ok": False}
        place = {"name": self._city_label(city), "lat": city[2], "lon": city[3]}
        probe = jma_sources.parse_p2p_quake(msg, jma_sources.locate_home(place, self.stations))
        with self.lock:
            if not probe or probe.get("reading") is None or not self.event or self.event.get("phase") != "measured":
                return {"ok": False}
            if self.base_home is None:
                self.base_home = self._home
            self._home = place
            self.home_info = jma_sources.locate_home(place, self.stations)
            self.viewing = True
            self.event["reading"] = {}
        self.handle_measured(msg)
        return {"ok": True}

    def _city_label(self, city):
        lang = (load_settings() or {}).get("language", "en")
        return city[1] if lang == "ja" else city[0]

    def view_back(self):
        with self.lock:
            self.restore_home()
        msg = self._last_measured_msg
        if msg is None or not self.event:
            return
        probe = jma_sources.parse_p2p_quake(msg, self.home_info)
        if probe and probe.get("reading") is not None:
            with self.lock:
                self.event["reading"] = {}
            self.handle_measured(msg)
        else:
            self.go_idle()   # the saved place did not feel this quake

    def restore_home(self):
        self.viewing = False
        if self.base_home is not None:
            self._home = self.base_home
            self.home_info = jma_sources.locate_home(self._home, self.stations)
            self.base_home = None

    # ------------------------------------------------------------ output

    def push(self, state):
        # Which place the screen is showing, so the screen can follow a switch (beta).
        state = dict(state, monitoring=(self._home or {}).get("name"), following=self.base_home is not None and not self.viewing, viewing=self.viewing,
                     series_recent=bool(self._series_recent()))
        self.current_state = state
        try:
            self.window.evaluate_js(f"window.applyState({json.dumps(state, ensure_ascii=False)})")
        except Exception:
            pass  # window may be closing, not fatal
        key = state.get("event_key")
        # A test/sample reading (trigger_test_event) is only ever a preview of
        # what a level looks like on screen — it must never wake the display,
        # steal foreground, or play the alarm chime the way a real JMA-sourced
        # reading does, no matter what alert_min_step is set to. The only
        # place sound is allowed to play for a sample is the explicit volume
        # preview button (preview_alert_sound), which a person triggers on
        # purpose to hear how loud a level is, not as a side effect of just
        # looking at a sample.
        is_test = state.get("display_source") == "test"
        if (not is_test and key and key not in self.alerted
                and alert.should_alert(state, min_step=self.alert_min_step)):
            self.alerted.add(key)
            alert.trigger(
                self.window, resource_path,
                wake_screen=self.alert_wake_screen, volume=self.alert_volume,
                state=state, quiet_hours=self.quiet_hours, voice=self.alert_voice,
                lang=(load_settings() or {}).get("language", "en"),
            )
            self.beta_ticked.add(key)
        elif (self.follow_quake and not is_test and key and key not in self.beta_ticked
                and state.get("status") == "active"):
            self.beta_ticked.add(key)
            alert.trigger_beta(self.window, resource_path, wake_screen=self.alert_wake_screen)

    def go_idle(self):
        self.event = None
        self.restore_home()
        self.push(dict(IDLE_STATE, updated_at=self.now_iso()))

    def show_cancelled(self):
        """JMA withdrew the warning on screen: say so for a short while
        (frontend: renderCancelled) instead of just going blank. Carries no
        event_key, so it can never trigger the alert chime."""
        self.event = None
        self.restore_home()
        self.push({"status": "cancelled", "updated_at": self.now_iso()})

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
            if st.get("status") == "cancelled":
                if self.minutes_since(st.get("updated_at")) > CANCEL_NOTICE_MINUTES:
                    self.go_idle()
                return
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
            "first_magnitude": ev.get("first_magnitude"),
            "first_seen_at": ev.get("first_seen_at"),
            "is_warning": bool(ev.get("is_warning")),
            "pref_name": (ev.get("pref") or {}).get("name"),
            "pref_max": (ev.get("pref") or {}).get("max"),
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
        # Plain "when was this area last felt" fact, for the frontend's
        # neutral last-felt line (steps 1-3 only, see screener_app.html
        # describeLastFelt). Deliberately just the timestamp: any framing
        # of what it might mean for this reading belongs in the UI copy,
        # not in data the backend hands over. Excludes the current event
        # itself, since record_felt_event may have already logged it by
        # the time this runs (handle_measured logs before pushing).
        s["last_felt_at"] = self.last_felt_before(ev.get("key"))
        return s

    def last_felt_before(self, exclude_key):
        """Most recent felt_log entry's timestamp, excluding exclude_key
        (today's own event, if it's already been logged). None if this
        would be the first felt earthquake on record."""
        entries = [e for e in load_felt_log() if e.get("event_key") != exclude_key]
        if not entries:
            return None
        return max(entries, key=lambda e: e.get("felt_at") or "").get("felt_at")

    # ------------------------------------------------------------ EEW (Wolfx)

    def handle_eew(self, payload):
        info = jma_sources.parse_eew(payload, self.home_info.get("area"))
        seen = {
            "event_id": str(payload.get("EventID") or ""), "serial": payload.get("Serial"),
            "cancel": bool(payload.get("isCancel")), "training": bool(payload.get("isTraining")),
            "magnitude": payload.get("Magnitude", payload.get("Magunitude")),
            "hypocenter": payload.get("Hypocenter"),
        }
        if not info:
            diag_log("eew_ignored", reason="training, wrong type, or no EventID", **seen)
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
                on_screen = bool(self.event and self.event.get("eew_id") == info["event_id"])
                if on_screen and self.event.get("phase") != "measured":
                    diag_log("eew_cancel_applied", **seen)
                    self.show_cancelled()
                else:
                    # Either another quake is on screen, nothing is, or measured
                    # values already replaced the warning (those stay).
                    diag_log("eew_cancel_not_applied", on_screen=on_screen, **seen)
                return
            if self.viewing and not (self.event and (self.event.get("eew_id") == info["event_id"]
                    or jma_sources.same_quake(self.event.get("origin_time"), info["origin_time"]))):
                self.restore_home()
                info = jma_sources.parse_eew(payload, self.home_info.get("area")) or info
            if self.follow_quake and info["lat"] is not None and not (
                    self.event and (self.event.get("eew_id") == info["event_id"]
                                    or jma_sources.same_quake(self.event.get("origin_time"), info["origin_time"]))):
                if self.follow_epicenter(info["lat"], info["lon"]):
                    info = jma_sources.parse_eew(payload, self.home_info.get("area")) or info
            dist = self.epicenter_km(info["lat"], info["lon"])
            relevant = info["area_forecast"] is not None or (dist is not None and dist <= EEW_NOTICE_RADIUS_KM)
            same = self.event and (self.event.get("eew_id") == info["event_id"]
                                   or jma_sources.same_quake(self.event.get("origin_time"), info["origin_time"]))
            if not same:
                if not relevant:
                    diag_log("eew_not_relevant", dist_km=dist, **seen)
                    return
                self.event = {"key": info["event_id"], "origin_time": info["origin_time"]}
            if self.event.get("phase") == "measured":
                diag_log("eew_ignored_measured_shown", **seen)
                return  # measured values already on screen; a late warning update must not replace them
            self.event.update(
                eew_id=info["event_id"], phase="eew", serial=info["serial"],
                maturity="final" if info["is_final"] else ("updated" if (info["serial"] or 1) > 1 else "preliminary"),
                hypocenter=info["hypocenter"], magnitude=info["magnitude"], depth_km=info["depth_km"],
                lat=info["lat"], lon=info["lon"], accuracy=info["accuracy"],
                jma_max=info["jma_max_forecast"], jma_max_kind="forecast",
                area_forecast=info["area_forecast"] or self.event.get("area_forecast"),
                pref=info["pref"] or self.event.get("pref"),
                is_warning=bool(info.get("is_warning")) or bool(self.event.get("is_warning")),
            )
            # First magnitude and first-seen time, kept for the "revised since the
            # first report" and "no measured values yet" notes (see screener_app.html).
            if self.event.get("first_magnitude") is None and info["magnitude"] is not None:
                self.event["first_magnitude"] = info["magnitude"]
            self.event.setdefault("first_seen_at", self.now_iso())
            af = self.event.get("area_forecast")
            # Counts and levels only: the prefecture NAME is left out so this file
            # still holds no monitored location (see docs/privacy.html).
            diag_log("eew_shown", dist_km=dist, area_from=(af or {}).get("from"), area_to=(af or {}).get("to"),
                     jma_max=info["jma_max_forecast"], pref_max=(info["pref"] or {}).get("max"),
                     warn_areas=info["warn_areas"], **seen)
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
        self.record_series(msg, info)
        with self.lock:
            same = self.event and jma_sources.same_quake(self.event.get("origin_time"), info["origin_time"])
            if not same and self.viewing:
                self.restore_home()
                info = jma_sources.parse_p2p_quake(msg, self.home_info) or info
            if not same and self.follow_quake and info.get("lat") is not None:
                if self.follow_epicenter(info["lat"], info["lon"]):
                    info = jma_sources.parse_p2p_quake(msg, self.home_info) or info
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
            if self.base_home is not None:
                own = jma_sources.parse_p2p_quake(msg, jma_sources.locate_home(self.base_home, self.stations))
                if own and own.get("felt_near_user") and own.get("reading"):
                    self.record_felt_event(dict(ev, reading=own["reading"]), home=self.base_home)
            self.push(self.build_state())

    def record_felt_event(self, ev, home=None):
        """Adds or updates this event's entry in the local "felt this"
        log (see FELT_LOG_PATH), for the Settings > History export. Not
        JMA data in itself, just a personal record of when the app's own
        relayed JMA readings actually showed something felt here."""
        reading = ev.get("reading") or {}
        step = reading.get("step")
        # While another place is on screen (follow mode, or a city picked in the Cities panel) the
        # reading is not "felt here"; handle_measured logs the saved place's own reading instead
        # and passes home=.
        if not step or (self.base_home is not None and home is None):
            return
        now = self.now_iso()
        origin = ev.get("origin_time")
        occurred = origin.astimezone(timezone.utc).isoformat() if origin else now
        # The saved location name at the time of the reading, so the history can
        # be told apart when the user changes location (older entries have none).
        location = (home or self._home or {}).get("name")
        entries = load_felt_log()
        probe = {"felt_at": occurred, "hypocenter": ev.get("hypocenter"), "magnitude": ev.get("magnitude")}
        for e in entries:
            if e.get("event_key") == ev["key"] or _same_logged_quake(e, probe):
                e.update(display_step=step, magnitude=ev.get("magnitude"), hypocenter=ev.get("hypocenter"),
                         station=reading.get("station"), station_km=reading.get("station_km"),
                         location=location, updated_at=now)
                break
        else:
            entries.append({
                "event_key": ev["key"], "felt_at": occurred, "updated_at": now,
                "display_step": step, "magnitude": ev.get("magnitude"), "hypocenter": ev.get("hypocenter"),
                "station": reading.get("station"), "station_km": reading.get("station_km"),
                "location": location,
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
            alert_min_step=alert.normalize_alert_min_step(settings.get("alert_min_step", alert.DEFAULT_ALERT_MIN_STEP)),
            alert_wake_screen=settings.get("alert_wake_screen", "on") == "on",
            alert_volume=settings.get("alert_volume", alert.DEFAULT_ALERT_VOLUME),
            sensitivity=settings.get("sensitivity", "normal"),
            quiet_hours=settings.get("quiet_hours", alert.DEFAULT_QUIET_HOURS) == "on",
            alert_voice=settings.get("alert_voice", alert.DEFAULT_ALERT_VOICE),
            follow_quake=settings.get("follow_quake", "off") == "on",
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
            alert_min_step = alert.normalize_alert_min_step(settings.get("alert_min_step", alert.DEFAULT_ALERT_MIN_STEP))
            alert_wake_screen = settings.get("alert_wake_screen", "on")
            alert_volume = settings.get("alert_volume", alert.DEFAULT_ALERT_VOLUME)
            sensitivity = settings.get("sensitivity", "normal")
            quiet_hours = settings.get("quiet_hours", alert.DEFAULT_QUIET_HOURS)
            alert_voice = settings.get("alert_voice", alert.DEFAULT_ALERT_VOICE)
            window.evaluate_js(f"window.applyLanguageOnly({json.dumps(lang)})")
            window.evaluate_js(f"window.applyTimeFormatOnly({json.dumps(time_fmt)})")
            window.evaluate_js(f"window.applyTextScaleOnly({json.dumps(text_scale)})")
            window.evaluate_js(f"window.applyMotionEffectsOnly({json.dumps(motion)})")
            window.evaluate_js(f"window.applyAlertMinStepOnly({json.dumps(alert_min_step)})")
            window.evaluate_js(f"window.applyAlertWakeScreenOnly({json.dumps(alert_wake_screen)})")
            window.evaluate_js(f"window.applyAlertVolumeOnly({json.dumps(alert_volume)})")
            window.evaluate_js(f"window.applySensitivityOnly({json.dumps(sensitivity)})")
            window.evaluate_js(f"window.applyQuietHoursOnly({json.dumps(quiet_hours)})")
            window.evaluate_js(f"window.applyAlertVoiceOnly({json.dumps(alert_voice)})")
            window.evaluate_js(f"window.applyFollowQuakeOnly({json.dumps(settings.get('follow_quake', 'off'))})")
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
