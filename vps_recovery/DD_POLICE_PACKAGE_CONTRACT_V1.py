# NairaPips Dedicated DD Police
# Connectivity is deliberately modelled on the proven Operations Engine:
# initialize terminal once -> mt5.login() account switching -> pool credential retry ->
# master read fallback -> exact-login/freshness gate -> hard reconnect only when needed.
# Business responsibility: DD policing ONLY.

import queue
import threading
import os
import time
import argparse
import hashlib
import json
from datetime import datetime, timezone, timedelta
import requests
import MetaTrader5 as mt5

RELEASE = "NAIRAPIPS_DD_POLICE_V3_20_CLEAN_LIVE_STATE_TELEMETRY_2026_10_08"
# V3.15 surgical performance repair:
# - NO change to DD calculation, DD limits, breach threshold, latch, close logic,
#   shard ownership, roster contract, or risk priorities.
# - Avoids the 30-90 day MT5 deal-history scan unless balance AND equity are
#   exactly at starting capital, which is the only state where that history
#   can affect the existing stale-data contradiction decision.

API_BASE_URL = os.getenv("NAIRAPIPS_MONITORING_API_BASE_URL",
                         "https://nairapips-monitoring-api.onrender.com").rstrip("/")
TRADERS_ENDPOINT = f"{API_BASE_URL}/monitorable_accounts"
SNAPSHOT_ENDPOINT = f"{API_BASE_URL}/monitoring_snapshot"
LIVE_STATE_URL = os.getenv("NP_LIVE_STATE_URL", "").rstrip("/")
LIVE_STATE_KEY = os.getenv("NP_LIVE_STATE_INGEST_KEY", "")
LIVE_STATE_ENDPOINT = f"{LIVE_STATE_URL}/live_state" if LIVE_STATE_URL else ""
DISABLE_ENDPOINT = f"{API_BASE_URL}/disable_mt5_access"
POOL_URL = os.getenv("NAIRAPIPS_ENGINE_MT5_POOL_URL", "").strip()

# Dedicated DD terminal; Operations stays on its own MetaTrader 5 terminal.
MT5_PATH = r"C:\Program Files\MEX Atlantic MT5 Terminal\terminal64.exe"

# V3 horizontal scaling. One worker owns one deterministic shard and one isolated MT5 terminal.
# Defaults preserve single-worker behaviour until explicit shard arguments are supplied.
SHARD_ID = 1
TOTAL_SHARDS = 1
GLOBAL_POPULATION = 0

def configure_worker():
    global SHARD_ID, TOTAL_SHARDS, MT5_PATH
    p=argparse.ArgumentParser(description="NairaPips DD Police V3 sharded worker")
    p.add_argument("--shard", type=int, default=int(os.getenv("NAIRAPIPS_DD_SHARD","1")))
    p.add_argument("--total-shards", type=int, default=int(os.getenv("NAIRAPIPS_DD_TOTAL_SHARDS","1")))
    p.add_argument("--terminal-path", default=os.getenv("NAIRAPIPS_DD_TERMINAL_PATH", MT5_PATH))
    a=p.parse_args()
    if a.total_shards < 1:
        raise SystemExit("--total-shards must be >= 1")
    if a.shard < 1 or a.shard > a.total_shards:
        raise SystemExit("--shard must be between 1 and --total-shards")
    SHARD_ID=a.shard
    TOTAL_SHARDS=a.total_shards
    MT5_PATH=str(a.terminal_path).strip()
    if not MT5_PATH:
        raise SystemExit("--terminal-path is required")
    return a

def owner_shard(login, total=None):
    """Stable rendezvous hashing: one MT5 -> exactly one shard; adding capacity moves only a subset."""
    n=int(total or TOTAL_SHARDS)
    if n <= 1:
        return 1
    best_shard=1
    best_score=-1
    key=str(login).encode("utf-8")
    for shard in range(1,n+1):
        digest=hashlib.sha256(key+b"|"+str(shard).encode("ascii")).digest()
        score=int.from_bytes(digest[:8],"big",signed=False)
        if score>best_score:
            best_score=score
            best_shard=shard
    return best_shard

DISCOVERY_TIMEOUT = 35
# V3.3: normal account switching only needs two exact-login confirmations.
# Deep stale-data recovery still gets the original eight-pass gate below.
FRESHNESS_ATTEMPTS = 2
FRESHNESS_SLEEP = 0.05
DEEP_FRESHNESS_ATTEMPTS = 8
DEEP_FRESHNESS_SLEEP = 0.50
IDLE_SECONDS = 4.0
ACTIVE_SECONDS = 0.25
WARNING_SECONDS = 0.10
CRITICAL_SECONDS = 0.05
HOT_SECONDS = 0.03
AUTH_RETRY_SECONDS = 2.0
POP_REFRESH_SECONDS = 20.0
# V3.10: roster discovery is now fed by a central local watcher.
# The DD hot path only checks local file metadata; JSON is loaded only when changed.
LOCAL_ROSTER_CHECK_SECONDS = 0.10
DIRECT_API_FALLBACK_SECONDS = 20.0
ROSTER_SNAPSHOT_FILE = os.getenv(
    "NAIRAPIPS_DD_ROSTER_SNAPSHOT",
    r"C:\NairaPips_DD_Fleet\state\monitorable_accounts_snapshot.json",
)
HEALTH_SECONDS = 10.0
CLOSE_VERIFY_PASSES = 4
CLOSE_RETRY_SECONDS = 0.35
MASTER_READINESS_TTL = 300.0  # re-check master close readiness every 5 minutes
AUTH_QUARANTINE_BASE_SECONDS = 15.0
AUTH_QUARANTINE_MAX_SECONDS = 120.0

rows = {}
pool_rows = {}
next_due = {}
verified = set()
last_ok = {}
latched = {}
backend_pending = {}
close_ready = set()
close_blocked = set()
last_master_check = {}
auth_fail_count = {}
auth_quarantine_until = {}
first_attempted = set()   # scheduling coverage: attempted once, even if credentials are bad
last_inspected = {}      # successful live DD observation timestamp
last_attempted_at = {}   # every inspection attempt, including auth failures
last_valid_dd_read = {}  # successful balance/equity/positions DD observation only
invalid_live_capital = {}  # login -> evidence string; excluded from fake DD decisions
last_population_refresh = 0.0
last_direct_population_refresh = 0.0
last_roster_snapshot_mtime = 0.0
last_pool_refresh = 0.0
last_health = 0.0

# V2 scheduling state only. These values do NOT change DD authority or breach rules.
risk_priority = {}          # login -> 0 idle, 1 active, 2 warning, 3 critical, 4 hot/breached
hot_burst_remaining = {}    # bounded immediate rechecks so one account cannot starve the population
HOT_BURST_PASSES = 4
CRITICAL_BURST_PASSES = 2
WARNING_BURST_PASSES = 1
MAX_CONSECUTIVE_RISK_TURNS = 4  # then service one due normal account if any
MAX_VALID_REVISIT_SECONDS = 30.0  # hard anti-burial coverage deadline for healthy verified accounts
STALE_HEALTH_SECONDS = 45.0       # health alert threshold; does not change DD authority
MAX_COVERAGE_RESCUE_STREAK = 1    # after one forced rescue, give risk lane a turn if due

