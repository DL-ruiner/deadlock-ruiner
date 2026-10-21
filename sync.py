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
REPLAY_DELAY = 2 * 60  # Valve needs a moment after the match to publish the replay
RETRY_STEPS = [0, 5, 10, 20, 40, 60]  # minutes between attempts for a match the API doesn't know yet
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


class QuotaError(RuntimeError):
    def __init__(self, wait):
        super().__init__(f"quota, retry in {wait}s")
        self.wait = wait


def demo_url(mid):
    r = requests.get(f"{API}/matches/{mid}/salts", timeout=30)
    if r.status_code in (429, 503):
        raise QuotaError(int(r.headers.get("Retry-After") or 600))
    r.raise_for_status()
    return r.json().get("demo_url")


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if isinstance(state, list):  # old format: plain list of sent ids
        state = {"sent": state}
    sent = set(state.get("sent", []))
    quota_until = state.get("quota_until", 0)  # every request while blocked extends the block, so wait it out
    tries = {int(k): v for k, v in state.get("tries", {}).items()}  # match -> [attempts, last_ts]
    now = time.time()
    running = game_running()

    ready, later = [], []
    for log in console_logs():
        for mid, (start, end) in matches_in_log(log).items():
            if mid in sent:
                continue
            finished_at = end or (None if running else log.stat().st_mtime)
            n_try, last = tries.get(mid, [0, 0])
            wait = RETRY_STEPS[min(n_try, len(RETRY_STEPS) - 1)] * 60
            if finished_at and now - finished_at >= REPLAY_DELAY and now - last >= wait:
                ready.append((mid, finished_at, start))
            else:
                later.append(mid)
    queue = [m for m in waiting() if m not in {r[0] for r in ready}]
    print("ready:", *[r[0] for r in ready] or ["-"], "| still playing or too fresh:", *later or ["-"],
          "| bot queue:", *queue or ["-"])
    if "--dry" in sys.argv or not (ready or queue):
        return

    parts, quota = [], now >= quota_until
    for mid, finished_at, start in ready + [(m, None, None) for m in queue]:
        when = f"@{int(start)}" if start else ""  # match start, so the site can show the date
        url, answered = None, False
        if quota:
            try:
                url = demo_url(mid)
                answered = True
            except QuotaError as e:
                quota = False
                quota_until = now + e.wait + 30
            except Exception as e:
                print(f"{mid}: {e}")
                answered = True
            if answered and not url:  # unknown to the API yet: back off instead of burning the hourly quota
                n_try, _ = tries.get(mid, [0, 0])
                tries[mid] = [n_try + 1, now]
        if url:
            parts.append(f"{mid}={url}{when}")
            sent.add(mid)
        elif finished_at and now - finished_at > GIVE_UP_AFTER:
            parts.append(f"{mid}{when}")  # let the bot try on its own
            sent.add(mid)
    if parts:
        gh = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
        subprocess.run([gh, "workflow", "run", "ruiner.yml", "-R", REPO, "-f", "request=sync " + " ".join(parts)],
                       check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print(f"sent {len(parts)} matches, the site updates in ~5-10 minutes")
    elif not quota:
        print(f"Steam quota used up, waiting until {datetime.datetime.fromtimestamp(quota_until):%H:%M}")
    STATE.write_text(json.dumps({"sent": sorted(sent), "quota_until": quota_until,
                                 "tries": {str(m): t for m, t in tries.items() if m not in sent}}))


if __name__ == "__main__":
    if sys.stdout is None:  # pythonw from Task Scheduler: keep a small log instead
        log = (ROOT / ".sync.log").open("a", encoding="utf-8")
        sys.stdout = sys.stderr = log
        print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), end=" ")
    try:
        main()
    except Exception as e:
        print("error:", e)
