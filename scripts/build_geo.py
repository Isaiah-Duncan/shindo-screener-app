"""Builds municipalities_jp.json (boundaries) and adds the "c" town codes to places_jp.json.

Source boundaries: MLIT National Land Numerical Information, administrative areas
(Kokudo Suchi Joho, gyosei kuiki), simplified to 1% by the SmartNews japan-topography
project (https://github.com/smartnews-smri/japan-topography). Download first:
  https://raw.githubusercontent.com/smartnews-smri/japan-topography/main/data/municipality/geojson/s0010/N03-21_210101.json
Run:  python scripts/build_geo.py N03-21_210101.json
MLIT requires a credit line for this data (shown in Settings > Support).
Names are matched on prefecture + municipality, so keep places_jp.json in the same folder layout.
"""
import json, sys, os

CREDIT = ("Municipality boundaries: MLIT National Land Numerical Information (administrative areas), "
          "simplified by SmartNews japan-topography.")

def norm(s):
    return s.replace("ヶ", "ケ").replace("ｹ", "ケ").replace("檮", "梼")

def rnd(c):
    return [[round(v, 4) for v in p] for p in c]

def geom(g):
    if g["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [rnd(r) for r in g["coordinates"]]}
    return {"type": "MultiPolygon", "coordinates": [[rnd(r) for r in pl] for pl in g["coordinates"]]}

def main(src, root="."):
    feats = json.load(open(src, encoding="utf-8"))["features"]
    prefs, by = {}, {}
    for x in feats:
        p = x["properties"]; c = p.get("N03_007")
        if not c:
            continue
        prefs.setdefault(c[:2], {}).setdefault(c, []).append(geom(x["geometry"]))
        pref, n3, n4 = p["N03_001"], p.get("N03_003") or "", p.get("N03_004") or ""
        keys = set()
        if n4: keys.add(n4)
        if n3 and not n3.endswith("郡"): keys.add(n3)
        if n3 and n4 and not n3.endswith("郡"): keys.add(n3 + n4)
        for k in keys:
            by.setdefault((pref, norm(k)), set()).add(c)
    out = {"_note": CREDIT, "prefs": {}}
    for pc, towns in prefs.items():
        out["prefs"][pc] = []
        for c, parts in sorted(towns.items()):
            polys = []
            for g in parts:
                polys += [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
            out["prefs"][pc].append({"c": c, "g": polys})  # list of polygons, each a list of rings
    json.dump(out, open(os.path.join(root, "municipalities_jp.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    pj = os.path.join(root, "places_jp.json")
    d = json.load(open(pj, encoding="utf-8"))
    miss = []
    for pl in d["places"]:
        cs = by.get((pl["p"], norm(pl["j"])))
        if not cs:
            miss.append(pl["p"] + pl["j"]); pl.pop("c", None); continue
        cs = sorted(c for c in cs if not c.startswith("0169")) or sorted(cs)
        if len(cs) > 1 and pl.get("t") == "m":
            cs = cs[:1]
        pl["c"] = cs
    json.dump(d, open(pj, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("places without a code:", len(miss), miss)

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ".")
