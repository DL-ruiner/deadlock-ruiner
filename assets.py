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


# Counter guide: hero traits come from ability data, game tags and hero stats; item lists are by mechanic
# and every item here was checked against its in-game description.
COUNTER_ITEMS = {
    "gun": ("основной урон — оружие",
            ["Metal Skin", "Plated Armor", "Return Fire", "Weapon Shielding", "Bullet Resilience", "Disarming Hex", "Inhibitor"]),
    "spirit": ("основной урон — способности",
               ["Spirit Shielding", "Spirit Resilience", "Spellbreaker", "Spirit Sap", "Silencer", "Witchmail", "Dispel Magic"]),
    "channel": ("ульта — канал, её сбивает стан или заморозка", ["Knockdown", "Arctic Blast"]),
    "cc": ("много контроля: стан, сон, обездвиживание", ["Unstoppable", "Debuff Reducer", "Reactive Barrier", "Divine Barrier", "Dispel Magic"]),
    "mobile": ("очень подвижный, его надо замедлять и сажать на землю",
               ["Slowing Hex", "Slowing Bullets", "Mystic Slow", "Cold Front", "Phantom Strike"]),
    "fly": ("часто в воздухе: Knockdown станит дольше, Slowing Hex и Phantom Strike тянут вниз",
            ["Knockdown", "Slowing Hex", "Phantom Strike"]),
    "heal": ("сильно лечится, нужен антихил", ["Healbane", "Toxic Bullets", "Inhibitor", "Crippling Headshot"]),
    "tank": ("много здоровья и сопротивлений: урон от процента HP и снижение брони",
             ["Toxic Bullets", "Decay", "Crippling Headshot", "Spirit Shredder", "Mystic Vulnerability", "Weakening Headshot",
              "Escalating Exposure"]),
}
TRAIT_WEIGHT = {"channel": 3, "heal": 3, "gun": 2, "spirit": 2, "cc": 2, "fly": 2, "mobile": 1.5, "tank": 1.5}
TRAIT_TAGS = {
    "heal": {"Lifesteal"}, "tank": {"Tank"}, "fly": {"High-Flying", "High-flying", "Soaring"},
    "mobile": {"Mobility", "Acrobatics", "Nimble", "Slippery", "Evasive", "Traveller"},
    "cc": {"Disruptor", "Disruption", "Disruptive", "Hexing", "Apprehender"},
}
# mechanics the ability data does not expose
TRAIT_FIX = {
    "Haze": {"channel"}, "Lash": {"cc"}, "Dynamo": {"cc"}, "Bebop": {"cc"}, "Mo & Krill": {"cc"},
    "Wraith": {"cc"}, "Paradox": {"cc"}, "Infernus": {"spirit"}, "Kelvin": {"heal"},
}
CC_KEY = re.compile(r"^(Stun|Silence|Sleep|Root|Disarm|Chain|Immobilize|Freeze|Hex)\w*Duration$", re.I)
MOB_KEY = re.compile(r"(Dash|Leap|Fly|Flight|Hover|Teleport|Grapple|JumpVelocity|Glide|Blink)", re.I)
NOT_SELF = re.compile(r"(Reduction|Slow|Enemy|Resist|Penalty|Threshold|Range)", re.I)
HEAL_KEY = re.compile(r"(Heal(?!th)|Lifesteal|HealthRegen|LifeDrain)", re.I)
FLY_KEY = re.compile(r"(Fly|Flight|Hover|Glide)", re.I)


def _nz_keys(x):
    out = set()
    for k, p in (x.get("properties") or {}).items():
        try:
            if float(p.get("value") or 0) != 0:
                out.add(k)
        except (TypeError, ValueError):
            pass
    return out


