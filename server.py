"""Web server: static site plus an API to queue matches for analysis."""
import functools
import json
import queue
import re
import threading
import time
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from analyze import STEAM64, analyze, save_report
from fetch import API, fetch_demo

ROOT = Path(__file__).parent
WEB = ROOT / "web"
REPORTS = WEB / "reports"

jobs = {}  # match_id -> job dict
jobs_lock = threading.Lock()
work = queue.Queue()


def set_job(mid, **kw):
    with jobs_lock:
        jobs.setdefault(mid, {"match_id": mid, "added": time.time()}).update(kw)


def enqueue(mid, start_time=None, who=None):
    if (REPORTS / f"{mid}.json").exists():
        set_job(mid, status="done", who=who)
        return
    with jobs_lock:
        if mid in jobs and jobs[mid].get("status") in ("queued", "downloading", "analyzing"):
            return
    set_job(mid, status="queued", start_time=start_time, who=who, error=None)
    work.put(mid)


def worker():
    from ruiner import rebuild_index
    while True:
        mid = work.get()
        demo = None
        try:
            set_job(mid, status="downloading")
            demo = fetch_demo(mid)
            set_job(mid, status="analyzing")
            rep = analyze(demo)
            st = jobs[mid].get("start_time")
            if st:
                rep["start_time"] = st
            save_report(rep, REPORTS)
            rebuild_index()
            set_job(mid, status="done")
        except Exception as e:
            traceback.print_exc()
            msg = str(e)
            if "no replay" in msg or "404" in msg:
                msg = "У Valve нет реплея этого матча (удалён или не записывался)"
            elif "400" in msg:
                msg = "Неверный ID матча"
            set_job(mid, status="error", error=msg)
        finally:
            if demo:
                demo.unlink(missing_ok=True)


def parse_match_id(text):
    nums = re.findall(r"\d{6,}", text)
    return int(nums[-1]) if nums else None


def resolve_player(text):
    """Nickname, account id, SteamID64 or profile URL -> list of candidate profiles."""
    text = text.strip()
    m = re.search(r"profiles/(\d{17})", text) or re.fullmatch(r"(\d{17})", text)
    if m:
        return [{"account_id": int(m.group(1)) - STEAM64, "name": None}]
    if re.fullmatch(r"\d{4,11}", text):
        return [{"account_id": int(text), "name": None}]
    r = requests.get(f"{API}/players/steam-search", params={"search_query": text, "limit": 8}, timeout=30)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return [{"account_id": p["account_id"], "name": p["personaname"], "avatar": p.get("avatarmedium"),
             "matches_30d": p.get("matches_played_last_30d")} for p in r.json()]


def queue_player(account_id, n):
    r = requests.get(f"{API}/players/{account_id}/match-history", timeout=60)
    r.raise_for_status()
    hist = sorted(r.json(), key=lambda m: m["start_time"], reverse=True)[:n]
    for m in hist:
        enqueue(m["match_id"], m["start_time"], who=account_id)
    return [m["match_id"] for m in hist]


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/ping":
                return self.send_json({"ok": True})
            if u.path == "/api/jobs":
                with jobs_lock:
                    items = sorted(jobs.values(), key=lambda j: -j["added"])[:40]
                return self.send_json(items)
            if u.path == "/api/search":
                return self.send_json(resolve_player(q.get("q", "")))
        except Exception as e:
            return self.send_json({"error": str(e)}, 500)
        return super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        try:
            if u.path == "/api/match":
                mid = parse_match_id(str(body.get("match", "")))
                if not mid:
                    return self.send_json({"error": "Не нашёл ID матча в тексте"}, 400)
                enqueue(mid)
                return self.send_json({"queued": [mid]})
            if u.path == "/api/player":
                ids = queue_player(int(body["account_id"]), max(1, min(20, int(body.get("n", 5)))))
                return self.send_json({"queued": ids})
        except Exception as e:
            return self.send_json({"error": str(e)}, 500)
        self.send_json({"error": "not found"}, 404)


def serve(port=8765, host="127.0.0.1"):
    threading.Thread(target=worker, daemon=True).start()
    handler = functools.partial(Handler, directory=str(WEB))
    with ThreadingHTTPServer((host, port), handler) as srv:
        print(f"serving http://localhost:{port}/")
        srv.serve_forever()


if __name__ == "__main__":
    import os
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8765))
    serve(port, os.environ.get("HOST", "127.0.0.1"))
