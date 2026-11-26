"""
Posts JMA earthquake reports to the Shindo Screener X account.

Run by .github/workflows/social-post.yml on a timer. No AI and no estimate is involved: every
number comes from JMA's own report, relayed by P2P地震情報 (the same source the app uses).

What it does on each run:
  1. Reads P2P's recent JMA reports (code 551, detailed reports only).
  2. Picks quakes whose maximum Shindo is POST_MIN_SCALE or more and that are not handled yet.
  3. Builds one image (quake facts plus the JMA reading at the big cities nearest the epicenter)
     and two texts (Japanese post, English reply).
  4. Posts them, EXCEPT a quake with maximum Shindo 5- or more, or any tsunami advisory or
     warning: that one is held, and a GitHub issue asks the owner to approve it
     (Actions > Post earthquakes to X > Run workflow > approve = the key, or "all").

Safe to run by hand. Without the four X keys in the environment it only writes the image and
the texts to out/ (dry run) and posts nothing:
    python scripts/social_post.py
"""

import ast
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
import jma_sources  # noqa: E402  (the app's own parser: one definition of "the reading at a place")

HISTORY_URL = "https://api.p2pquake.net/v2/history?codes=551&limit=40"
STATE_PATH = os.path.join(ROOT, "data", "social_state.json")
OUT_DIR = os.path.join(ROOT, "out")
REGION_EN_PATH = os.path.join(ROOT, "docs", "data", "region-names-en.json")

POST_MIN_SCALE = 30        # maximum Shindo 3 or more gets a post
HOLD_MIN_SCALE = 45        # maximum Shindo 5- or more waits for approval
SETTLE_MINUTES = 8         # wait this long after JMA's detailed report, so a quick follow-up report is included
MAX_AGE_MINUTES = 180      # never post about a report older than this
MAX_POSTS_PER_DAY = 12
CITY_RADIUS_KM = 300
CITY_ROWS = 6
JST = timezone(timedelta(hours=9))

SCALE = jma_sources.P2P_SCALE
BANDS = {"1": ("#e8e6dc", "#0e0e0c"), "2": ("#cfe6f2", "#0e0e0c"), "3": ("#3fc1d8", "#0e0e0c"),
         "4": ("#f5c518", "#0e0e0c"), "5-": ("#f08a24", "#0e0e0c"), "5+": ("#e25a1e", "#0e0e0c"),
         "6-": ("#d12f2a", "#f5f5f0"), "6+": ("#a8102a", "#f5f5f0"), "7": ("#7e1a6e", "#f5f5f0")}
JA_STEP = {"5-": "5弱", "5+": "5強", "6-": "6弱", "6+": "6強"}
# JMA's two "no action needed" tsunami answers. Anything else is held for the owner.
TSUNAMI_OK = {"None": ("この地震による津波の心配はありません。", "No tsunami risk (JMA)."),
              "NonEffective": ("若干の海面変動の可能性がありますが、被害の心配はありません。",
                               "Slight sea-level change possible, no damage expected (JMA).")}


