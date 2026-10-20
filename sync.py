"""Send finished matches from Deadlock's console.log (plus the bot's waiting queue) to the GitHub workflow.

Light by design: reads one text file and makes a few HTTP requests, nothing is analyzed locally.
Replay links are resolved from this PC's IP (deadlock-api allows ~3 new matches per hour per IP).

  python sync.py           # send finished and waiting matches
  python sync.py --dry     # only show what would be sent
"""
import datetime
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
STATE = ROOT / ".sync_state.json"
REPO = json.loads((ROOT / "web" / "config.json").read_text(encoding="utf-8"))["repo"]
API = "https://api.deadlock-api.com/v1"
REPLAY_DELAY = 5 * 60  # Valve needs a few minutes after the match to publish the replay
GIVE_UP_AFTER = 3 * 3600
STAMP = re.compile(r"^(\d\d)/(\d\d) (\d\d):(\d\d):(\d\d)")


def console_logs():
    vdf = Path(r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf")
    libs = [Path(r"C:\Program Files (x86)\Steam")]
    if vdf.exists():
        libs += [Path(line.split('"')[3].replace("\\\\", "\\")) for line in vdf.read_text(errors="ignore").splitlines()
                 if line.strip().startswith('"path"')]
    return [p for lib in libs if (p := lib / "steamapps/common/Deadlock/game/citadel/console.log").exists()]


def stamp(line, year):
    m = STAMP.match(line)
    if not m:
        return None
    mo, d, h, mi, s = map(int, m.groups())
    return datetime.datetime(year, mo, d, h, mi, s).timestamp()


def matches_in_log(path):
    """match_id -> (started, ended or None); a match ends when its game server connection shuts down."""
    year = datetime.datetime.fromtimestamp(path.stat().st_mtime).year
    started, server_of, ended = {}, {}, {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "match_id=" in line:
            mid = int(re.search(r"match_id=(\d+)", line).group(1))
            sid = re.search(r"server steamid:(\d+)", line)
            started.setdefault(mid, stamp(line, year))
            if sid:
                server_of[sid.group(1)] = mid
        elif "ConnectionShutdown" in line or "Disconnected from server" in line:
            sid = re.search(r"server steamid:(\d+)", line)
            if sid and sid.group(1) in server_of:
                ended.setdefault(server_of[sid.group(1)], stamp(line, year))
    return {mid: (started[mid], ended.get(mid)) for mid in started}


def game_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq deadlock.exe", "/NH"], capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    return "deadlock.exe" in out.lower()


def waiting():
    """Matches the bot could not fetch yet (Steam quota on GitHub's side)."""
    try:
        r = requests.get(f"https://raw.githubusercontent.com/{REPO}/data/reports/pending.json", timeout=20)
        return [int(x) for x in r.json()] if r.ok else []
    except Exception:
        return []


def demo_url(mid):
    r = requests.get(f"{API}/matches/{mid}/salts", timeout=30)
    if r.status_code in (429, 503):
        raise RuntimeError("quota")
    r.raise_for_status()
    return r.json().get("demo_url")


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if isinstance(state, list):  # old format: plain list of sent ids
        state = {"sent": state}
    sent = set(state.get("sent", []))
    now = time.time()
    running = game_running()

    ready, later = [], []
    for log in console_logs():
        for mid, (start, end) in matches_in_log(log).items():
            if mid in sent:
                continue
            finished_at = end or (None if running else log.stat().st_mtime)
            if finished_at and now - finished_at >= REPLAY_DELAY:
                ready.append((mid, finished_at))
            else:
                later.append(mid)
    queue = [m for m in waiting() if m not in {r[0] for r in ready}]
    print("ready:", *[r[0] for r in ready] or ["-"], "| still playing or too fresh:", *later or ["-"],
          "| bot queue:", *queue or ["-"])
    if "--dry" in sys.argv or not (ready or queue):
        return

    parts, quota = [], True
    for mid, finished_at in ready + [(m, None) for m in queue]:
        url = None
        if quota:
            try:
                url = demo_url(mid)
            except RuntimeError:
                quota = False
            except Exception as e:
                print(f"{mid}: {e}")
        if url:
            parts.append(f"{mid}={url}")
            sent.add(mid)
        elif finished_at and now - finished_at > GIVE_UP_AFTER:
            parts.append(str(mid))  # let the bot try on its own
            sent.add(mid)
    if parts:
        gh = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
        subprocess.run([gh, "workflow", "run", "ruiner.yml", "-R", REPO, "-f", "request=sync " + " ".join(parts)],
                       check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print(f"sent {len(parts)} matches, the site updates in ~5-10 minutes")
    elif not quota:
        print("Steam quota used up, next try in a few minutes")
    STATE.write_text(json.dumps({"sent": sorted(sent)}))


if __name__ == "__main__":
    if sys.stdout is None:  # pythonw from Task Scheduler: keep a small log instead
        log = (ROOT / ".sync.log").open("a", encoding="utf-8")
        sys.stdout = sys.stderr = log
        print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), end=" ")
    try:
        main()
    except Exception as e:
        print("error:", e)
