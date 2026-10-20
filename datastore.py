"""Keep reports in the orphan 'data' branch so parallel workflow runs merge instead of overwriting.

  python datastore.py restore   # copy saved reports into web/reports
  python datastore.py save      # merge with the latest saved reports and push
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
REPORTS = ROOT / "web" / "reports"
SETS = ("skipped.json", "pending.json")


def git(*args, cwd=ROOT, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True)


def fetch_saved(into):
    """Extract the data branch into a folder; returns its commit sha or None."""
    if git("fetch", "--depth=1", "origin", "data", check=False).returncode:
        return None
    sha = git("rev-parse", "FETCH_HEAD").stdout.decode().strip()
    tar = git("archive", "--format=tar", sha).stdout
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        tf.extractall(into, filter="data")
    return sha


def restore():
    REPORTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        if fetch_saved(tmp):
            for f in (Path(tmp) / "reports").glob("*.json"):
                if not (REPORTS / f.name).exists():
                    shutil.copy(f, REPORTS / f.name)


def save():
    from ruiner import rebuild_index
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["TOKEN"]
    for attempt in range(5):
        with tempfile.TemporaryDirectory() as tmp:
            sha = fetch_saved(tmp)
            saved = Path(tmp) / "reports"
            if saved.exists():
                for f in saved.glob("*.json"):
                    if f.stem.isdigit() and not (REPORTS / f.name).exists():
                        shutil.copy(f, REPORTS / f.name)
            done = {int(f.stem) for f in REPORTS.glob("*.json") if f.stem.isdigit()}
            for name in SETS:
                ids = set()
                for src in (saved / name, REPORTS / name):
                    if src.exists():
                        ids |= set(json.loads(src.read_text()))
                (REPORTS / name).write_text(json.dumps(sorted(ids - done)))
            rebuild_index()

            out = Path(tmp) / "out"
            (out / "reports").mkdir(parents=True)
            for f in REPORTS.glob("*.json"):
                shutil.copy(f, out / "reports" / f.name)
            git("init", "-q", "-b", "data", cwd=out)
            git("add", ".", cwd=out)
            git("-c", "user.name=ruiner-bot", "-c", "user.email=ruiner-bot@users.noreply.github.com",
                "commit", "-qm", "reports", cwd=out)
            lease = f"--force-with-lease=data:{sha}" if sha else "--force"
            url = f"https://x-access-token:{token}@github.com/{repo}.git"
            if git("push", "-q", lease, url, "data", cwd=out, check=False).returncode == 0:
                print(f"saved {len(done)} reports")
                return
        print("data branch moved, merging again")
    sys.exit("could not save reports")


if __name__ == "__main__":
    {"restore": restore, "save": save}[sys.argv[1]]()
