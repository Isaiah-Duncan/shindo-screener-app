# Shindo Screener

An ambient desktop display for real JMA (Japan Meteorological Agency)
earthquake intensity readings. Set your city once, then leave it running
on a wall display or a spare monitor. It shows the real JMA Shindo scale
(0 through 7, including 5-/5+/6-/6+), not a generic magnitude number, so
what you see is what you'd actually feel where you live.

Pulls live data from the [Wolfx](https://wolfx.jp/) JMA Earthquake Early
Warning feed.

## Download

Grab the latest Windows build from the
[Releases page](../../releases/latest).

## Features

- Real-time JMA Shindo readings for your set location, updated as events
  develop (preliminary, updated, final).
- Configurable alert: wakes the screen, brings the app to the front, and
  plays a chime when a live event reaches a Shindo level you choose (see
  Settings -> Alerts). Works when the display has idled off, not from
  true system sleep.
- English and Japanese display.
- Built-in bug reporting from Settings -> Support.

## Building it yourself

See [BUILD.md](BUILD.md) for the full PyInstaller build steps. Windows
only; PyInstaller builds for whatever OS it runs on.

## Status

Beta. If something looks wrong, use the "Report a bug" box in
Settings -> Support, it goes straight to the developer.
