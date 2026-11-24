#!/usr/bin/env python3
"""Decode-test the station list in bot.py before trusting it in a 24/7 rotation.

A healthy MP3 stream produces ~960000 bytes for 5 s of 48 kHz / stereo / s16
(192000 B/s). 0 bytes or an error line means the station is dead (stale URL).

Usage: python3 tools/check_stations.py            # test every station in bot.py
       python3 tools/check_stations.py URL ...    # test specific URLs
       python3 tools/check_stations.py --search   # candidate lofi stations
"""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

BYTES_5S = 960000
FFMPEG = "/usr/bin/ffmpeg"
BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


def stations_from_bot() -> list[tuple[str, str]]:
    """Read STATIONS out of bot.py without importing it (it needs env vars)."""
    import ast

    tree = ast.parse(open(BOT).read())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "STATIONS":
            data = ast.literal_eval(node.value)
            return [(k, v[1]) for k, v in data.items()]
    raise SystemExit("STATIONS not found in bot.py")


def search(tag: str = "lofi", limit: int = 20) -> None:
    query = urllib.parse.urlencode({
        "tag": tag, "limit": limit, "order": "clickcount", "reverse": "true",
        "hidebroken": "true", "codec": "MP3",
    })
    url = "https://de1.api.radio-browser.info/json/stations/search?" + query
    req = urllib.request.Request(url, headers={"User-Agent": "radio-bot-check/1.0"})
    for s in json.loads(urllib.request.urlopen(req, timeout=25).read()):
        print(f"{s['name'][:40]:42} | {s['bitrate']:>3}k | {s['url_resolved']}")


def check(url: str, seconds: int = 5) -> bool:
    cmd = [FFMPEG, "-hide_banner", "-v", "error", "-i", url, "-t", str(seconds),
           "-f", "s16le", "-ac", "2", "-ar", "48000", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=seconds + 20)
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT  {url}")
        return False
    got = len(proc.stdout)
    ok = got >= BYTES_5S * 0.9
    err = proc.stderr.decode(errors="replace").strip().splitlines()[:1]
    print(f"{'OK  ' if ok else 'FAIL'} {got:>8} bytes  {url}  {' '.join(err)[:90] if err else ''}")
    return ok


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--search":
        search(args[1] if len(args) > 1 else "lofi")
        raise SystemExit(0)
    targets = [(u, u) for u in args] if args else stations_from_bot()
    good = 0
    for name, url in targets:
        print(f"[{name}] ", end="", flush=True)
        good += check(url)
    print(f"\n{good}/{len(targets)} stations usable")
    raise SystemExit(0 if good == len(targets) else 1)
