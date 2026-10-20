"""Parse a Deadlock demo into a replay track, key moments and a per-player ruin score."""
import json
import statistics
from pathlib import Path

import boon
import polars as pl

STEAM64 = 76561197960265728
NEAR = 3000  # units, "close enough to help"
FAR = 5000  # units, clearly elsewhere on the map
LANE_CHECK_S = 600
FARM_WINDOW_S = 120
MAP_JSON = Path(__file__).parent / "web" / "assets" / "map.json"
OBJ_NAMES = {
    "walker": "Walker",
    "barracks": "Base Guardian",
    "shrine": "Shrine",
    "patron": "Patron",
    "mid_boss": "Mid-Boss",
}
LANE_NAMES = {1: "левой", 4: "центральной", 6: "правой"}  # by map x; lane colors not verified
SOURCE_NAMES = {
    "k_eLaneCreeps": "Крипы на линии", "k_eNeutrals": "Кемпы", "k_eBosses": "Боссы и объекты",
    "k_eTreasure": "Урна и сокровища", "k_eAssists": "Ассисты", "k_eDenies": "Денаи",
    "k_eTeamBonus": "Командный бонус", "k_eBreakable": "Ящики и статуи", "k_ePlayers": "Убийства героев",
    "k_eAbilityAssassinate": "Добивания", "k_eItemTrophyCollector": "Trophy Collector",
    "k_eItemCultistSacrifice": "Cultist Sacrifice",
}


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _clock(s):
    s = max(0, int(s))
    return f"{s // 60}:{s % 60:02d}"


class Match:
    def __init__(self, path):
        self.d = d = boon.Demo(str(path))
        self.rate = d.tick_rate
        self.heroes = boon.hero_names()
        self.teams = boon.team_names()
        self.players = d.players.to_dicts()
        self.by_hero = {p["hero_id"]: p for p in self.players}
        self.pt = d.player_ticks.select(
            "tick", "hero_id", "x", "y", "is_alive", "health", "max_health", "souls",
            "gold_net_worth", "kills", "deaths", "assists", "hero_damage",
            "objective_damage", "hero_healing", "kill_streak", "last_hits", "level", "death_time",
            "has_ultimate_trained", "ultimate_cooldown_start", "ultimate_cooldown_end",
        )
        self.end_tick = d.game_over_tick or d.total_ticks
        self.end_s = d.tick_to_match_seconds(self.end_tick)
        # one sample per match second for the replay and time lookups
        self.sec_ticks = []
        t, s = 0, 0
        while t <= self.end_tick:
            while t <= self.end_tick and d.tick_to_match_seconds(t) < s:
                t += 1
            self.sec_ticks.append(t)
            s += 1
            t += self.rate - 1
        self._frames = {}
        self.gt_offset = self._game_time_offset()

    def _game_time_offset(self):
        # entity timers (death_time, ult cooldowns) use server time, which runs ahead of tick/rate
        rows = (self.pt.filter(pl.col("death_time") > 0).sort("tick")
                .unique(["hero_id", "death_time"], keep="first"))
        offs = [r["death_time"] - r["tick"] / self.rate for r in rows.to_dicts()]
        return statistics.median(offs) if offs else 0.0

    def gt(self, tick):
        return tick / self.rate + self.gt_offset

    def tick_of_gt(self, gt):
        return int((gt - self.gt_offset) * self.rate)

    def sec(self, tick):
        return self.d.tick_to_match_seconds(tick)

    def tick_at(self, s):
        return self.sec_ticks[max(0, min(int(s), len(self.sec_ticks) - 1))]

    def frame(self, tick):
        """State of every hero at a tick: hero_id -> row dict."""
        # rows are sparse per hero, so take each hero's last row up to this tick
        if tick not in self._frames:
            rows = (self.pt.filter(pl.col("tick").is_between(tick - 30 * self.rate, tick))
                    .sort("tick").group_by("hero_id").last().to_dicts())
            self._frames[tick] = {r["hero_id"]: r for r in rows}
        return self._frames[tick]

    def ult_ready(self, row, tick):
        if not row or not row["has_ultimate_trained"]:
            return False
        ready_at = max(row["ultimate_cooldown_start"], row["ultimate_cooldown_end"])
        return self.gt(tick) >= ready_at - 0.25

    def sampled(self):
        """Every hero's state at each match second (as-of join over sparse rows)."""
        grid = pl.DataFrame({"tick": self.sec_ticks}, schema={"tick": pl.Int32}).with_row_index("i")
        out = []
        for h in self.by_hero:
            rows = self.pt.filter(pl.col("hero_id") == h).sort("tick")
            out.append(grid.join_asof(rows, on="tick", strategy="backward").with_columns(pl.lit(h).alias("hero_id")))
        return pl.concat(out)

    def team(self, hero):
        return self.by_hero[hero]["team_num"]

    def name(self, hero):
        return self.heroes.get(hero, f"hero {hero}")

    def zone(self, hero, pos):
        """Rough area name from the hero's team point of view."""
        y = pos[1] if self.team(hero) == 2 else -pos[1]  # Hidden King base is at negative y
        if y < -6500:
            return "у своей базы"
        if y < -1500:
            return "на своей половине"
        if y <= 1500:
            return "в центре карты"
        if y <= 6500:
            return "на чужой половине"
        return "у вражеской базы"