# Temporary exact-account quarantine lane.
# 477364582 has a confirmed backend persistence defect. Keep broker protection
# active (authenticate/read/close if exposure reappears), but do not let its
# failed paperwork retry monopolize a shard while the database row is repaired.
PERSISTENCE_QUARANTINE = {"477364582"}
QUARANTINE_RECHECK_SECONDS = 5.0

# V3.8 PURE DD POLICING
# The shard does NO lifecycle/database paperwork.
# Its job is strictly:
#   read live MT5 -> exact DD decision -> durable latch -> close/keep closed.
#
# A separate relay process reads the outbox and performs backend paperwork.
def _state_root():
    root = os.getenv("NAIRAPIPS_DD_LATCH_ROOT", r"C:\NairaPips_DD_Fleet\state")
    os.makedirs(root, exist_ok=True)
    return root

def _latch_file():
    return os.path.join(_state_root(), f"breach_latch_shard_{int(SHARD_ID):02d}.json")

def _outbox_file():
    return os.path.join(_state_root(), f"breach_outbox_shard_{int(SHARD_ID):02d}.jsonl")

def _save_latches():
    path = _latch_file()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(latched, fh, ensure_ascii=False, sort_keys=True)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except Exception:
            pass
    os.replace(tmp, path)

def _load_latches():
    path = _latch_file()
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
        if isinstance(data, dict):
            for login, evidence in data.items():
                if isinstance(evidence, dict):
                    latched[str(login)] = evidence
        if latched:
            log(f"DURABLE BREACH LATCH RECOVERED count={len(latched)} file={path}", "WARNING")
    except Exception as exc:
        log(f"DURABLE BREACH LATCH LOAD FAILED: {exc}", "ERROR")

def _append_breach_outbox(evidence):
    # One small local append only. No HTTP, no DB, no waiting on backend.
    line = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    with open(_outbox_file(), "a", encoding="utf-8", buffering=1) as fh:
        fh.write(line + "\n")

def _persist_first_breach_latch(login, row, evidence):
    login = str(login)
    if login in latched:
        return latched[login]

    first = dict(evidence or {})
    first["trader_account_id"] = row.get("trader_account_id") or row.get("current_account_id")
    first["current_account_id"] = first.get("trader_account_id")
    first["trader_id"] = row.get("id") or row.get("trader_id")
    first["mt5_login"] = login
    first["reason"] = "MAX_DRAWDOWN_BREACH"
    first["status"] = "breached"
    first["breach_latch_release"] = RELEASE

    # In-memory latch is immediate.
    latched[login] = first

    # Durable local latch is written immediately, but NO relay/backend signal
    # is emitted yet. Backend retirement is forbidden until broker exposure is
    # proven zero.
    try:
        _save_latches()
    except Exception as exc:
        log(f"LATCH FILE WRITE FAILED MT5={login}: {exc} | MEMORY LATCH ACTIVE", "CRITICAL")
    return latched[login]

def _queue_breach_after_zero_exposure(login, evidence):
    """Queue backend paperwork exactly once, only AFTER zero exposure is verified."""
    login = str(login)
    ev = latched.get(login) or evidence
    if not isinstance(ev, dict):
        return False
    if ev.get("relay_queued_at"):
        return True
    try:
        _append_breach_outbox(ev)
        ev["relay_queued_at"] = now_iso()
        latched[login] = ev
        try:
            _save_latches()
        except Exception as exc:
            log(f"LATCH RELAY-MARK WRITE FAILED MT5={login}: {exc}", "ERROR")
        log(
            f"BREACH RELAY QUEUED AFTER ZERO EXPOSURE MT5={login}",
            "WARNING",
        )
        return True
    except Exception as exc:
        log(
            f"BREACH OUTBOX WRITE FAILED MT5={login}: {exc} | "
            f"LATCH REMAINS ACTIVE AND ZERO EXPOSURE WILL BE RECHECKED",
            "CRITICAL",
        )
        return False


def _prune_latches_against_population():
    # Clear a latch only after authoritative roster no longer contains that MT5.
    removed = []
    for login in list(latched):
        if login not in rows:
            removed.append(login)
            latched.pop(login, None)
    if removed:
        try:
            _save_latches()
        except Exception as exc:
            log(f"LATCH PRUNE WRITE FAILED: {exc}", "ERROR")
        log(f"DURABLE BREACH LATCH CLEARED retired_from_feed={removed}", "WARNING")

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def log(msg, level="INFO"):
    print(f"{datetime.utcnow()} | {level} | {msg}", flush=True)

def norm(v):
    return str(v or "").strip()

def fnum(v, default=0.0):
    try:
        return float(str(v).replace(",", "").replace("₦", "").strip())
    except Exception:
        return float(default)

def unpack_rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data","sample","rows","accounts","mt5_pool","result"):
            if isinstance(data.get(k), list):
                return data[k]
    return []

def _apply_population_payload(payload, source):
    global GLOBAL_POPULATION
    found = {}
    all_valid = {}

    for row in unpack_rows(payload):
        if not isinstance(row, dict):
            continue
        login = norm(row.get("mt5_login"))
        server = norm(row.get("mt5_server"))
        if login.isdigit() and server:
            all_valid[login] = row
            if owner_shard(login) == SHARD_ID:
                found[login] = row
                next_due.setdefault(login, 0.0)

    previous_owned = set(rows.keys())
    new_owned = set(found.keys())

    GLOBAL_POPULATION = len(all_valid)
    rows.clear()
    rows.update(found)

    for stale in list(
        set(risk_priority.keys())
        | set(next_due.keys())
        | set(verified)
        | set(close_ready)
        | set(close_blocked)
    ):
        if stale not in new_owned:
            risk_priority.pop(stale, None)
            first_attempted.discard(stale)
            last_inspected.pop(stale, None)
            hot_burst_remaining.pop(stale, None)
            next_due.pop(stale, None)
            verified.discard(stale)
            close_ready.discard(stale)
            close_blocked.discard(stale)
            last_ok.pop(stale, None)
            last_master_check.pop(stale, None)

    added = sorted(new_owned - previous_owned)
    removed = sorted(previous_owned - new_owned)

    _prune_latches_against_population()

    if added or removed:
        log(
            f"ROSTER APPLIED source={source} GLOBAL_POP={GLOBAL_POPULATION} "
            f"OWNED={len(rows)} ADDED={added} REMOVED={removed}",
            "WARNING",
        )
    else:
        log(
            f"GLOBAL POPULATION={GLOBAL_POPULATION} | SHARD={SHARD_ID}/{TOTAL_SHARDS} "
            f"| OWNED={len(rows)} | source={source}"
        )

