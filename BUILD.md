# Building Shindo Screener as a standalone app

This must be run on Windows (PyInstaller builds for the OS it's run on,
it can't cross-compile a Windows .exe from another OS).

## 1. Install dependencies

```
pip install -r requirements.txt
```

## 2. Test it runs normally first

```
python app.py
```

A window should open. First run, it'll ask for a city name (try
"Sapporo" or your own city). After that it remembers it and goes
straight to the display next time.

If this step doesn't work, fix it before trying to package it:
packaging a broken app just produces a broken .exe, harder to debug.

## 3. Build the standalone executable

```
pyinstaller --onefile --windowed --name "ShindoScreener" --icon "icon.ico" --add-data "screener_app.html;." --add-data "cities_jp.json;." --add-data "icon.ico;." --add-data "alert_quiet.wav;." --add-data "alert_normal.wav;." --add-data "alert_loud.wav;." app.py
```

This produces `dist/ShindoScreener.exe`, a single file. That's the
thing to actually share, everything else in `dist/` and `build/` is
PyInstaller's intermediate output and can be deleted.

## The Shindo alert (screen wake, focus, chime)

When a live (or test) event reaches the chosen threshold, the app wakes
the display if Windows had turned it off, brings the app window to the
front, and plays a short alert chime (a three-note ascending trio,
played three times in a row so it isn't easy to sleep through). This is
`alert.py`, a small Windows-only module using `ctypes` and the stdlib
`winsound` module, no new pip dependency needed.

All three of the alert's dimensions are user-configurable from
Settings → Alerts, saved to `settings.json` like every other setting:
- **Alert me at**: the minimum Shindo step that triggers the alert
  (3, 4, 5-, 5+, 6-, 6+, or 7). Below Shindo 3 isn't offered, since an
  alert at that level is more noise than signal for a wake-the-room
  feature; the display still shows weaker events normally, they just
  don't trigger the wake/chime.
- **Wake screen automatically**: On/Off. When Off, the alert still
  plays its chime at whatever volume is set, but skips forcing the
  display awake and the window to the foreground, for anyone using the
  laptop for other things who doesn't want a quake alert yanking focus.
- **Chime volume**: Quiet / Normal / Loud. These are three separately
  pre-rendered `.wav` files (`alert_quiet.wav`, `alert_normal.wav`,
  `alert_loud.wav`), not one file scaled at runtime; Python 3.13 dropped
  the stdlib `audioop` module that runtime PCM scaling would have leaned
  on, and three fixed presets are already the right amount of control
  for this. All three need to be bundled (see the `pyinstaller` command
  above); if a preset's file is missing at runtime, the alert silently
  does nothing for that trigger rather than crashing.

**What this can and can't do:**
- If the laptop stays powered on with just the display off or the
  screen locked after inactivity: this works. The display turns back
  on, the window comes to the front, and the chime plays.
- If the laptop is actually asleep (Windows fully suspended): this
  can't wake it. Nothing running inside a suspended machine executes to
  detect the live event in the first place, so there is no code path
  running to trigger the alert until something else wakes the machine
  first (e.g. a Wake-on-LAN packet from another always-on device on the
  network, or the lid being opened). If this ever needs to survive true
  sleep, that's a separate, heavier project (external always-on
  hardware sending a wake signal, plus the app set to auto-launch at
  login), not something addressable from inside this app alone.
- The alert only fires once per new event (Wolfx's `Serial` field is
  used to detect a brand-new report vs. a routine
  preliminary → updated → final refresh of a quake already shown), so
  it won't re-fire repeatedly for the same earthquake.

## Known rough edges, honestly, not guaranteed to work first try

- **cities_jp.json lookup fixed for frozen builds (9/2026).** `severity.py`
  used to resolve `cities_jp.json`'s path from its own module `__file__`,
  which is a real path when running via `python app.py` but isn't
  guaranteed to be one once PyInstaller freezes it into a single .exe.
  `app.py` now overrides that path explicitly through the same
  `resource_path()` helper already used for `screener_app.html` and
  `icon.ico`, so this should no longer be a build-time risk. Flagging it
  here since it's exactly the category of bug that works fine as
  `python app.py` and silently breaks only in the packaged .exe.

- **pywebview + PyInstaller sometimes needs an extra nudge.** If the
  built .exe fails to open a window (works fine as `python app.py` but
  not as the .exe), the usual fix is adding
  `--hidden-import webview.platforms.winforms` (or `edgechromium`,
  depending on what pywebview picks on your machine) to the pyinstaller
  command. This is a known category of packaging quirk, not something
  I can test without a Windows machine to actually run PyInstaller on.
- **Windows SmartScreen will likely flag the .exe** the first time
  anyone (including you) runs it, since it's not code-signed. Click
  "More info" → "Run anyway." This is expected, not a sign something's
  wrong with the build.
- **File size will be large** (expect 50-150MB), since it bundles a
  full Python runtime. That's normal for PyInstaller --onefile builds,
  not a mistake.

## After it's built

**About the icon not showing up while testing with `python app.py`:**
that's expected and not a bug, running via `python app.py` shows
Python's own generic icon in the taskbar and File Explorer no matter
what, since Windows is really looking at `python.exe`, not your
script. The `--icon "icon.ico"` flag above is what actually sets the
icon on `ShindoScreener.exe` itself, that's what shows up correctly in
both the taskbar and Explorer, but only once it's the real packaged
executable, not before.

Test the actual .exe (not `python app.py`) on a clean run, delete
`%APPDATA%\ShindoScreener\settings.json` first to confirm the
first-run setup screen still works correctly from a truly fresh state,
that's the experience anyone downloading this for the first time will
actually get.