def ult_casts(m):
    """hero -> list of cast ticks, read from ultimate cooldown changes."""
    out = {}
    rows = (m.pt.filter(pl.col("has_ultimate_trained"))
            .select("tick", "hero_id", "ultimate_cooldown_start", "ultimate_cooldown_end").sort("tick"))
    for h in m.by_hero:
        casts = []
        for r in rows.filter(pl.col("hero_id") == h).iter_rows(named=True):
            cast_gt = min(r["ultimate_cooldown_start"], r["ultimate_cooldown_end"])
            # cooldown items rewrite the timer later; a real cast stamps "now"
            if cast_gt > 0 and abs(m.gt(r["tick"]) - cast_gt) < 2:
                tick = m.tick_of_gt(cast_gt)
                if not casts or tick - casts[-1] > 5 * m.rate:
                    casts.append(tick)
        out[h] = casts
    return out


def camp_states(m):
    """Neutral camps: alive intervals and who cleared them."""
    if not MAP_JSON.exists():
        return [], {}
    camps = json.loads(MAP_JSON.read_text(encoding="utf-8"))["camps"]
    ne = (m.d.neutrals.group_by("entity_id")
          .agg(pl.col("tick").min().alias("a"), pl.col("tick").max().alias("b"),
               pl.col("x").first(), pl.col("y").first()).to_dicts())
    per_camp = {i: [] for i in range(len(camps))}
    for e in ne:
        i = min(range(len(camps)), key=lambda c: _dist((e["x"], e["y"]), (camps[c]["x"], camps[c]["y"])))
        if _dist((e["x"], e["y"]), (camps[i]["x"], camps[i]["y"])) < 1500:
            per_camp[i].append(e)
    out, cleared_by = [], {h: 0 for h in m.by_hero}
    for i, ents in per_camp.items():
        ivs = sorted([e["a"], e["b"]] for e in ents)
        merged = []
        for a, b in ivs:
            if merged and a <= merged[-1][1] + 2 * m.rate:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        clears = []
        for a, b in merged:
            if b >= m.end_tick - m.rate:
                continue
            fr = m.frame(b)
            near = [(h, _dist((r["x"], r["y"]), (camps[i]["x"], camps[i]["y"]))) for h, r in fr.items() if r["is_alive"]]
            near = [x for x in near if x[1] < 2500]
            by = min(near, key=lambda x: x[1])[0] if near else None
            if by:
                cleared_by[by] += 1
            clears.append({"t": round(m.sec(b), 1), "by": by})
        out.append({"i": i, "alive": [[round(m.sec(a)), round(m.sec(b))] for a, b in merged], "clears": clears})
    return out, cleared_by


