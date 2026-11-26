"""
Windows-only "wake the screen and grab attention" behavior for a
qualifying live earthquake alert, the same job JQuake's own alert does:
optionally wake the display if it's idle-off, bring this app's window
to the front, and play an alert chime. All three of those are user
configurable from Settings (alert threshold, whether to force-wake the
screen, and chime volume); this module just carries out whatever the
saved settings say.

This does NOT wake the laptop from true sleep (Windows fully suspended).
Nothing running inside a suspended machine can detect a live WebSocket
event in the first place, since the whole process is paused along with
it; there's no way around that from application code alone, it would
need external hardware (e.g. a Wake-on-LAN packet from another always-on
device on the network). What this module DOES handle is the much more
common case for an always-on wall display: the laptop stays powered on
and running, but Windows has turned the monitor off (or locked the
screen) after a period of inactivity.

Deliberately Windows-only (ctypes/winsound), matching this app's
Windows-only build target (see BUILD.md); every public function here is
a silent no-op on any other platform.
"""

import ctypes
import os
import sys
import threading
import time

DEFAULT_ALERT_MIN_STEP = "4"
JMA_STEP_ORDER = ["0", "1", "2", "3", "4", "5-", "5+", "6-", "6+", "7"]
STEP_RANK = {step: i for i, step in enumerate(JMA_STEP_ORDER)}

# Alert thresholds a person can choose from in Settings. Below Shindo 3
# alerting is more noise than signal, and above 5- would miss quakes JMA
# itself warns about, so only 3, 4 and 5- are offered. The app still
# displays every level normally.
SELECTABLE_ALERT_STEPS = ["3", "4", "5-"]


def normalize_alert_min_step(step):
    """Map any saved threshold onto a selectable one. Older versions
    allowed 5+ and above; those are clamped to 5-."""
    if step in SELECTABLE_ALERT_STEPS:
        return step
    if step in STEP_RANK and STEP_RANK[step] >= STEP_RANK["5-"]:
        return "5-"
    return DEFAULT_ALERT_MIN_STEP
DEFAULT_ALERT_WAKE_SCREEN = True
DEFAULT_ALERT_VOLUME = "normal"
DEFAULT_QUIET_HOURS = "on"
DEFAULT_ALERT_VOICE = "warning"   # "warning" (strong warnings only), "all", or "off"
VALID_ALERT_VOICE = ("warning", "all", "off")
# Quiet hours: below Shindo 5-, a report plays a soft, fading-in chime with no voice and
# does not wake the screen. Strong warnings are never held back. Fixed times for now.
QUIET_START_HOUR = 22
QUIET_END_HOUR = 7
STRONG_STEP = "5-"
SOFT_SOUND_FILE = "alert_soft.wav"
BETA_SOUND_FILE = "alert_beta.wav"   # wood tick, used by the beta "follow the quake" mode

# Spoken lines. A recorded clip at voices/<lang>/<key>.wav is used when present
# (see voices/SCRIPT.md); otherwise a voice installed in Windows reads the text,
# and if no voice for the language is installed nothing is spoken.
VOICE_LINES = {
    "en": {
        "warning_strong": "Strong shaking expected. Take cover now.",
        "warning_arrived": "Strong shaking arriving. Take cover now.",
        "forecast": "Earthquake early warning. Shaking expected.",
        "felt_1": "Shindo 1 felt.", "felt_2": "Shindo 2 felt.", "felt_3": "Shindo 3 felt.",
        "felt_4": "Shindo 4 felt.", "felt_5m": "Shindo 5 lower felt.", "felt_5p": "Shindo 5 upper felt.",
        "felt_6m": "Shindo 6 lower felt.", "felt_6p": "Shindo 6 upper felt.", "felt_7": "Shindo 7 felt.",
    },
    "ja": {
        "warning_strong": "強い揺れが来ます。身の安全を確保してください。",
        "warning_arrived": "強い揺れが到達しています。身の安全を確保してください。",
        "forecast": "緊急地震速報です。揺れが予想されます。",
        "felt_1": "震度1を観測しました。", "felt_2": "震度2を観測しました。", "felt_3": "震度3を観測しました。",
        "felt_4": "震度4を観測しました。", "felt_5m": "震度5弱を観測しました。", "felt_5p": "震度5強を観測しました。",
        "felt_6m": "震度6弱を観測しました。", "felt_6p": "震度6強を観測しました。", "felt_7": "震度7を観測しました。",
    },
}
STEP_VOICE_KEY = {"1": "felt_1", "2": "felt_2", "3": "felt_3", "4": "felt_4", "5-": "felt_5m",
                  "5+": "felt_5p", "6-": "felt_6m", "6+": "felt_6p", "7": "felt_7"}