def hero_traits(hero, abilities, stats, hp_cut, shots_cut):
    keys = [_nz_keys(a) for a in abilities.values()]
    allk = set().union(*keys) if keys else set()
    ult = _nz_keys(abilities["signature4"]) if "signature4" in abilities else set()
    tags = set(hero.get("tags") or [])
    tr = set()
    if "AbilityChannelTime" in ult:
        tr.add("channel")
    if any(CC_KEY.search(k) for k in allk):
        tr.add("cc")
    if hero.get("hero_type") == "assassin" or any(MOB_KEY.search(k) and not NOT_SELF.search(k) for k in allk):
        tr.add("mobile")
    if any(FLY_KEY.search(k) and not NOT_SELF.search(k) for k in allk):
        tr.add("fly")
    if sum(1 for k in allk if HEAL_KEY.search(k) and not NOT_SELF.search(k)) >= 3:
        tr.add("heal")
    if stats and stats["hp"] >= hp_cut:
        tr.add("tank")
    if hero.get("hero_type") == "marksman" or (stats and stats["shots"] >= shots_cut):
        tr.add("gun")
    if hero.get("hero_type") == "mystic":
        tr.add("spirit")
    for t, names in TRAIT_TAGS.items():
        if tags & names:
            tr.add(t)
    tr |= TRAIT_FIX.get(hero["name"], set())
    if not tr & {"gun", "spirit"}:
        tr.add("gun" if hero.get("hero_type") in ("brawler", "assassin") else "spirit")
    return [t for t in TRAIT_WEIGHT if t in tr]


def build_guide(heroes_raw, items_raw, hero_stats, stat_rows):
    by_class = {x["class_name"]: x for x in items_raw}
    by_name = {x["name"]: x["id"] for x in items_raw if x.get("type") == "upgrade" and x.get("shopable")}
    hp = {h: v["total_max_health"] / v["matches"] for h, v in hero_stats.items() if v["matches"]}
    shots = {h: v["total_shots_hit"] / v["matches"] for h, v in hero_stats.items() if v["matches"]}
    hp_cut = sorted(hp.values())[int(len(hp) * 0.75)] if hp else 1e9
    shots_cut = sorted(shots.values())[int(len(shots) * 0.6)] if shots else 1e9
    guide = {}
    for h in heroes_raw:
        if not h.get("player_selectable") or h.get("disabled") or h["id"] not in hero_stats:
            continue
        abilities = {s: by_class[c] for s, c in (h.get("items") or {}).items() if s.startswith("signature") and c in by_class}
        traits = hero_traits(h, abilities, {"hp": hp.get(h["id"], 0), "shots": shots.get(h["id"], 0)}, hp_cut, shots_cut)
        lift = {r[0]: r[2] for r in stat_rows.get(h["id"], [])}
        score, why = {}, {}
        for t in traits:
            reason, names = COUNTER_ITEMS[t]
            for pos, name in enumerate(names):
                iid = by_name.get(name)
                if not iid:
                    continue
                score.setdefault(iid, []).append(TRAIT_WEIGHT[t] + 0.3 * (len(names) - pos) / len(names))
                why.setdefault(iid, []).append(reason)
        # the strongest reason counts fully, extra reasons add a little
        score = {i: max(w) + 0.35 * (sum(w) - max(w)) + max(0, min(3, lift.get(i, 0))) / 3 for i, w in score.items()}
        best = sorted(score, key=lambda i: -score[i])[:10]
        guide[h["id"]] = {"traits": traits,
                          "items": [{"id": i, "why": why[i], "lift": lift.get(i)} for i in best]}
    return guide


def meta(force=False):
    """Hero averages and per-enemy item win rates; kept with the reports so every run can reuse them."""
    META.mkdir(parents=True, exist_ok=True)
    old = META / "counters.json"
    saved = json.loads(old.read_text(encoding="utf-8")) if old.exists() else {}
    if not force and "guide" in saved and time.time() - saved.get("generated", 0) < META_MAX_AGE:
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
    all_rows = {}
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
        all_rows[hid] = rows
        time.sleep(0.5)
    heroes_raw = requests.get(f"{API}/heroes", timeout=60).json()
    items_raw = requests.get(f"{API}/items", timeout=60).json()
    raw_stats = {h["hero_id"]: h for h in requests.get(f"{STATS}/hero-stats", params=since, timeout=120).json()}
    counters["guide"] = build_guide(heroes_raw, items_raw, raw_stats, all_rows)
    (META / "counters.json").write_text(json.dumps(counters, ensure_ascii=False), encoding="utf-8")
    print(f"meta: {len(hero_ids)} heroes, {len(counters['base'])} items")


if __name__ == "__main__":
    if "--meta" in sys.argv:
        meta(force="--force" in sys.argv)
    else:
        main()