def analyze(path):
    m = Match(path)
    d = m.d
    heroes = [p["hero_id"] for p in m.players]
    moments = {h: [] for h in heroes}

    def add(hero, t, kind, good, title, text, pos=None, weight=1.0):
        moments[hero].append({
            "t": round(t, 1), "clock": _clock(t), "kind": kind, "good": good,
            "title": title, "text": text, "weight": weight,
            "x": round(pos[0]) if pos else None, "y": round(pos[1]) if pos else None,
        })

    casts = ult_casts(m)
    kills = d.kills.sort("tick").to_dicts()
    samples = m.sampled()

    # ---------- kills / deaths ----------
    kill_times = {h: [] for h in heroes}
    isolated_deaths = {h: 0 for h in heroes}
    deaths_with_ult = {h: 0 for h in heroes}
    for k in kills:
        v, a = k["victim_hero_id"], k["attacker_hero_id"]
        if v not in m.by_hero:
            continue
        t = m.sec(k["tick"])
        before = m.frame(max(0, k["tick"] - 1))
        vrow = before.get(v)
        if not vrow:
            continue
        vpos = (vrow["x"], vrow["y"])
        vteam = m.team(v)
        allies = [h for h, r in before.items() if h != v and m.team(h) == vteam and r["is_alive"]
                  and _dist(vpos, (r["x"], r["y"])) < NEAR]
        enemies = [h for h, r in before.items() if m.team(h) != vteam and r["is_alive"]
                   and _dist(vpos, (r["x"], r["y"])) < NEAR]
        unspent = vrow["souls"]
        nw = vrow["gold_net_worth"] or 1
        streak = vrow["kill_streak"]
        traded = sum(1 for k2 in kills
                     if 0 <= k2["tick"] - k["tick"] <= 8 * m.rate
                     and k2["victim_hero_id"] in m.by_hero and m.team(k2["victim_hero_id"]) != vteam)
        recent_cast = any(0 <= k["tick"] - c <= 15 * m.rate for c in casts[v])

        reasons = []
        bad_weight = 1.0
        if not allies and len(enemies) >= 2:
            reasons.append(f"рядом ни одного союзника, врагов: {len(enemies)}")
            isolated_deaths[v] += 1
            bad_weight += 1.0
        if m.ult_ready(vrow, k["tick"]) and not recent_cast:
            reasons.append("ульта была готова и не прожата")
            deaths_with_ult[v] += 1
            bad_weight += 0.7
        if unspent >= max(1500, 0.15 * nw):
            reasons.append(f"{unspent} непотраченных душ")
            bad_weight += 0.5
        if streak >= 3:
            reasons.append(f"отдал серию из {streak} убийств")
            bad_weight += 0.5
        if a == 0:
            reasons.append("убили крипы, башня или нейтралы")
        if traded:
            reasons.append(f"команда разменялась: {traded}")
            bad_weight -= 0.7
        killer = m.name(a) if a in m.by_hero else "карта"
        title = "Смерть в одиночку" if (not allies and len(enemies) >= 2) else "Смерть"
        add(v, t, "death", False, title,
            f"Убийца: {killer}" + (": " + "; ".join(reasons) if reasons else ""),
            vpos, max(0.3, bad_weight))

        if a in m.by_hero and m.team(a) != vteam:
            kill_times[a].append(t)
            if streak >= 3:
                add(a, t, "shutdown", True, "Прервал серию",
                    f"Остановил серию из {streak} убийств у {m.name(v)}", vpos, 1.5)
            elif not k["assister_hero_ids"] and len(allies) >= 1:
                add(a, t, "solo", True, "Переиграл",
                    f"Соло-килл на {m.name(v)}, хотя рядом был союзник врага", vpos, 1.2)

    for h, ts in kill_times.items():
        i = 0
        while i < len(ts):
            j = i
            while j + 1 < len(ts) and ts[j + 1] - ts[i] <= 12:
                j += 1
            n = j - i + 1
            if n >= 2:
                label = {2: "Double kill", 3: "Triple kill"}.get(n, f"{n}-kill rampage")
                add(h, ts[i], "multikill", True, label, f"{n} убийства за {ts[j] - ts[i]:.0f} с",
                    None, 1.0 + 0.5 * n)
            i = j + 1

    # ---------- lane phase ----------
    lane_diff = {h: 0 for h in heroes}
    lane_tick = m.tick_at(min(LANE_CHECK_S, m.end_s))
    f10 = m.frame(lane_tick)
    for h in heroes:
        p = m.by_hero[h]
        foes = [o for o in heroes if m.by_hero[o]["start_lane"] == p["start_lane"] and m.team(o) != p["team_num"]]
        if not foes or h not in f10:
            continue
        foe_nw = statistics.mean(f10[o]["gold_net_worth"] for o in foes if o in f10)
        diff = f10[h]["gold_net_worth"] - foe_nw
        lane_diff[h] = round(diff)
        vs = ", ".join(m.name(o) for o in foes)
        if diff >= 1500:
            add(h, LANE_CHECK_S, "lane", True, "Выиграл линию",
                f"+{diff:.0f} душ против {vs} к 10:00", None, 1.0 + diff / 3000)
        elif diff <= -1500:
            add(h, LANE_CHECK_S, "lane", False, "Проиграл линию",
                f"{diff:.0f} душ против {vs} к 10:00", None, 1.0 + -diff / 3000)

    # ---------- teamfights ----------
    dmg = (d.damage.filter(pl.col("attacker_hero_id").is_in(heroes) & pl.col("victim_hero_id").is_in(heroes)
                           & (pl.col("attacker_hero_id") != pl.col("victim_hero_id")))
           .select("tick", "attacker_hero_id", "victim_hero_id", "damage"))
    missed = {h: 0 for h in heroes}
    fight_rows = {h: [] for h in heroes}
    ult_fight = {h: {"used": 0, "unused": 0} for h in heroes}
    fights_out = []
    for f in d.teamfights(min_players=4).to_dicts():
        s_tick, e_tick = f["start_tick"], f["end_tick"]
        fk = [k for k in kills if s_tick <= k["tick"] <= e_tick + 3 * m.rate and k["victim_hero_id"] in m.by_hero]
        if len(fk) < 2:
            continue
        lost = {2: 0, 3: 0}
        for k in fk:
            lost[m.team(k["victim_hero_id"])] += 1
        winner = 2 if lost[2] < lost[3] else 3 if lost[3] < lost[2] else 0
        t0 = m.sec(s_tick)
        center = (f["center_x"], f["center_y"])
        fights_out.append({"t": round(t0, 1), "clock": _clock(t0), "end": round(m.sec(e_tick), 1),
                           "x": round(center[0]), "y": round(center[1]), "winner": winner,
                           "deaths": lost, "participants": f["participants"]})
        fd = dmg.filter(pl.col("tick").is_between(s_tick - 2 * m.rate, e_tick + 2 * m.rate))
        dealt = dict(fd.group_by("attacker_hero_id").agg(pl.col("damage").sum()).iter_rows())
        taken = dict(fd.group_by("victim_hero_id").agg(pl.col("damage").sum()).iter_rows())
        team_dealt = {tm: sum(dealt.get(h, 0) for h in heroes if m.team(h) == tm) or 1 for tm in (2, 3)}
        start, endf = m.frame(s_tick), m.frame(e_tick)
        first_death = {tm: next((k for k in fk if m.team(k["victim_hero_id"]) == tm), None) for tm in (2, 3)}
        for h in heroes:
            team = m.team(h)
            mates_in = [p for p in f["participants"] if p != h and m.team(p) == team]
            took_part = h in f["participants"]
            if took_part:
                score = sum(1 for k in fk if k["attacker_hero_id"] == h or h in (k["assister_hero_ids"] or []))
                my_dmg = dealt.get(h, 0)
                share = my_dmg / team_dealt[team]
                died = next((k for k in fk if k["victim_hero_id"] == h), None)
                used = any(s_tick - 3 * m.rate <= c <= e_tick + m.rate for c in casts[h])
                ready = m.ult_ready(start.get(h), s_tick)
                ult = "used" if used else "ready" if ready else ("cd" if start.get(h, {}).get("has_ultimate_trained") else "none")
                res = "W" if winner == team else "L" if winner else "="
                fight_rows[h].append({"t": round(t0, 1), "clock": _clock(t0), "res": res,
                                      "score": f"{lost[5 - team]}:{lost[team]}", "ka": score, "dmg": my_dmg,
                                      "taken": taken.get(h, 0), "share": round(share * 100), "died": bool(died),
                                      "ult": ult})
                bits = [f"урон {my_dmg} ({share * 100:.0f}% команды)", f"убийств и ассистов: {score}"]
                if used:
                    ult_fight[h]["used"] += 1
                    after = [k for k in fk if 0 <= k["tick"] - next(c for c in casts[h] if s_tick - 3 * m.rate <= c <= e_tick + m.rate) <= 8 * m.rate
                             and m.team(k["victim_hero_id"]) != team]
                    if res == "W" and len(after) >= 2:
                        add(h, t0, "ult", True, "Ульта решила драку",
                            f"После ульты команда забрала {len(after)} убийства", center, 1.6)
                elif ready:
                    ult_fight[h]["unused"] += 1
                    if res != "W" or died:
                        add(h, t0, "ult", False, "Не прожал ульту",
                            f"Ульта была готова всю драку ({'проиграли' if res == 'L' else 'разменялись' if res == '=' else 'выиграли, но ты умер'})",
                            center, 1.3)
                if died and first_death[team] is died and died["tick"] - s_tick <= 5 * m.rate and share < 0.12:
                    add(h, m.sec(died["tick"]), "fight", False, "Слил вход в драку",
                        f"Умер первым через {(died['tick'] - s_tick) / m.rate:.0f} с, урон всего {my_dmg}", center, 1.4)
                elif res == "W" and len(fk) >= 3 and share >= 0.3 and my_dmg == max(dealt.get(x, 0) for x in heroes if m.team(x) == team):
                    add(h, t0, "fight", True, "Лучший урон в драке",
                        f"Драка {lost[5 - team]} за {lost[team]}: " + ", ".join(bits), center, 1.4)
                elif res == "W" and score >= 1 and len(fk) >= 3:
                    add(h, t0, "fight", True, "Выиграли тимфайт",
                        f"Драка {lost[5 - team]} за {lost[team]}: " + ", ".join(bits), center, 1.0 + 0.3 * score)
                elif res == "L" and lost[team] >= 3 and score == 0:
                    add(h, t0, "fight", False, "Проиграли тимфайт",
                        f"Отдали {lost[team]} за {lost[5 - team]}: " + ", ".join(bits), center, 1.0)
            elif (len(mates_in) >= 3 and t0 >= LANE_CHECK_S and h in start and start[h]["is_alive"]):
                here = (start[h]["x"], start[h]["y"])
                if _dist(here, center) < FAR:
                    continue
                pushed = endf.get(h, start[h])["objective_damage"] - start[h]["objective_damage"]
                if pushed >= 400:
                    add(h, t0, "split", True, "Сплит-пуш",
                        f"Нанёс {pushed} урона по объектам, пока команда дралась", here, 0.8)
                else:
                    missed[h] += 1
                    res = "проиграли" if winner and winner != team else "выиграли" if winner == team else "разменялись"
                    add(h, t0, "missed_fight", False, "Команда дралась без тебя",
                        f"Союзников в драке: {len(mates_in)} ({res}), ты был {m.zone(h, here)}, в {_dist(here, center):.0f} юнитах",
                        here, 1.5 if res == "проиграли" else 0.8)

    # ---------- objectives, urn, mid-boss ----------
    obj = d.objectives
    destroyed = (obj.filter(pl.col("health") <= 0).sort("tick")
                 .unique(["objective_type", "team_num", "lane", "entity_id"], keep="first").sort("tick"))
    objectives_out = []
    for o in destroyed.to_dicts():
        if o["objective_type"] == "mid_boss":
            continue
        t = m.sec(o["tick"])
        name = OBJ_NAMES.get(o["objective_type"], o["objective_type"])
        lane = LANE_NAMES.get(o["lane"], "")
        objectives_out.append({"t": round(t, 1), "clock": _clock(t), "type": o["objective_type"], "name": name,
                               "lane": o["lane"], "team": o["team_num"], "x": round(o["x"]), "y": round(o["y"])})
        fr = m.frame(o["tick"])
        for h in heroes:
            r = fr.get(h)
            if r and r["is_alive"] and m.team(h) != o["team_num"] and _dist((r["x"], r["y"]), (o["x"], o["y"])) < NEAR:
                add(h, t, "objective", True, f"Снёс {name}", f"Вражеский {name} на {lane} линии" if lane else f"Вражеский {name}",
                    (o["x"], o["y"]), 0.8)
    all_objectives = (obj.filter(pl.col("objective_type") != "mid_boss").sort("tick")
                      .unique(["objective_type", "team_num", "lane", "entity_id"], keep="first"))
    objective_spots = [{"type": o["objective_type"], "team": o["team_num"], "x": round(o["x"]), "y": round(o["y"])}
                       for o in all_objectives.to_dicts()]

    events = []
    carrier = None
    for u in d.urn.sort("tick").to_dicts():
        t = m.sec(u["tick"])
        if u["event"] == "picked_up" and u["hero_id"] in m.by_hero:
            carrier = u["hero_id"]
            events.append({"t": round(t, 1), "kind": "urn_pick", "hero": carrier, "x": round(u["x"]), "y": round(u["y"])})
        elif u["event"] == "returned" and u["hero_id"] in m.by_hero:
            events.append({"t": round(t, 1), "kind": "urn_done", "hero": u["hero_id"], "x": round(u["x"]), "y": round(u["y"])})
            add(u["hero_id"], t, "urn", True, "Донёс урну", "Урна с душами доставлена", (u["x"], u["y"]), 1.2)
        elif u["event"] == "dropped" and carrier:
            events.append({"t": round(t, 1), "kind": "urn_drop", "hero": carrier, "x": round(u["x"]), "y": round(u["y"])})
            add(carrier, t, "urn", False, "Потерял урну", "Урну выбили по дороге", (u["x"], u["y"]), 1.0)
    for b in d.mid_boss.filter(pl.col("event") == "killed").to_dicts():
        events.append({"t": round(m.sec(b["tick"]), 1), "kind": "midboss", "team": b["team_num"]})
    for r in d.rift.to_dicts():
        if r.get("capture_tick"):
            events.append({"t": round(m.sec(r["capture_tick"]), 1), "kind": "rift", "team": r["winning_team"],
                           "x": round(r["x"]), "y": round(r["y"])})

    camps, camps_by = camp_states(m)

    # ---------- farm ----------
    nw = {h: samples.filter(pl.col("hero_id") == h).sort("i")["gold_net_worth"].fill_null(0).to_list() for h in heroes}
    alive = {h: samples.filter(pl.col("hero_id") == h).sort("i")["is_alive"].fill_null(False).to_list() for h in heroes}
    pos = {h: samples.filter(pl.col("hero_id") == h).sort("i").select("x", "y").fill_null(0).rows() for h in heroes}
    n = len(m.sec_ticks)
    fight_spans = {h: [(r["t"] - 5, r["t"] + 30) for r in fight_rows[h]] for h in heroes}
    windows = {h: [] for h in heroes}
    for s0 in range(LANE_CHECK_S - 120, n - FARM_WINDOW_S, 30):
        s1 = s0 + FARM_WINDOW_S
        gains = {h: nw[h][s1] - nw[h][s0] for h in heroes if all(alive[h][s0:s1:5])}
        if len(gains) < 6:
            continue
        med = statistics.median(gains.values()) or 1
        for h, g in gains.items():
            busy = any(a < s1 and b > s0 for a, b in fight_spans[h])
            windows[h].append((s0, s1, g, med, busy))
    farm_good, farm_bad = {h: 0 for h in heroes}, {h: 0 for h in heroes}
    for h in heroes:
        bad = sorted((w for w in windows[h] if not w[4] and w[2] < 0.45 * w[3]), key=lambda w: w[2] / w[3])
        good = sorted((w for w in windows[h] if w[2] > 1.7 * w[3] and w[2] > 1500), key=lambda w: -w[2] / w[3])
        for picked, is_good in ((bad, False), (good, True)):
            used = []
            for s0, s1, g, med, _ in picked:
                if any(abs(s0 - u) < FARM_WINDOW_S for u in used) or len(used) >= 3:
                    continue
                used.append(s0)
                where = m.zone(h, pos[h][(s0 + s1) // 2])
                text = f"{_clock(s0)}–{_clock(s1)}: +{g} душ, в среднем по лобби +{med:.0f}; был {where}"
                if is_good:
                    farm_good[h] += 1
                    add(h, s0, "farm", True, "Отличный фарм", text, pos[h][(s0 + s1) // 2], 1.0)
                else:
                    farm_bad[h] += 1
                    add(h, s0, "farm", False, "Слабый фарм", text + ", без драк", pos[h][(s0 + s1) // 2], 1.0)

    summ = d.summary()
    gs = summ["gold_sources"]
    last_snap = gs["snapshot_time_s"].max()
    sources = {h: [] for h in heroes}
    for r in gs.filter(pl.col("snapshot_time_s") == last_snap).to_dicts():
        if r["hero_id"] in sources and r["gold"]:
            sources[r["hero_id"]].append({"name": SOURCE_NAMES.get(r["source_name"], r["source_name"]), "gold": r["gold"]})
    for h in heroes:
        sources[h].sort(key=lambda x: -x["gold"])
    snaps = summ["snapshots"].sort("snapshot_time_s")
    snap_cols = {"lane": "lane_creep_gold", "camps": "neutral_creep", "heroes": "player_gold", "bosses": "boss_gold",
                 "other": None}
    farm_timeline = {h: [] for h in heroes}
    for r in snaps.to_dicts():
        h = r["hero_id"]
        if h not in farm_timeline:
            continue
        row = {"t": r["snapshot_time_s"], "nw": r["net_worth"], "lh": r["creep_kills"], "nk": r["neutral_kills"]}
        known = 0
        for key, col in snap_cols.items():
            if col:
                row[key] = r.get(col) or 0
                known += row[key]
        row["other"] = max(0, (r["net_worth"] or 0) - known)
        farm_timeline[h].append(row)
    camp_share = {h: (sum(s["gold"] for s in sources[h] if s["name"] == "Кемпы") /
                      max(1, sum(s["gold"] for s in sources[h]))) for h in heroes}
    lobby_camp = statistics.median(camp_share.values())

    # ---------- combat breakdown ----------
    hero_dmg = d.damage.filter(pl.col("attacker_hero_id").is_in(heroes) & pl.col("victim_hero_id").is_in(heroes)
                               & (pl.col("attacker_hero_id") != pl.col("victim_hero_id")))
    combat = {h: {"by_target": {}, "by_source": {}, "split": {"bullet": 0, "spirit": 0, "melee": 0}, "hs": 0, "hits": 0}
              for h in heroes}
    for r in hero_dmg.group_by("attacker_hero_id", "victim_hero_id").agg(pl.col("damage").sum()).to_dicts():
        a_, v_ = r["attacker_hero_id"], r["victim_hero_id"]
        if m.team(a_) != m.team(v_):
            combat[a_]["by_target"][v_] = r["damage"]
            combat[v_]["by_source"][a_] = r["damage"]
    kinds = {1: "bullet", 2: "spirit", 3: "melee"}
    for r in hero_dmg.group_by("attacker_hero_id", "citadel_type").agg(pl.col("damage").sum()).to_dicts():
        k_ = kinds.get(r["citadel_type"])
        if k_:
            combat[r["attacker_hero_id"]]["split"][k_] += r["damage"]
    shots = hero_dmg.filter((pl.col("citadel_type") == 1) & (pl.col("hitgroup_id") >= 0))
    for r in shots.group_by("attacker_hero_id").agg(pl.len().alias("n"),
                                                     pl.col("hitgroup_id").is_in([1, 19]).sum().alias("hs")).to_dicts():
        combat[r["attacker_hero_id"]]["hits"] = r["n"]
        combat[r["attacker_hero_id"]]["hs"] = r["hs"]
    kill_matrix = {h: {"killed": {}, "killed_by": {}} for h in heroes}
    for k in kills:
        a_, v_ = k["attacker_hero_id"], k["victim_hero_id"]
        if a_ in m.by_hero and v_ in m.by_hero and m.team(a_) != m.team(v_):
            kill_matrix[a_]["killed"][v_] = kill_matrix[a_]["killed"].get(v_, 0) + 1
            kill_matrix[v_]["killed_by"][a_] = kill_matrix[v_]["killed_by"].get(a_, 0) + 1

    items_cat = {}
    items_json = MAP_JSON.parent / "items.json"
    if items_json.exists():
        items_cat = {int(k_): v for k_, v in json.loads(items_json.read_text(encoding="utf-8")).items()}
    purchases = {h: [] for h in heroes}
    owned = {h: [] for h in heroes}
    for r in d.item_purchases.sort("tick").to_dicts():
        h, iid = r["hero_id"], r["ability_id"]
        if h not in purchases or iid not in items_cat or r["change"] not in ("purchased", "sold"):
            continue
        sold = r["change"] == "sold"
        purchases[h].append({"t": round(m.sec(r["tick"]), 1), "id": iid, "sold": sold})
        if sold:
            if iid in owned[h]:
                owned[h].remove(iid)
        else:
            owned[h].append(iid)
    first_t3 = {}
    for h in heroes:
        t3 = [x["t"] for x in purchases[h] if not x["sold"] and (items_cat[x["id"]].get("tier") or 0) >= 3]
        first_t3[h] = round(t3[0]) if t3 else None

    zones = {h: {} for h in heroes}
    for h in heroes:
        cnt = {}
        for i, (x, y) in enumerate(pos[h]):
            if alive[h][i] and (x or y):
                z_ = m.zone(h, (x, y))
                cnt[z_] = cnt.get(z_, 0) + 1
        tot = sum(cnt.values()) or 1
        zones[h] = {z_: round(100 * c / tot) for z_, c in cnt.items()}

    # ---------- per-player stats ----------
    final = {r["hero_id"]: r for r in m.pt.filter(pl.col("tick") <= m.end_tick).sort("tick")
             .group_by("hero_id").last().to_dicts()}
    team_kills = {2: 0, 3: 0}
    for h in heroes:
        team_kills[m.team(h)] += final[h]["kills"] if h in final else 0
    alive_frames = (samples.filter(pl.col("x").is_not_null())
                    .group_by("hero_id").agg((~pl.col("is_alive")).sum().alias("dead_s"),
                                             ((pl.col("souls") >= 3000) & pl.col("is_alive")).sum().alias("hoard_s")))
    dead_s = {r["hero_id"]: r["dead_s"] for r in alive_frames.to_dicts()}
    hoard_s = {r["hero_id"]: r["hoard_s"] for r in alive_frames.to_dicts()}
    minutes = max(1.0, m.end_s / 60)

    # ult readiness per second (also drawn on the map)
    ult_track, ult_stats = {}, {}
    for h in heroes:
        r = samples.filter(pl.col("hero_id") == h).sort("i")
        ready = []
        for tick, tr, a, b, al in zip(r["tick"], r["has_ultimate_trained"], r["ultimate_cooldown_start"],
                                      r["ultimate_cooldown_end"], r["is_alive"]):
            ready.append(1 if tr and al and m.gt(tick) >= max(a or 0, b or 0) - 0.25 else 0)
        ult_track[h] = ready
        trained = [i for i, x in enumerate(r["has_ultimate_trained"].fill_null(False)) if x]
        since = trained[0] if trained else n
        idle = sum(ready[since:])
        ult_stats[h] = {
            "unlocked": _clock(since) if trained else None, "casts": len(casts[h]),
            "cast_times": [_clock(m.sec(c)) for c in casts[h]],
            "idle_pct": round(100 * idle / max(1, n - since)), "deaths_ready": deaths_with_ult[h],
            "fights_used": ult_fight[h]["used"], "fights_unused": ult_fight[h]["unused"],
        }

    stats = {}
    for h in heroes:
        r = final.get(h, {})
        team = m.team(h)
        stats[h] = {
            "kills": r.get("kills", 0), "deaths": r.get("deaths", 0), "assists": r.get("assists", 0),
            "net_worth": r.get("gold_net_worth", 0), "hero_damage": r.get("hero_damage", 0),
            "objective_damage": r.get("objective_damage", 0), "healing": r.get("hero_healing", 0),
            "last_hits": r.get("last_hits", 0), "level": r.get("level", 0),
            "dpm": round(r.get("hero_damage", 0) / minutes), "spm": round(r.get("gold_net_worth", 0) / minutes),
            "kp": round((r.get("kills", 0) + r.get("assists", 0)) / max(1, team_kills[team]), 2),
            "dead_pct": round(100 * dead_s.get(h, 0) / max(1, m.end_s), 1),
            "hoard_pct": round(100 * hoard_s.get(h, 0) / max(1, m.end_s), 1),
            "lane_diff": lane_diff[h], "isolated_deaths": isolated_deaths[h], "missed_fights": missed[h],
            "camps": (farm_timeline[h][-1]["nk"] if farm_timeline[h] else 0) or 0, "camp_share": round(100 * camp_share[h]),
            "farm_bad": farm_bad[h], "ult_idle": ult_stats[h]["idle_pct"], "ult_deaths": deaths_with_ult[h],
            "ult_unused": ult_fight[h]["unused"],
            "hs_pct": round(100 * combat[h]["hs"] / combat[h]["hits"]) if combat[h]["hits"] >= 20 else None,
            "first_t3": first_t3[h],
        }

    # ---------- ruinometer ----------
    metrics = {  # key: (weight, bad label, good label)
        "net_worth": (1, "мало душ", "много душ"),
        "dpm": (1, "мало урона по героям", "много урона по героям"),
        "kp": (1, "мало участия в убийствах", "участвовал почти во всех убийствах"),
        "objective_damage": (1, "почти не бил объекты", "много урона по объектам"),
        "lane_diff": (1, "проиграл линию", "выиграл линию"),
        "deaths": (-1, "много смертей", "мало смертей"),
        "isolated_deaths": (-1.5, "часто умирал в одиночку", "не попадался в одиночку"),
        "missed_fights": (-1, "пропускал тимфайты", "был во всех драках"),
        "dead_pct": (-0.5, "много времени мёртвым", "мало времени мёртвым"),
        "hoard_pct": (-0.5, "копил души вместо покупок", "вовремя тратил души"),
        "farm_bad": (-0.7, "провисал по фарму", "фармил без провисаний"),
        "ult_deaths": (-0.7, "умирал с готовой ультой", "не умирал с ультой в кармане"),
        "ult_unused": (-0.7, "не прожимал ульту в драках", "прожимал ульту в драках"),
    }
    # z-scores inside each team: who pulled their own team down
    z = {h: {} for h in heroes}
    for team in (2, 3):
        mates = [h for h in heroes if m.team(h) == team]
        for key, (w, _, _) in metrics.items():
            vals = [stats[h][key] for h in mates]
            mu, sd = statistics.mean(vals), statistics.pstdev(vals) or 1
            for h in mates:
                z[h][key] = w * (stats[h][key] - mu) / sd
    wsum = sum(abs(w) for w, _, _ in metrics.values())
    winner = d.winning_team_num
    out_players = []
    for h in heroes:
        p = m.by_hero[h]
        s = stats[h]
        impact = sum(z[h].values()) / wsum * 3
        ruin = max(0, min(100, round(50 - impact * 18)))
        verdict = "Тащил" if ruin <= 35 else "Норм" if ruin <= 62 else "Под вопросом"
        worst = sorted((v, metrics[k][1]) for k, v in z[h].items() if v < -0.7)[:3]
        best = sorted(((v, metrics[k][2]) for k, v in z[h].items() if v > 0.7), reverse=True)[:3]
        tips = []
        if s["camp_share"] < 50 * lobby_camp and m.end_s > 1200:
            tips.append(f"Почти не фармил кемпы: {s['camp_share']}% душ с них, в среднем по лобби {100 * lobby_camp:.0f}%. "
                        "Между волнами забегай в ближайший кемп.")
        if s["hoard_pct"] >= 25:
            tips.append(f"{s['hoard_pct']:.0f}% времени ходил с 3000+ непотраченных душ. Чаще заходи в шоп.")
        if s["ult_deaths"] >= 2:
            tips.append(f"Умер с готовой ультой {s['ult_deaths']} раз. Если драка проиграна, прожимай перед смертью.")
        if ult_stats[h]["idle_pct"] >= 50 and ult_stats[h]["unlocked"]:
            tips.append(f"Ульта простаивала {ult_stats[h]['idle_pct']}% времени после открытия, "
                        f"прожато {ult_stats[h]['casts']} раз за матч.")
        if s["lane_diff"] <= -1500:
            tips.append(f"Линию проиграл на {-s['lane_diff']} душ к 10:00. Меньше трейдов в минус, больше ластхитов.")
        if s["isolated_deaths"] >= 3:
            tips.append(f"{s['isolated_deaths']} смертей в одиночку. Без инфы о врагах не ходи глубоко на чужую половину.")
        t3_med = statistics.median([v for v in first_t3.values() if v] or [0])
        if s["first_t3"] and t3_med and s["first_t3"] - t3_med >= 180:
            tips.append(f"Первый предмет 3-го тира купил на {_clock(s['first_t3'])}, а в среднем по лобби на {_clock(t3_med)}. "
                        "Меньше копи души и не дроби покупки.")
        if s["missed_fights"] >= 3:
            tips.append(f"Пропущено тимфайтов: {s['missed_fights']}. Команда дралась без тебя.")
        out_players.append({
            "hero_id": h, "hero": m.name(h), "name": p["player_name"], "steam_id": str(p["steam_id"]),
            "account_id": int(p["steam_id"]) - STEAM64, "team": p["team_num"], "team_name": m.teams.get(p["team_num"]),
            "lane": p["start_lane"], "won": p["team_num"] == winner, "stats": s,
            "impact": round(impact, 2), "ruin": ruin, "verdict": verdict,
            "why_bad": [lbl for _, lbl in worst], "why_good": [lbl for _, lbl in best], "tips": tips,
            "ult": ult_stats[h], "fights": fight_rows[h],
            "farm": {"sources": sources[h], "timeline": farm_timeline[h]},
            "combat": {"by_target": {str(k_): v for k_, v in combat[h]["by_target"].items()},
                       "by_source": {str(k_): v for k_, v in combat[h]["by_source"].items()},
                       "split": combat[h]["split"], "hs": combat[h]["hs"], "hits": combat[h]["hits"],
                       "killed": {str(k_): v for k_, v in kill_matrix[h]["killed"].items()},
                       "killed_by": {str(k_): v for k_, v in kill_matrix[h]["killed_by"].items()}},
            "items": purchases[h], "build": owned[h], "zones": zones[h],
            "moments": sorted(moments[h], key=lambda x: x["t"]),
        })
    losers = [p for p in out_players if not p["won"]]
    winners = [p for p in out_players if p["won"]]
    ruiner = max(losers, key=lambda p: p["ruin"]) if losers else None
    mvp = min(winners, key=lambda p: p["ruin"]) if winners else None

    # ---------- replay track (1 sample per second) ----------
    track = {}
    for h in heroes:
        r = samples.filter(pl.col("hero_id") == h).sort("i").fill_null(0)
        hp = (100 * r["health"] / r["max_health"].clip(lower_bound=1)).round().cast(pl.Int32)
        track[h] = {"x": r["x"].round().cast(pl.Int32).to_list(), "y": r["y"].round().cast(pl.Int32).to_list(),
                    "alive": r["is_alive"].cast(pl.Int8).to_list(), "hp": hp.to_list(),
                    "nw": r["gold_net_worth"].to_list(), "souls": r["souls"].to_list(), "ult": ult_track[h]}

    kills_out = []
    for k in kills:
        v = k["victim_hero_id"]
        if v not in m.by_hero:
            continue
        t = m.sec(k["tick"])
        r = m.frame(max(0, k["tick"] - 1)).get(v, {})
        kills_out.append({"t": round(t, 1), "clock": _clock(t), "victim": v, "attacker": k["attacker_hero_id"],
                          "assists": k["assister_hero_ids"] or [], "x": round(r.get("x", 0)), "y": round(r.get("y", 0))})

    return {
        "match_id": d.match_id, "duration": round(m.end_s), "clock": _clock(m.end_s), "build": d.build,
        "winner": winner, "teams": {str(k): v for k, v in m.teams.items()},
        "players": out_players,
        "ruiner": ruiner and ruiner["hero_id"], "mvp": mvp and mvp["hero_id"],
        "kills": kills_out, "objectives": objectives_out, "objective_spots": objective_spots,
        "fights": fights_out, "events": sorted(events, key=lambda e: e["t"]), "camps": camps,
        "track": {str(h): v for h, v in track.items()},
    }


def save_report(report, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{report['match_id']}.json"
    path.write_text(json.dumps(report, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    return path


if __name__ == "__main__":
    import sys
    rep = analyze(sys.argv[1])
    print(save_report(rep, Path(__file__).parent / "web" / "reports"))
    for p in sorted(rep["players"], key=lambda p: (p["team"], -p["ruin"])):
        print(f"{p['team_name']:12} {p['name'][:14]:14} {p['hero']:14} ruin={p['ruin']:3} {p['verdict']:12} "
              f"{p['stats']['kills']}/{p['stats']['deaths']}/{p['stats']['assists']} moments={len(p['moments'])} "
              f"ult={p['ult']['casts']}/{p['ult']['idle_pct']}% camps={p['stats']['camps']} bad={p['why_bad']}")
