#!/usr/bin/env python3
"""Alert event-driven untuk superbot superbot. Dijalankan systemd timer tiap 15 menit.

Cek: disk >85%, unit systemd gagal, docker container (restart policy) mati.
Dedup via state file: alert sekali saat mulai bermasalah + sekali saat pulih.
"""
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
CFG_DIR = f"{HOME}/.config/superbot"
STATE_FILE = f"{CFG_DIR}/alert_state.json"
HEARTBEAT_FILE = f"{CFG_DIR}/heartbeat"
OWNER_FILE = f"{CFG_DIR}/owner"
DISK_PCT = 85
BOT_STALE_S = 600       # superbot heartbeat older than this → alert


def _sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ""


def tg(method: str, **params):
    with open(f"{CFG_DIR}/token") as f:
        token = f.read().strip()
    with open(f"{CFG_DIR}/owner") as f:
        owner = f.read().strip()
    data = urllib.parse.urlencode(params).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/" + method,
                           data=data, timeout=60)


def check_disk() -> tuple[bool, str]:
    bad = []
    for line in _sh(["df", "-P"]).splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6 or parts[5].startswith(("/snap", "/boot", "/efi", "/run")):
            continue
        pct = int(parts[4].rstrip("%"))
        if pct >= DISK_PCT:
            bad.append(f"{parts[5]} {parts[4]} ({parts[2]}/{parts[1]})")
    return (not bad), ("disk ≥" if bad else "") + f"{DISK_PCT}%: " + ", ".join(bad)


def check_units() -> tuple[bool, str]:
    fails = []
    for scope in (["systemctl", "--failed"], ["systemctl", "--user", "--failed"]):
        out = _sh(scope)
        for line in out.splitlines():
            if line and line[0] in "●○×" and "loaded" in line and "failed" in line:
                name = line.split()[1]
                fails.append(name)
    return (not fails), "unit gagal: " + ", ".join(fails)


def check_docker() -> tuple[bool, str]:
    out = _sh(["docker", "ps", "-a", "--filter", "restart=always",
               "--format", "{{.Names}}|{{.State}}"])
    dead = [n for n, s in (l.split("|") for l in out.splitlines() if "|" in l) if s != "running"]
    return (not dead), "container mati: " + ", ".join(dead)


def check_bot() -> tuple[bool, str]:
    """Superbot liveness: heartbeat file mtime must be fresh (<10 min)."""
    try:
        age = time.time() - os.path.getmtime(HEARTBEAT_FILE)
    except OSError:
        if not os.path.exists(OWNER_FILE):
            return True, "superbot belum pernah jalan"   # fresh install: no alarm
        return False, "superbot heartbeat hilang (bot hang?)"
    if age > BOT_STALE_S:
        return False, f"superbot hang (heartbeat {int(age // 60)} menit lalu)"
    return True, ""


CHECKS = {"disk": check_disk, "units": check_units, "docker": check_docker, "bot": check_bot}


def run():
    try:
        with open(STATE_FILE) as f:
            prev = json.load(f)
    except (OSError, json.JSONDecodeError):
        prev = {}
    curr = {}
    for key, fn in CHECKS.items():
        try:
            curr[key] = fn()
        except Exception as e:
            curr[key] = tuple(prev[key]) if prev.get(key) else (True, f"(check {key} error: {e})")   # keep last status, no fake recovery
        prev_entry = prev.get(key)
        was_bad = (not prev_entry[0]) if prev_entry else True   # unknown → anggap bad, no spam start pertama
        now_bad = not curr[key][0]
        if now_bad and not was_bad:
            tg("sendMessage", chat_id=_owner(), text=f"🚨 ALERT {key}: {curr[key][1]}")
        elif not now_bad and was_bad and prev_entry is not None:
            tg("sendMessage", chat_id=_owner(), text=f"✅ PULIH {key}")
    with open(STATE_FILE, "w") as f:
        json.dump(curr, f)


def _owner() -> str:
    with open(OWNER_FILE) as f:
        return f.read().strip()


if __name__ == "__main__":
    run()
