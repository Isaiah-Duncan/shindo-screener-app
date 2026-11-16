"""
Felt-quake archive: every place JMA reported a Shindo reading for, per quake.

Reads JMA's detailed intensity reports (relayed by P2P地震情報, code 551,
type DetailScale) and merges them into monthly files:

    docs/data/felt/YYYY-MM.json   one file per month of quake origin time (JST)
    docs/data/felt/index.json     list of the months and their quake counts

Each quake keeps the same field names P2P uses (code, id, issue, earthquake,
points), cut down to what a reading lookup needs, so the app can feed a
record straight into its normal report parser. Each point is one place that
measured Shindo 1 or more: {"pref", "addr", "scale"} (plus "isArea": true for
an area value). Scale is P2P's code: 10 = 1, 20 = 2, 30 = 3, 40 = 4,
45 = 5-, 50 = 5+, 55 = 6-, 60 = 6+, 70 = 7.

Nothing here is estimated. A place that is absent from a DetailScale report
measured below Shindo 1. Tsunami fields are deliberately not stored.

Run by .github/workflows/update-events.yml on a timer. Standard library only.
Safe to run by hand:  python scripts/update_felt_archive.py
A quake is never deleted once recorded; a later report for the same quake
(a correction) replaces the earlier one.
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

HISTORY_URL = "https://api.p2pquake.net/v2/history?codes=551&limit=100&offset=%d"
MAX_PAGES = 12                    # P2P keeps a few weeks; the loop stops at the first empty page
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "felt")
SOURCE = "JMA detailed intensity reports, relayed by P2P地震情報 (https://www.p2pquake.net/)"
JST = timezone(timedelta(hours=9))


def fetch(offset):
    req = urllib.request.Request(HISTORY_URL % offset, headers={"User-Agent": "ShindoScreener-archive"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def compact(m):
    """A P2P 551 DetailScale report cut down to the fields the lookup needs, or None."""
    iss = m.get("issue") or {}
    eq = m.get("earthquake") or {}
    hyp = eq.get("hypocenter") or {}
    if m.get("code") != 551 or iss.get("type") != "DetailScale" or not eq.get("time"):
        return None
    points = []
    for p in m.get("points") or []:
        if not isinstance(p, dict) or not p.get("addr") or p.get("scale") is None:
            continue
        item = {"pref": p.get("pref") or "", "addr": p["addr"], "scale": p["scale"]}
        if p.get("isArea"):
            item["isArea"] = True
        points.append(item)
    if not points:
        return None
    return {
        "code": 551,
        "id": m.get("id"),
        "issue": {"type": "DetailScale", "time": iss.get("time"), "correct": iss.get("correct")},
        "earthquake": {
            "time": eq["time"],
            "maxScale": eq.get("maxScale"),
            "hypocenter": {k: hyp.get(k) for k in ("name", "latitude", "longitude", "depth", "magnitude")},
        },
        "points": points,
    }


def quake_key(q):
    """One quake = its origin time (to the minute) and its hypocenter name."""
    return (q["earthquake"]["time"], (q["earthquake"]["hypocenter"] or {}).get("name"))


def month_of(q):
    m = re.match(r"(\d{4})/(\d{2})/", q["earthquake"]["time"])
    return f"{m.group(1)}-{m.group(2)}" if m else None


def load_month(month):
    path = os.path.join(OUT_DIR, f"{month}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("quakes", [])
    except (OSError, ValueError):
        return []


def dump_month(month, quakes):
    quakes = sorted(quakes, key=lambda q: q["earthquake"]["time"], reverse=True)
    head = json.dumps({"source": SOURCE, "month": month, "count": len(quakes)}, ensure_ascii=False)
    lines = ",\n".join(json.dumps(q, ensure_ascii=False, separators=(",", ":")) for q in quakes)
    # one quake per line keeps the git diffs readable
    return head[:-1] + ',"quakes":[\n' + lines + "\n]}\n"


def main():
    fresh = {}
    for page in range(MAX_PAGES):
        try:
            rows = fetch(page * 100)
        except Exception as e:
            print("fetch failed:", type(e).__name__, e)
            if not fresh:
                return 1
            break
        if not rows:
            break
        for m in rows:
            q = compact(m) if isinstance(m, dict) else None
            if not q:
                continue
            k = quake_key(q)
            # keep the latest report (a correction) of each quake
            if k not in fresh or (q["issue"]["time"] or "") >= (fresh[k]["issue"]["time"] or ""):
                fresh[k] = q
        if len(rows) < 100:
            break
        time.sleep(1)

    by_month = {}
    for q in fresh.values():
        mo = month_of(q)
        if mo:
            by_month.setdefault(mo, []).append(q)

    os.makedirs(OUT_DIR, exist_ok=True)
    changed = False
    for mo, new in sorted(by_month.items()):
        merged = {quake_key(q): q for q in load_month(mo)}
        for q in new:
            merged[quake_key(q)] = q
        text = dump_month(mo, list(merged.values()))
        path = os.path.join(OUT_DIR, f"{mo}.json")
        try:
            with open(path, encoding="utf-8") as f:
                old = f.read()
        except OSError:
            old = None
        if old != text:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            changed = True
            print("wrote", mo, len(merged), "quakes")

    months = []
    for name in sorted(os.listdir(OUT_DIR)):
        m = re.fullmatch(r"(\d{4}-\d{2})\.json", name)
        if m:
            months.append({"month": m.group(1), "file": name, "count": len(load_month(m.group(1)))})
    index = {"source": SOURCE, "months": months}
    text = json.dumps(index, ensure_ascii=False, indent=1) + "\n"
    ipath = os.path.join(OUT_DIR, "index.json")
    try:
        with open(ipath, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        old = None
    if old != text:
        with open(ipath, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        changed = True
    print("changed" if changed else "no change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
