# NairaPips DD Fleet Supervisor V2 + Breach Relay
# 2026-10-03
#
# TEAM ARCHITECTURE
# -----------------
# 4 x DD Police V3.16 workers:
#   PURE DD POLICING ONLY
#   live MT5 -> exact DD -> durable latch -> close/keep closed
#
# 1 x Breach Relay:
#   OUTSIDE shard hot path
#   local breach outbox -> Monitoring API/backend paperwork
#
# Operations/Target Engine is NOT managed, stopped, restarted or modified here.

import os
import sys
import time
import shutil
import subprocess
import signal
from pathlib import Path
from datetime import datetime
from fleet_instance_lock import instance_lock

RELEASE = "NAIRAPIPS_DD_FLEET_SUPERVISOR_V7_V320_CLEAN_LIVE_STATE_2026_10_08"

DEFAULT_WORKERS = int(os.getenv("NAIRAPIPS_DD_WORKERS", "4"))
BASE_TERMINAL = Path(
    os.getenv("NAIRAPIPS_DD_BASE_TERMINAL", r"C:\Program Files\MEX Atlantic MT5 Terminal")
)
FLEET_ROOT = Path(
    os.getenv("NAIRAPIPS_DD_FLEET_ROOT", r"C:\NairaPips_DD_Fleet")
)

HERE = Path(__file__).resolve().parent
WORKER_FILE = HERE / "DD_POLICE_V3_20_CLEAN_LIVE_STATE_TELEMETRY_20261008.py"
RELAY_FILE = HERE / "DD_BREACH_RELAY_V1_20261003.py"
ROSTER_WATCHER_FILE = HERE / "DD_ROSTER_WATCHER_V1_20261003.py"
PYTHON = sys.executable


def log(msg, level="INFO"):
    print(
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {level} | {msg}",
        flush=True,
    )


def terminal_exe(folder):
    return folder / "terminal64.exe"


def validate():
    if not WORKER_FILE.exists():
        raise SystemExit(f"BLOCKED: V3.20 worker missing: {WORKER_FILE}")
    if not RELAY_FILE.exists():
        raise SystemExit(f"BLOCKED: breach relay missing: {RELAY_FILE}")
    if not ROSTER_WATCHER_FILE.exists():
        raise SystemExit(f"BLOCKED: roster watcher missing: {ROSTER_WATCHER_FILE}")
    if DEFAULT_WORKERS != 4:
        raise SystemExit(
            f"BLOCKED: this production cutover expects exactly 4 shards; "
            f"NAIRAPIPS_DD_WORKERS={DEFAULT_WORKERS}"
        )
    if not terminal_exe(BASE_TERMINAL).exists():
        raise SystemExit(
            f"BLOCKED: base MEX terminal missing: {terminal_exe(BASE_TERMINAL)}"
        )


