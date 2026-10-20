"""Post a summary of freshly analyzed matches to Discord (webhook URL in the DISCORD_WEBHOOK env var)."""
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).parent
REPORTS = ROOT / "web" / "reports"
NEW = REPORTS / ".new"
CONFIG = json.loads((ROOT / "web" / "config.json").read_text(encoding="utf-8"))
SITE = CONFIG.get("site", "").rstrip("/") + "/"
VERDICT_ICON = {"Тащил": "🟢", "Норм": "🟡", "Под вопросом": "🔴"}


def mark_new(match_id):
    REPORTS.mkdir(parents=True, exist_ok=True)
    with NEW.open("a") as f:
        f.write(f"{match_id}\n")


def embed(r, crew):
    mine = [p for p in r["players"] if p["account_id"] in crew]
    won = mine[0]["won"] if mine else None
    short = r["duration"] < 900
    title = ("Победа" if won else "Поражение") if mine else "Матч"
    lines = []
    for p in sorted(mine, key=lambda p: p["ruin"]):
        s = p["stats"]
        lines.append(f"{VERDICT_ICON.get(p['verdict'], '')} **{crew[p['account_id']]}** · {p['hero']} · "
                     f"{s['kills']}/{s['deaths']}/{s['assists']} · руин **{p['ruin']}**")
    fields = [{"name": "Наши", "value": "\n".join(lines) or "—", "inline": False}]
    if not short:
        by = {p["hero_id"]: p for p in r["players"]}
        mvp, ruiner = by.get(r["mvp"]), by.get(r["ruiner"])
        if mvp:
            fields.append({"name": "MVP", "value": f"{crew.get(mvp['account_id'], mvp['name'])} · {mvp['hero']}", "inline": True})
        if ruiner:
            fields.append({"name": "Руинер", "value": f"{crew.get(ruiner['account_id'], ruiner['name'])} · {ruiner['hero']}", "inline": True})
        # the loudest mistake and the best play inside our group
        moments = [(m, p) for p in mine for m in p["moments"]]
        worst = max((x for x in moments if not x[0]["good"]), key=lambda x: x[0]["weight"], default=None)
        best = max((x for x in moments if x[0]["good"]), key=lambda x: x[0]["weight"], default=None)
        for label, x in (("Лучший момент", best), ("Главный косяк", worst)):
            if x:
                m, p = x
                fields.append({"name": label, "value": f"{m['clock']} · **{crew[p['account_id']]}** — {m['title']}: {m['text']}"[:1000],
                               "inline": False})
    else:
        fields.append({"name": "Короткий матч", "value": "Меньше 15 минут, в статистику не идёт", "inline": False})
    fields.append({"name": "Полный разбор", "value": f"[Открыть на сайте]({SITE}#m{r['match_id']})"
                   + "".join(f" · [{crew[p['account_id']]}]({SITE}#p{p['account_id']})" for p in mine), "inline": False})
    out = {"title": f"{title} · {r['clock']}", "url": f"{SITE}#m{r['match_id']}", "fields": fields,
           "color": 0x5FBF8F if won else 0xD4553F if won is False else 0xC8A25A,
           "footer": {"text": f"Deadlock Ruiner · матч {r['match_id']}"}}
    heroes = json.loads((ROOT / "web" / "assets" / "heroes.json").read_text(encoding="utf-8"))
    star = next((p for p in r["players"] if p["hero_id"] == r["mvp"]), None) or (mine[0] if mine else None)
    icon = star and heroes.get(str(star["hero_id"]), {}).get("sm")
    if SITE.startswith("http") and icon:
        out["thumbnail"] = {"url": SITE + icon}
    return out


def main():
    hook = os.environ.get("DISCORD_WEBHOOK")
    if not NEW.exists():
        return
    ids = list(dict.fromkeys(int(x) for x in NEW.read_text().split()))
    NEW.unlink()
    if not hook:
        print("DISCORD_WEBHOOK is not set, skipping Discord")
        return
    crew = {f["account_id"]: f["name"] for f in json.loads((ROOT / "web" / "friends.json").read_text(encoding="utf-8"))}
    for mid in ids:
        f = REPORTS / f"{mid}.json"
        if not f.exists():
            continue
        r = json.loads(f.read_text(encoding="utf-8"))
        if not any(p["account_id"] in crew for p in r["players"]):
            continue
        resp = requests.post(hook, json={"username": "Deadlock Ruiner", "embeds": [embed(r, crew)]}, timeout=30)
        print(f"discord {mid}: {resp.status_code}")


if __name__ == "__main__":
    main() if len(sys.argv) == 1 else print(json.dumps(embed(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")),
                                                              {f["account_id"]: f["name"] for f in json.loads((ROOT / "web" / "friends.json").read_text(encoding="utf-8"))}),
                                                        ensure_ascii=False, indent=1))
