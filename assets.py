"""Download map, camp and hero art from deadlock-api into web/assets (run once per patch)."""
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

API = "https://api.deadlock-api.com/v1/assets"
OUT = Path(__file__).parent / "web" / "assets"
KEEP_ENTITIES = ("shops", "soul_urn_spawns", "teleporters", "bridge_buffs", "golden_statues",
                 "healing_snacks", "unstable_rifts", "bells")


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
                           "cost": it.get("cost"), "img": grab(it.get("image_webp") or it.get("image"), "items")}
    (OUT / "items.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    bench()
    print(f"map: {len(camps)} camps, heroes: {len(heroes)}, items: {len(items)}")


def bench():
    """Average player per hero over the last 30 days, for the 'vs average' block."""
    since = {"min_unix_timestamp": int(time.time()) - 30 * 86400}
    g = requests.get(f"{API.replace('/assets', '')}/analytics/game-stats", params=since, timeout=120).json()[0]
    out = {"duration": g["avg_duration_s"], "accuracy": g["avg_accuracy"], "heroes": {}}
    for h in requests.get(f"{API.replace('/assets', '')}/analytics/hero-stats", params=since, timeout=120).json():
        n = h["matches"] or 1
        shots = (h["total_shots_hit"] + h["total_shots_missed"]) or 1
        out["heroes"][h["hero_id"]] = {
            "matches": h["matches"], "winrate": round(h["wins"] / n, 3),
            "k": h["total_kills"] / n, "d": h["total_deaths"] / n, "a": h["total_assists"] / n,
            "nw": h["total_net_worth"] / n, "dmg": h["total_player_damage"] / n, "lh": h["total_last_hits"] / n,
            "acc": h["total_shots_hit"] / shots,
        }
    (OUT / "bench.json").write_text(json.dumps(out), encoding="utf-8")


if __name__ == "__main__":
    import sys
    bench() if "--bench" in sys.argv else main()
