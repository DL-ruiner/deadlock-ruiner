"""Record a live Deadlock match from Valve's HTTP broadcast and turn it into a regular .dem file.

Broadcast fragments carry the same demo commands as a replay file, framed as
u8 command, u32 tick, u8 reserved, u32 size, payload. Packets come without the
CDemoPacket wrapper, so we add it back.

  python broadcast.py record <broadcast_url> <out_dir>   # until the match ends
  python broadcast.py build <out_dir> <file.dem>
"""
import json
import struct
import sys
import time
from pathlib import Path

import requests

DEM_STOP, DEM_FILEHEADER, DEM_FILEINFO, DEM_SYNCTICK = 0, 1, 2, 3
DEM_SENDTABLES, DEM_CLASSINFO, DEM_PACKET, DEM_SIGNON, DEM_FULLPACKET = 4, 5, 7, 8, 13
WRAPPED = {DEM_PACKET, DEM_SIGNON}  # raw net messages that a .dem stores inside CDemoPacket


def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_bytes(field, data):
    return varint(field << 3 | 2) + varint(len(data)) + data


def pb_int(field, value):
    return varint(field << 3) + varint(value)


def commands(blob):
    i = 0
    while i + 10 <= len(blob):
        cmd, tick = blob[i], struct.unpack_from("<I", blob, i + 1)[0]
        size = struct.unpack_from("<I", blob, i + 6)[0]
        yield cmd, tick, blob[i + 10:i + 10 + size]
        i += 10 + size


def available(url, frag, kind="full"):
    try:
        r = requests.get(f"{url}/{frag}/{kind}", timeout=30, stream=True)
        r.close()
        return r.status_code == 200
    except requests.RequestException:
        return False


def oldest_full(url, current, step):
    """The server keeps only the last few minutes; find the earliest full snapshot still there."""
    lo, hi = 0, current  # hi is known to exist
    if available(url, 0):
        return 0
    while hi - lo > step:
        mid = (lo + hi) // 2
        if available(url, mid):
            hi = mid
        else:
            lo = mid
    for f in range(lo, hi + 1):
        if available(url, f):
            return f
    return hi


def record(url, out, poll=2.0, idle_stop=120):
    """Save start + every delta fragment (and the first full one) until the stream stops growing."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    s = requests.get(f"{url}/sync", timeout=20).json()
    (out / "sync.json").write_text(json.dumps(s))
    (out / "start.bin").write_bytes(requests.get(f"{url}/{s['signup_fragment']}/start", timeout=60).content)
    frag = oldest_full(url, s["fragment"], s.get("keyframe_interval") or 3)
    full = requests.get(f"{url}/{frag}/full", timeout=60)
    full.raise_for_status()
    (out / f"{frag:06d}_full.bin").write_bytes(full.content)
    last_ok = time.time()
    while time.time() - last_ok < idle_stop:
        try:
            r = requests.get(f"{url}/{frag}/delta", timeout=60)
        except requests.RequestException:  # a fragment that isn't published yet comes back broken
            r = None
        if r is not None and r.status_code == 200 and r.content:
            (out / f"{frag:06d}_delta.bin").write_bytes(r.content)
            frag += 1
            last_ok = time.time()
        else:
            time.sleep(poll)
    return frag


def build(out, dem_path, match_id=None):
    out = Path(out)
    parts = [DEM_FILEHEADER_MSG(match_id)]
    for cmd, tick, data in commands((out / "start.bin").read_bytes()):
        if cmd in (DEM_SIGNON, DEM_SENDTABLES, DEM_CLASSINFO):
            parts.append((cmd, 0xFFFFFFFF, pb_bytes(3, data) if cmd in WRAPPED else data))
    parts.append((DEM_SYNCTICK, 0xFFFFFFFF, b""))
    last_tick = 0
    fulls = sorted(out.glob("*_full.bin"))
    for cmd, tick, data in commands(fulls[0].read_bytes()):
        if cmd == DEM_PACKET:
            parts.append((DEM_PACKET, tick, pb_bytes(3, data)))
            last_tick = tick
    for f in sorted(out.glob("*_delta.bin")):
        for cmd, tick, data in commands(f.read_bytes()):
            if cmd == DEM_PACKET and tick > last_tick:
                parts.append((DEM_PACKET, tick, pb_bytes(3, data)))
                last_tick = tick
    body = bytearray()
    for cmd, tick, data in parts:
        body += varint(cmd) + varint(tick) + varint(len(data)) + data
    info_at = 16 + len(body)
    fileinfo = varint(1 << 3 | 5) + struct.pack("<f", last_tick / 64) + pb_int(2, last_tick) + pb_int(3, last_tick)
    body += varint(DEM_FILEINFO) + varint(last_tick) + varint(len(fileinfo)) + fileinfo
    body += varint(DEM_STOP) + varint(last_tick) + varint(0)
    Path(dem_path).write_bytes(b"PBDEMS2\x00" + struct.pack("<ii", info_at, info_at) + body)
    return last_tick


def latest_build():
    """Game build of the newest analyzed replay; the broadcast stream does not carry it."""
    best = 10932
    for f in (Path(__file__).parent / "web" / "reports").glob("*.json"):
        if f.stem.isdigit():
            try:
                best = max(best, int(json.loads(f.read_text(encoding="utf-8")).get("build") or 0))
            except (ValueError, OSError):
                pass
    return best


def DEM_FILEHEADER_MSG(match_id, build=None):
    # same fields Valve writes into replay files; boon needs the build number (13)
    msg = (pb_bytes(1, b"PBDEMS2\x00") + pb_int(2, 48) + pb_bytes(3, b"Deadlock Ruiner broadcast")
           + pb_bytes(4, b"SourceTV Demo") + pb_bytes(5, b"start") + pb_bytes(6, b"citadel")
           + pb_int(7, 2) + pb_int(8, 1) + pb_int(9, 1) + pb_bytes(11, b"valve_demo_2")
           + pb_int(13, build or latest_build()) + pb_int(15, 4521))
    return (DEM_FILEHEADER, 0xFFFFFFFF, msg)


if __name__ == "__main__":
    if sys.argv[1] == "record":
        print("last fragment", record(sys.argv[2], sys.argv[3]))
    else:
        print("last tick", build(sys.argv[2], sys.argv[3]))
