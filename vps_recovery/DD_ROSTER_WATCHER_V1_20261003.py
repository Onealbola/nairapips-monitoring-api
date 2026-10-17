# NairaPips DD Roster Watcher
# 2026-10-03
#
# Purpose:
#   Poll the authoritative Monitoring API independently of the DD shards.
#   When the registry roster changes, atomically publish a local snapshot.
#   DD shards read this local snapshot without putting network I/O in the DD hot path.
#
# Safety:
#   - no MT5 access
#   - no trading
#   - no lifecycle/database writes
#   - no breach logic
#   - only GET /monitorable_accounts + atomic local file update

import os
import json
import time
import hashlib
from pathlib import Path
from datetime import datetime, timezone

import requests
from fleet_instance_lock import instance_lock

RELEASE = "NAIRAPIPS_DD_ROSTER_WATCHER_V1_2026_10_03"

API_BASE_URL = os.getenv(
    "NAIRAPIPS_MONITORING_API_BASE_URL",
    "https://nairapips-monitoring-api.onrender.com",
).rstrip("/")
ENDPOINT = f"{API_BASE_URL}/monitorable_accounts"

FLEET_ROOT = Path(os.getenv("NAIRAPIPS_DD_FLEET_ROOT", r"C:\NairaPips_DD_Fleet"))
STATE_DIR = FLEET_ROOT / "state"
SNAPSHOT_FILE = STATE_DIR / "monitorable_accounts_snapshot.json"

POLL_SECONDS = float(os.getenv("NAIRAPIPS_DD_ROSTER_POLL_SECONDS", "0.75"))
HTTP_TIMEOUT = float(os.getenv("NAIRAPIPS_DD_ROSTER_HTTP_TIMEOUT", "12"))

def log(msg, level="INFO"):
    print(
        f"{datetime.now(timezone.utc).isoformat()} | {level} | {msg}",
        flush=True,
    )

def unpack_rows(data):
    if isinstance(data, dict):
        if data.get("success") is False or data.get("ok") is False or data.get("error"):
            raise ValueError("Roster endpoint reported an error")
        for key in ("data", "rows", "accounts", "mt5_pool", "result"):
            if isinstance(data.get(key), list):
                return data[key]
        raise ValueError("Roster response does not contain a complete account list")
    if isinstance(data, list):
        return data
    raise ValueError("Invalid roster response")

def normalize(rows):
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Malformed roster account; retaining previous snapshot")
        login = str(row.get("mt5_login") or "").strip()
        server = str(row.get("mt5_server") or "").strip()
        if not login.isdigit() or not server:
            raise ValueError("Roster account identity missing; retaining previous snapshot")
        if any(str(previous.get("mt5_login")) == login for previous in out):
            raise ValueError("Duplicate MT5 identity; retaining previous snapshot")
        out.append(row)
    out.sort(key=lambda r: str(r.get("mt5_login") or ""))
    return out

def fingerprint(rows):
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def atomic_write(payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except Exception:
            pass
    os.replace(tmp, SNAPSHOT_FILE)

def main():
    log(f"Starting {RELEASE}")
    log(f"ENDPOINT={ENDPOINT}")
    log(f"POLL_SECONDS={POLL_SECONDS}")
    last_hash = None
    last_count = None

    while True:
        started = time.monotonic()
        try:
            r = requests.get(ENDPOINT, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            rows = normalize(unpack_rows(r.json()))
            h = fingerprint(rows)

            if h != last_hash:
                atomic_write({
                    "release": RELEASE,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "count": len(rows),
                    "fingerprint": h,
                    "accounts": rows,
                })
                log(
                    f"ROSTER CHANGE | count={len(rows)} "
                    f"previous={last_count if last_count is not None else 'none'} "
                    f"snapshot={SNAPSHOT_FILE}"
                )
                last_hash = h
                last_count = len(rows)

        except Exception as exc:
            log(f"Roster poll failed: {type(exc).__name__}: {exc}", "WARNING")

        elapsed = time.monotonic() - started
        time.sleep(max(POLL_SECONDS - elapsed, 0.05))

if __name__ == "__main__":
    with instance_lock(STATE_DIR, "roster-watcher"):
        raise SystemExit(main())
