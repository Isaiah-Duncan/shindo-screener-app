"""
Fetch JMA's latest earthquake list (via Wolfx) and merge it into
docs/data/events.json, the site's permanent event log.

Run by .github/workflows/update-events.yml on a timer. Standard library
only, so the workflow needs no pip install. Safe to run by hand:
    python scripts/update_events.py

Each quake is keyed by JMA's EventID. A later report for the same quake
(for example a full hypocenter report after a quick intensity report)
replaces the earlier fields, so each quake appears once with JMA's
latest values. Quakes are never deleted once recorded, since the file
also serves as a validation dataset for the app's Shindo model.
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

SOURCE_URL = "https://api.wolfx.jp/jma_eqlist.json"
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "events.json")

# JMA writes 5-/5+/6-/6+ as 5弱/5強/6弱/6強 in some feeds; store the
# same short form the app itself uses.
SHINDO_MAP = {"5弱": "5-", "5強": "5+", "6弱": "6-", "6強": "6+"}
VALID_SHINDO = {"1", "2", "3", "4", "5-", "5+", "6-", "6+", "7"}


def to_float(text):
    """'2.5' -> 2.5; anything unparseable ('不明', '') -> None."""
    m = re.search(r"-?\d+(\.\d+)?", str(text or ""))
    return float(m.group()) if m else None


def depth_km(text):
    """'10km' -> 10.0, 'ごく浅い' (very shallow) -> 0.0, unknown -> None."""
    text = str(text or "")
    if "ごく浅い" in text:
        return 0.0
    return to_float(text)


def shindo(text):
    s = SHINDO_MAP.get(str(text or "").strip(), str(text or "").strip())
    return s if s in VALID_SHINDO else None


def iso_jst(text):
    """'2026/09/25 17:04:00' (JST) -> '2026-09-25T17:04:00+09:00'."""
    try:
        return datetime.strptime(text, "%Y/%m/%d %H:%M:%S").strftime("%Y-%m-%dT%H:%M:%S+09:00")
    except (TypeError, ValueError):
        return None


def normalize(raw):
    """Turn one Wolfx entry into this site's event record, or None."""
    event_id = str(raw.get("EventID") or "").strip()
    time = iso_jst(raw.get("time_full"))
    if not event_id or not time:
        return None
    return {
        "id": event_id,
        "time": time,
        "location_ja": (raw.get("location") or "").strip() or None,
        "lat": to_float(raw.get("latitude")),
        "lon": to_float(raw.get("longitude")),
        "depth_km": depth_km(raw.get("depth")),
        "magnitude": to_float(raw.get("magnitude")),
        "max_shindo": shindo(raw.get("shindo")),
        "report_type": (raw.get("Title") or "").strip() or None,
        "info_ja": (raw.get("info") or "").strip() or None,
    }


def merge(existing_events, fetched_events):
    """Merge by id; a newer report's known values replace older ones."""
    by_id = {e["id"]: e for e in existing_events}
    for new in fetched_events:
        old = by_id.get(new["id"], {})
        by_id[new["id"]] = {**old, **{k: v for k, v in new.items() if v is not None}}
    return sorted(by_id.values(), key=lambda e: (e["time"], e["id"]), reverse=True)


def fetch():
    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "shindo-screener-site"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main():
    data = fetch()
    fetched = [normalize(v) for k, v in data.items() if k.startswith("No") and isinstance(v, dict)]
    fetched = [e for e in fetched if e]
    if not fetched:
        # An empty or malformed response must never wipe the log.
        print("No usable entries in the feed; leaving events.json unchanged.")
        return 0

    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            current = json.load(f)
    except FileNotFoundError:
        current = {"events": []}

    merged = merge(current.get("events", []), fetched)
    if merged == current.get("events"):
        print(f"No changes ({len(merged)} events).")
        return 0

    out = {
        "source": "JMA earthquake information, via Wolfx (" + SOURCE_URL + ")",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(merged),
        "events": merged,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"Updated: {len(merged) - len(current.get('events', []))} new, {len(merged)} total.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
