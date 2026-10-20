"""Deadlock Ruiner CLI.

  python ruiner.py match <match_id> [--keep]     analyze one match
  python ruiner.py player <account_id> [-n 5]    analyze a player's latest matches
  python ruiner.py file <path.dem>               analyze a local demo (e.g. from the in-game Download Replay)
  python ruiner.py local                         analyze every new demo in Deadlock's replays folder
  python ruiner.py serve [--port 8765]           open the web viewer
  python ruiner.py friends [-n 5]                analyze new matches of everyone in web/friends.json
  python ruiner.py request "<text>"              match ids, or "player <nick|id>", from a GitHub issue
"""
import argparse
import json
import sys
import webbrowser
from pathlib import Path

import requests

from analyze import STEAM64, analyze, save_report
from fetch import API, fetch_demo

ROOT = Path(__file__).parent
WEB = ROOT / "web"
REPORTS = WEB / "reports"


SHORT_MATCH_S = 900  # shorter games are too noisy for averages

SUMMARY_STATS = ("kills", "deaths", "assists", "net_worth", "spm", "dpm", "kp", "hs_pct", "lane_diff", "isolated_deaths",
                 "missed_fights", "ult_deaths", "ult_unused", "camp_share", "dead_pct", "objective_damage", "healing",
                 "first_t3", "farm_bad", "join_s", "lh10")


def _count_titles(moments, good):
    out = {}
    for mo in moments:
        if mo["good"] == good:
            out[mo["title"]] = out.get(mo["title"], 0) + 1
    return out


def rebuild_index():
    rows = []
    for f in sorted(REPORTS.glob("*.json")):
        if not f.stem.isdigit():
            continue
        r = json.loads(f.read_text(encoding="utf-8"))
        rows.append({
            "match_id": r["match_id"], "duration": r["duration"], "clock": r["clock"], "winner": r["winner"],
            "short": r["duration"] < SHORT_MATCH_S,
            "start_time": r.get("start_time"), "ruiner": r["ruiner"], "mvp": r["mvp"],
            "pairs": [{k: x[k] for k in ("a", "b", "died", "kills")} for x in r.get("pairs", [])],
            "players": [{k: p[k] for k in ("account_id", "name", "hero", "hero_id", "team", "won", "ruin", "verdict")}
                        | {"kda": f"{p['stats']['kills']}/{p['stats']['deaths']}/{p['stats']['assists']}",
                           "s": {k: p["stats"].get(k) for k in SUMMARY_STATS},
                           "mist": _count_titles(p["moments"], False), "good": _count_titles(p["moments"], True)}
                        for p in r["players"]],
        })
    rows.sort(key=lambda r: r["match_id"], reverse=True)
    (REPORTS / "index.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def run_file(path, start_time=None):
    rep = analyze(path)
    if start_time:
        rep["start_time"] = start_time
    out = save_report(rep, REPORTS)
    rebuild_index()
    ruiner = next((p for p in rep["players"] if p["hero_id"] == rep["ruiner"]), None)
    print(f"match {rep['match_id']} -> {out.name}" + (f"  ruiner: {ruiner['name']} ({ruiner['hero']})" if ruiner else ""))
    return rep


def run_match(match_id, keep=False, start_time=None, url=None):
    if (REPORTS / f"{match_id}.json").exists():
        print(f"match {match_id}: already analyzed", file=sys.stderr)
        return
    demo = fetch_demo(match_id, url)
    try:
        run_file(demo, start_time)
    finally:
        if not keep:
            demo.unlink(missing_ok=True)


def run_player(account_id, n):
    if account_id > STEAM64:  # SteamID64 -> account id
        account_id -= STEAM64
    r = requests.get(f"{API}/players/{account_id}/match-history", timeout=60)
    r.raise_for_status()
    hist = sorted(r.json(), key=lambda m: m["start_time"], reverse=True)[:n]
    if not hist:
        print("no matches found (profile may be private or not indexed by deadlock-api yet)")
    for m in hist:
        try:
            run_match(m["match_id"], start_time=m["start_time"])
        except Exception as e:  # expired replays are common, keep going
            print(f"match {m['match_id']}: skipped ({e})")


def _load_set(name):
    f = REPORTS / name
    return set(json.loads(f.read_text())) if f.exists() else set()


def _save_set(name, values):
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / name).write_text(json.dumps(sorted(values)))


def _transient(err):
    return "busy" in str(err) or "503" in str(err) or "429" in str(err) or "timed out" in str(err).lower()