def major_cities():
    """The app's own MAJOR_CITIES list, read from app.py without importing it (app.py needs a GUI)."""
    with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "MAJOR_CITIES" for t in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError("MAJOR_CITIES not found in app.py")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def fetch_history():
    req = urllib.request.Request(HISTORY_URL, headers={"User-Agent": "ShindoScreener-social"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def quake_key(msg):
    eq = msg.get("earthquake") or {}
    return f"{eq.get('time')}|{(eq.get('hypocenter') or {}).get('name')}"


def issued_at(msg):
    return jma_sources._parse_jst((msg.get("issue") or {}).get("time"))


# ---------------------------------------------------------------- the draft (all facts for one post)

def build_draft(msg, stations, cities, region_en):
    eq = msg["earthquake"]
    hyp = eq.get("hypocenter") or {}
    origin = jma_sources._parse_jst(eq.get("time"))
    by_name = {}
    for p in msg.get("points") or []:
        step = SCALE.get(p.get("scale"))
        if step and not p.get("isArea"):
            by_name[p["addr"]] = max(by_name.get(p["addr"], step), step, key=jma_sources.rank)
    lat, lon = hyp.get("latitude"), hyp.get("longitude")
    known = isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and lat > -90 and lon > -180
    rows = []
    if known:
        for en, ja, clat, clon in cities:
            dist = jma_sources.haversine_km(lat, lon, clat, clon)
            if dist > CITY_RADIUS_KM:
                continue
            near = jma_sources.locate_home({"lat": clat, "lon": clon}, stations)["stations"]
            step = by_name.get(near[0]["name"]) if near else None
            if step:
                rows.append({"en": en, "ja": ja, "step": step, "km": round(dist)})
        rows.sort(key=lambda r: (-jma_sources.rank(r["step"]), r["km"]))
    max_step = SCALE.get(eq.get("maxScale"))
    strongest = sorted((n for n, s in by_name.items() if s == max_step))[:3]
    mag, depth = hyp.get("magnitude"), hyp.get("depth")
    return {
        "key": quake_key(msg), "origin": origin.isoformat() if origin else None,
        "place_ja": hyp.get("name") or "", "place_en": region_en.get(hyp.get("name") or "", ""),
        "magnitude": mag if isinstance(mag, (int, float)) and mag >= 0 else None,
        "depth_km": depth if isinstance(depth, (int, float)) and depth >= 0 else None,
        "max_step": max_step, "max_scale": eq.get("maxScale"), "tsunami": eq.get("domesticTsunami"),
        "cities": rows[:CITY_ROWS], "strongest_ja": strongest,
    }


def needs_hold(d):
    return (d.get("max_scale") or 0) >= HOLD_MIN_SCALE or d.get("tsunami") not in TSUNAMI_OK


def weighted_len(text):
    """X counts most CJK characters as 2 and the rest as 1 (limit 280)."""
    return sum(2 if ord(ch) > 0x10FF else 1 for ch in text)


def _fit(build, cities):
    rows = list(cities)
    while True:
        text = build(rows)
        if weighted_len(text) <= 280 or not rows:
            return text
        rows = rows[:-1]


def text_ja(d):
    o = datetime.fromisoformat(d["origin"]).astimezone(JST)
    mag = f" M{d['magnitude']:.1f}" if d["magnitude"] is not None else ""
    depth = "ごく浅い" if d["depth_km"] == 0 else (f"深さ約{d['depth_km']:.0f}km" if d["depth_km"] is not None else "")
    tsu = TSUNAMI_OK.get(d["tsunami"], ("", ""))[0]

    def build(rows):
        lines = [f"【地震情報】{o.month}月{o.day}日 {o.hour}時{o.minute:02d}分ごろ",
                 f"震源: {d['place_ja']}{mag} {depth}".rstrip(),
                 f"最大震度{JA_STEP.get(d['max_step'], d['max_step'])}"]
        if d["strongest_ja"]:
            lines.append("最大震度の観測点: " + "、".join(d["strongest_ja"][:2]))
        if rows:
            lines.append("近くの大都市の最寄り観測点の震度: " + " / ".join(f"{r['ja']} {JA_STEP.get(r['step'], r['step'])}" for r in rows))
        if tsu:
            lines.append(tsu)
        lines.append("気象庁発表(P2P地震情報経由) #地震")
        return "\n".join(lines)
    return _fit(build, d["cities"])


def text_en(d):
    o = datetime.fromisoformat(d["origin"]).astimezone(JST)
    hour = o.hour % 12 or 12
    when = f"{o.strftime('%b')} {o.day}, {hour}:{o.minute:02d} {'AM' if o.hour < 12 else 'PM'} JST"
    place = f"{d['place_en']} ({d['place_ja']})" if d["place_en"] else d["place_ja"]
    mag = f", M{d['magnitude']:.1f}" if d["magnitude"] is not None else ""
    depth = ", very shallow" if d["depth_km"] == 0 else (f", depth about {d['depth_km']:.0f} km" if d["depth_km"] is not None else "")
    tsu = TSUNAMI_OK.get(d["tsunami"], ("", ""))[1]

    def build(rows):
        lines = [f"Earthquake in Japan: {when}", f"{place}{mag}{depth}", f"Max Shindo {d['max_step']} (JMA)"]
        if rows:
            lines.append("Big cities, nearest station: " + " / ".join(f"{r['en']} {r['step']}" for r in rows))
        if tsu:
            lines.append(tsu)
        lines.append("Source: JMA via P2P地震情報. Not an official alert. #earthquake")
        return "\n".join(lines)
    return _fit(build, d["cities"])


# ---------------------------------------------------------------- the image

def _font(size, bold=True):
    from PIL import ImageFont
    for path in (f"/usr/share/fonts/opentype/noto/NotoSansCJK-{'Bold' if bold else 'Regular'}.ttc",
                 f"/usr/share/fonts/noto-cjk/NotoSansCJK-{'Bold' if bold else 'Regular'}.ttc",
                 "C:/Windows/Fonts/YuGothB.ttc" if bold else "C:/Windows/Fonts/YuGothM.ttc",
                 "C:/Windows/Fonts/meiryo.ttc"):
        if os.path.exists(path):
            return ImageFont.truetype(path, size, index=0)
    raise RuntimeError("No Japanese font found (install fonts-noto-cjk)")


def render_image(d, path):
    from PIL import Image, ImageDraw
    W, H = 1600, 900
    img = Image.new("RGB", (W, H), "#0b0b0b")
    g = ImageDraw.Draw(img)
    fg, dim = "#f5f5f0", "#9a9a94"
    o = datetime.fromisoformat(d["origin"]).astimezone(JST)
    hour = o.hour % 12 or 12
    g.text((70, 56), "EARTHQUAKE REPORT  地震情報", font=_font(30), fill=dim)
    g.text((W - 70, 56), f"{o.year}/{o.month}/{o.day}  {hour}:{o.minute:02d} {'AM' if o.hour < 12 else 'PM'} JST",
           font=_font(30), fill=dim, anchor="ra")
    # left: the quake
    g.rounded_rectangle((70, 130, 760, 770), radius=28, fill="#151515")
    bg, tfg = BANDS.get(d["max_step"], ("#333333", fg))
    g.text((110, 165), "MAXIMUM SHINDO  最大震度", font=_font(26), fill=dim)
    g.rounded_rectangle((110, 215, 370, 475), radius=36, fill=bg)
    g.text((240, 345), d["max_step"], font=_font(150), fill=tfg, anchor="mm")
    if d["magnitude"] is not None:
        g.text((410, 250), f"M{d['magnitude']:.1f}", font=_font(96), fill=fg)
    if d["depth_km"] is not None:
        g.text((414, 372), "Very shallow" if d["depth_km"] == 0 else f"Depth {d['depth_km']:.0f} km", font=_font(30, False), fill=dim)
        g.text((414, 414), "ごく浅い" if d["depth_km"] == 0 else f"深さ約{d['depth_km']:.0f}km", font=_font(28, False), fill=dim)
    size = 58
    while size > 30 and g.textlength(d["place_ja"], font=_font(size)) > 610:
        size -= 2
    g.text((110, 520), d["place_ja"], font=_font(size), fill=fg)
    if d["place_en"]:
        size = 34
        while size > 18 and g.textlength(d["place_en"], font=_font(size, False)) > 610:
            size -= 1
        g.text((110, 600), d["place_en"], font=_font(size, False), fill=dim)
    tsu = TSUNAMI_OK.get(d["tsunami"])
    if tsu:
        for i, line in enumerate(tsu):
            size = 24
            while size > 14 and g.textlength(line, font=_font(size, False)) > 610:
                size -= 1
            g.text((110, 672 + i * 36), line, font=_font(size, False), fill=dim)
    # right: cities
    g.rounded_rectangle((800, 130, 1530, 770), radius=28, fill="#151515")
    g.text((840, 146), "NEAREST STATION TO NEARBY BIG CITIES", font=_font(26), fill=dim)
    g.text((840, 182), "近くの大都市の最寄り観測点", font=_font(24), fill=dim)
    if d["cities"]:
        y = 225
        for r in d["cities"]:
            cbg, cfg = BANDS.get(r["step"], ("#333333", fg))
            g.rounded_rectangle((840, y, 930, y + 72), radius=16, fill=cbg)
            g.text((885, y + 36), r["step"], font=_font(44), fill=cfg, anchor="mm")
            g.text((960, y + 4), r["en"], font=_font(40), fill=fg)
            g.text((960 + g.textlength(r["en"], font=_font(40)) + 18, y + 14), r["ja"], font=_font(30, False), fill=dim)
            g.text((1490, y + 16), f"{r['km']} km", font=_font(28, False), fill=dim, anchor="ra")
            y += 88
    else:
        g.text((840, 235), "No Shindo 1 or more at the nearest stations", font=_font(32, False), fill=fg)
        g.text((840, 285), "近くの大都市の最寄り観測点で震度1以上の観測なし", font=_font(28, False), fill=dim)
        if d["strongest_ja"]:
            g.text((840, 370), "Strongest reading  最大震度の観測点", font=_font(26), fill=dim)
            for i, n in enumerate(d["strongest_ja"]):
                g.text((840, 420 + i * 56), n, font=_font(38), fill=fg)
    g.text((70, 806), "Source: Japan Meteorological Agency (JMA), relayed by P2P地震情報. Distances are from the epicenter. Not an official alert.",
           font=_font(22, False), fill=dim)
    g.text((W - 70, 838), "shindoscreener.com", font=_font(30), fill=fg, anchor="ra")
    g.text((70, 842), "Shindo Screener", font=_font(26), fill=fg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "PNG")


# ---------------------------------------------------------------- X

def x_session():
    keys = [os.environ.get(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")]
    if not all(keys) or os.environ.get("SOCIAL_DRY_RUN") == "1":
        return None
    from requests_oauthlib import OAuth1Session
    return OAuth1Session(keys[0], keys[1], keys[2], keys[3])


def x_post(session, png_path, ja, en):
    """Japanese post with the image, then the English text as a reply. Returns the first post's id."""
    with open(png_path, "rb") as f:
        r = session.post("https://api.x.com/2/media/upload", files={"media": f}, data={"media_category": "tweet_image"}, timeout=60)
    if r.status_code // 100 == 2:
        body = r.json()
        media_id = str((body.get("data") or {}).get("id") or body.get("media_id_string"))
    else:   # older upload endpoint, kept as a fallback
        with open(png_path, "rb") as f:
            r = session.post("https://upload.twitter.com/1.1/media/upload.json", files={"media": f}, timeout=60)
        r.raise_for_status()
        media_id = r.json()["media_id_string"]
    r = session.post("https://api.x.com/2/tweets", json={"text": ja, "media": {"media_ids": [media_id]}}, timeout=30)
    if r.status_code // 100 != 2:
        raise RuntimeError(f"post failed: {r.status_code} {r.text[:300]}")
    first = r.json()["data"]["id"]
    try:
        session.post("https://api.x.com/2/tweets", json={"text": en, "reply": {"in_reply_to_tweet_id": first}}, timeout=30)
    except Exception as e:   # the main post is out; a failed reply must not cause a second main post
        print("English reply failed:", e)
    return first


def open_issue(d, ja, en):
    """Tells the owner a post is waiting. Needs the workflow's GITHUB_TOKEN; silent when run by hand."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return
    body = (f"A post is held because the maximum Shindo is 5- or more, or JMA issued tsunami information "
            f"(`{d.get('tsunami')}`).\n\nTo post it: Actions > Post earthquakes to X > Run workflow, and put this in "
            f"**approve**:\n\n`{d['key']}`\n\nTo drop it, put the same text in **reject**.\n\n---\n\n{ja}\n\n---\n\n{en}\n")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues", method="POST",
        data=json.dumps({"title": f"Held for review: {d['place_ja']} max Shindo {d['max_step']}", "body": body}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "ShindoScreener-social"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        print("Could not open the review issue:", e)


# ---------------------------------------------------------------- main

def main():
    now = datetime.now(timezone.utc)
    state = load_json(STATE_PATH, {})
    handled, held = state.setdefault("handled", {}), state.setdefault("held", {})
    session = x_session()
    dry = session is None
    stations = jma_sources.load_stations(os.path.join(ROOT, "stations_jp.json"))
    cities = major_cities()
    region_en = load_json(REGION_EN_PATH, {})

    def publish(d):
        ja, en = text_ja(d), text_en(d)
        name = "".join(c if c.isalnum() else "_" for c in d["key"])[:60]
        png = os.path.join(OUT_DIR, name + ".png")
        render_image(d, png)
        with open(os.path.join(OUT_DIR, name + ".txt"), "w", encoding="utf-8") as f:
            f.write(ja + "\n\n-----\n\n" + en + "\n")
        if dry:
            print("DRY RUN, not posted:", d["key"])
            return None
        return x_post(session, png, ja, en)

    # 1. approvals and rejections asked for by hand
    approve, reject = os.environ.get("APPROVE", "").strip(), os.environ.get("REJECT", "").strip()
    for key in list(held):
        if reject and reject in (key, "all"):
            handled[key] = {"status": "rejected", "at": now.isoformat()}
            held.pop(key)
        elif approve and approve in (key, "all"):
            post_id = publish(held[key])
            if not dry:
                handled[key] = {"status": "posted", "at": now.isoformat(), "post_id": post_id}
                held.pop(key)

    # 2. new quakes
    rows = [m for m in fetch_history() if (m.get("issue") or {}).get("type") == "DetailScale"]
    latest = {}
    for m in sorted(rows, key=lambda m: (m.get("issue") or {}).get("time") or ""):
        latest[quake_key(m)] = m
    if not state.get("bootstrapped"):
        # First run ever: everything already published by JMA counts as handled, so the account
        # does not open with a burst of old quakes.
        for key in latest:
            handled.setdefault(key, {"status": "before_start", "at": now.isoformat()})
        state["bootstrapped"] = True
        save_state(state)
        print("First run: marked", len(latest), "existing quakes as handled.")
        return
    posted_today = sum(1 for v in handled.values() if v.get("status") == "posted" and v.get("at", "")[:10] == now.isoformat()[:10])
    for key, m in latest.items():
        if key in handled or key in held:
            continue
        issued = issued_at(m)
        age = (now - issued).total_seconds() / 60 if issued else None
        if age is None or age < SETTLE_MINUTES:
            continue                       # too fresh: next run
        scale = (m.get("earthquake") or {}).get("maxScale") or 0
        if scale < POST_MIN_SCALE or age > MAX_AGE_MINUTES or not jma_sources._parse_jst((m.get("earthquake") or {}).get("time")):
            handled[key] = {"status": "skipped", "at": now.isoformat()}
            continue
        d = build_draft(m, stations, cities, region_en)
        if needs_hold(d):
            held[key] = d
            open_issue(d, text_ja(d), text_en(d))
            print("HELD for review:", key)
            continue
        if posted_today >= MAX_POSTS_PER_DAY:
            handled[key] = {"status": "skipped_daily_cap", "at": now.isoformat()}
            continue
        try:
            post_id = publish(d)
        except Exception as e:
            print("Post failed, will retry next run:", key, e)
            continue
        if not dry:
            handled[key] = {"status": "posted", "at": now.isoformat(), "post_id": post_id}
            posted_today += 1
    # keep the state file small
    cutoff = (now - timedelta(days=14)).isoformat()
    state["handled"] = {k: v for k, v in handled.items() if v.get("at", "") >= cutoff}
    save_state(state)


if __name__ == "__main__":
    main()
