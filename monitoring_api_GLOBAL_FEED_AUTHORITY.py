from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client
from datetime import datetime, timezone
import os, re

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
MAIN_API_URL = os.getenv("NAIRAPIPS_MAIN_API_URL", "https://nairapips-api.onrender.com").rstrip("/")
MAX_DD_PERCENT = float(os.getenv("NAIRAPIPS_MAX_DD_PERCENT", "20"))
MONITORABLE_LIMIT = int(os.getenv("NAIRAPIPS_MONITORABLE_LIMIT", "1000"))

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ok(data=None, message="ok", status=200):
    res = jsonify({"success": True, "message": message, "data": data})
    res.status_code = status
    return res


def bad(message, status=400):
    res = jsonify({"success": False, "error": str(message)})
    res.status_code = status
    return res


def num(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(str(v).replace("₦", "").replace(",", "").strip())
    except Exception:
        return default


def clean_login(v):
    return str(v or "").strip()


def valid_login(v):
    v = clean_login(v)
    return bool(v and v.isdigit() and not any(x in v.upper() for x in ["NEW", "LOGIN", "NONE", "NULL"]))


ACTIVE_ACCOUNT_STATUSES = {"assigned_active", "active", "current_active", "phase1_active", "phase2_active", "funded_active", "live_active", "live", "funded", "approved_active"}
TERMINAL_ACCOUNT_WORDS = ("archived", "breached", "closed", "locked", "disabled", "passed", "reset")
PURCHASE_BLOCK_WORDS = ("waiting", "reset", "archived", "breached", "disabled", "closed", "cancelled", "canceled", "rejected", "passed_review")
POOL_ACTIVE_STATUSES = {"assigned", "active", "in_use", "used", "allocated", "assigned_active"}
ACCOUNT_ORIGIN_FIELDS = ("account_origin", "source_type", "programme_type", "campaign_id", "grant_id", "referral_reward_id", "competition_id")
NO_PURCHASE_AUDIT_KEYS = set()


def is_active_monitoring_account(row):
    status = str((row or {}).get("account_status") or (row or {}).get("status") or "").strip().lower()
    if not row or status not in ACTIVE_ACCOUNT_STATUSES:
        return False
    if any(word in status for word in TERMINAL_ACCOUNT_WORDS):
        return False
    if (row or {}).get("archived_at") or (row or {}).get("reset_at"):
        return False
    if str((row or {}).get("mt5_access_disabled") or "").lower() in {"true", "1", "yes"}:
        return False
    return valid_login((row or {}).get("mt5_login"))


def bool_false(value):
    return str(value).strip().lower() in {"false", "0", "no", "off"}


def bool_true(value):
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def lifecycle_blob(row, keys):
    return " ".join(str((row or {}).get(k) or "").strip().lower() for k in keys)


def account_origin(account):
    return {key: (account or {}).get(key) for key in ACCOUNT_ORIGIN_FIELDS if (account or {}).get(key) not in (None, "")}


def log_lifecycle_inconsistency(reason, account=None, purchase=None, mt5_pool=None, trader=None):
    evidence = {
        "reason": reason,
        "trader_id": (account or {}).get("trader_id") or (purchase or {}).get("trader_id") or (trader or {}).get("id"),
        "trader_account_id": (account or {}).get("id") or (purchase or {}).get("trader_account_id"),
        "purchase_id": (account or {}).get("purchase_id") or (purchase or {}).get("id"),
        "mt5_login": clean_login((account or {}).get("mt5_login") or (purchase or {}).get("mt5_login")),
        "account_status": (account or {}).get("account_status"),
        "purchase_status": (purchase or {}).get("status"),
        "purchase_lifecycle_state": (purchase or {}).get("lifecycle_state"),
        "pool_status": (mt5_pool or {}).get("status"),
        "trader_state": (trader or {}).get("challenge_state") or (trader or {}).get("status"),
        "account_origin": account_origin(account),
    }
    print("MONITORING LIFECYCLE INCONSISTENCY:", evidence, flush=True)
    try:
        safe_insert("monitoring_events", {
            "trader_id": evidence["trader_id"],
            "trader_account_id": evidence["trader_account_id"],
            "mt5_login": evidence["mt5_login"],
            "event_type": "lifecycle_inconsistency",
            "risk_zone": "investigate",
            "message": reason,
            "payload": evidence,
            "created_at": now_iso(),
        })
    except Exception:
        pass


def log_no_purchase_monitoring_allowed(account):
    key = str((account or {}).get("id") or "")
    if not key or key in NO_PURCHASE_AUDIT_KEYS:
        return
    NO_PURCHASE_AUDIT_KEYS.add(key)
    reason = "active account has no purchase_id; monitoring allowed from exact trader_account evidence"
    evidence = {
        "reason": reason,
        "trader_id": (account or {}).get("trader_id"),
        "trader_account_id": (account or {}).get("id"),
        "purchase_id": None,
        "mt5_login": clean_login((account or {}).get("mt5_login")),
        "account_status": (account or {}).get("account_status"),
        "account_origin": account_origin(account),
    }
    print("MONITORING NO-PURCHASE ACCOUNT ALLOWED:", evidence, flush=True)
    try:
        safe_insert("monitoring_events", {
            "trader_id": evidence["trader_id"],
            "trader_account_id": evidence["trader_account_id"],
            "mt5_login": evidence["mt5_login"],
            "event_type": "monitoring_allowed_no_purchase_id",
            "risk_zone": "audit",
            "message": reason,
            "payload": evidence,
            "created_at": now_iso(),
        })
    except Exception:
        pass


def is_active_purchase_for_account(purchase, account):
    if not purchase:
        return False, "linked purchase not found"
    if str(purchase.get("id") or "") != str((account or {}).get("purchase_id") or ""):
        return False, "purchase_id mismatch"
    if str(purchase.get("trader_id") or "") != str((account or {}).get("trader_id") or ""):
        return False, "purchase trader_id mismatch"
    purchase_account_id = str(purchase.get("trader_account_id") or "").strip()
    if purchase_account_id and purchase_account_id != str((account or {}).get("id") or ""):
        return False, "purchase linked to a different trader_account_id"
    purchase_login = clean_login(purchase.get("mt5_login"))
    account_login = clean_login((account or {}).get("mt5_login"))
    if purchase_login and purchase_login != account_login:
        return False, "purchase mt5_login mismatch"
    purchase_pool_id = str(purchase.get("mt5_pool_id") or purchase.get("assigned_mt5_id") or "").strip()
    account_pool_id = str((account or {}).get("mt5_pool_id") or "").strip()
    if purchase_pool_id and account_pool_id and purchase_pool_id != account_pool_id:
        return False, "purchase mt5_pool_id mismatch"
    blob = lifecycle_blob(purchase, ["status", "payment_status", "lifecycle_state", "stage", "phase", "admin_note"])
    if any(word in blob for word in PURCHASE_BLOCK_WORDS):
        return False, "purchase lifecycle is not monitorable"
    return True, "purchase active"


def is_active_pool_for_account(mt5_pool, account):
    pool_id = str((account or {}).get("mt5_pool_id") or "").strip()
    if not pool_id:
        return True, "no mt5_pool_id on account"
    if not mt5_pool:
        return False, "linked mt5_pool row not found"
    status = str(mt5_pool.get("status") or "").strip().lower()
    if any(word in status for word in TERMINAL_ACCOUNT_WORDS):
        return False, "mt5_pool is terminal"
    if status and status not in POOL_ACTIVE_STATUSES:
        return False, "mt5_pool status is not active"
    pool_account_id = str(mt5_pool.get("trader_account_id") or "").strip()
    if pool_account_id and pool_account_id != str((account or {}).get("id") or ""):
        return False, "mt5_pool linked to a different trader_account_id"
    pool_trader_id = str(mt5_pool.get("assigned_trader_id") or mt5_pool.get("trader_id") or "").strip()
    if pool_trader_id and pool_trader_id != str((account or {}).get("trader_id") or ""):
        return False, "mt5_pool linked to a different trader"
    pool_login = clean_login(mt5_pool.get("mt5_login"))
    account_login = clean_login((account or {}).get("mt5_login"))
    if pool_login and pool_login != account_login:
        return False, "mt5_pool mt5_login mismatch"
    return True, "mt5_pool active"


def monitoring_eligibility(account, purchase=None, mt5_pool=None, trader=None, require_server=True):
    if not is_active_monitoring_account(account):
        return False, "account is not monitorable"
    if require_server and not str((account or {}).get("mt5_server") or "").strip():
        return False, "account has no mt5_server"
    if bool_false((account or {}).get("monitoring_enabled")):
        return False, "account monitoring_enabled is false"
    if bool_true((account or {}).get("mt5_access_disabled")):
        return False, "account mt5_access_disabled is true"
    if (account or {}).get("superseded_at") or (account or {}).get("replaced_at") or bool_true((account or {}).get("superseded")):
        return False, "account is superseded"
    purchase_id = str((account or {}).get("purchase_id") or "").strip()
    if purchase_id:
        ok_purchase, reason = is_active_purchase_for_account(purchase, account)
        if not ok_purchase:
            return False, reason
    ok_pool, reason = is_active_pool_for_account(mt5_pool, account)
    if not ok_pool:
        return False, reason
    if trader:
        t_blob = lifecycle_blob(trader, ["challenge_state", "status", "phase"])
        if any(word in t_blob for word in ("waiting", "reset", "breached", "archived", "disabled", "closed", "passed_review")):
            log_lifecycle_inconsistency("trader-level lifecycle disagrees with eligible active account; account remains monitorable", account, purchase, mt5_pool, trader)
    if not purchase_id:
        log_no_purchase_monitoring_allowed(account)
    return True, "eligible"


def fetch_trader_by_id(trader_id):
    try:
        if not trader_id:
            return {}
        rows = supabase.table("traders").select("*").eq("id", trader_id).limit(1).execute().data or []
        return rows[0] if rows else {}
    except Exception as e:
        print("TRADER FETCH ERROR:", e)
        return {}


def fetch_purchase_by_id(purchase_id):
    try:
        if not purchase_id:
            return {}
        rows = supabase.table("challenge_purchases").select("*").eq("id", purchase_id).limit(1).execute().data or []
        return rows[0] if rows else {}
    except Exception as e:
        print("PURCHASE FETCH ERROR:", e)
        return {}


def fetch_pool_by_id(pool_id):
    try:
        if not pool_id:
            return {}
        rows = supabase.table("mt5_pool").select("*").eq("id", pool_id).limit(1).execute().data or []
        return rows[0] if rows else {}
    except Exception as e:
        print("MT5 POOL FETCH ERROR:", e)
        return {}


def account_is_eligible(account, caches=None, require_server=True):
    caches = caches if isinstance(caches, dict) else {}
    purchases = caches.setdefault("purchases", {})
    pools = caches.setdefault("pools", {})
    traders = caches.setdefault("traders", {})
    purchase_id = str((account or {}).get("purchase_id") or "").strip()
    pool_id = str((account or {}).get("mt5_pool_id") or "").strip()
    trader_id = str((account or {}).get("trader_id") or "").strip()
    if purchase_id and purchase_id not in purchases:
        purchases[purchase_id] = fetch_purchase_by_id(purchase_id)
    if pool_id and pool_id not in pools:
        pools[pool_id] = fetch_pool_by_id(pool_id)
    if trader_id and trader_id not in traders:
        traders[trader_id] = fetch_trader_by_id(trader_id)
    eligible, reason = monitoring_eligibility(
        account,
        purchases.get(purchase_id) or {},
        pools.get(pool_id) or {},
        traders.get(trader_id) or {},
        require_server=require_server,
    )
    if not eligible:
        log_lifecycle_inconsistency(reason, account, purchases.get(purchase_id) or {}, pools.get(pool_id) or {}, traders.get(trader_id) or {})
    return eligible, reason


def eligible_accounts_without_login_ambiguity(rows, context="monitoring"):
    caches = {}
    eligible_rows = []
    by_login = {}
    for row in rows or []:
        eligible, _reason = account_is_eligible(row, caches)
        if not eligible:
            continue
        login = clean_login(row.get("mt5_login"))
        by_login.setdefault(login, []).append(row)
    for login, group in by_login.items():
        if len(group) == 1:
            eligible_rows.append(group[0])
            continue
        for row in group:
            log_lifecycle_inconsistency(
                "mt5_login resolves to multiple eligible active accounts; exact trader_account_id required",
                row,
                caches.get("purchases", {}).get(str(row.get("purchase_id") or "").strip()) or {},
                caches.get("pools", {}).get(str(row.get("mt5_pool_id") or "").strip()) or {},
                caches.get("traders", {}).get(str(row.get("trader_id") or "").strip()) or {},
            )
            print(f"MONITORING {context.upper()} EXCLUDED AMBIGUOUS LOGIN:", {"mt5_login": login, "trader_account_id": row.get("id")}, flush=True)
    return eligible_rows


def target_for_stage(stage):
    stage = str(stage or "").strip().lower()
    if stage == "phase1":
        return 10.0
    if stage == "phase2":
        return 8.0
    return 0.0


def active_state(stage):
    return "funded_active" if str(stage).lower() == "funded" else f"{stage}_active"


def waiting_after_pass(stage):
    stage = str(stage or "").strip().lower()
    if stage == "phase1":
        return "phase2_waiting_mt5", "phase2"
    if stage == "phase2":
        return "funded_waiting_mt5", "funded"
    return "passed_review", stage or "phase1"


def risk_zone(current_dd_percent):
    d = num(current_dd_percent)
    if d >= 20:
        return "breached"
    if d >= 18:
        return "critical"
    if d >= 15:
        return "danger"
    if d >= 10:
        return "warning"
    return "safe"


def static_dd(start_balance, equity):
    start = num(start_balance)
    eq = num(equity)
    if start <= 0:
        return 0.0
    return round(max(((start - eq) / start) * 100, 0.0), 2)


def dd_used_from_static(dd_percent):
    if MAX_DD_PERCENT <= 0:
        return 0.0
    return round(max((num(dd_percent) / MAX_DD_PERCENT) * 100, 0.0), 2)


def fetch_traders_by_ids(ids):
    ids = [str(x) for x in ids if x]
    if not ids:
        return {}
    out = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i+100]
        try:
            rows = supabase.table("traders").select("*").in_("id", chunk).execute().data or []
            for r in rows:
                out[str(r.get("id"))] = r
        except Exception as e:
            print("TRADER BATCH FETCH ERROR:", e)
    return out


