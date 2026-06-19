from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client
from datetime import datetime, timezone
import os

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

ACTIVE_ACCOUNT_STATUSES = {
    "assigned_active", "active", "current_active", "approved_active", "assigned",
    "phase1", "phase1_active", "phase1_assigned",
    "phase2", "phase2_active", "phase2_assigned",
    "funded_active", "live", "funded"
}
TERMINAL_ACCOUNT_STATUSES = {
    "breached", "breached_archived", "archived_phase1", "archived_phase2",
    "passed", "disabled", "locked", "profit_protected"
}


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
    return bool(v and v.isdigit() and not any(x in v.upper() for x in ["NEW", "LOGIN", "NONE", "NULL", "TEST_LOGIN"]))


def target_for_stage(stage):
    stage = str(stage or "").strip().lower().replace(" ", "")
    if stage in {"phase1", "phase_1"}:
        return 10.0
    if stage in {"phase2", "phase_2"}:
        return 8.0
    # Funded/live accounts do NOT have phase pass target.
    return 0.0


def waiting_after_pass(stage):
    stage = str(stage or "").strip().lower()
    if stage == "phase1":
        return "phase2_waiting_mt5", "phase2"
    if stage == "phase2":
        return "funded_waiting_mt5", "funded"
    return "passed_review", stage or "phase1"


def risk_zone(current_dd_percent):
    d = num(current_dd_percent)
    if d >= MAX_DD_PERCENT:
        return "breached"
    if d >= 18:
        return "critical"
    if d >= 15:
        return "danger"
    if d >= 10:
        return "warning"
    return "safe"


def static_dd(start_balance, live_value):
    start = num(start_balance)
    v = num(live_value)
    if start <= 0:
        return 0.0
    return round(max(((start - v) / start) * 100, 0.0), 2)


def dd_used_from_static(dd_percent):
    if MAX_DD_PERCENT <= 0:
        return 0.0
    return round(max((num(dd_percent) / MAX_DD_PERCENT) * 100, 0.0), 2)


def account_is_monitorable(row):
    if not row:
        return False
    status = str(row.get("account_status") or row.get("status") or "").lower().strip()
    phase = str(row.get("stage") or row.get("phase") or "").lower().strip()
    combined = f"{status} {phase}"
    if row.get("mt5_access_disabled") is True or row.get("monitoring_enabled") is False:
        return False
    if any(x in combined for x in TERMINAL_ACCOUNT_STATUSES):
        return False
    if not valid_login(row.get("mt5_login")) or not str(row.get("mt5_server") or "").strip():
        return False
    # Any valid assigned/active phase account is monitorable, even if an older writer used
    # status words like approved_active/new_signup on the trader row.
    if not status:
        return True
    if status in ACTIVE_ACCOUNT_STATUSES or phase in ACTIVE_ACCOUNT_STATUSES:
        return True
    if any(x in combined for x in ["active", "assigned", "approved", "phase1", "phase2", "funded", "live", "new_signup"]):
        return True
    return False


def fetch_traders_by_ids(ids):
    ids = [str(x) for x in ids if x]
    if not ids:
        return {}
    out = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            rows = supabase.table("traders").select("*").in_("id", chunk).execute().data or []
            for r in rows:
                out[str(r.get("id"))] = r
        except Exception as e:
            print("TRADER BATCH FETCH ERROR:", e)
    return out


