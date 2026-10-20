"""Download a Deadlock demo by match id via deadlock-api salts."""
import bz2
import shutil
import sys
import time
from pathlib import Path

import requests
import zstandard

API = "https://api.deadlock-api.com/v1"
DEMOS = Path(__file__).parent / "demos"


def _unpack(raw: Path, out: Path) -> None:
    # Valve names them .dem.bz2, but the payload is often zstd
    magic = raw.open("rb").read(4)
    with raw.open("rb") as src, out.open("wb") as dst:
        if magic == b"\x28\xb5\x2f\xfd":
            zstandard.ZstdDecompressor().copy_stream(src, dst)
        elif magic[:3] == b"BZh":
            shutil.copyfileobj(bz2.BZ2File(src), dst)
        else:
            shutil.copyfileobj(src, dst)


def fetch_demo(match_id: int) -> Path:
    DEMOS.mkdir(exist_ok=True)
    out = DEMOS / f"{match_id}.dem"
    if out.exists():
        return out
    raw = DEMOS / f"{match_id}.raw"
    if not raw.exists():
        for attempt in range(4):
            r = requests.get(f"{API}/matches/{match_id}/salts", timeout=30)
            if r.status_code not in (429, 503):
                break
            time.sleep(min(60, int(r.headers.get("Retry-After") or 20)))
        if r.status_code in (429, 503):
            raise RuntimeError(f"Steam busy for match {match_id}, retry later")
        r.raise_for_status()
        url = r.json().get("demo_url")
        if not url:
            raise RuntimeError(f"Valve has no replay for match {match_id} (expired or not recorded)")
        print("downloading", url)
        with requests.get(url, stream=True, timeout=600) as resp:
            resp.raise_for_status()
            with raw.open("wb") as f:
                shutil.copyfileobj(resp.raw, f)
    _unpack(raw, out)
    raw.unlink()
    return out


if __name__ == "__main__":
    print(fetch_demo(int(sys.argv[1])))