def refresh_population(force=False):
    global last_population_refresh
    global last_direct_population_refresh
    global last_roster_snapshot_mtime

    now = time.monotonic()

    # Very cheap local metadata check. No network call in the DD hot path.
    if not force and now - last_population_refresh < LOCAL_ROSTER_CHECK_SECONDS:
        return

    last_population_refresh = now

    # Primary path: central watcher publishes only when the authoritative roster changes.
    try:
        if os.path.exists(ROSTER_SNAPSHOT_FILE):
            mtime = os.path.getmtime(ROSTER_SNAPSHOT_FILE)
            if force or mtime > last_roster_snapshot_mtime:
                with open(ROSTER_SNAPSHOT_FILE, "r", encoding="utf-8") as fh:
                    snap = json.load(fh)
                accounts = snap.get("accounts") if isinstance(snap, dict) else None
                if isinstance(accounts, list):
                    _apply_population_payload(accounts, "local_roster_watcher")
                    last_roster_snapshot_mtime = mtime
                    return
    except Exception as e:
        log(f"Local roster snapshot read failed: {e}", "WARNING")

    # V3.16 NON-BLOCKING ROSTER TRANSPORT
    # When the local watcher snapshot exists and has already been loaded, it remains
    # the DD hot-path authority between roster changes. Do NOT make a synchronous
    # Monitoring API call merely because the file mtime is unchanged: that can block
    # a shard for DISCOVERY_TIMEOUT and starve valid DD reads.
    #
    # Direct API discovery remains a bootstrap/emergency fallback only when there is
    # no usable local snapshot yet. The dedicated roster watcher owns ongoing network
    # discovery and preserves the last known-good snapshot on API failures.
    have_local_snapshot = bool(
        os.path.exists(ROSTER_SNAPSHOT_FILE)
        and last_roster_snapshot_mtime > 0
    )
    if not have_local_snapshot and (
        force or now - last_direct_population_refresh >= DIRECT_API_FALLBACK_SECONDS
    ):
        try:
            r = requests.get(TRADERS_ENDPOINT, timeout=DISCOVERY_TIMEOUT)
            r.raise_for_status()
            _apply_population_payload(r.json(), "direct_monitoring_api_fallback")
            last_direct_population_refresh = time.monotonic()
        except Exception as e:
            log(f"Population refresh failed: {e}", "ERROR")

def refresh_pool(force=False):
    global last_pool_refresh
    if not POOL_URL:
        return
    if not force and time.monotonic()-last_pool_refresh < POP_REFRESH_SECONDS:
        return
    try:
        r=requests.get(POOL_URL,timeout=20)
        r.raise_for_status()
        found={}
        for a in unpack_rows(r.json()):
            if not isinstance(a,dict): continue
            login=norm(a.get("mt5_login") or a.get("login") or a.get("account_login")
                       or a.get("account_number") or a.get("number"))
            if login: found[login]=a
        pool_rows.clear(); pool_rows.update(found)
        last_pool_refresh=time.monotonic()
        log(f"MT5 POOL CACHE={len(pool_rows)}")
    except Exception as e:
        log(f"MT5 pool refresh failed: {e}", "WARNING")

def pool_account(login):
    # Same principle as old engine: pool is a credential recovery source.
    if POOL_URL:
        refresh_pool()
    return pool_rows.get(norm(login),{})

def row_investor(row):
    return (row.get("mt5_investor_password") or row.get("investor_password")
            or row.get("investor") or "")

def row_master(row):
    return (row.get("mt5_master_password") or row.get("master_password")
            or row.get("mt5_password") or "")

def pool_investor(login):
    a=pool_account(login)
    return a.get("mt5_investor_password") or a.get("investor_password") or a.get("investor") or ""

def pool_master(login):
    a=pool_account(login)
    return (a.get("mt5_master_password") or a.get("master_password")
            or a.get("password") or a.get("mt5_password") or "")

def initialize_mt5():
    # Dedicated MEX terminal may remember a dead/invalid previous session.
    # First try the proven normal attach. If that fails with authorization,
    # bootstrap ONE discovered account using the same row -> pool credential
    # authority used by the working Operations engine. After bootstrap the
    # normal authenticate() path uses mt5.login() for account rotation.
    try:
        mt5.shutdown(); time.sleep(1)
    except Exception:
        pass

    if mt5.initialize(path=MT5_PATH):
        log(f"MT5 initialized: {mt5.version()} | terminal={MT5_PATH}")
        return True

    first_error = mt5.last_error()
    log(f"BARE TERMINAL ATTACH FAILED: {first_error} | trying credential bootstrap", "WARNING")

    refresh_population(True)
    refresh_pool(True)

    attempts = 0
    for login, row in list(rows.items()):
        server = norm(row.get("mt5_server"))
        if not login.isdigit() or not server:
            continue

        candidates = []
        for source, pwd in (
            ("row-investor", row_investor(row)),
            ("pool-investor", pool_investor(login)),
            ("row-master", row_master(row)),
            ("pool-master", pool_master(login)),
        ):
            if pwd and pwd not in [x[1] for x in candidates]:
                candidates.append((source, pwd))

        for source, pwd in candidates:
            attempts += 1
            try:
                mt5.shutdown()
            except Exception:
                pass
            time.sleep(0.35)

            # Explicit account bootstrap is ONLY for terminal startup.
            ok = mt5.initialize(
                path=MT5_PATH,
                login=int(login),
                password=pwd,
                server=server,
                timeout=60000,
            )
            if not ok:
                log(f"BOOTSTRAP REJECTED MT5={login} source={source} error={mt5.last_error()}", "WARNING")
                continue

            time.sleep(0.75)
            info = mt5.account_info()
            current = norm(getattr(info, "login", "")) if info else ""
            if current != login:
                log(f"BOOTSTRAP IDENTITY MISMATCH target={login} terminal={current or 'NONE'}", "WARNING")
                continue

            log(f"TERMINAL BOOTSTRAPPED | MT5={login} source={source} server={server} attempts={attempts}", "WARNING")
            log(f"MT5 initialized: {mt5.version()} | terminal={MT5_PATH}")
            return True

    log(f"MT5 STARTUP BLOCKED: no discovered credential could bootstrap dedicated terminal. attempts={attempts}", "CRITICAL")
    return False

def reinitialize_and_login(login,server,password):
    # EXACT old-engine recovery pattern; used only when normal switch/freshness fails.
    try:
        mt5.shutdown()
    except Exception:
        pass
    time.sleep(1.0)
    if not mt5.initialize(path=MT5_PATH):
        log(f"HARD RECONNECT initialize failed {login}: {mt5.last_error()}","ERROR")
        return False
    time.sleep(.75)
    if not mt5.login(login=int(login),password=password,server=server):
        log(f"HARD RECONNECT login failed {login}: {mt5.last_error()}","ERROR")
        return False
    return True

def exact_account_info(login):
    info=mt5.account_info()
    if info is None: return None
    return info if norm(getattr(info,"login",""))==norm(login) else None

def freshness_trade_evidence(row):
    now=datetime.now(timezone.utc)
    start=now-timedelta(days=30)
    raw=row.get("started_at") or row.get("created_at") or ""
    try:
        if raw:
            d=datetime.fromisoformat(str(raw).replace("Z","+00:00"))
            if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
            start=max(d.astimezone(timezone.utc),now-timedelta(days=90))
    except Exception:
        pass
    try: deals=mt5.history_deals_get(start,now)
    except Exception: deals=None
    if deals is None: return 0,0.0
    types={getattr(mt5,"DEAL_TYPE_BUY",0),getattr(mt5,"DEAL_TYPE_SELL",1)}
    count=0; net=0.0
    for d in deals:
        if getattr(d,"type",None) not in types: continue
        count+=1
        for k in ("profit","commission","swap","fee"):
            net+=float(getattr(d,k,0.0) or 0.0)
    return count,round(net,2)