def prepare_terminal(shard):
    dst = FLEET_ROOT / f"terminal_{shard:02d}"
    exe = terminal_exe(dst)
    if exe.exists():
        return dst

    log(f"Preparing isolated MT5 terminal for shard {shard}/{DEFAULT_WORKERS}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(BASE_TERMINAL, dst, dirs_exist_ok=False)

    if not exe.exists():
        raise RuntimeError(f"terminal copy incomplete for shard {shard}")
    return dst


def open_log(name):
    logs = FLEET_ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / name
    fh = open(path, "a", encoding="utf-8", buffering=1)
    return path, fh


def spawn_shard(shard, folder):
    logfile, fh = open_log(f"dd_police_shard_{shard:02d}.log")

    cmd = [
        PYTHON,
        str(WORKER_FILE),
        "--shard", str(shard),
        "--total-shards", str(DEFAULT_WORKERS),
        "--terminal-path", str(terminal_exe(folder)),
    ]

    env = os.environ.copy()
    env["NAIRAPIPS_DD_LATCH_ROOT"] = str(FLEET_ROOT / "state")
    env["NAIRAPIPS_DD_FLEET_ROOT"] = str(FLEET_ROOT)
    env["NAIRAPIPS_DD_ROSTER_SNAPSHOT"] = str(
        FLEET_ROOT / "state" / "monitorable_accounts_snapshot.json"
    )

    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    p = subprocess.Popen(
        cmd,
        stdout=fh,
        stderr=subprocess.STDOUT,
        cwd=str(HERE),
        creationflags=flags,
        env=env,
    )

    log(
        f"STARTED shard {shard}/{DEFAULT_WORKERS} PID={p.pid} "
        f"terminal={terminal_exe(folder)} log={logfile}"
    )
    return {
        "kind": "shard",
        "name": f"shard-{shard}",
        "process": p,
        "log": fh,
        "folder": folder,
        "restarts": 0,
        "shard": shard,
    }


def spawn_relay():
    logfile, fh = open_log("dd_breach_relay.log")

    env = os.environ.copy()
    env["NAIRAPIPS_DD_LATCH_ROOT"] = str(FLEET_ROOT / "state")

    cmd = [PYTHON, str(RELAY_FILE)]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    p = subprocess.Popen(
        cmd,
        stdout=fh,
        stderr=subprocess.STDOUT,
        cwd=str(HERE),
        creationflags=flags,
        env=env,
    )

    log(f"STARTED breach-relay PID={p.pid} log={logfile}")
    return {
        "kind": "relay",
        "name": "breach-relay",
        "process": p,
        "log": fh,
        "folder": None,
        "restarts": 0,
        "shard": None,
    }



def spawn_roster_watcher():
    logfile, fh = open_log("dd_roster_watcher.log")

    env = os.environ.copy()
    env["NAIRAPIPS_DD_FLEET_ROOT"] = str(FLEET_ROOT)
    env["NAIRAPIPS_DD_ROSTER_SNAPSHOT"] = str(
        FLEET_ROOT / "state" / "monitorable_accounts_snapshot.json"
    )

    cmd = [PYTHON, str(ROSTER_WATCHER_FILE)]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    p = subprocess.Popen(
        cmd,
        stdout=fh,
        stderr=subprocess.STDOUT,
        cwd=str(HERE),
        creationflags=flags,
        env=env,
    )

    log(f"STARTED roster-watcher PID={p.pid} log={logfile}")
    return {
        "kind": "roster",
        "name": "roster-watcher",
        "process": p,
        "log": fh,
        "folder": None,
        "restarts": 0,
        "shard": None,
    }

def stop_child(item):
    p = item["process"]
    if p.poll() is not None:
        return

    try:
        if os.name == "nt":
            p.send_signal(signal.CTRL_BREAK_EVENT)
            p.wait(timeout=8)
        else:
            p.terminate()
            p.wait(timeout=8)
    except Exception:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def respawn(item, terminals):
    if item["kind"] == "relay":
        new = spawn_relay()
    elif item["kind"] == "roster":
        new = spawn_roster_watcher()
    else:
        shard = item["shard"]
        new = spawn_shard(shard, terminals[shard])

    new["restarts"] = item["restarts"]
    return new


def main():
    validate()
    FLEET_ROOT.mkdir(parents=True, exist_ok=True)
    (FLEET_ROOT / "state").mkdir(parents=True, exist_ok=True)

    log(RELEASE)
    log("TEAM=4 PURE DD SHARDS + 1 ASYNC BREACH RELAY + 1 INSTANT ROSTER WATCHER")
    log("Operations/Target Engine is NOT managed by this supervisor.")
    log(
        "SHARD HOT PATH HAS NO BACKEND PAPERWORK: "
        "MT5 READ -> EXACT DD -> DURABLE LATCH -> CLOSE",
        "WARNING",
    )
    log(
        "Existing DD fleet supervisors and helper instances must be reconciled before cutover.",
        "WARNING",
    )

    terminals = {}
    try:
        for shard in range(1, DEFAULT_WORKERS + 1):
            terminals[shard] = prepare_terminal(shard)
    except Exception as exc:
        log(
            f"FLEET PREPARATION BLOCKED: {type(exc).__name__}: {exc}",
            "CRITICAL",
        )
        return 2

    children = {}

    try:
        # Start roster watcher first so fresh assignments become locally visible
        # before workers begin polling. It is outside the DD hot path.
        children["roster"] = spawn_roster_watcher()
        time.sleep(1.0)

        # Start relay so any immediately-detected breach outbox is consumed.
        children["relay"] = spawn_relay()
        time.sleep(0.5)

        for shard in range(1, DEFAULT_WORKERS + 1):
            children[f"shard-{shard}"] = spawn_shard(shard, terminals[shard])
            time.sleep(0.75)

        log(
            "TEAM STARTED. Supervisor restarts crashed shards/relay/roster-watcher independently; "
            "one failure cannot stop the other DD workers."
        )

        while True:
            time.sleep(2.0)

            for key, item in list(children.items()):
                if item.get("awaiting_restart"):
                    if time.monotonic() < item["restart_at"]:
                        continue
                    try:
                        children[key] = respawn(item, terminals)
                    except Exception as exc:
                        item["restart_at"] = time.monotonic() + 60
                        log(f"{key} restart failed: {type(exc).__name__}", "ERROR")
                    continue
                p = item["process"]
                rc = p.poll()
                if rc is None:
                    continue

                try:
                    item["log"].close()
                except Exception:
                    pass

                item["restarts"] += 1
                backoff = min(5 * item["restarts"], 60)

                log(
                    f"{item['name'].upper()} EXITED rc={rc}; "
                    f"restart in {backoff}s",
                    "ERROR",
                )

                item["restart_at"] = time.monotonic() + backoff
                item["awaiting_restart"] = True

    except KeyboardInterrupt:
        log(
            "DD team stop requested. Stopping managed DD shards, relay and roster watcher only.",
            "WARNING",
        )
    finally:
        # Stop relay/watcher first, then workers. Operations Engine remains untouched.
        relay = children.get("relay")
        if relay:
            stop_child(relay)

        roster = children.get("roster")
        if roster:
            stop_child(roster)

        for key, item in children.items():
            if key in ("relay", "roster"):
                continue
            stop_child(item)

        for item in children.values():
            try:
                item["log"].close()
            except Exception:
                pass

        log("DD team supervisor stopped. Operations Engine was untouched.")

    return 0


if __name__ == "__main__":
    with instance_lock(FLEET_ROOT / "state", "supervisor"):
        raise SystemExit(main())