def get_account_by_id_or_login(account_id=None, mt5_login=None):
    try:
        if account_id:
            rows = supabase.table("trader_accounts").select("*").eq("id", account_id).limit(1).execute().data or []
            if rows:
                return rows[0]
        login = clean_login(mt5_login)
        if login:
            rows = supabase.table("trader_accounts").select("*").eq("mt5_login", login).order("updated_at", desc=True).limit(10).execute().data or []
            for r in rows:
                if account_is_monitorable(r):
                    return r
            return rows[0] if rows else None
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
    for table in ["monitoring_alerts", "account_alerts", "admin_alerts"]:
        try:
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

    start = num(account.get("start_balance") or account.get("account_size") or snapshot.get("account_size") or snapshot.get("balance") or 0)
    # MT5 engine may send balance as account size/reference in old versions, so prefer explicit live balance keys first.
    live_balance = num(
        snapshot.get("mt5_balance")
        or snapshot.get("live_balance")
        or snapshot.get("account_balance")
        or snapshot.get("current_balance")
        or account.get("current_balance")
        or start
    )
    equity = num(snapshot.get("equity") or snapshot.get("current_equity") or account.get("current_equity") or live_balance or start)
    stage = str(account.get("stage") or snapshot.get("phase_label") or "phase1").strip().lower()
    target = target_for_stage(stage)
    breach_level = round(start * (1 - MAX_DD_PERCENT / 100), 2) if start else 0.0

    old_high = num(account.get("highest_equity") or start)
    old_low_equity = num(account.get("lowest_equity") or start)
    old_low_balance = num(account.get("lowest_balance") or old_low_equity or start)
    snap_high = num(snapshot.get("highest_equity") or 0)
    snap_low_equity = num(snapshot.get("lowest_equity") or snapshot.get("recorded_lowest_equity") or 0)
    snap_low_balance = num(snapshot.get("lowest_balance") or snapshot.get("recorded_lowest_balance") or 0)

    highest = round(max(start, equity, old_high, snap_high), 2)
    low_equity_candidates = [x for x in [start, equity, old_low_equity, snap_low_equity] if x and x > 0]
    lowest_equity = round(min(low_equity_candidates), 2) if low_equity_candidates else equity
    low_balance_candidates = [x for x in [start, live_balance, old_low_balance, snap_low_balance] if x and x > 0]
    lowest_balance = round(min(low_balance_candidates), 2) if low_balance_candidates else live_balance

    # NairaPips rule: static DD breach is based on the lower of live balance/equity, and history is permanent.
    live_risk_value = min([x for x in [live_balance, equity] if x and x > 0] or [equity or live_balance or start])
    worst_risk_value = min([x for x in [lowest_balance, lowest_equity] if x and x > 0] or [live_risk_value])

    current_dd = static_dd(start, live_risk_value)
    current_dd_used = dd_used_from_static(current_dd)
    worst_dd = static_dd(start, worst_risk_value)
    worst_dd_used = dd_used_from_static(worst_dd)
    dd_remaining = round(max(MAX_DD_PERCENT - current_dd, 0), 2)
    zone = risk_zone(current_dd)

    profit = round(highest - start, 2) if start else 0.0
    profit_percent = round((profit / start) * 100, 2) if start else 0.0
    current_profit = round(equity - start, 2) if start else 0.0
    current_profit_percent = round((current_profit / start) * 100, 2) if start else 0.0
    target_equity = round(start * (1 + target / 100), 2) if target else 0.0
    pass_progress = round(max(0, profit_percent / target * 100), 2) if target else 0.0

    # Funded/live has target=0, so it cannot phase-pass by profit target.
    target_hit = bool(target and highest >= target_equity and profit_percent >= target)
    breached = bool(start and worst_risk_value <= breach_level)

    status = str(account.get("account_status") or "assigned_active").lower()
    phase_pass_status = ""
    lifecycle_state = None
    next_phase = stage

    if breached:
        zone = "breached"
        status = "breached_archived"
        lifecycle_state = "breached"
        next_phase = stage
    elif target_hit:
        zone = "passed"
        phase_pass_status = f"{stage}_passed"
        status = f"archived_{stage}" if stage in {"phase1", "phase2"} else "passed"
        lifecycle_state, next_phase = waiting_after_pass(stage)

    update = {
        "current_balance": live_balance,
        "current_equity": equity,
        "profit": profit,
        "profit_percent": profit_percent,
        "current_profit": current_profit,
        "current_profit_percent": current_profit_percent,
        "highest_equity": highest,
        "lowest_equity": lowest_equity,
        "lowest_balance": lowest_balance,
        "absolute_drawdown_percent": current_dd,
        "drawdown_percent": current_dd,
        "dd_used_percent": current_dd_used,
        "max_drawdown_used": current_dd_used,
        "worst_static_drawdown_percent": worst_dd,
        "worst_dd_used_percent": worst_dd_used,
        "dd_remaining_percent": dd_remaining,
        "breach_equity_level": breach_level,
        "breach_balance_level": breach_level,
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
        if breached:
            update["breach_detected_at"] = now_iso()
            update["breach_reason"] = update["archive_reason"]

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
        "event_type": "breached" if breached else ("phase_passed" if target_hit else "snapshot"),
        "risk_zone": zone,
        "phase_label": stage,
        "phase_pass_status": phase_pass_status,
        "balance": live_balance,
        "account_size": start,
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
        "lowest_equity": lowest_equity,
        "lowest_balance": lowest_balance,
        "breach_equity_level": breach_level,
        "breach_balance_level": breach_level,
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

    if breached:
        alert_once(account, "breached", "ACCOUNT BREACHED", f"MT5 {account.get('mt5_login')} crossed static DD limit. Worst value {worst_risk_value} <= breach level {breach_level}.", "critical", event)
    elif target_hit:
        alert_once(account, "phase_passed", f"{stage.upper()} PASSED", f"MT5 {account.get('mt5_login')} reached {target}% target. Awaiting next-stage MT5 assignment.", "success", event)
    elif current_dd >= 10:
        alert_once(account, "dd_warning", "DRAWDOWN WARNING", f"MT5 {account.get('mt5_login')} static DD is {current_dd}%. DD limit used {current_dd_used}%.", "warning", event)

    return {"account_id": account.get("id"), "mt5_login": account.get("mt5_login"), "zone": zone, "target_hit": target_hit, "breached": breached, "profit_percent": profit_percent, "current_dd": current_dd, "dd_used_percent": current_dd_used, "worst_dd": worst_dd, "worst_dd_used_percent": worst_dd_used}


def account_output(a, t=None):
    t = t or {}
    return {
        "id": t.get("id") or a.get("trader_id"),
        "trader_id": a.get("trader_id") or t.get("id"),
        "trader_account_id": a.get("id"),
        "current_account_id": a.get("id"),
        "name": t.get("name") or t.get("trader_name") or a.get("name") or "Trader",
        "full_name": t.get("full_name") or t.get("name") or t.get("trader_name") or a.get("name") or "Trader",
        "email": t.get("email") or a.get("email"),
        "phone": t.get("phone") or a.get("phone") or "",
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
        "account_size": num(a.get("account_size") or a.get("start_balance") or t.get("account_size") or t.get("balance")),
        "balance": num(a.get("start_balance") or a.get("account_size") or t.get("account_size") or t.get("balance")),
        "equity": num(a.get("current_equity") or a.get("current_balance") or a.get("start_balance") or a.get("account_size") or t.get("equity") or t.get("balance")),
        "highest_equity": num(a.get("highest_equity") or a.get("current_equity") or a.get("start_balance") or a.get("account_size")),
        "lowest_equity": num(a.get("lowest_equity") or a.get("start_balance") or a.get("account_size")),
        "profit_percent": num(a.get("profit_percent")),
        "risk_zone": a.get("risk_zone") or "safe",
        "_source_of_truth": "monitoring_api",
    }


@app.route("/")
def home():
    return ok({"service": "NairaPips Monitoring API", "status": "live"})


@app.route("/health")
def health():
    return ok({"health": "ok", "service": "monitoring", "time": now_iso()})


@app.route("/monitorable_accounts")
def monitorable_accounts():
    """Fast endpoint for MT5 engine. One row per live MT5 account, including legacy active trader rows."""
    try:
        rows = []
        try:
            rows = supabase.table("trader_accounts").select("*").in_("account_status", list(ACTIVE_ACCOUNT_STATUSES)).limit(MONITORABLE_LIMIT).execute().data or []
        except Exception:
            raw = supabase.table("trader_accounts").select("*").limit(MONITORABLE_LIMIT).execute().data or []
            rows = [r for r in raw if account_is_monitorable(r)]
        rows = [r for r in rows if account_is_monitorable(r)]

        traders = fetch_traders_by_ids([r.get("trader_id") for r in rows])
        out = []
        seen = set()
        for a in rows:
            login = clean_login(a.get("mt5_login"))
            if login in seen:
                continue
            seen.add(login)
            out.append(account_output(a, traders.get(str(a.get("trader_id")), {}) or {}))

        # Legacy fallback: if admin/trader_dashboard has MT5 on the traders row but trader_accounts row is missing/wrong status,
        # still feed it to the VPS engine. This fixes cases like a visible MT5 account missing from /monitorable_accounts.
        try:
            legacy = supabase.table("traders").select("*").limit(MONITORABLE_LIMIT).execute().data or []
            for t in legacy:
                login = clean_login(t.get("mt5_login"))
                server = str(t.get("mt5_server") or "").strip()
                status = str(t.get("status") or "").lower().strip()
                payment = str(t.get("payment_status") or "").lower().strip()
                if login in seen or not valid_login(login) or not server:
                    continue
                if t.get("mt5_access_disabled") is True or status in {"breached", "locked", "disabled"}:
                    continue
                # Any valid visible MT5 on the trader row is monitorable unless explicitly terminal.
                # This covers new assignments where the dashboard already shows MT5 but status still says new_signup/approved_active.
                a = {
                    "id": None,
                    "trader_id": t.get("id"),
                    "email": t.get("email"),
                    "phone": t.get("phone"),
                    "name": t.get("name"),
                    "stage": t.get("phase") or "phase1",
                    "account_status": "assigned_active",
                    "mt5_login": login,
                    "mt5_server": server,
                    "mt5_master_password": t.get("mt5_master_password") or t.get("mt5_password") or t.get("master_password") or "",
                    "mt5_password": t.get("mt5_password") or t.get("mt5_master_password") or t.get("master_password") or "",
                    "master_password": t.get("master_password") or t.get("mt5_master_password") or t.get("mt5_password") or "",
                    "mt5_investor_password": t.get("mt5_investor_password") or t.get("investor_password") or "",
                    "investor_password": t.get("investor_password") or t.get("mt5_investor_password") or "",
                    "account_size": t.get("account_size") or t.get("balance"),
                    "start_balance": t.get("account_size") or t.get("balance"),
                    "current_equity": t.get("equity") or t.get("balance"),
                    "current_balance": t.get("balance") or t.get("account_size"),
                    "highest_equity": t.get("highest_equity") or t.get("equity") or t.get("balance"),
                    "lowest_equity": t.get("lowest_equity") or t.get("equity") or t.get("balance"),
                    "risk_zone": t.get("risk_zone") or "safe",
                }
                seen.add(login)
                out.append(account_output(a, t))
        except Exception as e:
            print("LEGACY TRADER FALLBACK SKIPPED:", e)

        # Purchase fallback: newest approved/assigned purchase with MT5 must also feed the VPS engine.
        # This fixes fresh assignment cases where trader_accounts insertion is delayed/missing.
        try:
            purchases = supabase.table("challenge_purchases").select("*").limit(MONITORABLE_LIMIT).execute().data or []
            trader_ids = [p.get("trader_id") for p in purchases if p.get("trader_id")]
            traders_by_id = fetch_traders_by_ids(trader_ids)
            for p in purchases:
                login = clean_login(p.get("mt5_login"))
                server = str(p.get("mt5_server") or "").strip()
                st = str(p.get("status") or "").lower().strip()
                pay = str(p.get("payment_status") or "").lower().strip()
                if login in seen or not valid_login(login) or not server:
                    continue
                if any(x in (st + " " + pay) for x in ["breached", "archived", "locked", "disabled", "rejected"]):
                    continue
                if not ("approved" in st or "approved" in pay or "active" in st or "assigned" in st):
                    continue
                t = traders_by_id.get(str(p.get("trader_id")), {}) or {}
                a = {
                    "id": p.get("trader_account_id") or p.get("current_account_id"),
                    "trader_id": p.get("trader_id") or t.get("id"),
                    "email": p.get("email") or t.get("email"),
                    "phone": p.get("phone") or t.get("phone") or "",
                    "name": p.get("trader_name") or t.get("name") or "Trader",
                    "stage": p.get("assigned_phase") or p.get("phase") or t.get("phase") or "phase1",
                    "account_status": "assigned_active",
                    "mt5_login": login,
                    "mt5_server": server,
                    "mt5_master_password": p.get("mt5_master_password") or p.get("mt5_password") or p.get("master_password") or t.get("mt5_master_password") or t.get("mt5_password") or t.get("master_password") or "",
                    "mt5_password": p.get("mt5_master_password") or p.get("mt5_password") or p.get("master_password") or t.get("mt5_password") or "",
                    "master_password": p.get("mt5_master_password") or p.get("mt5_password") or p.get("master_password") or t.get("master_password") or "",
                    "mt5_investor_password": p.get("mt5_investor_password") or p.get("investor_password") or t.get("mt5_investor_password") or t.get("investor_password") or "",
                    "investor_password": p.get("mt5_investor_password") or p.get("investor_password") or t.get("investor_password") or "",
                    "account_size": p.get("account_size") or t.get("account_size") or t.get("balance"),
                    "start_balance": p.get("account_size") or t.get("account_size") or t.get("balance"),
                    "current_equity": p.get("account_size") or t.get("equity") or t.get("balance"),
                    "current_balance": p.get("account_size") or t.get("balance"),
                    "highest_equity": p.get("account_size") or t.get("highest_equity") or t.get("equity") or t.get("balance"),
                    "lowest_equity": p.get("account_size") or t.get("lowest_equity") or t.get("equity") or t.get("balance"),
                    "risk_zone": "safe",
                }
                seen.add(login)
                out.append(account_output(a, t))
        except Exception as e:
            print("PURCHASE FALLBACK SKIPPED:", e)


        # MT5 pool fallback: if assignment exists in mt5_pool but trader_accounts/traders/purchases have not synced yet,
        # feed it to the VPS engine so fresh assignments start monitoring immediately.
        try:
            pool_rows = supabase.table("mt5_pool").select("*").limit(MONITORABLE_LIMIT).execute().data or []
            assigned_ids = [m.get("assigned_trader_id") for m in pool_rows if m.get("assigned_trader_id")]
            pool_traders = fetch_traders_by_ids(assigned_ids)
            for m in pool_rows:
                login = clean_login(m.get("mt5_login"))
                server = str(m.get("mt5_server") or m.get("server") or "").strip()
                status = str(m.get("status") or "").lower().strip()
                if login in seen or not valid_login(login) or not server:
                    continue
                if not ("assign" in status or "use" in status or status in {"allocated", "in_use", "used"}):
                    continue
                t = pool_traders.get(str(m.get("assigned_trader_id")), {}) or {}
                a = {
                    "id": None,
                    "trader_id": m.get("assigned_trader_id") or t.get("id"),
                    "email": m.get("assigned_email") or t.get("email"),
                    "phone": t.get("phone") or "",
                    "name": m.get("assigned_trader_name") or t.get("name") or "Trader",
                    "stage": m.get("assigned_phase") or t.get("phase") or "phase1",
                    "account_status": "assigned_active",
                    "mt5_login": login,
                    "mt5_server": server,
                    "mt5_master_password": m.get("mt5_master_password") or m.get("mt5_password") or m.get("master_password") or "",
                    "mt5_password": m.get("mt5_master_password") or m.get("mt5_password") or m.get("master_password") or "",
                    "master_password": m.get("mt5_master_password") or m.get("mt5_password") or m.get("master_password") or "",
                    "mt5_investor_password": m.get("mt5_investor_password") or m.get("investor_password") or "",
                    "investor_password": m.get("mt5_investor_password") or m.get("investor_password") or "",
                    "account_size": m.get("account_size") or t.get("account_size") or t.get("balance"),
                    "start_balance": m.get("account_size") or t.get("account_size") or t.get("balance"),
                    "current_equity": m.get("account_size") or t.get("equity") or t.get("balance"),
                    "current_balance": m.get("account_size") or t.get("balance"),
                    "highest_equity": m.get("account_size") or t.get("highest_equity") or t.get("equity") or t.get("balance"),
                    "lowest_equity": m.get("account_size") or t.get("lowest_equity") or t.get("equity") or t.get("balance"),
                    "risk_zone": "safe",
                }
                seen.add(login)
                out.append(account_output(a, t))
        except Exception as e:
            print("MT5 POOL FALLBACK SKIPPED:", e)

        return ok(out, f"{len(out)} monitorable account(s)")
    except Exception as e:
        return bad(e, 500)


@app.route("/monitoring_snapshot", methods=["POST", "OPTIONS"])
def monitoring_snapshot():
    if request.method == "OPTIONS":
        return ok({})
    data = request.get_json(silent=True) or {}
    account = get_account_by_id_or_login(data.get("trader_account_id") or data.get("current_account_id"), data.get("mt5_login"))
    if not account:
        return bad("Account not found for snapshot", 404)
    result = apply_intelligence(account, data)
    return ok(result, "snapshot applied")


@app.route("/disable_mt5_access", methods=["POST", "OPTIONS"])
def disable_mt5_access():
    if request.method == "OPTIONS":
        return ok({})
    data = request.get_json(silent=True) or {}
    account = get_account_by_id_or_login(data.get("trader_account_id") or data.get("current_account_id"), data.get("mt5_login"))
    if not account:
        return bad("Account not found", 404)
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
    for trade in trades[:500]:
        if not isinstance(trade, dict):
            continue
        row = dict(trade)
        row["synced_at"] = now_iso()
        row["updated_at"] = now_iso()
        if not row.get("created_at"):
            row["created_at"] = now_iso()
        try:
            supabase.table("trader_trades").upsert(row, on_conflict="ticket,mt5_login").execute()
        except Exception:
            try:
                supabase.table("trader_trades").insert(row).execute()
            except Exception as e:
                print("TRADE SAVE SKIPPED:", e)
                continue
        saved += 1
    return ok({"received": len(trades), "saved": saved}, "trades synced")



@app.route("/sync_monitoring_accounts", methods=["GET", "POST", "OPTIONS"])
def sync_monitoring_accounts():
    """Repair endpoint: creates missing trader_accounts rows from assigned traders, purchases and MT5 pool rows."""
    if request.method == "OPTIONS":
        return ok({})
    synced = []
    errors = []

    def stage_for(row):
        raw = str((row or {}).get("assigned_phase") or (row or {}).get("stage") or (row or {}).get("phase") or "phase1").lower().replace(" ", "")
        if raw in {"funded/live", "funded_live", "funded", "live"}:
            return "funded", "funded_active", 0
        if raw in {"phase2", "phase_2"}:
            return "phase2", "phase2_active", 8
        return "phase1", "assigned_active", 10

    def upsert_from(trader=None, source=None):
        trader = trader or {}
        source = source or {}
        stage, account_status, target = stage_for(source or trader)
        login = clean_login(source.get("mt5_login") or trader.get("mt5_login"))
        server = str(source.get("mt5_server") or source.get("server") or trader.get("mt5_server") or trader.get("server") or "").strip()
        trader_id = source.get("trader_id") or source.get("assigned_trader_id") or trader.get("id")
        size = num(source.get("account_size") or source.get("start_balance") or trader.get("account_size") or trader.get("balance"))
        if not trader_id or not valid_login(login) or not server or size <= 0:
            return None
        master = source.get("mt5_master_password") or source.get("mt5_password") or source.get("master_password") or trader.get("mt5_master_password") or trader.get("mt5_password") or trader.get("master_password") or ""
        investor = source.get("mt5_investor_password") or source.get("investor_password") or trader.get("mt5_investor_password") or trader.get("investor_password") or ""
        payload = {
            "trader_id": trader_id,
            "account_reference": trader.get("account_reference") or source.get("account_reference"),
            "account_size": size,
            "start_balance": size,
            "balance": size,
            "current_balance": size,
            "equity": size,
            "current_equity": size,
            "highest_equity": size,
            "lowest_equity": size,
            "lowest_balance": size,
            "profit": 0,
            "profit_percent": 0,
            "current_profit": 0,
            "current_profit_percent": 0,
            "drawdown_percent": 0,
            "absolute_drawdown_percent": 0,
            "dd_used_percent": 0,
            "max_drawdown_used": 0,
            "risk_zone": "safe",
            "stage": stage,
            "phase": stage,
            "account_status": account_status,
            "status": "active",
            "payment_status": "approved",
            "monitoring_enabled": True,
            "mt5_access_disabled": False,
            "mt5_account_active": True,
            "mt5_login": login,
            "mt5_server": server,
            "server": server,
            "mt5_master_password": master,
            "mt5_password": master,
            "master_password": master,
            "mt5_investor_password": investor,
            "investor_password": investor,
            "target_percent": target,
            "profit_target": target,
            "assigned_at": source.get("assigned_at") or trader.get("assigned_at") or now_iso(),
            "started_at": source.get("assigned_at") or trader.get("challenge_started_at") or now_iso(),
            "updated_at": now_iso(),
            "admin_note": "Monitoring repair sync",
            "approved_by": "sync",
        }
        try:
            existing = supabase.table("trader_accounts").select("*").eq("mt5_login", login).order("updated_at", desc=True).limit(1).execute().data or []
        except Exception:
            existing = []
        if existing and existing[0].get("id"):
            row = safe_update("trader_accounts", payload, "id", existing[0].get("id"))
            out = row[0] if row else dict(existing[0], **payload)
        else:
            payload["created_at"] = now_iso()
            row = safe_insert("trader_accounts", payload)
            out = row[0] if row else payload
        try:
            safe_update("traders", {
                "current_account_id": out.get("id"),
                "trader_account_id": out.get("id"),
                "mt5_login": login,
                "mt5_server": server,
                "mt5_master_password": master,
                "mt5_password": master,
                "master_password": master,
                "mt5_investor_password": investor,
                "investor_password": investor,
                "account_size": size,
                "balance": size,
                "equity": size,
                "phase": stage,
                "status": "active" if stage == "phase1" else account_status,
                "payment_status": "approved",
                "monitoring_enabled": True,
                "mt5_access_disabled": False,
                "updated_at": now_iso(),
            }, "id", trader_id)
        except Exception:
            pass
        return out

    try:
        traders = supabase.table("traders").select("*").limit(MONITORABLE_LIMIT).execute().data or []
        traders_by_id = {str(t.get("id")): t for t in traders if t.get("id")}
    except Exception as e:
        traders = []; traders_by_id = {}; errors.append(f"traders:{e}")

    for t in traders:
        status = str(t.get("status") or "").lower(); phase = str(t.get("phase") or "").lower()
        if t.get("mt5_access_disabled") is True or any(x in status + " " + phase for x in ["breached", "archived", "locked", "disabled", "rejected"]):
            continue
        row = upsert_from(trader=t, source=t)
        if row: synced.append(row.get("mt5_login"))

    try:
        purchases = supabase.table("challenge_purchases").select("*").limit(MONITORABLE_LIMIT).execute().data or []
    except Exception as e:
        purchases = []; errors.append(f"purchases:{e}")
    for p in purchases:
        state = str(p.get("status") or "").lower() + " " + str(p.get("payment_status") or "").lower()
        if any(x in state for x in ["breached", "archived", "locked", "disabled", "rejected"]):
            continue
        if not any(x in state for x in ["approved", "active", "assigned"]):
            continue
        row = upsert_from(trader=traders_by_id.get(str(p.get("trader_id")), {}), source=p)
        if row: synced.append(row.get("mt5_login"))

    try:
        pool = supabase.table("mt5_pool").select("*").limit(MONITORABLE_LIMIT).execute().data or []
    except Exception as e:
        pool = []; errors.append(f"mt5_pool:{e}")
    for m in pool:
        st = str(m.get("status") or "").lower()
        if not ("assign" in st or "use" in st or st in {"allocated", "in_use", "used"}):
            continue
        row = upsert_from(trader=traders_by_id.get(str(m.get("assigned_trader_id")), {}), source=m)
        if row: synced.append(row.get("mt5_login"))

    unique=[]
    for x in synced:
        if x and x not in unique:
            unique.append(x)
    return ok({"synced_count": len(unique), "mt5_logins": unique, "errors": errors}, f"synced {len(unique)} account(s)")


@app.route("/account_intelligence_scan")
def account_intelligence_scan():
    try:
        rows = supabase.table("trader_accounts").select("*").in_("account_status", list(ACTIVE_ACCOUNT_STATUSES)).limit(MONITORABLE_LIMIT).execute().data or []
        results = []
        for account in rows:
            snapshot = {
                "trader_account_id": account.get("id"),
                "mt5_login": account.get("mt5_login"),
                "equity": account.get("current_equity") or account.get("start_balance") or account.get("account_size"),
                "current_balance": account.get("current_balance") or account.get("start_balance") or account.get("account_size"),
                "highest_equity": account.get("highest_equity") or account.get("current_equity") or account.get("start_balance") or account.get("account_size"),
                "lowest_equity": account.get("lowest_equity") or account.get("start_balance") or account.get("account_size"),
                "lowest_balance": account.get("lowest_balance") or account.get("start_balance") or account.get("account_size"),
                "timestamp": now_iso(),
            }
            results.append(apply_intelligence(account, snapshot))
        return ok(results, f"scanned {len(results)} active account(s)")
    except Exception as e:
        return bad(e, 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
