"""Send match ids from Deadlock's console.log (plus the bot's waiting queue) to the GitHub workflow.

Replay links are resolved from this PC's IP (deadlock-api allows ~3 new matches per hour per IP),
so GitHub only downloads and analyzes. Nothing heavy runs locally.

  python sync.py           # send new and waiting matches
  python sync.py --dry     # only show what would be sent
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).parent
STATE = ROOT / ".sync_state.json"
REPO = json.loads((ROOT / "web" / "config.json").read_text(encoding="utf-8"))["repo"]


def console_logs():
    from ruiner import replay_dirs
    return [d.parent / "console.log" for d in replay_dirs() if (d.parent / "console.log").exists()]


def waiting():
    """Matches the bot could not fetch yet (Steam quota on GitHub's side)."""
    try:
        r = requests.get(f"https://raw.githubusercontent.com/{REPO}/data/reports/pending.json", timeout=20)
        return [int(x) for x in r.json()] if r.ok else []
    except Exception:
        return []


def main():
    sent = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    found = []
    for log in console_logs():
        for mid in re.findall(r"match_id=(\d{8,})", log.read_text(encoding="utf-8", errors="ignore")):
            if int(mid) not in sent and int(mid) not in found:
                found.append(int(mid))
    queue = [m for m in waiting() if m not in found]
    if not found and not queue:
        print("nothing to send")
        return
    print("new:", *found or ["-"], "| waiting:", *queue or ["-"])
    if "--dry" in sys.argv:
        return

    from fetch import demo_url
    parts, quota_left = [], True
    for mid in found + queue:
        if quota_left:
            try:
                parts.append(f"{mid}={demo_url(mid)}")
                continue
            except Exception as e:
                print(f"{mid}: {e}")
                quota_left = "busy" not in str(e)
        if mid in found:
            parts.append(str(mid))
    resolved = [p for p in parts if "=" in p]
    if not resolved and not found:
        print("Steam quota used up, try again in an hour")
        return
    request = "sync " + " ".join(parts)
    subprocess.run(["gh", "workflow", "run", "ruiner.yml", "-R", REPO, "-f", f"request={request}"], check=True)
    STATE.write_text(json.dumps(sorted(sent | set(found))))
    print(f"sent {len(parts)} matches ({len(resolved)} with replay links), the site updates in ~5-10 minutes")


if __name__ == "__main__":
    main()