def verified_live_info(login,server,password,row):
    expected=fnum(row.get("start_balance") or row.get("account_size"),0)
    def sample(label, attempts=FRESHNESS_ATTEMPTS, sleep_s=FRESHNESS_SLEEP):
        last=None
        exact_hits=0
        for i in range(1,attempts+1):
            try: mt5.positions_get()
            except Exception: pass
            try: mt5.orders_get()
            except Exception: pass
            info=mt5.account_info()
            if info is not None and norm(getattr(info,"login",""))==norm(login):
                last=info
                exact_hits+=1
                # Two consecutive exact-login reads are enough for the normal hot path.
                if exact_hits>=2:
                    return last
            else:
                exact_hits=0
                current=norm(getattr(info,"login","")) if info else "NONE"
                log(f"FRESHNESS {label} {i}/{attempts}: target={login} terminal={current}","WARNING")
            if i<attempts: time.sleep(sleep_s)
        return last if exact_hits>0 else None
    info=sample("SETTLE")
    if info is None:
        # V3.5 stability law: NEVER shutdown/reinitialize a healthy shard terminal
        # because one target account failed to settle. A hard reconnect here was
        # knocking the terminal offline and recreating the old login circle.
        # Fail closed for this account only; authenticate() applies bounded
        # quarantine and the shard immediately continues with other accounts.
        log(f"ACCOUNT SWITCH ISOLATED MT5={login}: exact-login settle failed; terminal preserved | NO DD DECISION","WARNING")
        return None
    bal=float(info.balance or 0); eq=float(info.equity or 0)

    # V3.15 HOT-PATH FRESHNESS GUARD
    # The expensive MT5 history scan is evidence for one very specific stale-data
    # contradiction only: live balance AND equity still exactly equal starting capital.
    # Preserve that business/safety test exactly, but do not scan 30-90 days of deal
    # history when the prerequisite cannot possibly be true.
    at_starting_capital = bool(
        expected > 0
        and abs(bal-expected) < .01
        and abs(eq-expected) < .01
    )
    if at_starting_capital:
        dc,pnl=freshness_trade_evidence(row)
        contradiction=bool(dc>0 and abs(pnl)>=1)
    else:
        dc,pnl=0,0.0
        contradiction=False

    if contradiction and password:
        log(f"STALE MT5 SUSPECTED {login}; hard refreshing","CRITICAL")
        if reinitialize_and_login(login,server,password):
            info=sample("RECONNECT", DEEP_FRESHNESS_ATTEMPTS, DEEP_FRESHNESS_SLEEP)
            if info is not None:
                bal=float(info.balance or 0); eq=float(info.equity or 0)
                dc,pnl=freshness_trade_evidence(row)
                contradiction=bool(expected>0 and abs(bal-expected)<.01 and abs(eq-expected)<.01 and dc>0 and abs(pnl)>=1)
    if contradiction:
        log(f"FRESHNESS GATE BLOCKED {login} | NO DD DECISION","CRITICAL")
        return None
    return info

def authenticate(row, master_only=False):
    # V3.5: bad/stale credentials are isolated per account. They must never
    # hard-reset the shard terminal or delay valid traders.
    login=norm(row.get("mt5_login")); server=norm(row.get("mt5_server"))
    now=time.monotonic()
    if not master_only and now < auth_quarantine_until.get(login, 0.0):
        return None
    if not login.isdigit() or not server:
        log(f"AUTH PENDING MT5={login or 'UNKNOWN'} reason=missing_login_or_server | NO DD DECISION","WARNING")
        return None
    if master_only:
        candidates=[]
        for source,pwd in (("row-master",row_master(row)),("pool-master",pool_master(login))):
            if pwd and pwd not in [x[1] for x in candidates]: candidates.append((source,pwd))
    else:
        candidates=[]
        inv=row_investor(row)
        if inv: candidates.append(("row-investor",inv))
        pinv=pool_investor(login)
        if pinv and pinv not in [x[1] for x in candidates]: candidates.append(("pool-investor",pinv))
        rm=row_master(row)
        if rm and rm not in [x[1] for x in candidates]: candidates.append(("row-master-fallback",rm))
        pm=pool_master(login)
        if pm and pm not in [x[1] for x in candidates]: candidates.append(("pool-master-fallback",pm))
    first_error=None
    for source,pwd in candidates:
        ok=mt5.login(login=int(login),password=pwd,server=server)
        if not ok:
            if first_error is None: first_error=mt5.last_error()
            continue
        info=verified_live_info(login,server,pwd,row)
        if info is not None:
            auth_fail_count.pop(login, None)
            auth_quarantine_until.pop(login, None)
            return {"info":info,"password":pwd,"source":source,"server":server}
    if not master_only:
        failures=auth_fail_count.get(login,0)+1
        auth_fail_count[login]=failures
        cooldown=min(AUTH_QUARANTINE_BASE_SECONDS*(2**min(failures-1,3)), AUTH_QUARANTINE_MAX_SECONDS)
        auth_quarantine_until[login]=time.monotonic()+cooldown
        log(f"AUTH ISOLATED MT5={login} failures={failures} retry_in={cooldown:.0f}s | first={first_error} final={mt5.last_error()} | NO DD DECISION","WARNING")
    else:
        log(f"AUTH PENDING MT5={login} master_only=1 | first={first_error} final={mt5.last_error()} | NO DD DECISION","WARNING")
    return None

def verify_master_close_readiness(row, force=False):
    """Credential-only safety check. NEVER sends, changes, cancels or closes an order."""
    login=norm(row.get("mt5_login")); server=norm(row.get("mt5_server"))
    if not login.isdigit() or not server:
        close_ready.discard(login); close_blocked.add(login)
        last_master_check[login]=time.monotonic()
        log(f"CLOSE READINESS BLOCKED MT5={login or 'UNKNOWN'} reason=missing_login_or_server","CRITICAL")
        return False

    now=time.monotonic()
    if not force and now-last_master_check.get(login,0.0) < MASTER_READINESS_TTL:
        return login in close_ready

    candidates=[]
    for source,pwd in (("row-master",row_master(row)),("pool-master",pool_master(login))):
        if pwd and pwd not in [x[1] for x in candidates]:
            candidates.append((source,pwd))

    last_master_check[login]=now
    if not candidates:
        close_ready.discard(login); close_blocked.add(login)
        log(f"CLOSE READINESS BLOCKED MT5={login} reason=no_master_credential","CRITICAL")
        return False

    for source,pwd in candidates:
        try:
            ok=mt5.login(login=int(login),password=pwd,server=server)
        except Exception:
            ok=False
        if not ok:
            continue
        info=exact_account_info(login)
        if info is not None:
            if not execution_environment_ready(login):
                close_ready.discard(login); close_blocked.add(login)
                log(f"CLOSE READINESS BLOCKED MT5={login} reason=execution_environment","CRITICAL")
                return False
            close_blocked.discard(login); close_ready.add(login)
            log(f"CLOSE READY MT5={login} source={source}")
            return True

    close_ready.discard(login); close_blocked.add(login)
    log(f"CLOSE READINESS BLOCKED MT5={login} reason=master_auth_failed error={mt5.last_error()}","CRITICAL")
    return False

def dd_rule(row):
    if row.get("dd_authority_present") is False:
        return False,0.0,"feed.authority_missing"
    for k in ("dd_limit_percent","max_drawdown","max_drawdown_percent","total_dd","static_dd_limit_percent"):
        raw=row.get(k)
        if raw is None or norm(raw)=="": continue
        v=fnum(raw,0)
        if v>0: return True,v,norm(row.get("dd_authority_source")) or k
    return False,0.0,"authority_missing"

def starting_capital(row,current_balance):
    # Exact proven Operations field precedence.
    for k in ("account_size","initial_balance","starting_balance","challenge_balance","original_balance"):
        v=fnum(row.get(k),0)
        if v>0: return v
    v=fnum(row.get("balance"),0)
    return v if v>0 else float(current_balance or 0)