def get_account_by_id_or_login(account_id=None, mt5_login=None):
    caches = {}
    try:
        if account_id:
            rows = supabase.table("trader_accounts").select("*").eq("id", account_id).limit(1).execute().data or []
            account = rows[0] if rows else None
            if not account:
                return None
            login = clean_login(mt5_login)
            if login and clean_login(account.get("mt5_login")) != login:
                log_lifecycle_inconsistency("snapshot/trade supplied trader_account_id but mt5_login does not match", account)
                return None
            eligible, _reason = account_is_eligible(account, caches)
            if eligible:
                return rows[0]
            return None
        login = clean_login(mt5_login)
        if login:
            rows = supabase.table("trader_accounts").select("*").eq("mt5_login", login).order("updated_at", desc=True).limit(10).execute().data or []
            eligible_rows = []
            for r in rows:
                eligible, _reason = account_is_eligible(r, caches)
                if eligible:
                    eligible_rows.append(r)
            if len(eligible_rows) == 1:
                return eligible_rows[0]
            if len(eligible_rows) > 1:
                for r in eligible_rows:
                    log_lifecycle_inconsistency("mt5_login resolves to multiple eligible active accounts; exact trader_account_id required", r)
                return None
    except Exception as e:
        print("ACCOUNT FETCH ERROR:", e)
    return None


