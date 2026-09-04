import time
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client
from datetime import datetime, timezone
import os, re, json
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

app = Flask(__name__)
NAIRAPIPS_RELEASE = "MT5_BALANCE_INPUT_NORMALIZED_FINAL_2026_07_23"
CORS(app)
NAIRAPIPS_MONITORING_RELEASE = "EXACT_PLAN_RULE_AUTHORITY_10_15_TARGET_DD_2026_09_02"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
MAIN_API_URL = os.getenv("NAIRAPIPS_MAIN_API_URL", "https://nairapips-api.onrender.com").rstrip("/")
MAX_DD_PERCENT = float(os.getenv("NAIRAPIPS_MAX_DD_PERCENT", "20"))
MONITORABLE_LIMIT = int(os.getenv("NAIRAPIPS_MONITORABLE_LIMIT", "1000"))
TERMINAL_RETIRE_AFTER_DAYS = int(os.getenv("NAIRAPIPS_TERMINAL_RETIRE_AFTER_DAYS", "30"))

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


def require_main_api_admin():
    """Validate the Admin bearer token with the main API before any recall write."""
    auth = str(request.headers.get("Authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        return None, bad("Admin authentication is required", 401)
    try:
        probe = urlrequest.Request(
            MAIN_API_URL + "/admin_bootstrap",
            headers={"Authorization": auth, "Accept": "application/json"},
            method="GET",
        )
        with urlrequest.urlopen(probe, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        if payload.get("success") is False:
            return None, bad("Invalid or expired admin token", 401)
        return payload, None
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        print("RECALL ADMIN AUTH FAILED:", str(exc), flush=True)
        return None, bad("Admin authentication could not be verified", 401)


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
    # A verified breach is an irreversible lifecycle event for this exact account row.
    # Removing a reversible lock or seeing a later recovered balance must never resurrect it.
    if (row or {}).get("breached_at"):
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



_RULE_CACHE = {}
_RULE_CACHE_SECONDS = 60

def fetch_plan_by_id(plan_id):
    try:
        if not plan_id:
            return {}
        rows = supabase.table("challenge_plans").select("*").eq("id", plan_id).limit(1).execute().data or []
        return rows[0] if rows else {}
    except Exception as e:
        print("PLAN FETCH ERROR:", e)
        return {}

def _rule_cache_get(key):
    row = _RULE_CACHE.get(key)
    if not row:
        return None
    ts, value = row
    if time.time() - ts > _RULE_CACHE_SECONDS:
        _RULE_CACHE.pop(key, None)
        return None
    return value

def _rule_cache_set(key, value):
    _RULE_CACHE[key] = (time.time(), value)
    return value

def resolve_account_rules(account, stage=None):
    """Resolve exact commercial rules for THIS trader_account.

    Authority order:
      1) frozen trader_accounts values,
      2) linked challenge purchase,
      3) linked challenge plan.

    NairaPips has multiple commercial rule sets (including 10% and 15%
    targets and plan-specific DD). Missing authority must never be converted
    into a guessed pass/breach rule.
    """
    account = account or {}
    stage = str(stage or account.get("stage") or "phase1").strip().lower()
    cache_key = "rules:" + str(account.get("id") or account.get("mt5_login") or "")
    cached = _rule_cache_get(cache_key)
    if cached:
        return dict(cached)

    purchase = {}
    plan = {}
    purchase_id = str(account.get("purchase_id") or account.get("challenge_purchase_id") or "").strip()
    if purchase_id:
        purchase = fetch_purchase_by_id(purchase_id) or {}

    plan_id = str(
        account.get("plan_id")
        or purchase.get("plan_id")
        or purchase.get("challenge_plan_id")
        or ""
    ).strip()
    if plan_id:
        plan = fetch_plan_by_id(plan_id) or {}

    def first_num(*values):
        for v in values:
            if v not in (None, ""):
                n = num(v, None)
                if n is not None and n > 0:
                    return float(n)
        return None

    dd_limit = first_num(
        account.get("dd_limit_percent"),
        account.get("max_drawdown"),
        account.get("max_drawdown_percent"),
        purchase.get("dd_limit_percent"),
        purchase.get("max_drawdown"),
        purchase.get("max_drawdown_percent"),
        plan.get("dd_limit_percent"),
        plan.get("max_drawdown"),
        plan.get("max_drawdown_percent"),
        plan.get("total_dd"),
    )
    dd_authority_present = dd_limit is not None

    if stage == "phase1":
        target = first_num(
            account.get("target_percent"),
            account.get("profit_target"),
            account.get("phase1_target"),
            purchase.get("target_percent"),
            purchase.get("phase1_target"),
            purchase.get("profit_target"),
            plan.get("target_percent"),
            plan.get("phase1_target"),
            plan.get("profit_target"),
        )
    elif stage == "phase2":
        target = first_num(
            account.get("target_percent"),
            account.get("profit_target"),
            account.get("phase2_target"),
            purchase.get("target_percent"),
            purchase.get("phase2_target"),
            purchase.get("profit_target"),
            plan.get("target_percent"),
            plan.get("phase2_target"),
            plan.get("profit_target"),
        )
    else:
        target = 0.0

    target_authority_present = (stage not in {"phase1", "phase2"}) or (target is not None)

    second_life_enabled = bool_true(
        purchase.get("second_life_enabled")
        if purchase.get("second_life_enabled") is not None
        else plan.get("second_life_enabled")
    )

    one_phase = second_life_enabled
    journey_text = " ".join(str(x or "") for x in (
        account.get("challenge_journey"),
        purchase.get("challenge_journey"),
        purchase.get("journey_stages"),
        purchase.get("route"),
        plan.get("challenge_journey"),
        plan.get("journey_stages"),
    )).lower()
    if "one_phase" in journey_text or "1-phase" in journey_text or "1 phase" in journey_text:
        one_phase = True

    rules = {
        "dd_limit_percent": float(dd_limit) if dd_limit is not None else 0.0,
        "dd_authority_present": bool(dd_authority_present),
        "target_percent": float(target) if target is not None else 0.0,
        "target_authority_present": bool(target_authority_present),
        "second_life_enabled": bool(second_life_enabled),
        "one_phase": bool(one_phase),
        "purchase_id": purchase_id or None,
        "plan_id": plan_id or None,
        "plan_name": plan.get("name") or plan.get("plan_name") or purchase.get("plan_name"),
    }
    return _rule_cache_set(cache_key, rules)


def target_for_stage(stage):
    # Compatibility only. Never use this as commercial rule authority.
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


def risk_zone(current_dd_percent, dd_limit_percent=None):
    d = num(current_dd_percent)
    limit = num(dd_limit_percent, MAX_DD_PERCENT or 20)
    if limit <= 0:
        limit = 20.0
    if d >= limit:
        return "breached"
    if d >= limit * 0.90:
        return "critical"
    if d >= limit * 0.75:
        return "danger"
    if d >= limit * 0.50:
        return "warning"
    return "safe"


def static_dd(start_balance, equity):
    start = num(start_balance)
    eq = num(equity)
    if start <= 0:
        return 0.0
    return round(max(((start - eq) / start) * 100, 0.0), 2)


def dd_used_from_static(dd_percent, dd_limit_percent=None):
    limit = num(dd_limit_percent, MAX_DD_PERCENT or 20)
    if limit <= 0:
        return 0.0
    return round(max((num(dd_percent) / limit) * 100, 0.0), 2)


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
    work = dict(payload or {})
    removed = []
    for _ in range(24):
        try:
            return supabase.table(table).insert(work).execute().data or []
        except Exception as e:
            # Evidence tables have evolved over time. Remove only a column that PostgREST
            # explicitly reports as unavailable; never guess or drop core account data here.
            import re
            text = str(e or '')
            m = re.search(r"Could not find the '([^']+)' column", text, flags=re.I)
            missing = m.group(1) if m else None
            if missing and missing in work:
                removed.append(missing)
                work.pop(missing, None)
                print(f"ADAPTIVE INSERT {table}: removed unavailable column {missing}; retrying", flush=True)
                continue
            print(f"SAFE INSERT FAILED {table}:", e, flush=True)
            return []
    print(f"SAFE INSERT FAILED {table}: too many unavailable columns removed={removed}", flush=True)
    return []


def safe_update(table, payload, col, val):
    try:
        return supabase.table(table).update(payload).eq(col, val).execute().data or []
    except Exception as e:
        print(f"SAFE UPDATE FAILED {table}.{col}:", e)
        return []


# 2026-09-03 FORENSIC FIX V2 — adaptive verified persistence for live MT5 snapshots/breaches.
# IMPORTANT: the production database has compatibility triggers that validate derived DD fields.
# A narrow "core only" retry can therefore still fail when it changes balance/equity without also
# updating the matching derived field (for example worst_static_drawdown_percent).
# Instead, retry the SAME coherent snapshot while removing only columns PostgREST explicitly says
# do not exist. This preserves trigger-consistent values and prevents one new optional column from
# blocking every account snapshot.

def _np_missing_column_from_error(exc):
    text = str(exc or '')
    import re
    patterns = [
        r"Could not find the '([^']+)' column",
        r'Could not find the "([^"]+)" column',
        r"column ['\"]?([A-Za-z0-9_]+)['\"]? does not exist",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            return m.group(1)
    return None


def _np_adaptive_table_update(table, match_col, match_val, payload, max_missing_columns=32):
    work = dict(payload or {})
    removed = []
    last_error = None
    for _ in range(max_missing_columns + 1):
        if not work:
            return False, removed, last_error or 'empty_payload'
        try:
            supabase.table(table).update(work).eq(match_col, match_val).execute()
            return True, removed, None
        except Exception as e:
            last_error = e
            missing = _np_missing_column_from_error(e)
            if missing and missing in work:
                removed.append(missing)
                work.pop(missing, None)
                print(f"ADAPTIVE {table} UPDATE: removed unavailable column {missing}; retrying coherent snapshot", flush=True)
                continue
            return False, removed, e
    return False, removed, last_error


def verified_account_update(account_id, payload):
    account_id = str(account_id or '').strip()
    if not account_id:
        return False, {}, 'missing_account_id'

    ok, removed, err = _np_adaptive_table_update('trader_accounts', 'id', account_id, payload)
    if not ok:
        print('VERIFIED ACCOUNT ADAPTIVE UPDATE FAILED:', err, flush=True)
        return False, {}, f'adaptive_update_failed:{err}'

    try:
        rows = supabase.table('trader_accounts').select('*').eq('id', account_id).limit(1).execute().data or []
        if not rows:
            return False, {}, 'readback_missing'
        mode = 'adaptive_full' if removed else 'full'
        if removed:
            print(f"VERIFIED ACCOUNT UPDATE OK after removing unavailable columns: {removed}", flush=True)
        return True, rows[0], mode
    except Exception as e:
        print('VERIFIED ACCOUNT READBACK FAILED:', e, flush=True)
        return False, {}, f'readback_failed:{e}'

def verified_trader_update(trader_id, payload):
    trader_id = str(trader_id or "").strip()
    if not trader_id:
        return False
    try:
        supabase.table("traders").update(payload).eq("id", trader_id).execute()
        rows = supabase.table("traders").select("id").eq("id", trader_id).limit(1).execute().data or []
        return bool(rows)
    except Exception as e:
        print("VERIFIED TRADER UPDATE FAILED:", e, flush=True)
        return False


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

    start = num(
        account.get("start_balance")
        or account.get("account_size")
        or snapshot.get("starting_balance")
        or snapshot.get("initial_balance")
        or snapshot.get("balance")
        or 0
    )

    # MT5 bridges do not all use the same field names. Normalize every known
    # live-balance/equity alias before applying intelligence.
    raw_balance = next((
        snapshot.get(key)
        for key in (
            "current_balance", "balance", "account_balance", "Balance",
            "ACCOUNT_BALANCE", "mt5_balance", "live_balance"
        )
        if snapshot.get(key) not in (None, "")
    ), None)

    raw_closed_profit = next((
        snapshot.get(key)
        for key in (
            "closed_profit", "closed_pnl", "realized_profit",
            "realised_profit", "net_closed_profit"
        )
        if snapshot.get(key) not in (None, "")
    ), None)

    if raw_balance not in (None, ""):
        current_balance = num(raw_balance, start)
    elif raw_closed_profit not in (None, "") and start:
        current_balance = round(start + num(raw_closed_profit), 2)
    else:
        current_balance = num(account.get("current_balance"), start)

    raw_equity = next((
        snapshot.get(key)
        for key in (
            "current_equity", "equity", "account_equity", "Equity",
            "ACCOUNT_EQUITY", "mt5_equity", "live_equity"
        )
        if snapshot.get(key) not in (None, "")
    ), None)
    equity = num(
        raw_equity if raw_equity not in (None, "")
        else account.get("current_equity")
        if account.get("current_equity") not in (None, "")
        else current_balance,
        current_balance
    )
    stage = str(account.get("stage") or snapshot.get("phase_label") or "phase1").strip().lower()
    rules = resolve_account_rules(account, stage)
    target = num(rules.get("target_percent"), 0.0)
    target_authority_present = bool(rules.get("target_authority_present"))
    dd_limit_percent = num(rules.get("dd_limit_percent"), 0.0)
    dd_authority_present = bool(rules.get("dd_authority_present"))
    breach_level = round(start * (1 - dd_limit_percent / 100), 2) if start and dd_authority_present and dd_limit_percent > 0 else 0.0

    old_high = num(account.get("highest_equity") or start)
    old_low = num(account.get("lowest_equity") or start)
    snap_high = num(snapshot.get("highest_equity") or 0)
    snap_low = num(snapshot.get("lowest_equity") or snapshot.get("recorded_lowest_equity") or 0)

    highest = round(max(start, equity, old_high, snap_high), 2)
    low_candidates = [x for x in [start, equity, old_low, snap_low] if x and x > 0]
    lowest = round(min(low_candidates), 2) if low_candidates else equity

    current_dd = static_dd(start, equity)
    current_dd_used = dd_used_from_static(current_dd, dd_limit_percent) if dd_authority_present else 0.0
    worst_dd = static_dd(start, lowest)
    worst_dd_used = dd_used_from_static(worst_dd, dd_limit_percent) if dd_authority_present else 0.0
    dd_remaining = round(max(dd_limit_percent - current_dd, 0), 2) if dd_authority_present else 0.0
    zone = risk_zone(current_dd, dd_limit_percent) if dd_authority_present else "authority_missing"

    # Current payout/closed profit follows the actual MT5 balance.
    # highest_equity remains pass-target evidence only.
    profit = round(current_balance - start, 2) if start else 0.0
    profit_percent = round((profit / start) * 100, 2) if start else 0.0
    current_profit = profit
    current_profit_percent = profit_percent
    floating_profit = round(equity - current_balance, 2)
    target_equity = round(start * (1 + target / 100), 2) if target else 0.0
    pass_progress = round(max(0, profit_percent / target * 100), 2) if target else 0.0

    target_hit = bool(target_authority_present and target and highest >= target_equity)

    # TERMINAL EVENT AUTHORITY:
    # A real static DD breach must never be erased because the account also touched
    # its profit target. Use the worst evidence seen while this account is still live.
    breached_by_equity = bool(dd_authority_present and start and equity <= breach_level)
    breached_by_balance = bool(dd_authority_present and start and current_balance <= breach_level)
    breached_by_recorded_low = bool(dd_authority_present and start and lowest <= breach_level)
    terminal_breach_recorded = bool(account.get("breached_at"))
    breached = bool(terminal_breach_recorded or breached_by_equity or breached_by_balance or breached_by_recorded_low)

    status = str(account.get("account_status") or "assigned_active").lower()
    phase_pass_status = ""
    lifecycle_state = None
    next_phase = stage

    if breached:
        zone = "breached"
        status = "breached_archived"
        lifecycle_state = "breached"
        next_phase = stage
        phase_pass_status = ""
    elif target_hit:
        zone = "passed"
        phase_pass_status = f"{stage}_passed"
        status = f"archived_{stage}" if stage in {"phase1", "phase2"} else "passed"
        if stage == "phase1" and rules.get("one_phase"):
            lifecycle_state, next_phase = "funded_waiting_mt5", "funded"
        else:
            lifecycle_state, next_phase = waiting_after_pass(stage)

    update = {
        "current_balance": current_balance,
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
        "target_authority_present": target_authority_present,
        "dd_authority_present": dd_authority_present,
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
        update["archive_reason"] = snapshot.get("reason") or ("Static drawdown breached" if breached else "Target reached")
        if breached:
            update["breached_at"] = account.get("breached_at") or now_iso()
            update["breach_reason"] = snapshot.get("reason") or (
                f"Static {dd_limit_percent:g}% drawdown breached. "
                f"Lowest/current evidence reached {min(lowest, equity, current_balance):,.2f} "
                f"against breach level {breach_level:,.2f}."
            )
            update["phase_pass_status"] = ""
            update["passed_at"] = None
        elif target_hit:
            update["passed_at"] = now_iso()

    account_write_ok, persisted_account, account_write_mode = verified_account_update(account.get("id"), update)
    if not account_write_ok:
        print(f"CRITICAL SNAPSHOT ACCOUNT WRITE FAILED mt5={account.get('mt5_login')} account_id={account.get('id')}", flush=True)

    trader_update = {
        "equity": equity,
        "balance": current_balance,
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
            "status": "breached" if breached else "active",
            "mt5_access_disabled": True,
            "monitoring_enabled": False,
            "phase_pass_status": phase_pass_status,
            "lifecycle_updated_at": now_iso(),
        })
    trader_write_ok = verified_trader_update(account.get("trader_id"), trader_update)

    event = {
        "trader_id": account.get("trader_id"),
        "trader_account_id": account.get("id"),
        "mt5_login": clean_login(account.get("mt5_login") or snapshot.get("mt5_login")),
        "event_type": "breached" if breached else ("phase_passed" if target_hit else "snapshot"),
        "risk_zone": zone,
        "phase_label": stage,
        "phase_pass_status": phase_pass_status,
        "balance": current_balance,
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
        "intelligence_version": "NIC_SPRINT1",
        "intelligence_event_id": f"{account.get('id')}:{snapshot.get('timestamp') or now_iso()}",
        "starting_balance": start,
        "current_balance": current_balance,
        "current_equity": equity,
        "floating_profit": floating_profit,
        "drawdown_amount": max(0, start - min(current_balance, equity)),
        "drawdown_remaining_percent": max(0, dd_limit_percent - current_dd),
        "dd_limit_percent": dd_limit_percent,
        "breach_source": snapshot.get("breach_source") or ("equity" if equity <= breach_level else ""),
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
        alert_once(
            account, "breached", "ACCOUNT BREACHED",
            f"MT5 {account.get('mt5_login')} hit/below its static {dd_limit_percent:g}% DD level {breach_level:,.2f}.",
            "critical", event
        )
    elif target_hit:
        next_label = "Funded" if (stage == "phase1" and rules.get("one_phase")) else "next-stage"
        alert_once(account, "phase_passed", f"{stage.upper()} PASSED", f"MT5 {account.get('mt5_login')} reached {target}% target. Awaiting {next_label} MT5 assignment.", "success", event)
    elif current_dd >= dd_limit_percent * 0.50:
        alert_once(account, "dd_warning", "DRAWDOWN WARNING", f"MT5 {account.get('mt5_login')} static DD is {current_dd}% of a {dd_limit_percent:g}% limit.", "warning", event)

    return {"account_id": account.get("id"), "mt5_login": account.get("mt5_login"), "zone": zone, "target_hit": target_hit, "breached": breached, "profit_percent": profit_percent, "current_dd": current_dd, "dd_used_percent": current_dd_used, "account_write_ok": account_write_ok, "account_write_mode": account_write_mode, "trader_write_ok": trader_write_ok, "persisted_account_status": (persisted_account or {}).get("account_status"), "persisted_balance": (persisted_account or {}).get("current_balance"), "persisted_equity": (persisted_account or {}).get("current_equity")}


@app.route("/")
def home():
    return ok({"service": "NairaPips Monitoring API", "status": "live"})


def _parse_iso_ts(v):
    if not v:
        return None
    try:
        s = str(v).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None

def _monitoring_freshness_payload(stale_after_seconds=180):
    now = datetime.now(timezone.utc)
    # Best-effort maintenance: old terminal rows stay in history but leave live monitoring.
    retire_old_terminal_accounts()
    rows = (
        supabase.table("trader_accounts")
        .select("id,trader_id,mt5_login,stage,account_status,last_sync_at,updated_at,current_balance,current_equity")
        .in_("account_status", sorted(ACTIVE_ACCOUNT_STATUSES))
        .limit(MONITORABLE_LIMIT)
        .execute()
        .data
        or []
    )
    active = []
    stale = []
    never = []
    newest = None
    for r in rows:
        if not str(r.get("mt5_server") or "").strip():
            # Some schemas/queries may not include server in this lightweight health query.
            pass
        ts = _parse_iso_ts(r.get("last_sync_at"))
        age = None
        if ts:
            age = max(0, int((now - ts).total_seconds()))
            if newest is None or ts > newest:
                newest = ts
        item = {
            "trader_account_id": r.get("id"),
            "trader_id": r.get("trader_id"),
            "mt5_login": r.get("mt5_login"),
            "stage": r.get("stage"),
            "account_status": r.get("account_status"),
            "last_sync_at": r.get("last_sync_at"),
            "sync_age_seconds": age,
            "current_balance": r.get("current_balance"),
            "current_equity": r.get("current_equity"),
        }
        active.append(item)
        if not ts:
            never.append(item)
        elif age is not None and age > stale_after_seconds:
            stale.append(item)

    newest_age = None
    if newest:
        newest_age = max(0, int((now - newest).total_seconds()))

    if not active:
        state = "no_active_accounts"
    elif newest is None:
        state = "engine_not_seen"
    elif newest_age is not None and newest_age > stale_after_seconds:
        state = "engine_stale"
    elif stale:
        state = "partial_stale"
    else:
        state = "live"

    return {
        "health": "ok" if state in {"live", "partial_stale"} else "warning",
        "service": "monitoring",
        "release": NAIRAPIPS_MONITORING_RELEASE,
        "monitoring_state": state,
        "server_time": now_iso(),
        "active_accounts": len(active),
        "stale_accounts": len(stale),
        "never_synced_accounts": len(never),
        "newest_snapshot_at": newest.isoformat() if newest else None,
        "newest_snapshot_age_seconds": newest_age,
        "stale_after_seconds": stale_after_seconds,
        "stale_sample": stale[:20],
        "never_synced_sample": never[:20],
    }

@app.route("/health")
def health():
    try:
        return ok(_monitoring_freshness_payload())
    except Exception as e:
        return bad({"health": "warning", "service": "monitoring", "error": str(e), "time": now_iso()}, 500)

@app.route("/monitoring_health")
def monitoring_health():
    """Management diagnostic endpoint.

    This does not invent MT5 data. It tells management whether fresh snapshots
    are actually reaching the Monitoring API and which active accounts are stale.
    """
    try:
        seconds = request.args.get("stale_after_seconds", "180")
        try:
            seconds = max(60, min(int(seconds), 86400))
        except Exception:
            seconds = 180
        return ok(_monitoring_freshness_payload(seconds))
    except Exception as e:
        return bad(e, 500)


@app.route("/admin_recall_wrong_assignment", methods=["POST", "OPTIONS"])
def admin_recall_wrong_assignment():
    """Recall one unused mistaken assignment while preserving the real active account."""
    if request.method == "OPTIONS":
        return ok({})
    _admin, auth_error = require_main_api_admin()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    trader_id = str(data.get("trader_id") or "").strip()
    account_id = str(data.get("trader_account_id") or "").strip()
    note = str(data.get("admin_note") or "Assigned to wrong trader").strip()
    if not trader_id or not account_id:
        return bad("trader_id and exact trader_account_id are required", 400)

    try:
        account_rows = supabase.table("trader_accounts").select("*").eq("id", account_id).limit(1).execute().data or []
        if not account_rows:
            return bad("Selected account was not found", 404)
        account = account_rows[0]
        if str(account.get("trader_id") or "") != trader_id:
            return bad("Selected account does not belong to this trader", 409)
        status_before = str(account.get("account_status") or "").strip().lower()
        if status_before not in ACTIVE_ACCOUNT_STATUSES:
            if status_before == "archived" and str(account.get("archive_reason") or "") == "wrong_assignment_recalled":
                return ok({"idempotent": True}, "Wrong assignment was already recalled")
            return bad("Only an active account can be recalled", 409)

        trades = supabase.table("trader_trades").select("id").eq("trader_account_id", account_id).limit(1).execute().data or []
        start = num(account.get("start_balance") or account.get("account_size"))
        balance = num(account.get("current_balance"), start)
        equity = num(account.get("current_equity"), balance)
        activity = [
            num(account.get("profit") or account.get("current_profit")),
            num(account.get("profit_percent") or account.get("current_profit_percent")),
            num(account.get("dd_used_percent")), num(account.get("absolute_drawdown_percent")),
            num(account.get("worst_dd_used_percent")), num(account.get("worst_static_drawdown_percent")),
        ]
        tolerance = max(0.01, abs(start) * 0.000001)
        if trades or any(abs(value) > 0.000001 for value in activity) or abs(balance - start) > tolerance or abs(equity - start) > tolerance:
            return bad("Recall blocked: this account has trading or balance activity. Use Reset Account instead.", 409)

        remaining = (
            supabase.table("trader_accounts").select("*").eq("trader_id", trader_id)
            .in_("account_status", sorted(ACTIVE_ACCOUNT_STATUSES)).order("updated_at", desc=True).limit(100).execute().data or []
        )
        remaining = [row for row in remaining if str(row.get("id") or "") != account_id]
        if not remaining:
            return bad("Recall blocked: no genuine active account remains for this trader. Use Reset Account if a replacement is required.", 409)
        genuine = remaining[0]

        purchase_id = str(account.get("purchase_id") or "").strip()
        if purchase_id:
            purchase_rows = supabase.table("challenge_purchases").select("id,trader_id,trader_account_id").eq("id", purchase_id).limit(1).execute().data or []
            if not purchase_rows:
                return bad("Linked purchase was not found; recall cancelled before any change", 409)
            if str(purchase_rows[0].get("trader_id") or trader_id) != trader_id:
                return bad("Linked purchase belongs to another trader", 409)

        now = now_iso()
        reason = "wrong_assignment_recalled"
        evidence = f"Wrong assignment recalled. {note} | trader_account_id={account_id} | MT5={account.get('mt5_login') or ''}"

        account_ok, _removed, account_error = _np_adaptive_table_update("trader_accounts", "id", account_id, {
            "account_status": "archived", "risk_zone": "archived", "archive_reason": reason,
            "archived_at": now, "breach_reason": None, "breached_at": None,
            "monitoring_enabled": False, "updated_at": now,
        })
        if not account_ok:
            return bad(f"Recall failed while archiving the selected account: {account_error}", 500)

        if purchase_id:
            purchase_ok, _removed, purchase_error = _np_adaptive_table_update("challenge_purchases", "id", purchase_id, {
                "status": "archived", "lifecycle_state": "archived", "account_state": "archived",
                "trader_account_id": None, "assigned_mt5_id": None, "mt5_login": "", "mt5_server": "",
                "mt5_master_password": "", "mt5_password": "", "master_password": "",
                "mt5_investor_password": "", "investor_password": "", "archive_reason": reason,
                "archived_at": now, "admin_note": evidence + " | no replacement required", "updated_at": now,
            })
            if not purchase_ok:
                return bad(f"Account archived but linked purchase reconciliation failed: {purchase_error}", 500)

        pool_id = str(account.get("mt5_pool_id") or "").strip()
        if pool_id:
            pool_ok, _removed, pool_error = _np_adaptive_table_update("mt5_pool", "id", pool_id, {
                "status": "recalled_hold", "assigned_trader_id": None, "assigned_trader_name": None,
                "assigned_email": None, "trader_account_id": None, "archived_at": now,
                "archive_reason": reason, "admin_note": evidence + " | ROTATE BOTH PASSWORDS BEFORE REUSE", "updated_at": now,
            })
            if not pool_ok:
                return bad(f"Account recalled but MT5 security hold failed: {pool_error}", 500)

        genuine_stage = str(genuine.get("stage") or "phase1").strip().lower()
        trader_payload = {
            "current_account_id": genuine.get("id"),
            "challenge_state": "funded_active" if genuine_stage == "funded" else f"{genuine_stage}_active",
            "status": "active", "phase": genuine_stage, "mt5_login": genuine.get("mt5_login") or "",
            "mt5_server": genuine.get("mt5_server") or "", "mt5_master_password": genuine.get("mt5_master_password") or "",
            "mt5_password": genuine.get("mt5_master_password") or "", "master_password": genuine.get("mt5_master_password") or "",
            "mt5_investor_password": genuine.get("mt5_investor_password") or "", "investor_password": genuine.get("mt5_investor_password") or "",
            "monitoring_enabled": bool(genuine.get("monitoring_enabled", True)), "mt5_account_active": True,
            "mt5_access_disabled": False, "mt5_reset_reason": None, "admin_note": evidence,
            "lifecycle_updated_at": now, "updated_at": now,
        }
        if not verified_trader_update(trader_id, trader_payload):
            return bad("Recall completed but trader current-account reconciliation failed", 500)

        safe_insert("lifecycle_events", {
            "trader_id": trader_id, "trader_account_id": account_id, "from_state": status_before,
            "to_state": "archived", "action": "admin_recall_wrong_assignment", "details": evidence,
            "created_by": str(data.get("admin_username") or data.get("admin_name") or "admin"), "created_at": now,
        })
        safe_insert("monitoring_events", {
            "trader_id": trader_id, "trader_account_id": account_id, "mt5_login": account.get("mt5_login"),
            "event_type": "admin_recall_wrong_assignment", "risk_zone": "archived", "message": evidence, "created_at": now,
        })
        return ok({
            "recalled_account_id": account_id, "recalled_mt5_login": account.get("mt5_login"),
            "current_account_id": genuine.get("id"), "current_mt5_login": genuine.get("mt5_login"),
            "pool_status": "recalled_hold",
        }, "Wrong assignment recalled. Genuine account preserved. Rotate both MT5 passwords before reuse.")
    except Exception as exc:
        print("ADMIN RECALL ERROR:", str(exc), flush=True)
        return bad(exc, 500)



def _bulk_rows(table_name, ids, select="*"):
    """One bounded Supabase query for a set of IDs. Never N+1 inside discovery."""
    clean_ids = [str(x).strip() for x in (ids or []) if str(x or "").strip()]
    if not clean_ids:
        return {}
    # preserve order while deduplicating
    clean_ids = list(dict.fromkeys(clean_ids))
    try:
        rows = (
            supabase.table(table_name)
            .select(select)
            .in_("id", clean_ids)
            .execute()
            .data
            or []
        )
        return {str(r.get("id")): r for r in rows if r.get("id")}
    except Exception as e:
        print(f"FAST DISCOVERY BULK FETCH ERROR table={table_name}: {e}", flush=True)
        return {}


def _quiet_monitoring_eligibility(account, purchase=None, mt5_pool=None):
    """Exact account safety checks without writes, alerts or per-row DB queries.

    Discovery must stay read-only and fast. Lifecycle disagreements are handled by
    lifecycle/event processing elsewhere; they must never make Gunicorn time out.
    """
    if not is_active_monitoring_account(account):
        return False, "account is not monitorable"
    if not str((account or {}).get("mt5_server") or "").strip():
        return False, "account has no mt5_server"
    if bool_false((account or {}).get("monitoring_enabled")):
        return False, "account monitoring_enabled is false"
    if bool_true((account or {}).get("mt5_access_disabled")):
        return False, "account mt5_access_disabled is true"
    if (account or {}).get("superseded_at") or (account or {}).get("replaced_at") or bool_true((account or {}).get("superseded")):
        return False, "account is superseded"

    purchase_id = str((account or {}).get("purchase_id") or "").strip()
    if purchase_id:
        ok_purchase, reason = is_active_purchase_for_account(purchase or {}, account)
        if not ok_purchase:
            return False, reason

    ok_pool, reason = is_active_pool_for_account(mt5_pool or {}, account)
    if not ok_pool:
        return False, reason

    return True, "eligible"


def _fast_rule_values(account, purchase=None, plan=None):
    """Resolve exact monitoring rules without guessed commercial fallbacks."""
    account = account or {}
    purchase = purchase or {}
    plan = plan or {}
    stage = str(account.get("stage") or account.get("phase") or "phase1").strip().lower()

    def first_num(*values):
        for v in values:
            if v not in (None, ""):
                n = num(v, None)
                if n is not None and n > 0:
                    return float(n)
        return None

    dd_limit = first_num(
        account.get("dd_limit_percent"),
        account.get("max_drawdown"),
        account.get("max_drawdown_percent"),
        purchase.get("dd_limit_percent"),
        purchase.get("max_drawdown"),
        purchase.get("max_drawdown_percent"),
        plan.get("dd_limit_percent"),
        plan.get("max_drawdown"),
        plan.get("max_drawdown_percent"),
        plan.get("total_dd"),
    )

    if stage == "phase1":
        target = first_num(
            account.get("target_percent"),
            account.get("profit_target"),
            account.get("phase1_target"),
            purchase.get("target_percent"),
            purchase.get("phase1_target"),
            purchase.get("profit_target"),
            plan.get("target_percent"),
            plan.get("phase1_target"),
            plan.get("profit_target"),
        )
    elif stage == "phase2":
        target = first_num(
            account.get("target_percent"),
            account.get("profit_target"),
            account.get("phase2_target"),
            purchase.get("target_percent"),
            purchase.get("phase2_target"),
            purchase.get("profit_target"),
            plan.get("target_percent"),
            plan.get("phase2_target"),
            plan.get("profit_target"),
        )
    else:
        target = 0.0

    def source_for(value, candidates):
        if value is None:
            return "authority_missing"
        for label, raw in candidates:
            try:
                if raw not in (None, "") and float(raw) > 0 and abs(float(raw) - float(value)) < 1e-9:
                    return label
            except Exception:
                pass
        return "resolved_exact"

    dd_source = source_for(dd_limit, [
        ("account.dd_limit_percent", account.get("dd_limit_percent")),
        ("account.max_drawdown", account.get("max_drawdown")),
        ("purchase.dd_limit_percent", purchase.get("dd_limit_percent")),
        ("purchase.max_drawdown", purchase.get("max_drawdown")),
        ("plan.dd_limit_percent", plan.get("dd_limit_percent")),
        ("plan.max_drawdown", plan.get("max_drawdown")),
    ])
    target_source = source_for(target, [
        ("account.target_percent", account.get("target_percent")),
        ("account.profit_target", account.get("profit_target")),
        (f"account.{stage}_target", account.get("phase1_target") if stage == "phase1" else account.get("phase2_target")),
        ("purchase.target_percent", purchase.get("target_percent")),
        (f"purchase.{stage}_target", purchase.get("phase1_target") if stage == "phase1" else purchase.get("phase2_target")),
        ("plan.target_percent", plan.get("target_percent")),
        (f"plan.{stage}_target", plan.get("phase1_target") if stage == "phase1" else plan.get("phase2_target")),
    ])

    return {
        "dd_limit_percent": float(dd_limit) if dd_limit is not None else 0.0,
        "dd_authority_present": bool(dd_limit is not None),
        "dd_authority_source": dd_source,
        "target_percent": float(target) if target is not None else 0.0,
        "target_authority_present": bool(stage not in {"phase1", "phase2"} or target is not None),
        "target_authority_source": target_source,
    }


TERMINAL_RETIRE_STATUSES = {
    "archived", "archived_phase1", "archived_phase2", "archived_funded",
    "archived_reset", "archived_reset_phase1", "archived_reset_phase2", "archived_reset_funded",
    "breached", "breached_archived", "passed", "closed", "disabled", "locked",
    "disqualified"
}

def _parse_iso_dt(value):
    if not value:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def _terminal_retirement_reference_time(account):
    for key in ("archived_at", "breached_at", "passed_at", "reset_at", "updated_at", "created_at", "started_at"):
        dt = _parse_iso_dt((account or {}).get(key))
        if dt is not None:
            return dt
    return None

def _should_retire_terminal_account(account, now=None):
    from datetime import datetime, timezone, timedelta
    now = now or datetime.now(timezone.utc)
    status = str((account or {}).get("account_status") or (account or {}).get("status") or "").strip().lower()
    is_terminal = status in TERMINAL_RETIRE_STATUSES or status.startswith("archived_reset")
    if not is_terminal:
        return False, "not_terminal"
    ref = _terminal_retirement_reference_time(account)
    if ref is None:
        return False, "missing_terminal_timestamp"
    if ref > now - timedelta(days=max(1, TERMINAL_RETIRE_AFTER_DAYS)):
        return False, "within_retention_window"
    return True, f"terminal_{TERMINAL_RETIRE_AFTER_DAYS}d_plus"

def retire_old_terminal_accounts():
    """Best-effort retirement: keep history, stop MT5 monitoring. No deletes."""
    try:
        rows = (
            supabase.table("trader_accounts")
            .select("id,account_status,status,monitoring_enabled,archived_at,breached_at,passed_at,reset_at,updated_at,created_at,started_at")
            .limit(MONITORABLE_LIMIT)
            .execute().data or []
        )
        retired = 0
        for row in rows:
            should, reason = _should_retire_terminal_account(row)
            if not should:
                continue
            if bool_false(row.get("monitoring_enabled")):
                continue
            safe_update("trader_accounts", {
                "monitoring_enabled": False,
                "updated_at": now_iso(),
            }, "id", row.get("id"))
            retired += 1
        if retired:
            print(f"TERMINAL RETIREMENT: retired {retired} old terminal account(s) from MT5 monitoring", flush=True)
        return retired
    except Exception as e:
        print("TERMINAL RETIREMENT ERROR:", e, flush=True)
        return 0

def _fast_monitorable_feed():
    """Production-critical MT5 discovery path.

    Maximum normal DB work:
      1 trader_accounts query
      1 challenge_purchases bulk query
      1 mt5_pool bulk query
      1 traders bulk query
      1 challenge_plans bulk query

    No per-account queries. No monitoring_events writes. No lifecycle reconciliation.
    """
    rows = (
        supabase.table("trader_accounts")
        .select("*")
        .in_("account_status", sorted(ACTIVE_ACCOUNT_STATUSES))
        .limit(MONITORABLE_LIMIT)
        .execute()
        .data
        or []
    )

    # Cheapest account-level safety first.
    base = []
    for a in rows:
        if not is_active_monitoring_account(a):
            continue
        if not str(a.get("mt5_server") or "").strip():
            continue
        if bool_false(a.get("monitoring_enabled")) or bool_true(a.get("mt5_access_disabled")):
            continue
        if a.get("superseded_at") or a.get("replaced_at") or bool_true(a.get("superseded")):
            continue
        base.append(a)

    purchase_map = _bulk_rows("challenge_purchases", [a.get("purchase_id") for a in base])
    pool_map = _bulk_rows("mt5_pool", [a.get("mt5_pool_id") for a in base])

    eligible = []
    excluded = []
    for a in base:
        purchase = purchase_map.get(str(a.get("purchase_id") or "")) or {}
        pool = pool_map.get(str(a.get("mt5_pool_id") or "")) or {}
        ok, reason = _quiet_monitoring_eligibility(a, purchase, pool)
        if ok:
            eligible.append(a)
        else:
            excluded.append((a, reason))

    # Exact-login ambiguity remains a hard safety exclusion, but logging is console-only.
    by_login = {}
    for a in eligible:
        by_login.setdefault(clean_login(a.get("mt5_login")), []).append(a)

    clean = []
    ambiguous = 0
    for login, group in by_login.items():
        if login and len(group) == 1:
            clean.append(group[0])
        else:
            ambiguous += len(group)
            print(
                "FAST DISCOVERY EXCLUDED AMBIGUOUS LOGIN:",
                {"mt5_login": login, "account_ids": [r.get("id") for r in group]},
                flush=True,
            )

    trader_map = _bulk_rows("traders", [a.get("trader_id") for a in clean])

    # Plan lookup is also bulk and only used as fallback when account/purchase does not
    # already carry frozen commercial rules.
    plan_ids = []
    for a in clean:
        p = purchase_map.get(str(a.get("purchase_id") or "")) or {}
        plan_id = a.get("plan_id") or p.get("plan_id") or p.get("challenge_plan_id")
        if plan_id:
            plan_ids.append(plan_id)
    plan_map = _bulk_rows("challenge_plans", plan_ids)

    out = []
    for a in clean:
        t = trader_map.get(str(a.get("trader_id") or "")) or {}
        p = purchase_map.get(str(a.get("purchase_id") or "")) or {}
        plan_id = a.get("plan_id") or p.get("plan_id") or p.get("challenge_plan_id")
        plan = plan_map.get(str(plan_id or "")) or {}
        rule_values = _fast_rule_values(a, p, plan)
        dd_limit = rule_values["dd_limit_percent"]
        target = rule_values["target_percent"]

        out.append({
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
            "dd_limit_percent": dd_limit,
            "dd_authority_present": rule_values["dd_authority_present"],
            "dd_authority_source": rule_values.get("dd_authority_source") or "authority_missing",
            "target_percent": target,
            "target_authority_present": rule_values["target_authority_present"],
            "target_authority_source": rule_values.get("target_authority_source") or "authority_missing",
            "balance": num(a.get("current_balance") or a.get("start_balance") or a.get("account_size")),
            "current_balance": num(a.get("current_balance") or a.get("start_balance") or a.get("account_size")),
            "equity": num(a.get("current_equity") or a.get("current_balance") or a.get("start_balance") or a.get("account_size")),
            "current_equity": num(a.get("current_equity") or a.get("current_balance") or a.get("start_balance") or a.get("account_size")),
            "highest_equity": num(a.get("highest_equity") or a.get("current_equity") or a.get("start_balance") or a.get("account_size")),
            "lowest_equity": num(a.get("lowest_equity") or a.get("start_balance") or a.get("account_size")),
            "profit_percent": num(a.get("profit_percent")),
            "risk_zone": a.get("risk_zone") or "safe",
            "_source_of_truth": "monitoring_api_fast_discovery",
        })

    print(
        "FAST DISCOVERY COMPLETE:",
        {
            "active_rows": len(rows),
            "base_eligible": len(base),
            "monitorable": len(out),
            "excluded": len(excluded),
            "ambiguous": ambiguous,
        },
        flush=True,
    )
    return out


@app.route("/monitorable_accounts")
def monitorable_accounts():
    """Fast, read-only discovery endpoint for the Windows MT5 engine."""
    try:
        out = _fast_monitorable_feed()
        return ok(out, f"{len(out)} monitorable account(s)")
    except Exception as e:
        print("FAST DISCOVERY FATAL ERROR:", repr(e), flush=True)
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
    if not isinstance(result, dict) or not result.get("account_write_ok"):
        return bad(f"Snapshot persistence failed for MT5 {data.get('mt5_login')}: {result}", 500)
    if result.get("breached") and str(result.get("persisted_account_status") or "").lower() != "breached_archived":
        return bad(f"Breach persistence verification failed for MT5 {data.get('mt5_login')}: status={result.get('persisted_account_status')}", 500)
    return ok(result, "snapshot applied and verified")


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
    # Redundant final-evidence persistence: if /monitoring_snapshot failed because an
    # optional schema column rejected the full payload, the lock endpoint still saves
    # the real broker numbers with the terminal state.
    evidence_map = {
        "current_balance": data.get("current_balance") if data.get("current_balance") not in (None, "") else data.get("mt5_balance"),
        "current_equity": data.get("equity"),
        "profit": data.get("profit"),
        "profit_percent": data.get("profit_percent"),
        "highest_equity": data.get("highest_equity"),
        "lowest_equity": data.get("lowest_equity"),
        "absolute_drawdown_percent": data.get("drawdown_percent") if data.get("drawdown_percent") not in (None, "") else data.get("drawdown"),
        "drawdown_percent": data.get("drawdown_percent") if data.get("drawdown_percent") not in (None, "") else data.get("drawdown"),
        "dd_used_percent": data.get("dd_used_percent"),
        "phase_pass_status": "" if "breach" in status else data.get("phase_pass_status"),
        "breached_at": now_iso() if "breach" in status else account.get("breached_at"),
        "breach_reason": reason if "breach" in status else account.get("breach_reason"),
    }
    for k, v in evidence_map.items():
        if v not in (None, ""):
            payload[k] = v

    account_write_ok, persisted, write_mode = verified_account_update(account.get("id"), payload)
    trader_write_ok = verified_trader_update(account.get("trader_id"), {"status": "breached" if "breach" in status else status, "challenge_state": status, "mt5_access_disabled": True, "monitoring_enabled": False, "updated_at": now_iso()})
    safe_insert("monitoring_events", {"trader_id": account.get("trader_id"), "trader_account_id": account.get("id"), "mt5_login": account.get("mt5_login"), "event_type": status, "risk_zone": "breached" if "breach" in status else status, "message": reason, "balance": payload.get("current_balance"), "equity": payload.get("current_equity"), "drawdown_percent": payload.get("drawdown_percent"), "dd_used_percent": payload.get("dd_used_percent"), "created_at": now_iso()})
    alert_once(account, status, status.upper(), reason, "critical", data)
    expected_status = "breached_archived" if "breach" in status else status
    persisted_status = str((persisted or {}).get("account_status") or "").lower()
    if not account_write_ok or persisted_status != str(expected_status).lower():
        return bad(f"MT5 lock persistence failed: account_write_ok={account_write_ok}, mode={write_mode}, persisted_status={persisted_status}, expected={expected_status}", 500)
    return ok({"account_id": account.get("id"), "status": status, "persisted_account_status": persisted_status, "persisted_balance": (persisted or {}).get("current_balance"), "persisted_equity": (persisted or {}).get("current_equity"), "account_write_mode": write_mode, "trader_write_ok": trader_write_ok}, "access disabled and verified")


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



@app.route("/rule_authority_health", methods=["GET"])
def rule_authority_health():
    """Read-only production gate: challenge accounts must have exact target + DD authority."""
    try:
        items = _fast_monitorable_feed()
        unresolved = []
        for row in items:
            stage = str(row.get("stage") or row.get("phase") or "").lower()
            if stage not in {"phase1", "phase2"}:
                continue
            missing = []
            if not row.get("target_authority_present"):
                missing.append("target")
            if not row.get("dd_authority_present"):
                missing.append("dd")
            if missing:
                unresolved.append({
                    "trader_account_id": row.get("trader_account_id") or row.get("id"),
                    "mt5_login": row.get("mt5_login"),
                    "stage": stage,
                    "missing": missing,
                    "target_percent": row.get("target_percent"),
                    "target_source": row.get("target_authority_source"),
                    "dd_limit_percent": row.get("dd_limit_percent"),
                    "dd_source": row.get("dd_authority_source"),
                })
        if unresolved:
            print("CRITICAL RULE AUTHORITY UNRESOLVED:", unresolved, flush=True)
        return jsonify({
            "success": True,
            "healthy": len(unresolved) == 0,
            "monitorable_count": len(items),
            "unresolved_count": len(unresolved),
            "unresolved": unresolved,
            "release": "PAYING_CUSTOMER_TARGET_DD_HARDENED_2026_09_02",
        })
    except Exception as exc:
        return jsonify({"success": False, "healthy": False, "error": str(exc)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