def position_evidence():
    ps=mt5.positions_get()
    if ps is None: return None
    out=[]
    for p in ps:
        out.append({
            "ticket":str(p.ticket),"symbol":p.symbol,
            "side":"BUY" if p.type==mt5.POSITION_TYPE_BUY else "SELL",
            "volume":float(p.volume),"open_price":float(p.price_open),
            "current_price":float(p.price_current),"profit":float(p.profit or 0)
        })
    return out

def send_snapshot(payload):
    try:
        r=requests.post(SNAPSHOT_ENDPOINT,json=payload,timeout=45)
        return r.status_code in (200,201)
    except Exception as e:
        log(f"Snapshot failed: {e}","ERROR"); return False


# V3.20 CLEAN LIVE-STATE DASHBOARD TELEMETRY — transport only.
# DD inspection, DD calculation, breach thresholds, close logic, shard ownership,
# scheduling and durable breach persistence are unchanged.
#
# Law: dashboard telemetry is disposable state, not a financial event queue.
# A failed/slow HTTP observation is NEVER retried. The next valid MT5 inspection
# publishes a newer observation and replaces any older unsent observation for the
# same MT5. This prevents minutes/hours-old balance/equity from reaching Render.
_dashboard_feed_lock = threading.Lock()
_dashboard_feed_latest = {}
_dashboard_feed_wakeup = threading.Event()
_DASHBOARD_CONNECT_TIMEOUT = 1.0
_DASHBOARD_READ_TIMEOUT = 3.0
_DASHBOARD_MAX_OBSERVATION_AGE_SECONDS = 30.0
_DASHBOARD_WORKERS = 4

def _dashboard_payload_age_seconds(payload):
    try:
        raw = str((payload or {}).get("timestamp") or "").strip()
        if not raw:
            return 10**9
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return 10**9

def _dashboard_take_latest():
    with _dashboard_feed_lock:
        if not _dashboard_feed_latest:
            _dashboard_feed_wakeup.clear()
            return None, None
        # Pop one account. publish_dashboard_snapshot_nonblocking() always overwrites
        # this map, therefore an unsent older observation can never survive a newer one.
        login = next(iter(_dashboard_feed_latest))
        payload = _dashboard_feed_latest.pop(login, None)
        if not _dashboard_feed_latest:
            _dashboard_feed_wakeup.clear()
        return login, payload

def _dashboard_feed_worker():
    while True:
        _dashboard_feed_wakeup.wait(timeout=0.25)
        login, payload = _dashboard_take_latest()
        if not login or not isinstance(payload, dict):
            time.sleep(0.05)
            continue

        # Never send an observation that became stale while waiting locally.
        age = _dashboard_payload_age_seconds(payload)
        if age > _DASHBOARD_MAX_OBSERVATION_AGE_SECONDS:
            log(f"DASHBOARD FEED DROP STALE account={login} age={age:.1f}s | newest MT5 observation will replace it | DD POLICING UNAFFECTED", "WARNING")
            continue

        try:
            outbound = dict(payload)
            outbound["source"] = "dd_police_live_mt5_v320"
            outbound["observed_at"] = outbound.get("timestamp") or now_iso()
            if not LIVE_STATE_ENDPOINT or not LIVE_STATE_KEY:
                log(f"DASHBOARD FEED DROP account={login}: clean live-state env missing | DD POLICING UNAFFECTED", "WARNING")
                continue
            r = requests.post(
                LIVE_STATE_ENDPOINT, json=outbound,
                headers={"X-NP-Live-Key": LIVE_STATE_KEY},
                timeout=(_DASHBOARD_CONNECT_TIMEOUT, _DASHBOARD_READ_TIMEOUT),
            )
            if r.status_code not in (200, 201):
                # NO RETRY. Dashboard state is latest-only. Retrying old state is the
                # defect V3.19 removes; a future valid MT5 read will publish again.
                log(f"DASHBOARD FEED DROP account={login} HTTP={r.status_code}; no stale retry | DD POLICING UNAFFECTED", "WARNING")
        except Exception as e:
            # NO RETRY for the same reason. This also prevents a slow Render endpoint
            # from building a hidden backlog behind DD policing.
            log(f"DASHBOARD FEED DROP account={login}: {e}; no stale retry | DD POLICING UNAFFECTED", "WARNING")

def publish_dashboard_snapshot_nonblocking(payload):
    login = str((payload or {}).get("mt5_login") or "").strip()
    if not login:
        return False
    # Constant-time latest-only handoff: newest observation wins per MT5.
    with _dashboard_feed_lock:
        _dashboard_feed_latest[login] = dict(payload)
        _dashboard_feed_wakeup.set()
    return True

for _np_dash_worker_i in range(_DASHBOARD_WORKERS):
    threading.Thread(
        target=_dashboard_feed_worker,
        name=f"np-dashboard-feed-v320-{_np_dash_worker_i+1}",
        daemon=True,
    ).start()

def disable_access(row,login,reason,evidence):
    aid=row.get("trader_account_id") or row.get("current_account_id")
    payload={"trader_id":row.get("id"),"trader_account_id":aid,"current_account_id":aid,
             "mt5_login":str(login),"reason":reason,"status":"breached",**evidence}
    try:
        r=requests.post(DISABLE_ENDPOINT,json=payload,timeout=45)
        if r.status_code in (200,201):
            log(f"BREACH PERSISTED MT5={login}","WARNING"); return True
        log(f"Breach persistence HTTP {r.status_code}: {(r.text or '')[:180]}","ERROR")
    except Exception as e: log(f"Breach persistence error {login}: {e}","ERROR")
    return False

def execution_environment_ready(login):
    """Fail-fast diagnostic for broker close execution. Does not change terminal settings."""
    try:
        ti=mt5.terminal_info()
        ai=mt5.account_info()
    except Exception as e:
        log(f"EXECUTION READINESS UNKNOWN MT5={login}: {e}","CRITICAL")
        return False
    if ti is None or ai is None:
        log(f"EXECUTION READINESS UNKNOWN MT5={login}: terminal/account info unavailable","CRITICAL")
        return False
    terminal_allowed=getattr(ti,"trade_allowed",None)
    api_disabled=getattr(ti,"tradeapi_disabled",None)
    account_allowed=getattr(ai,"trade_allowed",None)
    account_expert=getattr(ai,"trade_expert",None)
    blocked=(terminal_allowed is False) or (api_disabled is True) or (account_allowed is False)
    if blocked:
        log(f"EXECUTION BLOCKED MT5={login} terminal_trade_allowed={terminal_allowed} "
            f"tradeapi_disabled={api_disabled} account_trade_allowed={account_allowed} "
            f"account_trade_expert={account_expert} | emergency close cannot be guaranteed", "CRITICAL")
        return False
    return True