# Three pre-rendered volume presets rather than one file scaled at
# runtime: simpler and more robust than reading/rescaling/rewriting PCM
# samples on every trigger (Python 3.13 also dropped the stdlib audioop
# module this could otherwise have leaned on), and three fixed levels
# are already the right amount of control for a wall-display alert.
ALERT_SOUND_FILES = {
    "quiet": "alert_quiet.wav",
    "normal": "alert_normal.wav",
    "loud": "alert_loud.wav",
}

# How long to keep the display forced awake once an alert fires, so it
# doesn't immediately idle back off before anyone has actually seen it.
# This is a temporary hold, not a permanent override of the user's power
# settings; it releases itself automatically.
KEEP_AWAKE_SECONDS = 300

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
SW_RESTORE = 9
MOUSEEVENTF_MOVE = 0x0001
# IsIconic (checked below) reports whether a window is minimized, so
# _bring_to_front can tell "actually needs restoring" apart from
# "already sitting right there on screen".

IS_WINDOWS = sys.platform == "win32"


def meets_alert_threshold(jma_step, min_step=DEFAULT_ALERT_MIN_STEP):
    """True if jma_step is at or above min_step on the real JMA scale.
    Unknown/missing steps never alert, since a step of None means the
    state couldn't be computed, not that it's severe."""
    if min_step not in STEP_RANK:
        min_step = DEFAULT_ALERT_MIN_STEP
    return STEP_RANK.get(jma_step, -1) >= STEP_RANK[min_step]


def should_alert(state, min_step=DEFAULT_ALERT_MIN_STEP):
    """Single entry point Listener.push() calls with the pushed state
    dict. alert_step is always a JMA value (JMA's warning forecast for
    the user's area, or JMA's measured intensity near the user), never
    one the app computed. Listener.push() makes sure each quake chimes
    at most once."""
    return (
        state.get("status") == "active"
        and meets_alert_threshold(state.get("alert_step"), min_step)
    )


def in_quiet_hours(hour):
    """True from QUIET_START_HOUR up to (not including) QUIET_END_HOUR, across midnight."""
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def plan(state, volume=DEFAULT_ALERT_VOLUME, quiet_hours=True, voice="warning", wake_screen=True, hour=12):
    """Decides what one alert does. Pure function (testable anywhere). Returns
    {"tier", "sound", "voice_key", "voice_first", "wake"}.
    tier "warning": JMA's warning flag, or an early-warning forecast of Shindo 5- or more for
      the user's area. Always full level, never held back by quiet hours, spoken by default.
    tier "report": everything else that reached the alert threshold (an early-warning forecast
      below 5-, or a measured report). In quiet hours and below Shindo 5-: soft chime, no voice,
      no screen wake. A measured Shindo 5- or more is never held back."""
    step = state.get("alert_step")
    rank = STEP_RANK.get(step, -1)
    strong_level = rank >= STEP_RANK[STRONG_STEP]
    is_eew = state.get("phase") == "eew"
    af = state.get("area_forecast") or {}
    if is_eew and (state.get("is_warning") or strong_level):
        sound = "normal" if volume == "quiet" else volume   # never the quietest preset for a strong warning
        key = "warning_arrived" if af.get("arrived") else "warning_strong"
        return {"tier": "warning", "sound": sound, "voice_key": key if voice != "off" else None,
                "voice_first": True, "wake": wake_screen}
    quiet_now = quiet_hours and in_quiet_hours(hour) and not (not is_eew and strong_level)
    if quiet_now:
        return {"tier": "report", "sound": "soft", "voice_key": None, "voice_first": False, "wake": False}
    if is_eew:
        key = "forecast" if voice == "all" else None
    else:
        key = STEP_VOICE_KEY.get(step) if (voice == "all" or (voice == "warning" and strong_level)) else None
    return {"tier": "report", "sound": volume, "voice_key": key, "voice_first": strong_level, "wake": wake_screen}


def trigger(window, resource_path_fn, wake_screen=DEFAULT_ALERT_WAKE_SCREEN, volume=DEFAULT_ALERT_VOLUME,
            state=None, quiet_hours=True, voice=DEFAULT_ALERT_VOICE, lang="en"):
    """
    Fire-and-forget: runs the wake/foreground/sound sequence on a
    background thread so it never blocks the WebSocket listener loop
    (winsound.PlaySound would otherwise stall it for the sound's
    duration even with the async flag's brief setup cost).
    """
    if not IS_WINDOWS:
        return
    p = plan(state or {}, volume, quiet_hours, voice, wake_screen, time.localtime().tm_hour)
    threading.Thread(
        target=_run, args=(window, resource_path_fn, p, lang), daemon=True
    ).start()


def trigger_beta(window, resource_path_fn, wake_screen=DEFAULT_ALERT_WAKE_SCREEN):
    """Beta "follow the quake" mode: one wood tick when the screen starts showing a quake for
    the followed location. Fire-and-forget like trigger(). Not held back by quiet hours, because
    the point of the beta is to be heard while testing."""
    if not IS_WINDOWS:
        return
    p = {"tier": "beta", "sound": "beta", "voice_key": None, "voice_first": False, "wake": wake_screen}
    threading.Thread(target=_run, args=(window, resource_path_fn, p, "en"), daemon=True).start()


