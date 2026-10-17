# NairaPips DD Breach Relay V1
# OUTSIDE SHARD HOT PATH.
# Reads local breach outbox records written by pure DD workers and performs
# backend persistence. Failure/retry here can never delay MT5 drawdown policing.

import os
import json
import time
from pathlib import Path
import requests
from fleet_instance_lock import instance_lock

RELEASE = "NAIRAPIPS_DD_BREACH_RELAY_V1_2026_10_03"

API_BASE_URL = os.getenv(
    "NAIRAPIPS_MONITORING_API_BASE_URL",
    "https://nairapips-monitoring-api.onrender.com"
).rstrip("/")
SNAPSHOT_ENDPOINT = f"{API_BASE_URL}/monitoring_snapshot"
DISABLE_ENDPOINT = f"{API_BASE_URL}/disable_mt5_access"

STATE_ROOT = Path(os.getenv("NAIRAPIPS_DD_LATCH_ROOT", r"C:\NairaPips_DD_Fleet\state"))
RELAY_STATE = STATE_ROOT / "breach_relay_offsets.json"
POLL_SECONDS = float(os.getenv("NAIRAPIPS_DD_RELAY_POLL_SECONDS", "1.0"))
HTTP_TIMEOUT = 20

def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}", flush=True)

def load_offsets():
    try:
        return json.loads(RELAY_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_offsets(offsets):
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = RELAY_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(offsets, sort_keys=True), encoding="utf-8")
    os.replace(tmp, RELAY_STATE)

def persist_event(ev):
    # Snapshot is useful evidence, but disable endpoint is the authoritative
    # terminal lock. Retry until disable succeeds.
    try:
        requests.post(SNAPSHOT_ENDPOINT, json=ev, timeout=HTTP_TIMEOUT)
    except Exception:
        pass

    payload = dict(ev)
    payload["status"] = "breached"
    payload["reason"] = "MAX_DRAWDOWN_BREACH"
    try:
        r = requests.post(DISABLE_ENDPOINT, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code in (200, 201):
            return True
        log(f"BACKEND REJECT mt5={ev.get('mt5_login')} http={r.status_code} body={(r.text or '')[:180]}")
    except Exception as exc:
        log(f"BACKEND ERROR mt5={ev.get('mt5_login')} error={exc}")
    return False

def main():
    log(f"Starting {RELEASE}")
    offsets = load_offsets()
    while True:
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        for path in sorted(STATE_ROOT.glob("breach_outbox_shard_*.jsonl")):
            key = str(path)
            offset = int(offsets.get(key, 0) or 0)
            try:
                with path.open("r", encoding="utf-8") as fh:
                    fh.seek(offset)
                    while True:
                        pos = fh.tell()
                        line = fh.readline()
                        if not line:
                            break
                        try:
                            ev = json.loads(line)
                        except Exception:
                            offsets[key] = fh.tell()
                            save_offsets(offsets)
                            continue
                        if persist_event(ev):
                            offsets[key] = fh.tell()
                            save_offsets(offsets)
                            log(f"BREACH PERSISTED mt5={ev.get('mt5_login')}")
                        else:
                            fh.seek(pos)
                            break
            except FileNotFoundError:
                continue
            except Exception as exc:
                log(f"OUTBOX ERROR file={path} error={exc}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    with instance_lock(STATE_ROOT, "breach-relay"):
        main()