def run_friends(n):
    """Analyze fresh matches of the whole group; remember matches Valve has no replay for."""
    skipped = _load_set("skipped.json")
    pending = _load_set("pending.json")
    friends = json.loads((WEB / "friends.json").read_text(encoding="utf-8"))
    seen = set()
    for mid in sorted(pending):
        seen.add(mid)
        try:
            run_match(mid)
            pending.discard(mid)
        except Exception as e:
            print(f"match {mid}: still pending ({e})")
            if not _transient(e):
                pending.discard(mid)
                skipped.add(mid)
    for f in friends:
        try:
            r = requests.get(f"{API}/players/{f['account_id']}/match-history", timeout=60)
            r.raise_for_status()
        except Exception as e:
            print(f"{f['name']}: history unavailable ({e})")
            continue
        for m in sorted(r.json(), key=lambda m: m["start_time"], reverse=True)[:n]:
            mid = m["match_id"]
            if mid in seen or mid in skipped or (REPORTS / f"{mid}.json").exists():
                continue
            seen.add(mid)
            try:
                run_match(mid, start_time=m["start_time"])
            except Exception as e:
                print(f"match {mid}: skipped ({e})")
                if _transient(e):
                    pending.add(mid)
                elif "no replay" in str(e) or "404" in str(e) or "400" in str(e):
                    skipped.add(mid)
    _save_set("skipped.json", skipped)
    _save_set("pending.json", pending)
    rebuild_index()


def run_request(text):
    """Handle a free-form request; prints a short report for the issue comment."""
    import re
    urls = {}
    if re.search(r"(?i)player|игрок", text):
        from server import resolve_player
        query = re.sub(r"(?i)^.*?(player|игрок)\s*:?\s*", "", text).strip().splitlines()[0]
        found = resolve_player(query)
        if not found:
            print(f"Игрок «{query}» не найден.")
            return
        acc = found[0]["account_id"]
        print(f"Игрок: {found[0].get('name') or acc} ({acc}), последние 5 матчей:")
        r = requests.get(f"{API}/players/{acc}/match-history", timeout=60)
        r.raise_for_status()
        todo = [(m["match_id"], m["start_time"]) for m in sorted(r.json(), key=lambda m: -m["start_time"])[:5]]
    else:
        limit = 30 if re.search(r"(?i)sync", text) else 5
        urls = {int(mid): u for mid, u in re.findall(r"(\d{8,})=(https?://\S+)", text)}
        text_ids = re.sub(r"=https?://\S+", "", text)
        todo = [(int(x), None) for x in dict.fromkeys(re.findall(r"\d{8,}", text_ids))][:limit]
        if re.search(r"(?i)reanalyze|переразобрать", text):
            for mid, _ in todo:
                old = REPORTS / f"{mid}.json"
                if old.exists():
                    todo[todo.index((mid, None))] = (mid, json.loads(old.read_text(encoding="utf-8")).get("start_time"))
                    old.unlink()
    if not todo:
        print("Не нашёл ID матча. Напиши в заголовке номер матча, например: match 119848007")
    for mid, st in todo:
        try:
            run_match(mid, start_time=st, url=urls.get(mid))
            print(f"- {mid}: готово")
        except Exception as e:
            if _transient(e):
                _save_set("pending.json", _load_set("pending.json") | {mid})
                print(f"- {mid}: Steam сейчас занят, матч в очереди, бот разберёт его в следующем прогоне")
                continue
            reason = "у Valve нет реплея" if "no replay" in str(e) or "404" in str(e) else str(e)[:120]
            print(f"- {mid}: не разобран ({reason})")


def replay_dirs():
    vdf = Path(r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf")
    libs = [Path(r"C:\Program Files (x86)\Steam")]
    if vdf.exists():
        libs += [Path(line.split('"')[3].replace("\\\\", "\\")) for line in vdf.read_text(errors="ignore").splitlines()
                 if line.strip().startswith('"path"')]
    return [d for lib in libs if (d := lib / "steamapps/common/Deadlock/game/citadel/replays").is_dir()]


def run_local():
    found = 0
    for d in replay_dirs():
        for dem in sorted(d.glob("*.dem")):
            found += 1
            mid = dem.stem.split("_")[0]
            if mid.isdigit() and (REPORTS / f"{mid}.json").exists():
                continue
            try:
                run_file(dem)
            except Exception as e:
                print(f"{dem.name}: skipped ({e})")
    print(f"local demos found: {found}")


def serve(port):
    import server
    webbrowser.open(f"http://localhost:{port}/")
    server.serve(port)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("match"); a.add_argument("match_id", type=int); a.add_argument("--keep", action="store_true")
    b = sub.add_parser("player"); b.add_argument("account_id", type=int); b.add_argument("-n", type=int, default=5)
    c = sub.add_parser("file"); c.add_argument("path")
    d = sub.add_parser("serve"); d.add_argument("--port", type=int, default=8765)
    sub.add_parser("reindex")
    sub.add_parser("local")
    e = sub.add_parser("friends"); e.add_argument("-n", type=int, default=5)
    g = sub.add_parser("request"); g.add_argument("text")
    args = ap.parse_args()
    if args.cmd == "match":
        run_match(args.match_id, args.keep)
    elif args.cmd == "player":
        run_player(args.account_id, args.n)
    elif args.cmd == "file":
        run_file(args.path)
    elif args.cmd == "serve":
        serve(args.port)
    elif args.cmd == "request":
        run_request(args.text)
    elif args.cmd == "friends":
        run_friends(args.n)
    elif args.cmd == "local":
        run_local()
    elif args.cmd == "reindex":
        rebuild_index()


if __name__ == "__main__":
    main()