def close_positions_and_orders(login,row):
    a=authenticate(row,master_only=True)
    if not a:
        log(f"BREACH CLOSE BLOCKED MT5={login}: master authentication failed","CRITICAL")
        return False
    if exact_account_info(login) is None:
        log(f"BREACH CLOSE ABORT MT5={login}: exact-login mismatch","CRITICAL"); return False
    if not execution_environment_ready(login):
        close_ready.discard(login); close_blocked.add(login)
        return False

    # Cancel pending orders.
    orders=mt5.orders_get()
    if orders is None: return False
    for o in orders:
        res=mt5.order_send({"action":mt5.TRADE_ACTION_REMOVE,"order":int(o.ticket),
                            "magic":909090,"comment":"NairaPips DD breach"})
        if res is None or res.retcode!=mt5.TRADE_RETCODE_DONE:
            log(f"Pending cancel failed MT5={login} ticket={o.ticket}","ERROR")

    # Close positions, using symbol-supported filling modes rather than one blind mode.
    ps=mt5.positions_get()
    if ps is None: return False
    for p in ps:
        if not mt5.symbol_select(p.symbol,True):
            log(f"Symbol select failed {p.symbol} ticket={p.ticket}","ERROR"); continue
        tick=mt5.symbol_info_tick(p.symbol)
        if tick is None: continue
        typ=mt5.ORDER_TYPE_SELL if p.type==mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price=tick.bid if p.type==mt5.POSITION_TYPE_BUY else tick.ask
        base={"action":mt5.TRADE_ACTION_DEAL,"symbol":p.symbol,"volume":float(p.volume),
              "type":typ,"position":int(p.ticket),"price":price,"deviation":500,
              "magic":909090,"type_time":mt5.ORDER_TIME_GTC,"comment":"NairaPips DD breach"}
        done=False
        close_errors=[]
        for fill in (mt5.ORDER_FILLING_IOC,mt5.ORDER_FILLING_FOK,mt5.ORDER_FILLING_RETURN):
            # Refresh price immediately before every broker close attempt.
            live_tick=mt5.symbol_info_tick(p.symbol)
            if live_tick is None:
                close_errors.append(f"fill={fill}:no_tick")
                continue
            req=dict(base)
            req["price"]=live_tick.bid if p.type==mt5.POSITION_TYPE_BUY else live_tick.ask
            req["type_filling"]=fill
            res=mt5.order_send(req)
            if res is not None and res.retcode==mt5.TRADE_RETCODE_DONE:
                done=True
                log(f"BREACH CLOSED MT5={login} ticket={p.ticket} {p.symbol} fill={fill}","WARNING")
                break
            if res is None:
                close_errors.append(f"fill={fill}:none last_error={mt5.last_error()}")
            else:
                close_errors.append(
                    f"fill={fill}:retcode={getattr(res,'retcode',None)} "
                    f"comment={getattr(res,'comment','')} price={req['price']}"
                )
        if not done:
            log(f"BREACH CLOSE FAILED MT5={login} ticket={p.ticket} symbol={p.symbol} | "
                + " | ".join(close_errors),"CRITICAL")

    for _ in range(CLOSE_VERIFY_PASSES):
        ps=mt5.positions_get(); os_=mt5.orders_get()
        if ps is not None and os_ is not None and len(ps)==0 and len(os_)==0:
            log(f"ZERO EXPOSURE VERIFIED MT5={login}","WARNING"); return True
        time.sleep(CLOSE_RETRY_SECONDS)
    log(f"ZERO EXPOSURE NOT VERIFIED MT5={login}","CRITICAL")
    return False

from package_dd_runtime import PackageDDJournal, PackageDDUnavailable
_package_dd_journal = None

