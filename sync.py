"""Send match ids from Deadlock's console.log to the GitHub workflow (no analysis runs locally).

  python sync.py           # send new ids
  python sync.py --dry     # only show what would be sent
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
STATE = ROOT / ".sync_state.json"
REPO = json.loads((ROOT / "web" / "config.json").read_text(encoding="utf-8"))["repo"]


def console_logs():
    from ruiner import replay_dirs
    return [d.parent / "console.log" for d in replay_dirs() if (d.parent / "console.log").exists()]


def main():
    sent = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    found = []
    for log in console_logs():
        for mid in re.findall(r"match_id=(\d{8,})", log.read_text(encoding="utf-8", errors="ignore")):
            if int(mid) not in sent and int(mid) not in found:
                found.append(int(mid))
    if not found:
        print("no new matches")
        return
    print("new matches:", *found)
    if "--dry" in sys.argv:
        return
    request = "sync " + " ".join(map(str, found))
    subprocess.run(["gh", "workflow", "run", "ruiner.yml", "-R", REPO, "-f", f"request={request}"], check=True)
    STATE.write_text(json.dumps(sorted(sent | set(found))))
    print("sent to GitHub, the site updates in ~5-10 minutes")


if __name__ == "__main__":
    main()
