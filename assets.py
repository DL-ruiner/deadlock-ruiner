"""Game art and stats from deadlock-api.

  python assets.py            # map, camps, hero and item art into web/assets (once per patch)
  python assets.py --meta     # hero averages and item counters into web/reports (refreshed every 3 days)
"""
import html
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

API = "https://api.deadlock-api.com/v1/assets"
STATS = "https://api.deadlock-api.com/v1/analytics"
OUT = Path(__file__).parent / "web" / "assets"
META = Path(__file__).parent / "web" / "reports"
KEEP_ENTITIES = ("shops", "soul_urn_spawns", "teleporters", "bridge_buffs", "golden_statues",
                 "healing_snacks", "unstable_rifts", "bells")
META_MAX_AGE = 3 * 86400


def grab(url, sub):
    if not url:
        return None
    dst = OUT / sub / Path(urlparse(url).path).name
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        dst.write_bytes(r.content)
    return f"assets/{sub}/{dst.name}"


def plain_desc(raw):
    """Game tooltip HTML -> text with **highlights** kept as <b>."""
    text = (raw or {}).get("desc") if isinstance(raw, dict) else raw
    if not text:
        return ""
    text = re.sub(r'<span class="highlight">(.*?)</span>', r"[[\1]]", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    return text


def item_props(it):
    out = []
    for key, p in (it.get("properties") or {}).items():
        v = p.get("value")
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        if num == 0 or not p.get("label") or len(out) >= 6:
            continue
        out.append({"label": p["label"], "value": v, "postfix": p.get("postfix") or ""})
    return out


def main():
    m = requests.get(f"{API}/map", timeout=60).json()
    camps = [{"name": c["name"], "kind": c["kind"], "x": c["position"][0], "y": c["position"][1],
              "icon": grab(c.get("icon"), "map")} for c in m["neutral_camps"]]
    ents = {k: [{"x": e["position"][0], "y": e["position"][1], "kind": e.get("kind")} for e in m["entities"].get(k, [])]
            for k in KEEP_ENTITIES}
    zips = []
    for z in m["zipline_paths"]:
        ox, oy = z["origin"][0], z["origin"][1]
        zips.append({"color": z["color"], "points": [[ox + p[0], oy + p[1]] for p in z["P0_points"]]})
    mp = {"radius": m["radius"], "image": grab(m["images"]["mid"], "map"), "frame": grab(m["images"].get("frame"), "map"),
          "camps": camps, "entities": ents, "ziplines": zips, "objectives": m["objective_positions"]}
    (OUT / "map.json").write_text(json.dumps(mp), encoding="utf-8")

    heroes = {}
    for h in requests.get(f"{API}/heroes", timeout=60).json():
        img = h.get("images") or {}
        heroes[h["id"]] = {"name": h["name"], "mm": grab(img.get("minimap_image_webp") or img.get("minimap_image"), "heroes"),
                           "sm": grab(img.get("icon_image_small_webp") or img.get("icon_image_small"), "heroes")}
    (OUT / "heroes.json").write_text(json.dumps(heroes), encoding="utf-8")

    items = {}
    for it in requests.get(f"{API}/items", timeout=60).json():
        if it.get("type") != "upgrade" or not it.get("shopable"):
            continue
        items[it["id"]] = {"name": it["name"], "slot": it.get("item_slot_type"), "tier": it.get("item_tier"),
                           "cost": it.get("cost"), "img": grab(it.get("image_webp") or it.get("image"), "items"),
                           "active": bool(it.get("is_active_item")), "desc": plain_desc(it.get("description")),
                           "props": item_props(it)}
    (OUT / "items.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    print(f"map: {len(camps)} camps, heroes: {len(heroes)}, items: {len(items)}")


def meta(force=False):
    """Hero averages and per-enemy item win rates; kept with the reports so every run can reuse them."""
    META.mkdir(parents=True, exist_ok=True)
    old = META / "counters.json"
    if not force and old.exists() and time.time() - json.loads(old.read_text()).get("generated", 0) < META_MAX_AGE:
        print("meta is fresh")
        return
    since = {"min_unix_timestamp": int(time.time()) - 30 * 86400}
    g = requests.get(f"{STATS}/game-stats", params=since, timeout=120).json()[0]
    bench = {"duration": g["avg_duration_s"], "accuracy": g["avg_accuracy"], "heroes": {}}
    hero_ids = []
    for h in requests.get(f"{STATS}/hero-stats", params=since, timeout=120).json():
        n = h["matches"] or 1
        shots = (h["total_shots_hit"] + h["total_shots_missed"]) or 1
        hero_ids.append(h["hero_id"])
        bench["heroes"][h["hero_id"]] = {
            "matches": h["matches"], "winrate": round(h["wins"] / n, 3),
            "k": h["total_kills"] / n, "d": h["total_deaths"] / n, "a": h["total_assists"] / n,
            "nw": h["total_net_worth"] / n, "dmg": h["total_player_damage"] / n, "lh": h["total_last_hits"] / n,
            "acc": h["total_shots_hit"] / shots,
        }
    (META / "bench.json").write_text(json.dumps(bench), encoding="utf-8")

    def item_stats(**params):
        for attempt in range(3):
            r = requests.get(f"{STATS}/item-stats", params={**since, **params}, timeout=120)
            if r.ok:
                return {x["item_id"]: x for x in r.json()}
            time.sleep(10)
        r.raise_for_status()

    base = item_stats()
    counters = {"generated": int(time.time()),
                "base": {i: {"wr": round(x["wins"] / x["matches"], 4), "m": x["matches"], "buy": round(x["avg_buy_time_s"] or 0)}
                         for i, x in base.items() if x["matches"] >= 2000},
                "vs": {}}
    for hid in hero_ids:
        vs = item_stats(enemy_hero_ids=hid)
        raw = []
        for i, x in vs.items():
            b = counters["base"].get(i)
            if b and x["matches"] >= 1500:
                wr = x["wins"] / x["matches"]
                raw.append((i, wr, wr - b["wr"], x["matches"]))
        # facing a strong hero lowers everyone's win rate; keep only what an item adds on top of that
        shift = sum(d * n for _, _, d, n in raw) / (sum(n for *_, n in raw) or 1)
        rows = [[i, round(wr, 4), round((d - shift) * 100, 2), n] for i, wr, d, n in raw]
        rows.sort(key=lambda r: -r[2])
        counters["vs"][hid] = rows[:10]
        time.sleep(0.5)
    (META / "counters.json").write_text(json.dumps(counters), encoding="utf-8")
    print(f"meta: {len(hero_ids)} heroes, {len(counters['base'])} items")


if __name__ == "__main__":
    if "--meta" in sys.argv:
        meta(force="--force" in sys.argv)
    else:
        main()