def inspect(row):
    global _package_dd_journal
    package_account=bool(row.get("package_contract_id") or row.get("package_rules_hash"))
    package_dd_result=None
    login=norm(row.get("mt5_login"))
    ok,limit,rule_source=dd_rule(row)
    if not ok:
        log(f"DD AUTHORITY MISSING MT5={login} | NO DD DECISION","ERROR")
        return IDLE_SECONDS

    # V3.6 coverage law: one broken credential must not keep the shard in "initial coverage" forever.
    # Record that this owned account has received its first attempt. Authentication failures
    # remain fail-closed for that account, but no longer outrank already-verified risky traders.
    first_attempted.add(login)
    last_attempted_at[login]=time.monotonic()
    a=authenticate(row)
    if not a:
        quarantine_left=max(auth_quarantine_until.get(login,0.0)-time.monotonic(),0.0)
        return max(AUTH_RETRY_SECONDS, quarantine_left)

    first=login not in verified
    verified.add(login); last_ok[login]=time.monotonic()
    info=a["info"]
    bal=float(info.balance or 0); eq=float(info.equity or 0)
    start=starting_capital(row,bal)
    if start<=0 or (not package_account and (bal<=0 or eq<=0)):
        invalid_live_capital[login]=f"start={start} balance={bal} equity={eq}"
        log(f"INVALID LIVE CAPITAL MT5={login} start={start} balance={bal} equity={eq} | NO DD DECISION","ERROR")
        return AUTH_RETRY_SECONDS
    invalid_live_capital.pop(login,None)

    positions=position_evidence()
    if positions is None:
        log(f"POSITION READ FAILED MT5={login} | NO DD DECISION","ERROR")
        return AUTH_RETRY_SECONDS

    # V3.13 true coverage timestamp: only a complete, usable live DD observation
    # counts as protected coverage.
    last_valid_dd_read[login]=time.monotonic()
    last_inspected[login]=last_valid_dd_read[login]

    if package_account:
        try:
            if _package_dd_journal is None:
                _package_dd_journal=PackageDDJournal(os.getenv("NP_PACKAGE_DD_JOURNAL_DIR",os.path.join(os.path.dirname(os.path.abspath(__file__)),"package_dd_state")))
            package_dd_result=_package_dd_journal.assess(row,bal,eq,now_iso())
            start=package_dd_result["capital"];limit=package_dd_result["limit"]
            risk=eq;dd=package_dd_result["drawdown_percent"];used=package_dd_result["used_percent"]
            breach_level=package_dd_result["breach_equity_level"];rule_source="immutable_package_contract"
        except PackageDDUnavailable:
            log(f"PACKAGE DD STATE UNAVAILABLE MT5={login}; no default rule or watermark reset is permitted","CRITICAL")
            return AUTH_RETRY_SECONDS
    else:
        risk=min(bal,eq)
        dd=max(((start-risk)/start)*100.0,0.0)
        used=(dd/limit)*100.0 if limit>0 else 0.0
        breach_level=start*(1-limit/100.0)

    # V3.17: publish the same already-computed valid MT5 observation asynchronously.
    _account_id = row.get("trader_account_id") or row.get("current_account_id")
    if _account_id:
        publish_dashboard_snapshot_nonblocking({
            "timestamp": now_iso(),
            "trader_id": row.get("trader_id") or row.get("id"),
            "trader_account_id": _account_id,
            "current_account_id": _account_id,
            "mt5_login": str(login),
            "open_positions":len(positions), "position_count":len(positions),
            "balance": bal, "current_balance": bal, "mt5_balance": bal,
            "equity": eq, "current_equity": eq, "mt5_equity": eq,
            "start_balance": start, "account_size": start,
            "drawdown": round(dd, 4),
            "drawdown_percent": round(dd, 4),
            "actual_drawdown_percent": round(dd, 4),
            "current_drawdown_percent": round(dd, 4),
            "dd_used_percent": round(used, 2),
            "max_drawdown_used": round(used, 2),
            "static_dd_limit_percent": limit,
            "dd_limit_percent": limit,
            "breach_equity_level": round(breach_level, 2),
            "phase_label": row.get("phase") or row.get("stage") or "",
            "dd_authority_source": rule_source,
            "breached": False, "zone": "live", "status": "active",
        })

    # Scheduling evidence only. Business DD calculation above remains unchanged.
    if dd>=limit or used>=90:
        risk_priority[login]=4
    elif used>=75:
        risk_priority[login]=3
    elif used>=50:
        risk_priority[login]=2
    elif positions:
        risk_priority[login]=1
    else:
        risk_priority[login]=0

    if first:
        log(f"ACCOUNT VERIFIED MT5={login} source={a['source']} | coverage={len(verified)}/{len(rows)}")
    if positions or used>=50:
        log(f"DD WATCH MT5={login} positions={len(positions)} balance={bal:.2f} equity={eq:.2f} "
            f"DD={dd:.3f}%/{limit:.3f}% used={used:.1f}%")

    # Exact unrounded breach authority. Never use display rounding to decide.
    breached_now = (risk <= breach_level) or bool(package_dd_result and package_dd_result["breached"])

    # PURE POLICING LAW:
    # Once this MT5 touches/crosses the DD limit, it stays breached forever
    # for this lifecycle even if equity later recovers.
    if breached_now or login in latched:
        if login not in latched:
            evidence={
                "timestamp":now_iso(),"balance":bal,"equity":eq,"mt5_balance":bal,
                "current_balance":bal,"start_balance":start,"account_size":start,
                "drawdown":round(dd,4),"drawdown_percent":round(dd,4),
                "actual_drawdown_percent":round(dd,4),"current_drawdown_percent":round(dd,4),
                "dd_used_percent":round(used,2),"max_drawdown_used":round(used,2),
                "static_dd_limit_percent":limit,"dd_limit_percent":limit,
                "breach_equity_level":round(breach_level,2),"breach_evidence_value":risk,
                "breached_by_balance":False if package_account else bal<=breach_level,"breached_by_equity":eq<=breach_level,
                "breach_source":"equity" if package_account else ("balance" if bal<=breach_level else "equity"),
                "breached":True,"zone":"breached","status":"breached",
                "phase_label":row.get("phase") or row.get("stage") or "",
                "dd_authority_source":rule_source,
                "open_positions_at_breach":positions,
            }
            if package_dd_result and package_dd_result["first_breach"]:
                first=package_dd_result["first_breach"]
                evidence.update(first)
                evidence.update(balance=float(first["balance"]),equity=float(first["equity"]),current_balance=float(first["balance"]),current_equity=float(first["equity"]),mt5_balance=float(first["balance"]),mt5_equity=float(first["equity"]),actual_drawdown_percent=first["drawdown_percent"],current_drawdown_percent=first["drawdown_percent"],drawdown=first["drawdown_percent"],max_drawdown_used=first["dd_used_percent"],breached_by_balance=False,breached_by_equity=True,breach_evidence_value=float(first["equity"]))
                if not package_dd_result["new_breach"]:
                    evidence.pop("open_positions_at_breach",None)
                    evidence["first_breach_positions_unavailable"]=True
                evidence.update(package_contract_id=row["package_contract_id"],package_rules_hash=row["package_rules_hash"],breached=True,breach_source="equity")
            evidence = _persist_first_breach_latch(login, row, evidence)
            log(
                f"BREACH LATCHED PERMANENTLY MT5={login} "
                f"risk={risk:.2f} breach_level={breach_level:.2f} "
                f"DD={dd:.4f}% limit={limit:.4f}% positions={len(positions)}",
                "CRITICAL",
            )
        else:
            evidence = latched[login]

        risk_priority[login]=4

        # Nothing outranks closing/keeping a breached account at zero exposure.
        # No snapshot HTTP. No lifecycle endpoint. No DB retry. No paperwork.
        closed=close_positions_and_orders(login,row)
        if not closed:
            # CRITICAL LAW: never tell backend to retire/remove this MT5 while
            # any position or pending order remains unverified.
            return HOT_SECONDS

        # Broker exposure is now proven zero. Only now may the separate relay
        # receive the breach event and perform lifecycle paperwork.
        _queue_breach_after_zero_exposure(login, evidence)

        log(
            f"BREACH POLICED MT5={login} | ZERO EXPOSURE VERIFIED | "
            f"first_breach={evidence.get('timestamp')} | RECOVERY CANNOT UNBREACH",
            "CRITICAL",
        )
        return HOT_SECONDS

    # Prove that this account can be switched to with a MASTER credential before a future breach.
    # This is credential-only: no trade request is sent. First verified pass checks immediately;
    # then it refreshes every MASTER_READINESS_TTL.
    if first or time.monotonic()-last_master_check.get(login,0.0) >= MASTER_READINESS_TTL:
        verify_master_close_readiness(row, force=True)

    if not positions: return IDLE_SECONDS
    if used>=90: return HOT_SECONDS
    if used>=75: return CRITICAL_SECONDS
    if used>=50: return WARNING_SECONDS
    return ACTIVE_SECONDS

def retry_backend():
    for login,(row,evidence) in list(backend_pending.items()):
        if str(login) in PERSISTENCE_QUARANTINE:
            # Broker/DD protection remains in inspect(); only broken persistence
            # paperwork is isolated until the exact DB row is repaired.
            continue
        if disable_access(row,login,"MAX_DRAWDOWN_BREACH",evidence):
            backend_pending.pop(login,None)

def health():
    global last_health
    now=time.monotonic()
    if now-last_health<HEALTH_SECONDS: return
    last_health=now

    active=critical=hot=0
    for login,row in rows.items():
        delay=max(next_due.get(login,0)-now,0)
        if delay<=ACTIVE_SECONDS: active+=1
        if delay<=CRITICAL_SECONDS: critical+=1
        if delay<=HOT_SECONDS: hot+=1

    population=set(rows.keys())
    ready=len(population & close_ready)
    blocked=len(population & close_blocked)
    pending=max(len(population)-ready-blocked,0)

    unverified=sorted(population-verified)
    invalid=sorted([x for x in population if x in invalid_live_capital])

    valid_ages={}
    for login in sorted(population):
        if login in last_valid_dd_read:
            valid_ages[login]=max(now-last_valid_dd_read[login],0.0)

    oldest_mt5=None
    oldest_valid=-1.0
    if valid_ages:
        oldest_mt5,oldest_valid=max(valid_ages.items(), key=lambda kv: kv[1])

    stale=sorted([
        login for login,age in valid_ages.items()
        if age>=STALE_HEALTH_SECONDS
    ], key=lambda x: valid_ages.get(x,0), reverse=True)

    never_valid=sorted([
        login for login in population
        if login in verified
        and login not in invalid_live_capital
        and login not in last_valid_dd_read
    ])

    log(f"HEALTH | SHARD={SHARD_ID}/{TOTAL_SHARDS} GLOBAL_POP={GLOBAL_POPULATION} OWNED={len(rows)} "
        f"MONITOR_READY={len(verified)}/{len(rows)} "
        f"MONITOR_UNVERIFIED={len(unverified)} "
        f"CLOSE_READY={ready}/{len(rows)} CLOSE_BLOCKED={blocked} CLOSE_PENDING={pending} "
        f"ACTIVE_DUE={active} CRITICAL_DUE={critical} HOT_DUE={hot} "
        f"OLDEST_VALID={oldest_valid:.1f}s OLDEST_MT5={oldest_mt5 or '-'} "
        f"STALE_COUNT={len(stale)} INVALID_CAPITAL_COUNT={len(invalid)} BACKEND_PENDING=0")

    if unverified:
        log(f"COVERAGE UNVERIFIED MT5={unverified}", "WARNING")
    if invalid:
        log(f"COVERAGE INVALID_CAPITAL MT5={invalid}", "ERROR")
    if never_valid:
        log(f"COVERAGE NEVER_VALID_DD MT5={never_valid}", "WARNING")
    if stale:
        detail=[f"{x}:{valid_ages.get(x,0):.1f}s" for x in stale[:20]]
        log(f"COVERAGE STALE MT5={detail}", "CRITICAL")

    # Local machine-readable health state for an independent fleet watchdog.
    try:
        root=_state_root()
        path=os.path.join(root, f"dd_health_shard_{SHARD_ID:02d}.json")
        tmp=path+".tmp"
        payload={
            "release": RELEASE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "shard": SHARD_ID,
            "total_shards": TOTAL_SHARDS,
            "global_population": GLOBAL_POPULATION,
            "owned": len(rows),
            "monitor_ready": len(verified),
            "unverified_mt5": unverified,
            "close_ready": ready,
            "close_blocked": blocked,
            "close_pending": pending,
            "oldest_valid_seconds": oldest_valid,
            "oldest_mt5": oldest_mt5,
            "stale_mt5": [{"mt5_login":x,"age_seconds":round(valid_ages.get(x,0),3)} for x in stale],
            "never_valid_dd_mt5": never_valid,
            "invalid_capital_mt5": [{"mt5_login":x,"evidence":invalid_live_capital.get(x)} for x in invalid],
        }
        with open(tmp,"w",encoding="utf-8") as fh:
            json.dump(payload,fh,separators=(",",":"))
            fh.flush()
            try: os.fsync(fh.fileno())
            except Exception: pass
        os.replace(tmp,path)
    except Exception as e:
        log(f"HEALTH SNAPSHOT WRITE FAILED: {e}","WARNING")