def _run(window, resource_path_fn, p, lang):
    if p["wake"]:
        _wake_display()
        _bring_to_front(window)
    _play_sound(resource_path_fn, p["sound"])
    if p["voice_key"]:
        # A warning cuts the chime short so the spoken instruction comes within about a
        # second and a half; a report lets the chime finish first.
        time.sleep(1.6 if p["voice_first"] else 3.4)
        _speak(resource_path_fn, p["voice_key"], lang, cut_chime=p["voice_first"])


def preview(resource_path_fn, volume):
    """
    One-shot playback of a chosen volume preset, sound only, no wake or
    foreground behavior. Used by the Settings panel so a person can hear
    how loud Quiet/Normal/Loud actually is before picking one, without
    the rest of a real alert's side effects firing while they're already
    looking right at the app.
    """
    if not IS_WINDOWS:
        return
    threading.Thread(
        target=_play_sound, args=(resource_path_fn, volume), daemon=True
    ).start()


def _wake_display():
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
        # SetThreadExecutionState mainly prevents the display from
        # idling off again soon; it does not reliably reverse one
        # already off. A tiny simulated mouse nudge is the well
        # established trick for actually forcing it back on, the same
        # mechanism as jiggling the mouse yourself.
        user32 = ctypes.windll.user32
        user32.mouse_event(MOUSEEVENTF_MOVE, 1, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(MOUSEEVENTF_MOVE, -1, 0, 0, 0)
        threading.Thread(target=_release_keep_awake, daemon=True).start()
    except Exception:
        pass


def _release_keep_awake():
    time.sleep(KEEP_AWAKE_SECONDS)
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


def _bring_to_front(window):
    """
    Only actually moves the window if it genuinely isn't already the
    thing on screen (minimized, or some other window is in front of
    it) — this is the common "always-on wall display" case this
    module's docstring describes, where the window is already sitting
    there visible every single time an alert fires. Calling
    window.restore() / ShowWindow(SW_RESTORE) / SetForegroundWindow()
    unconditionally, even when none of them change anything, still
    makes WebView2 repaint the window, and on Windows that repaint can
    show a brief blank/white frame before the already-rendered page
    content reappears on top of it — a flash with nothing to do with
    this app's own HTML/CSS, which never sets the screen to white.
    Skipping the no-op case removes that flash without touching the
    genuine "window was minimized or behind something" recovery path.
    """
    try:
        hwnd = window.native.Handle.ToInt32()
    except Exception:
        hwnd = None

    if hwnd is not None:
        try:
            user32 = ctypes.windll.user32
            is_minimized = bool(user32.IsIconic(hwnd))
            is_foreground = user32.GetForegroundWindow() == hwnd
            if not is_minimized and is_foreground:
                return  # already exactly where it needs to be; nothing to do
        except Exception:
            pass  # if we can't tell, fall through and restore as before

    try:
        window.restore()
    except Exception:
        pass
    try:
        if hwnd is None:
            hwnd = window.native.Handle.ToInt32()
        ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass  # best-effort; window.restore() above already covers the common case


def _speak(resource_path_fn, key, lang, cut_chime=False):
    """Recorded clip if one is bundled, else a Windows voice for this language, else nothing."""
    lang = "ja" if lang == "ja" else "en"
    try:
        import winsound
        clip = resource_path_fn(os.path.join("voices", lang, key + ".wav"))
        if os.path.exists(clip):
            winsound.PlaySound(clip, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
    except Exception:
        pass
    text = VOICE_LINES.get(lang, {}).get(key)
    if not text:
        return
    if cut_chime:
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$v = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like ($env:SHINDO_TTS_LANG + '*') } | Select-Object -First 1;"
        "if ($null -eq $v) { exit 3 };"
        "$s.SelectVoice($v.VoiceInfo.Name); $s.Rate = -1; $s.Volume = 100;"
        "$s.Speak($env:SHINDO_TTS_TEXT)"
    )
    try:
        import subprocess
        env = dict(os.environ, SHINDO_TTS_LANG=lang, SHINDO_TTS_TEXT=text)
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                       creationflags=0x08000000, timeout=20)
    except Exception:
        pass


def _play_sound(resource_path_fn, volume):
    try:
        import winsound
        if volume == "beta":
            relative_path = BETA_SOUND_FILE
        elif volume == "soft":
            relative_path = SOFT_SOUND_FILE
        else:
            relative_path = ALERT_SOUND_FILES.get(volume, ALERT_SOUND_FILES[DEFAULT_ALERT_VOLUME])
        path = resource_path_fn(relative_path)
        if os.path.exists(path):
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception:
        pass
