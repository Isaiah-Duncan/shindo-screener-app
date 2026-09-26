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

# Alert thresholds a person can actually choose from in Settings. Below
# Shindo 3, alerting is more noise than signal for a wake-the-room
# feature, so 0/1/2 aren't offered as choices (the app still displays
# them normally, they just never trigger the wake/sound alert).
SELECTABLE_ALERT_STEPS = ["3", "4", "5-", "5+", "6-", "6+", "7"]
DEFAULT_ALERT_WAKE_SCREEN = True
DEFAULT_ALERT_VOLUME = "normal"

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


def trigger(window, resource_path_fn, wake_screen=DEFAULT_ALERT_WAKE_SCREEN, volume=DEFAULT_ALERT_VOLUME):
    """
    Fire-and-forget: runs the wake/foreground/sound sequence on a
    background thread so it never blocks the WebSocket listener loop
    (winsound.PlaySound would otherwise stall it for the sound's
    duration even with the async flag's brief setup cost).
    """
    if not IS_WINDOWS:
        return
    threading.Thread(
        target=_run, args=(window, resource_path_fn, wake_screen, volume), daemon=True
    ).start()


def _run(window, resource_path_fn, wake_screen, volume):
    if wake_screen:
        _wake_display()
        _bring_to_front(window)
    _play_sound(resource_path_fn, volume)


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
    try:
        window.restore()
    except Exception:
        pass
    try:
        hwnd = window.native.Handle.ToInt32()
        ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass  # best-effort; window.restore() above already covers the common case


def _play_sound(resource_path_fn, volume):
    try:
        import winsound
        relative_path = ALERT_SOUND_FILES.get(volume, ALERT_SOUND_FILES[DEFAULT_ALERT_VOLUME])
        path = resource_path_fn(relative_path)
        if os.path.exists(path):
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception:
        pass