def main():
    configure_worker()
    _load_latches()
    log(f"Starting {RELEASE}")
    log("ROLE=PURE DD POLICING ONLY | no backend paperwork in shard hot path","WARNING")
    log(f"SHARD={SHARD_ID}/{TOTAL_SHARDS} | deterministic rendezvous ownership","WARNING")
    log(f"DEDICATED TERMINAL={MT5_PATH}","WARNING")
    log("V3.14 PURE DD: GLOBAL coverage rescue across all owned healthy accounts; max one rescue turn before yielding to due risk lane; exact stale/unverified/invalid-capital health; durable latch; relay only after zero exposure; DD hot path network-free", "WARNING")
    refresh_population(True); refresh_pool(True)
    if not initialize_mt5():
        return

    # V3.12 bounded-priority scheduler state.
    risk_lane_streak = 0
    coverage_rescue_streak = 0

    try:
        while True:
            refresh_population(); refresh_pool(); health()
            now=time.monotonic()
            ordinary_due=[login for login in rows if next_due.get(login,0)<=now]

            # V3.14 GLOBAL COVERAGE RESCUE
            # ----------------------------
            # Coverage rescue is computed from ALL owned verified healthy accounts,
            # not merely from accounts whose ordinary next_due has already expired.
            # This removes the last path by which an account could remain buried
            # behind a permanently busy risk lane.
            coverage_overdue = []
            for x in rows:
                if x not in verified or x in invalid_live_capital:
                    continue
                last_good=last_valid_dd_read.get(x)
                age=(now-last_good) if last_good is not None else float("inf")
                if age>=MAX_VALID_REVISIT_SECONDS:
                    coverage_overdue.append(x)

            coverage_overdue.sort(
                key=lambda x: last_valid_dd_read.get(x,0.0)
            )

            if not ordinary_due and not coverage_overdue:
                time.sleep(.01); continue

            # First-attempt discovery stays highest priority until every newly
            # owned account has at least been attempted once.
            first_due=[
                x for x in ordinary_due
                if x not in first_attempted
            ]

            # Risk lane due now.
            risk_due=[
                x for x in ordinary_due
                if x in verified and risk_priority.get(x,0)>=2
            ]
            risk_due.sort(
                key=lambda x: (-risk_priority.get(x,0), next_due.get(x,0))
            )

            # Hard global rescue turn. One forced stale account is serviced, then
            # the risk lane gets a turn if one is due. This bounds burial without
            # letting rescue backlog suppress breach-near accounts indefinitely.
            force_coverage_rescue = bool(
                coverage_overdue
                and not first_due
                and (
                    coverage_rescue_streak < MAX_COVERAGE_RESCUE_STREAK
                    or not risk_due
                )
            )

            if first_due:
                first_due.sort(key=lambda x: next_due.get(x,0))
                login=first_due[0]
                selected_lane="first_attempt"
            elif force_coverage_rescue:
                login=coverage_overdue[0]
                selected_lane="coverage_rescue"
            else:
                # V3.12 bounded risk priority underneath global rescue.
                normal_due = [
                    x for x in ordinary_due
                    if x in verified and risk_priority.get(x,0) < 2
                ]
                force_normal_turn = bool(
                    normal_due and risk_lane_streak >= MAX_CONSECUTIVE_RISK_TURNS
                )

                def sched_key(x):
                    if x in verified:
                        pri=risk_priority.get(x,0)
                        if force_normal_turn and pri < 2:
                            return (0, 0, next_due.get(x,0))
                        if pri >= 2:
                            return (1, -pri, next_due.get(x,0))
                        return (2, 0, next_due.get(x,0))
                    return (3, 0, max(next_due.get(x,0), auth_quarantine_until.get(x,0.0)))

                ordinary_due.sort(key=sched_key)
                login=ordinary_due[0]
                selected_lane="risk_or_normal"

            row=rows.get(login)
            if not row:
                next_due.pop(login,None); risk_priority.pop(login,None); hot_burst_remaining.pop(login,None); continue
            try:
                delay=inspect(row)
            except Exception as e:
                log(f"ACCOUNT ISOLATED MT5={login}: {type(e).__name__}: {e} | NO DD DECISION","ERROR")
                delay=AUTH_RETRY_SECONDS

            pri=risk_priority.get(login,0)

            if selected_lane=="coverage_rescue":
                coverage_rescue_streak += 1
            else:
                coverage_rescue_streak = 0

            if pri >= 2:
                risk_lane_streak += 1
            else:
                risk_lane_streak = 0

            # V3.1 FAIR BURST: initialise a burst only when none exists.
            # The previous max(current, N) logic refilled the burst on every pass,
            # allowing one HOT account to monopolise a shard indefinitely.
            if pri>=2 and login not in hot_burst_remaining:
                if pri>=4:
                    hot_burst_remaining[login]=HOT_BURST_PASSES
                elif pri==3:
                    hot_burst_remaining[login]=CRITICAL_BURST_PASSES
                else:
                    hot_burst_remaining[login]=WARNING_BURST_PASSES

            burst=hot_burst_remaining.get(login,0)
            coverage_complete = all(x in first_attempted for x in rows)
            if burst>0 and pri>=2 and coverage_complete and str(login) not in PERSISTENCE_QUARANTINE:
                hot_burst_remaining[login]=burst-1
                next_due[login]=time.monotonic()+.05
            else:
                hot_burst_remaining.pop(login,None)
                next_due[login]=time.monotonic()+max(float(delay),.05)
    except KeyboardInterrupt:
        log("DD Police stopped by operator.")
    finally:
        mt5.shutdown()

if __name__=="__main__":
    main()