def safe_insert(table, payload):
    try:
        return supabase.table(table).insert(payload).execute().data or []
    except Exception as e:
        print(f"SAFE INSERT FAILED {table}:", e)
        return []


def safe_update(table, payload, col, val):
    try:
        return supabase.table(table).update(payload).eq(col, val).execute().data or []
    except Exception as e:
        print(f"SAFE UPDATE FAILED {table}.{col}:", e)
        return []


def alert_once(account, event_type, title, message, severity="info", snapshot=None):
    """Create admin action evidence without depending on the main API."""
    account_id = account.get("id") if account else None
    trader_id = account.get("trader_id") if account else None
    key = f"{event_type}:{account_id or ''}:{clean_login((account or {}).get('mt5_login'))}"
    payload = {
        "trader_id": trader_id,
        "trader_account_id": account_id,
        "mt5_login": clean_login((account or {}).get("mt5_login")),
        "event_type": event_type,
        "alert_type": event_type,
        "title": title,
        "message": message,
        "severity": severity,
        "status": "unread",
        "dedupe_key": key,
        "payload": snapshot or {},
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    # Try common alert/event tables. Fail-safe: monitoring_events always records evidence.
    for table in ["monitoring_alerts", "account_alerts", "admin_alerts"]:
        try:
            # If a unique dedupe_key exists, upsert prevents alert spam. If not, insert may still work.
            supabase.table(table).upsert(payload, on_conflict="dedupe_key").execute()
            return True
        except Exception:
            try:
                supabase.table(table).insert(payload).execute()
                return True
            except Exception:
                pass
    return False


def apply_intelligence(account, snapshot):
    if not account:
        return None
    if not is_active_monitoring_account(account):
        print("MONITORING SNAPSHOT IGNORED FOR NON-ACTIVE ACCOUNT:", {"account_id": account.get("id"), "trader_id": account.get("trader_id"), "mt5_login": account.get("mt5_login"), "account_status": account.get("account_status")}, flush=True)
        return {"account_id": account.get("id"), "mt5_login": account.get("mt5_login"), "ignored": True, "reason": "account_not_active_for_monitoring"}

    start = num(account.get("start_balance") or account.get("account_size") or snapshot.get("balance") or 0)
    equity = num(snapshot.get("equity") or snapshot.get("current_equity") or account.get("current_equity") or start)
    stage = str(account.get("stage") or snapshot.get("phase_label") or "phase1").strip().lower()
    target = target_for_stage(stage)
    breach_level = round(start * (1 - MAX_DD_PERCENT / 100), 2) if start else 0.0

    old_high = num(account.get("highest_equity") or start)
    old_low = num(account.get("lowest_equity") or start)
    snap_high = num(snapshot.get("highest_equity") or 0)
    snap_low = num(snapshot.get("lowest_equity") or snapshot.get("recorded_lowest_equity") or 0)

    highest = round(max(start, equity, old_high, snap_high), 2)
    low_candidates = [x for x in [start, equity, old_low, snap_low] if x and x > 0]
    lowest = round(min(low_candidates), 2) if low_candidates else equity

    current_dd = static_dd(start, equity)
    current_dd_used = dd_used_from_static(current_dd)
    worst_dd = static_dd(start, lowest)
    worst_dd_used = dd_used_from_static(worst_dd)
    dd_remaining = round(max(MAX_DD_PERCENT - current_dd, 0), 2)
    zone = risk_zone(current_dd)

    profit = round(highest - start, 2) if start else 0.0
    profit_percent = round((profit / start) * 100, 2) if start else 0.0
    current_profit = round(equity - start, 2) if start else 0.0
    current_profit_percent = round((current_profit / start) * 100, 2) if start else 0.0
    target_equity = round(start * (1 + target / 100), 2) if target else 0.0
    pass_progress = round(max(0, profit_percent / target * 100), 2) if target else 0.0

    target_hit = bool(target and highest >= target_equity and profit_percent >= target)
    breached = bool((not target_hit) and start and equity <= breach_level)

    status = str(account.get("account_status") or "assigned_active").lower()
    phase_pass_status = ""
    lifecycle_state = None
    next_phase = stage

    if target_hit:
        zone = "passed"
        phase_pass_status = f"{stage}_passed"
        status = f"archived_{stage}" if stage in {"phase1", "phase2"} else "passed"
        lifecycle_state, next_phase = waiting_after_pass(stage)
    elif breached:
        zone = "breached"
        status = "breached_archived"
        lifecycle_state = "breached"
        next_phase = stage

    update = {
        "current_balance": num(snapshot.get("balance") or account.get("current_balance") or start),
        "current_equity": equity,
        "profit": profit,
        "profit_percent": profit_percent,
        "current_profit": current_profit,
        "current_profit_percent": current_profit_percent,
        "highest_equity": highest,
        "lowest_equity": lowest,
        "absolute_drawdown_percent": current_dd,
        "drawdown_percent": current_dd,
        "dd_used_percent": current_dd_used,
        "max_drawdown_used": current_dd_used,
        "worst_static_drawdown_percent": worst_dd,
        "worst_dd_used_percent": worst_dd_used,
        "dd_remaining_percent": dd_remaining,
        "breach_equity_level": breach_level,
        "target_percent": target,
        "target_equity": target_equity,
        "pass_progress_percent": pass_progress,
        "risk_zone": zone,
        "phase_pass_status": phase_pass_status or account.get("phase_pass_status") or "",
        "last_sync_at": snapshot.get("timestamp") or now_iso(),
        "updated_at": now_iso(),
    }
    if target_hit or breached:
        update["account_status"] = status
        update["monitoring_enabled"] = False
        update["archived_at"] = now_iso()
        update["archive_reason"] = snapshot.get("reason") or ("Target reached" if target_hit else "Static drawdown breached")
        if target_hit:
            update["passed_at"] = now_iso()

    safe_update("trader_accounts", update, "id", account.get("id"))

    trader_update = {
        "equity": equity,
        "balance": start,
        "profit": profit,
        "profit_percent": profit_percent,
        "drawdown_percent": current_dd,
        "max_drawdown_used": current_dd_used,
        "updated_at": now_iso(),
    }
    if target_hit or breached:
        trader_update.update({
            "challenge_state": lifecycle_state,
            "phase": next_phase,
            "status": "active" if target_hit else "breached",
            "mt5_access_disabled": True,
            "monitoring_enabled": False,
            "phase_pass_status": phase_pass_status,
            "lifecycle_updated_at": now_iso(),
        })
    safe_update("traders", trader_update, "id", account.get("trader_id"))

    event = {
        "trader_id": account.get("trader_id"),
        "trader_account_id": account.get("id"),
        "mt5_login": clean_login(account.get("mt5_login") or snapshot.get("mt5_login")),
        "event_type": "phase_passed" if target_hit else ("breached" if breached else "snapshot"),
        "risk_zone": zone,
        "phase_label": stage,
        "phase_pass_status": phase_pass_status,
        "balance": start,
        "equity": equity,
        "profit": profit,
        "profit_percent": profit_percent,
        "current_profit": current_profit,
        "current_profit_percent": current_profit_percent,
        "drawdown_percent": current_dd,
        "dd_used_percent": current_dd_used,
        "max_drawdown_used": current_dd_used,
        "worst_static_drawdown_percent": worst_dd,
        "worst_dd_used_percent": worst_dd_used,
        "highest_equity": highest,
        "lowest_equity": lowest,
        "breach_equity_level": breach_level,
        "target_percent": target,
        "target_equity": target_equity,
        "pass_progress_percent": pass_progress,
        "message": snapshot.get("reason") or "Monitoring snapshot applied",
        "created_at": now_iso(),
    }
    safe_insert("monitoring_events", event)
    try:
        snap = dict(event)
        snap["zone"] = zone
        snap["created_at"] = now_iso()
        safe_insert("monitoring_snapshots", snap)
    except Exception:
        pass

    if target_hit:
        alert_once(account, "phase_passed", f"{stage.upper()} PASSED", f"MT5 {account.get('mt5_login')} reached {target}% target. Awaiting next-stage MT5 assignment.", "success", event)
    elif breached:
        alert_once(account, "breached", "ACCOUNT BREACHED", f"MT5 {account.get('mt5_login')} equity {equity} hit/below breach level {breach_level}.", "critical", event)
    elif current_dd >= 10:
        alert_once(account, "dd_warning", "DRAWDOWN WARNING", f"MT5 {account.get('mt5_login')} static DD is {current_dd}%.", "warning", event)

    return {"account_id": account.get("id"), "mt5_login": account.get("mt5_login"), "zone": zone, "target_hit": target_hit, "breached": breached, "profit_percent": profit_percent, "current_dd": current_dd, "dd_used_percent": current_dd_used}


@app.route("/")
def home():
    return ok({"service": "NairaPips Monitoring API", "status": "live"})


@app.route("/health")
def health():
    return ok({"health": "ok", "service": "monitoring", "time": now_iso()})


@app.route("/monitorable_accounts")
def monitorable_accounts():
    """Fast endpoint for MT5 engine. One row per live MT5 account. No dashboard scans."""
    try:
        rows = (
            supabase.table("trader_accounts")
            .select("*")
            .in_("account_status", sorted(ACTIVE_ACCOUNT_STATUSES))
            .limit(MONITORABLE_LIMIT)
            .execute()
            .data
            or []
        )
        rows = [r for r in eligible_accounts_without_login_ambiguity(rows, "monitorable_accounts") if str(r.get("mt5_server") or "").strip()]
        traders = fetch_traders_by_ids([r.get("trader_id") for r in rows])
        out = []
        for a in rows:
            t = traders.get(str(a.get("trader_id")), {}) or {}
            out.append({
                # Account-scoped identity: one trader may legitimately own many
                # simultaneous active challenge accounts. Never use trader_id as
                # the row identity or downstream consumers may collapse them.
                "id": a.get("id"),
                "trader_id": a.get("trader_id"),
                "trader_account_id": a.get("id"),
                "current_account_id": a.get("id"),
                "name": t.get("name") or t.get("trader_name") or "Trader",
                "full_name": t.get("full_name") or t.get("name") or t.get("trader_name") or "Trader",
                "email": t.get("email") or a.get("email"),
                "phone": t.get("phone") or "",
                "phase": a.get("stage") or t.get("phase") or "phase1",
                "stage": a.get("stage") or t.get("phase") or "phase1",
                "status": "active",
                "account_status": a.get("account_status") or "assigned_active",
                "payment_status": "approved",
                "monitoring_enabled": True,
                "mt5_access_disabled": False,
                "mt5_login": clean_login(a.get("mt5_login")),
                "mt5_server": a.get("mt5_server") or "",
                "mt5_master_password": a.get("mt5_master_password") or a.get("mt5_password") or a.get("master_password") or "",
                "mt5_password": a.get("mt5_master_password") or a.get("mt5_password") or a.get("master_password") or "",
                "master_password": a.get("mt5_master_password") or a.get("mt5_password") or a.get("master_password") or "",
                "mt5_investor_password": a.get("mt5_investor_password") or a.get("investor_password") or "",
                "investor_password": a.get("mt5_investor_password") or a.get("investor_password") or "",
                "account_size": num(a.get("account_size") or a.get("start_balance")),
                "balance": num(a.get("start_balance") or a.get("account_size")),
                "equity": num(a.get("current_equity") or a.get("current_balance") or a.get("start_balance") or a.get("account_size")),
                "highest_equity": num(a.get("highest_equity") or a.get("current_equity") or a.get("start_balance") or a.get("account_size")),
                "lowest_equity": num(a.get("lowest_equity") or a.get("start_balance") or a.get("account_size")),
                "profit_percent": num(a.get("profit_percent")),
                "risk_zone": a.get("risk_zone") or "safe",
                "_source_of_truth": "monitoring_api",
            })
        return ok(out, f"{len(out)} monitorable account(s)")
    except Exception as e:
        return bad(e, 500)


@app.route("/monitoring_snapshot", methods=["POST", "OPTIONS"])
def monitoring_snapshot():
    if request.method == "OPTIONS":
        return ok({})
    data = request.get_json(silent=True) or {}
    account_id = data.get("trader_account_id") or data.get("current_account_id")
    if not account_id:
        return bad("Exact trader_account_id is required for snapshot", 400)
    account = get_account_by_id_or_login(account_id, data.get("mt5_login"))
    if not account:
        return bad("Active account not found or ownership evidence mismatched", 404)
    result = apply_intelligence(account, data)
    print(f"GLOBAL_FEED SNAPSHOT APPLIED mt5={data.get('mt5_login')} result={result}", flush=True)
    return ok(result, "snapshot applied")


@app.route("/disable_mt5_access", methods=["POST", "OPTIONS"])
def disable_mt5_access():
    if request.method == "OPTIONS":
        return ok({})
    data = request.get_json(silent=True) or {}
    account_id = data.get("trader_account_id") or data.get("current_account_id")
    if not account_id:
        return bad("Exact trader_account_id is required", 400)
    account = get_account_by_id_or_login(account_id, data.get("mt5_login"))
    if not account:
        return bad("Active account not found or ownership evidence mismatched", 404)
    status = str(data.get("status") or "breached").lower()
    reason = data.get("reason") or "MT5 access disabled by monitoring engine"
    payload = {
        "account_status": "breached_archived" if "breach" in status else status,
        "monitoring_enabled": False,
        "risk_zone": "breached" if "breach" in status else status,
        "archive_reason": reason,
        "archived_at": now_iso(),
        "updated_at": now_iso(),
    }
    safe_update("trader_accounts", payload, "id", account.get("id"))
    safe_update("traders", {"status": "breached" if "breach" in status else status, "challenge_state": status, "mt5_access_disabled": True, "monitoring_enabled": False, "updated_at": now_iso()}, "id", account.get("trader_id"))
    safe_insert("monitoring_events", {"trader_id": account.get("trader_id"), "trader_account_id": account.get("id"), "mt5_login": account.get("mt5_login"), "event_type": status, "risk_zone": status, "message": reason, "created_at": now_iso()})
    alert_once(account, status, status.upper(), reason, "critical", data)
    return ok({"account_id": account.get("id"), "status": status}, "access disabled")


@app.route("/sync_trades", methods=["POST", "OPTIONS"])
def sync_trades():
    if request.method == "OPTIONS":
        return ok({})
    data = request.get_json(silent=True) or {}
    trades = data.get("trades") or []
    if not isinstance(trades, list):
        return bad("trades must be a list")
    saved = 0
    skipped = 0
    account_cache = {}
    for trade in trades[:500]:
        if not isinstance(trade, dict):
            continue
        row = dict(trade)
        lookup_id = row.get("trader_account_id") or row.get("current_account_id") or data.get("trader_account_id") or data.get("current_account_id")
        lookup_login = row.get("mt5_login") or data.get("mt5_login")
        if not lookup_id:
            skipped += 1
            print("TRADE SYNC SKIPPED WITHOUT EXACT ACCOUNT ID:", {"mt5_login": clean_login(lookup_login)}, flush=True)
            continue
        cache_key = f"{lookup_id or ''}:{clean_login(lookup_login)}"
        account = account_cache.get(cache_key)
        if cache_key not in account_cache:
            account = get_account_by_id_or_login(lookup_id, lookup_login)
            account_cache[cache_key] = account
        if not account:
            skipped += 1
            print("TRADE SYNC SKIPPED NON-ACTIVE ACCOUNT:", {"trader_account_id": lookup_id, "mt5_login": clean_login(lookup_login)}, flush=True)
            continue
        row["trader_id"] = account.get("trader_id")
        row["trader_account_id"] = account.get("id")
        row["mt5_login"] = clean_login(account.get("mt5_login"))
        row["synced_at"] = now_iso()
        row["updated_at"] = now_iso()
        if not row.get("created_at"):
            row["created_at"] = now_iso()
        # Keep this fast. Upsert if DB has a suitable unique key, otherwise insert fallback.
        try:
            supabase.table("trader_trades").upsert(row, on_conflict="ticket,mt5_login").execute()
        except Exception:
            try:
                supabase.table("trader_trades").insert(row).execute()
            except Exception as e:
                print("TRADE SAVE SKIPPED:", e)
                continue
        saved += 1
    return ok({"received": len(trades), "saved": saved, "skipped_non_active": skipped}, "trades synced")


@app.route("/traders")
def traders_compat():
    """Compatibility alias: old engines may still call /traders.
    It returns the same clean account-level feed as /monitorable_accounts, not legacy trader rows.
    """
    return monitorable_accounts()


@app.route("/traders_raw")
def traders_raw_compat():
    return monitorable_accounts()


@app.route("/debug/supabase")
def debug_supabase_compat():
    return monitorable_accounts()


@app.route("/trader_current_account/<path:lookup>")
def trader_current_account_compat(lookup):
    """Lightweight global-feed account lookup so no call falls back to stale legacy MT5 data."""
    lookup = str(lookup or "").strip()
    try:
        trader = None
        accounts = []
        if "@" in lookup:
            trs = supabase.table("traders").select("*").eq("email", lookup).order("updated_at", desc=True).limit(1).execute().data or []
            trader = trs[0] if trs else None
            if trader:
                accounts = supabase.table("trader_accounts").select("*").eq("trader_id", trader.get("id")).order("updated_at", desc=True).limit(50).execute().data or []
        elif lookup.isdigit():
            accounts = supabase.table("trader_accounts").select("*").eq("mt5_login", lookup).order("updated_at", desc=True).limit(50).execute().data or []
        else:
            trs = supabase.table("traders").select("*").eq("id", lookup).limit(1).execute().data or []
            trader = trs[0] if trs else None
            if trader:
                accounts = supabase.table("trader_accounts").select("*").eq("trader_id", trader.get("id")).order("updated_at", desc=True).limit(50).execute().data or []
        caches = {}
        if trader and trader.get("id"):
            caches.setdefault("traders", {})[str(trader.get("id"))] = trader
        active_accounts = [a for a in accounts if account_is_eligible(a, caches)[0]]
        current = None
        if lookup.isdigit():
            if len(active_accounts) == 1:
                current = active_accounts[0]
                trader_id = current.get("trader_id")
                if trader_id:
                    trs = supabase.table("traders").select("*").eq("id", trader_id).limit(1).execute().data or []
                    trader = trs[0] if trs else None
            elif len(active_accounts) > 1:
                trader = None
                for row in active_accounts:
                    log_lifecycle_inconsistency(
                        "mt5_login resolves to multiple eligible active accounts; exact trader_account_id required",
                        row,
                        caches.get("purchases", {}).get(str(row.get("purchase_id") or "").strip()) or {},
                        caches.get("pools", {}).get(str(row.get("mt5_pool_id") or "").strip()) or {},
                        caches.get("traders", {}).get(str(row.get("trader_id") or "").strip()) or {},
                    )
            # Never guess between duplicate eligible rows for a login-only lookup.
        else:
            current = active_accounts[0] if active_accounts else None
        return ok({"source_of_truth": "trader_accounts", "trader": trader or {}, "current_account": current, "active_accounts": active_accounts, "accounts": accounts}, "global feed account loaded")
    except Exception as e:
        return bad(e, 500)


@app.route("/account_intelligence_scan")
def account_intelligence_scan():
    try:
        rows = (
            supabase.table("trader_accounts")
            .select("*")
            .in_("account_status", sorted(ACTIVE_ACCOUNT_STATUSES))
            .limit(MONITORABLE_LIMIT)
            .execute()
            .data
            or []
        )
        rows = eligible_accounts_without_login_ambiguity(rows, "account_intelligence_scan")
        results = []
        for account in rows:
            snapshot = {
                "trader_account_id": account.get("id"),
                "mt5_login": account.get("mt5_login"),
                "equity": account.get("current_equity") or account.get("start_balance") or account.get("account_size"),
                "highest_equity": account.get("highest_equity") or account.get("current_equity") or account.get("start_balance") or account.get("account_size"),
                "lowest_equity": account.get("lowest_equity") or account.get("start_balance") or account.get("account_size"),
                "timestamp": now_iso(),
            }
            results.append(apply_intelligence(account, snapshot))
        return ok(results, f"scanned {len(results)} active account(s)")
    except Exception as e:
        return bad(e, 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
