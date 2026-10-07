# V17: breach persistence constraint compatibility (breach_reason + breach_at + breach_equity_level)
NAIRAPIPS_MONITORING_RELEASE = "V45_FAST_ROSTER_ACTIVITY_PROOF_2026_10_07"
import time
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client
from datetime import datetime, timezone, timedelta
import os, re, json
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

app = Flask(__name__)
NAIRAPIPS_RELEASE = "MT5_BALANCE_INPUT_NORMALIZED_FINAL_2026_07_23"
CORS(app)
# V23 removed duplicate NAIRAPIPS_MONITORING_RELEASE override

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


ACTIVE_ACCOUNT_STATUSES = {"assigned_active", "active", "current_active", "phase1_active", "phase2_active", "funded_active", "live_active", "live", "funded", "approved_active", "funded_profit_cap_reached", "profit_protected"}
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

    # V21 authoritative terminal evidence only.
    # Never cut a live account because of age, MT5-number series, a rounded/stale
    # risk-zone label, or a mutable purchase/pool mirror.
    if (row or {}).get("breach_at") or (row or {}).get("breached_at"):
        return False
    if (row or {}).get("reset_at"):
        return False
    if (row or {}).get("archived_at") and not is_funded_cap_lock(row):
        return False
    if (row or {}).get("superseded_at") or (row or {}).get("replaced_at") or bool_true((row or {}).get("superseded")):
        return False

    # Funded 15% cap remains live/watchdog-monitorable even while broker access is disabled.
    if str((row or {}).get("mt5_access_disabled") or "").lower() in {"true", "1", "yes"} and not is_funded_cap_lock(row):
        return False
    return valid_login((row or {}).get("mt5_login"))


def bool_false(value):
    return str(value).strip().lower() in {"false", "0", "no", "off"}


def bool_true(value):
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def is_funded_cap_lock(row):
    """Funded financial lock states are LIVE/WATCHDOG, not terminal.

    - funded_profit_cap_reached: 15% funded cycle-cap lock
    - profit_protected: exact funded payout-request lock

    Both must remain visible to monitoring so the watchdog can enforce
    no-further-trading while the payout/cap cycle is outstanding.
    """
    status = str((row or {}).get("account_status") or (row or {}).get("status") or "").strip().lower()
    return status in {"funded_profit_cap_reached", "profit_protected"}


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
    """Exact active trader_account is the live-monitoring authority.

    Lifecycle mirrors in challenge_purchases and mt5_pool can legitimately move
    to a newer child account (Second Life, payout renewal, funded replacement,
    etc.). Those mutable mirrors must not silently remove an otherwise genuine
    active MT5 from monitoring.

    Hard safety contradictions still block:
      - the exact trader_account is terminal/disabled/superseded
      - no MT5 server/login
      - purchase or MT5-pool ownership points to ANOTHER trader
      - MT5-pool login points to a different login
    """
    if not is_active_monitoring_account(account):
        return False, "account is not monitorable"
    if require_server and not str((account or {}).get("mt5_server") or "").strip():
        return False, "account has no mt5_server"
    if bool_false((account or {}).get("monitoring_enabled")):
        return False, "account monitoring_enabled is false"
    if bool_true((account or {}).get("mt5_access_disabled")) and not is_funded_cap_lock(account):
        return False, "account mt5_access_disabled is true"
    if (account or {}).get("superseded_at") or (account or {}).get("replaced_at") or bool_true((account or {}).get("superseded")):
        return False, "account is superseded"

    account_trader_id = str((account or {}).get("trader_id") or "").strip()
    account_login = clean_login((account or {}).get("mt5_login"))
    purchase_id = str((account or {}).get("purchase_id") or "").strip()

    # Purchase is journey/provenance evidence, not live monitoring authority.
    # Only a different OWNER is a hard block. Child-account pointers, MT5 pointer,
    # pool pointer and waiting/reset lifecycle words are logged but tolerated.
    if purchase_id:
        if purchase:
            purchase_trader_id = str((purchase or {}).get("trader_id") or "").strip()
            if purchase_trader_id and account_trader_id and purchase_trader_id != account_trader_id:
                return False, "purchase trader_id mismatch"
            ok_purchase, reason = is_active_purchase_for_account(purchase, account)
            if not ok_purchase:
                log_lifecycle_inconsistency(
                    "purchase mirror disagrees with exact active trader_account; account remains monitorable: " + str(reason),
                    account, purchase, mt5_pool or {}, trader or {}
                )
        else:
            log_lifecycle_inconsistency(
                "linked purchase not found; exact active trader_account remains monitorable",
                account, {}, mt5_pool or {}, trader or {}
            )
    else:
        log_no_purchase_monitoring_allowed(account)

    # MT5 pool is inventory/history after assignment. A stale status or stale
    # trader_account pointer must not freeze a valid active account. But ownership
    # to ANOTHER trader or a different MT5 login is a hard safety contradiction.
    if mt5_pool:
        pool_trader_id = str((mt5_pool or {}).get("assigned_trader_id") or (mt5_pool or {}).get("trader_id") or "").strip()
        if pool_trader_id and account_trader_id and pool_trader_id != account_trader_id:
            return False, "mt5_pool linked to a different trader"
        pool_login = clean_login((mt5_pool or {}).get("mt5_login"))
        if pool_login and account_login and pool_login != account_login:
            return False, "mt5_pool mt5_login mismatch"
        ok_pool, reason = is_active_pool_for_account(mt5_pool, account)
        if not ok_pool:
            log_lifecycle_inconsistency(
                "mt5_pool mirror disagrees with exact active trader_account; account remains monitorable: " + str(reason),
                account, purchase or {}, mt5_pool, trader or {}
            )

    if trader:
        t_blob = lifecycle_blob(trader, ["challenge_state", "status", "phase"])
        if any(word in t_blob for word in ("waiting", "reset", "breached", "archived", "disabled", "closed", "passed_review")):
            log_lifecycle_inconsistency(
                "trader-level lifecycle disagrees with exact active account; account remains monitorable",
                account, purchase or {}, mt5_pool or {}, trader
            )

    return True, "eligible_exact_active_trader_account"


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


def static_dd_precise(start_balance, equity):
    """Full-precision DD for lifecycle/risk decisions; rounded DD is display-only."""
    start = num(start_balance)
    eq = num(equity)
    if start <= 0:
        return 0.0
    return max(((start - eq) / start) * 100, 0.0)


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


def get_account_by_id_any_status(account_id=None, mt5_login=None):
    """Resolve one exact trader_accounts row without active/eligible filtering.

    Used only for terminal/idempotent writes after a verified live snapshot may have
    already changed the account from active to breached_archived.  If an MT5 login
    is supplied it MUST still match the row, so this never falls back by trader.
    """
    try:
        if not account_id:
            return None
        rows = supabase.table("trader_accounts").select("*").eq("id", account_id).limit(1).execute().data or []
        account = rows[0] if rows else None
        if not account:
            return None
        login = clean_login(mt5_login)
        if login and clean_login(account.get("mt5_login")) != login:
            log_lifecycle_inconsistency("terminal write supplied trader_account_id but mt5_login does not match", account)
            return None
        return account
    except Exception as e:
        print("ACCOUNT ANY-STATUS FETCH ERROR:", e, flush=True)
        return None


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

    current_dd_precise = static_dd_precise(start, equity)
    current_dd = round(current_dd_precise, 2)
    current_dd_used = dd_used_from_static(current_dd_precise, dd_limit_percent) if dd_authority_present else 0.0
    worst_dd_precise = static_dd_precise(start, lowest)
    worst_dd = round(worst_dd_precise, 2)
    worst_dd_used = dd_used_from_static(worst_dd_precise, dd_limit_percent) if dd_authority_present else 0.0
    dd_remaining = round(max(dd_limit_percent - current_dd_precise, 0), 2) if dd_authority_present else 0.0
    zone = risk_zone(current_dd_precise, dd_limit_percent) if dd_authority_present else "authority_missing"

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
    terminal_breach_recorded = bool(account.get("breach_at") or account.get("breached_at"))
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
            _breach_ts = account.get("breach_at") or account.get("breached_at") or now_iso()
            update["breach_at"] = _breach_ts
            update["breached_at"] = _breach_ts
            update["breach_reason"] = snapshot.get("reason") or (
                f"Static {dd_limit_percent:g}% drawdown breached. "
                f"Lowest/current evidence reached {min(lowest, equity, current_balance):,.2f} "
                f"against breach level {breach_level:,.2f}."
            )
            update["phase_pass_status"] = ""
            update["passed_at"] = None
        elif target_hit:
            update["passed_at"] = now_iso()

    # V14: DB guard-safe breach preflight.
    # First transaction writes ONLY breach_reason. This deliberately avoids
    # account_status, breached_at, archived_at and every other terminal field.
    # After readback proves the reason exists, the existing coherent update
    # performs the breached_archived transition.
    if breached:
        preflight_reason = str(update.get("breach_reason") or "").strip()
        if not preflight_reason:
            return {
                "account_id": account.get("id"), "mt5_login": account.get("mt5_login"),
                "breached": True, "account_write_ok": False,
                "account_write_mode": "breach_reason_missing",
                "persisted_account_status": account.get("account_status"),
            }
        preflight_ok, preflight_row, preflight_mode = verified_account_update(
            account.get("id"), {
                "breach_reason": preflight_reason,
                "breach_at": update.get("breach_at") or now_iso(),
                "breach_equity_level": update.get("breach_equity_level") or min(lowest, equity, current_balance),
            }
        )
        stored_reason = str((preflight_row or {}).get("breach_reason") or "").strip()
        if not preflight_ok or not stored_reason:
            print(
                f"CRITICAL V14 BREACH_REASON-ONLY PREFLIGHT FAILED "
                f"mt5={account.get('mt5_login')} account_id={account.get('id')} "
                f"mode={preflight_mode}",
                flush=True,
            )
            return {
                "account_id": account.get("id"), "mt5_login": account.get("mt5_login"),
                "breached": True, "account_write_ok": False,
                "account_write_mode": f"breach_reason_only_preflight_failed:{preflight_mode}",
                "persisted_account_status": (preflight_row or {}).get("account_status") or account.get("account_status"),
            }
        print(f"V14 BREACH_REASON PREFLIGHT VERIFIED MT5={account.get('mt5_login')}", flush=True)

    account_write_ok, persisted_account, account_write_mode = verified_account_update(account.get("id"), update)
    if not account_write_ok:
        print(f"CRITICAL SNAPSHOT ACCOUNT WRITE FAILED mt5={account.get('mt5_login')} account_id={account.get('id')}", flush=True)

    # V27 permanent registry handoff:
    # once this exact account is successfully terminal (breach or pass), remove
    # it from the live registry immediately. Historical trader_accounts data stays.
    if account_write_ok and (breached or target_hit):
        persisted_status_v27 = str((persisted_account or {}).get("account_status") or "").lower()
        terminal_v27 = (
            persisted_status_v27.startswith("breached")
            or persisted_status_v27.startswith("archived")
            or persisted_status_v27.startswith("passed")
        )
        if terminal_v27:
            _retire_registry_exact(
                account.get("id"),
                "breach_completed" if breached else f"{stage}_passed",
            )

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


def _np_recall_main_journey_snapshot(auth_header, trader_id, journey_id):
    """Read the existing Main-API Journey Authority. This function never mutates lifecycle state."""
    if not auth_header or not trader_id or not journey_id:
        return None, "missing journey authority identity"
    try:
        qs = urlencode({
            "trader_id": str(trader_id),
            "journey_id": str(journey_id),
            "fresh": str(int(time.time() * 1000)),
        })
        req = urlrequest.Request(
            MAIN_API_URL + "/admin_journey_authority?" + qs,
            headers={"Authorization": auth_header, "Accept": "application/json"},
            method="GET",
        )
        with urlrequest.urlopen(req, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        if payload.get("success") is False:
            return None, str(payload.get("error") or payload.get("message") or "Journey Authority rejected the request")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        journey = data.get("journey") if isinstance(data, dict) else None
        if not isinstance(journey, dict):
            return None, "Journey Authority returned no journey"
        return journey, None
    except Exception as exc:
        return None, str(exc)


def _np_recall_consumed_authority(journey, account):
    """Return the exact entitlement that produced THIS account, or fail closed."""
    journey = journey or {}
    account = account or {}
    if journey.get("blocked"):
        return None, "Journey Authority is blocked; reconcile the journey before Recall"

    account_id = str(account.get("id") or "").strip()
    mt5_login = str(account.get("mt5_login") or "").strip()
    ledger = list(journey.get("ledger") or [])

    exact = []
    for idx, event in enumerate(ledger):
        if str((event or {}).get("type") or "").strip().upper() != "MT5_ASSIGNED":
            continue
        if account_id and str((event or {}).get("account_id") or "").strip() == account_id:
            exact.append((idx, event))
        elif mt5_login and str((event or {}).get("mt5_login") or "").strip() == mt5_login:
            exact.append((idx, event))

    if not exact:
        return None, "The selected MT5 has no exact MT5_ASSIGNED event in this journey"

    idx, event = exact[-1]
    authority = (event or {}).get("authority") or {}
    if not isinstance(authority, dict):
        authority = {}

    entitlement_key = str(authority.get("entitlement_key") or "").strip()
    entitlement_type = str(authority.get("entitlement_type") or "").strip()
    target_stage = str(authority.get("target_stage") or (event or {}).get("stage") or "").strip().lower()
    if target_stage in {"live", "funded_live"}:
        target_stage = "funded"

    account_stage = str(account.get("stage") or account.get("phase") or "").strip().lower()
    if account_stage in {"live", "funded_live"}:
        account_stage = "funded"

    if not entitlement_key or not entitlement_type:
        return None, "The selected assignment has no exact entitlement evidence"
    if not target_stage or target_stage != account_stage:
        return None, "The selected account stage does not match the entitlement that created it"

    # Never recall an older link after a later MT5 has already been delivered in the same journey.
    for later in ledger[idx + 1:]:
        if str((later or {}).get("type") or "").strip().upper() != "MT5_ASSIGNED":
            continue
        later_id = str((later or {}).get("account_id") or "").strip()
        later_login = str((later or {}).get("mt5_login") or "").strip()
        if later_id and later_id != account_id:
            return None, f"A later MT5 assignment ({later_login or later_id}) already exists in this journey"

    return {
        "entitlement_key": entitlement_key,
        "entitlement_type": entitlement_type,
        "source_account_id": str(authority.get("source_account_id") or "").strip(),
        "source_mt5": str(authority.get("source_mt5") or "").strip(),
        "evidence_id": str(authority.get("evidence_id") or "").strip(),
        "target_stage": target_stage,
    }, None


@app.route("/admin_recall_wrong_assignment", methods=["POST", "OPTIONS"])
def admin_recall_wrong_assignment():
    """Void ONE unused invalid MT5 assignment and restore ONLY the exact entitlement that created it.

    Global safety law:
      bad MT5 assignment -> Recall exact child -> same entitlement becomes WAITING again.
    Recall itself never creates a new payout/reset/life/pass entitlement and never advances stage.
    """
    if request.method == "OPTIONS":
        return ok({})
    _admin, auth_error = require_main_api_admin()
    if auth_error:
        return auth_error

    auth_header = str(request.headers.get("Authorization") or "").strip()
    data = request.get_json(silent=True) or {}
    trader_id = str(data.get("trader_id") or "").strip()
    account_id = str(data.get("trader_account_id") or "").strip()
    note = str(data.get("admin_note") or "Invalid/stale MT5 assignment").strip()
    if not trader_id or not account_id:
        return bad("trader_id and exact trader_account_id are required", 400)

    try:
        account_rows = supabase.table("trader_accounts").select("*").eq("id", account_id).limit(1).execute().data or []
        if not account_rows:
            return bad("Selected account was not found", 404)
        account = account_rows[0]
        if str(account.get("trader_id") or "") != trader_id:
            return bad("Selected account does not belong to this trader", 409)

        status_before = str(account.get("account_status") or account.get("status") or "").strip().lower()
        archive_blob = " ".join(str(account.get(k) or "") for k in ("archive_reason", "admin_note", "status", "account_status")).lower()
        if "np_terminal:recalled_wrong_assignment" in archive_blob or "wrong_assignment_recalled" in archive_blob:
            return ok({"idempotent": True, "recalled_account_id": account_id}, "Invalid MT5 was already recalled")
        if status_before not in ACTIVE_ACCOUNT_STATUSES:
            return bad("Only the exact active invalid assignment can be recalled", 409)

        journey_id = str(account.get("purchase_id") or account.get("challenge_purchase_id") or "").strip()
        if not journey_id:
            return bad(
                "Recall blocked safely: this MT5 has no exact journey link. Use manual review; no replacement entitlement was created.",
                409,
            )

        # PRE-FLIGHT: prove the exact event/entitlement that created this MT5 BEFORE changing anything.
        before_journey, before_error = _np_recall_main_journey_snapshot(auth_header, trader_id, journey_id)
        if before_error:
            return bad("Recall preflight failed: " + before_error, 409)
        consumed_authority, authority_error = _np_recall_consumed_authority(before_journey, account)
        if authority_error:
            return bad("Recall blocked safely: " + authority_error, 409)

        # Authoritative use check: an actually traded account cannot be undone as an invalid assignment.
        trades = (
            supabase.table("trader_trades").select("id")
            .eq("trader_account_id", account_id).limit(1).execute().data or []
        )
        if trades:
            return bad(
                "Recall blocked: an actual trade exists on this exact MT5. Use the appropriate lifecycle/reset/recovery rule instead.",
                409,
            )

        # Financial use is also irreversible through this technical Recall path.
        payout_use = []
        try:
            payout_use = (
                supabase.table("payouts").select("id,status")
                .eq("trader_id", trader_id).eq("trader_account_id", account_id)
                .limit(1).execute().data or []
            )
            if not payout_use and str(account.get("mt5_login") or "").strip():
                payout_use = (
                    supabase.table("payouts").select("id,status")
                    .eq("trader_id", trader_id).eq("mt5_login", str(account.get("mt5_login") or "").strip())
                    .limit(1).execute().data or []
                )
        except Exception as exc:
            return bad("Recall payout-use verification failed closed: " + str(exc), 500)
        if payout_use:
            return bad("Recall blocked: this exact MT5 already has payout activity and cannot be technically undone.", 409)

        # Preserve any other genuinely active account owned by the trader.
        remaining = (
            supabase.table("trader_accounts").select("*").eq("trader_id", trader_id)
            .in_("account_status", sorted(ACTIVE_ACCOUNT_STATUSES)).order("updated_at", desc=True).limit(100).execute().data or []
        )
        remaining = [row for row in remaining if str(row.get("id") or "") != account_id]
        genuine = remaining[0] if remaining else None

        now = now_iso()
        ent_key = consumed_authority["entitlement_key"]
        ent_type = consumed_authority["entitlement_type"]
        evidence_id = consumed_authority.get("evidence_id") or ""
        source_id = consumed_authority.get("source_account_id") or ""
        target_stage = consumed_authority["target_stage"]
        mt5_login = str(account.get("mt5_login") or "").strip()

        evidence = (
            f"Invalid MT5 assignment recalled. {note} | "
            f"[NP_JOURNEY:{journey_id}] | [NP_RECALL:{account_id}] | "
            f"[NP_RECALL_ENTITLEMENT:{ent_key}] | [NP_RECALL_TYPE:{ent_type}] | "
            f"[NP_RECALL_EVIDENCE:{evidence_id}] | [NP_RECALL_SOURCE:{source_id}] | "
            f"target_stage={target_stage} | MT5={mt5_login}"
        )
        terminal_reason = (
            "wrong_assignment_recalled | [NP_TERMINAL:RECALLED_WRONG_ASSIGNMENT] | "
            f"[NP_JOURNEY:{journey_id}] | [NP_RECALL:{account_id}] | "
            f"[NP_RECALL_ENTITLEMENT:{ent_key}] | [NP_RECALL_TYPE:{ent_type}] | "
            f"[NP_RECALL_EVIDENCE:{evidence_id}] | [NP_RECALL_SOURCE:{source_id}]"
        )

        # 1) Void ONLY the bad child account. It remains immutable audit history.
        account_ok, _removed, account_error = _np_adaptive_table_update("trader_accounts", "id", account_id, {
            "account_status": "archived",
            "status": "archived",
            "risk_zone": "archived",
            "archive_reason": terminal_reason,
            "admin_note": evidence,
            "archived_at": now,
            "phase_pass_status": None,
            "pass_status": None,
            "passed_at": None,
            "breach_reason": None,
            "breached_at": None,
            "monitoring_enabled": False,
            "updated_at": now,
        })
        if not account_ok:
            return bad(f"Recall failed while archiving the selected account: {account_error}", 500)

        # 2) Quarantine the invalid Exness credential. NEVER return it to AVAILABLE.
        pool_id = str(account.get("mt5_pool_id") or "").strip()
        if pool_id:
            pool_ok, _removed, pool_error = _np_adaptive_table_update("mt5_pool", "id", pool_id, {
                "status": "recalled_hold",
                "assigned_trader_id": None,
                "assigned_trader_name": None,
                "assigned_email": None,
                "trader_account_id": None,
                "archived_at": now,
                "archive_reason": "invalid_or_stale_mt5_recalled",
                "admin_note": evidence + " | DO NOT REUSE UNTIL BROKER CREDENTIAL IS REVALIDATED",
                "updated_at": now,
            })
            if not pool_ok:
                return bad(f"Account recalled but MT5 security hold failed: {pool_error}", 500)

        # IMPORTANT: DO NOT archive/close/reset the root purchase here.
        # The purchase/payout/pass/reset event is the business authority we need to preserve.
        # Journey Authority will now ignore this recalled unused child and reveal the SAME
        # exact entitlement again. Recall creates no new entitlement.

        if genuine:
            genuine_stage = str(genuine.get("stage") or "phase1").strip().lower()
            trader_payload = {
                "current_account_id": genuine.get("id"),
                "challenge_state": "funded_active" if genuine_stage == "funded" else f"{genuine_stage}_active",
                "status": "active",
                "phase": genuine_stage,
                "mt5_login": genuine.get("mt5_login") or "",
                "mt5_server": genuine.get("mt5_server") or "",
                "mt5_master_password": genuine.get("mt5_master_password") or "",
                "mt5_password": genuine.get("mt5_master_password") or "",
                "master_password": genuine.get("mt5_master_password") or "",
                "mt5_investor_password": genuine.get("mt5_investor_password") or "",
                "investor_password": genuine.get("mt5_investor_password") or "",
                "monitoring_enabled": bool(genuine.get("monitoring_enabled", True)),
                "mt5_account_active": True,
                "mt5_access_disabled": False,
                "mt5_reset_reason": None,
                "admin_note": evidence,
                "lifecycle_updated_at": now,
                "updated_at": now,
            }
            reconcile_message = "Another genuine active account was preserved"
        else:
            trader_payload = {
                "current_account_id": None,
                "challenge_state": "waiting_mt5",
                "status": "active",
                "phase": target_stage,
                "mt5_login": "",
                "mt5_server": "",
                "mt5_master_password": "",
                "mt5_password": "",
                "master_password": "",
                "mt5_investor_password": "",
                "investor_password": "",
                "monitoring_enabled": False,
                "mt5_account_active": False,
                "mt5_access_disabled": False,
                "mt5_reset_reason": None,
                "admin_note": evidence + " | exact entitlement restoration pending Main Journey Authority",
                "lifecycle_updated_at": now,
                "updated_at": now,
            }
            reconcile_message = "Invalid MT5 removed; trader is waiting for the same entitlement to be fulfilled"

        if not verified_trader_update(trader_id, trader_payload):
            return bad("Recall completed but trader current-account reconciliation failed", 500)

        safe_insert("lifecycle_events", {
            "trader_id": trader_id,
            "trader_account_id": account_id,
            "from_state": status_before,
            "to_state": "recalled_wrong_assignment",
            "action": "admin_recall_wrong_assignment",
            "details": evidence,
            "created_by": str(data.get("admin_username") or data.get("admin_name") or "admin"),
            "created_at": now,
        })

        # POST-FLIGHT: the exact entitlement key that was consumed by the invalid child
        # must be the exact key now outstanding. Anything else fails closed for review.
        after_journey, after_error = _np_recall_main_journey_snapshot(auth_header, trader_id, journey_id)
        restored = False
        restored_entitlement = None
        review_reason = None
        if after_error:
            review_reason = "Post-recall Journey Authority check failed: " + after_error
        elif after_journey.get("blocked"):
            review_reason = "Post-recall journey is blocked for reconciliation"
        else:
            ent = after_journey.get("outstanding_entitlement") or {}
            if str(ent.get("entitlement_key") or "").strip() == ent_key:
                ent_target = str(ent.get("target_stage") or "").strip().lower()
                if ent_target in {"live", "funded_live"}:
                    ent_target = "funded"
                if ent_target == target_stage:
                    restored = True
                    restored_entitlement = ent
                else:
                    review_reason = "Restored entitlement target stage does not match the recalled assignment"
            else:
                review_reason = "The original entitlement key did not reappear after Recall"

        outcome = (
            f"same exact entitlement restored: {ent_key}; waiting for one {target_stage} MT5"
            if restored
            else f"recall quarantined; RECONCILIATION REQUIRED: {review_reason}"
        )
        safe_insert("monitoring_events", {
            "trader_id": trader_id,
            "trader_account_id": account_id,
            "mt5_login": mt5_login,
            "event_type": "admin_recall_wrong_assignment",
            "risk_zone": "archived",
            "message": evidence + " | " + outcome,
            "created_at": now,
        })

        if not restored:
            # Do not roll the bad/invalid MT5 back into service. It remains safely quarantined.
            # Crucially, no replacement is released unless Main Journey Authority proves the
            # original entitlement. This is the fail-closed protection against over-assignment.
            return ok({
                "recalled_account_id": account_id,
                "recalled_mt5_login": mt5_login,
                "pool_status": "recalled_hold",
                "replacement_created": False,
                "replacement_entitlement_reopened": False,
                "requires_review": True,
                "review_reason": review_reason,
                "original_entitlement_key": ent_key,
                "original_entitlement_type": ent_type,
                "target_stage": target_stage,
                "trader_reconciliation": reconcile_message,
            }, "Invalid MT5 recalled and quarantined. No replacement was released because exact entitlement reconciliation requires review.", 202)

        # Current production payout-renewal is event-driven (not broad DB sweeping).
        # Once Recall has PROVEN the exact same entitlement is restored, wake ONLY
        # that exact entitlement. For payout renewal this uses its immutable payout_id.
        # Any failure leaves the entitlement safely WAITING; Recall itself stays valid.
        replacement_kick = _np_recall_kick_exact_replacement_v9(
            auth_header=auth_header,
            trader_id=trader_id,
            journey_id=journey_id,
            entitlement_type=ent_type,
            evidence_id=evidence_id,
        )

        return ok({
            "recalled_account_id": account_id,
            "recalled_mt5_login": mt5_login,
            "pool_status": "recalled_hold",
            "replacement_created": bool((replacement_kick or {}).get("assigned")),
            "replacement_entitlement_reopened": True,
            "replacement_entitlement_key": ent_key,
            "replacement_entitlement_kind": ent_type,
            "source_account_id": consumed_authority.get("source_account_id") or None,
            "source_mt5": consumed_authority.get("source_mt5") or None,
            "evidence_id": consumed_authority.get("evidence_id") or None,
            "target_stage": target_stage,
            "restored_entitlement": restored_entitlement,
            "replacement_kick": replacement_kick,
            "trader_reconciliation": reconcile_message,
        }, "Invalid MT5 recalled safely. The SAME exact entitlement was restored; exact replacement automation was kicked where supported.")

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
    """Fast read-only version of the exact-active-account monitoring law."""
    if not is_active_monitoring_account(account):
        return False, "account is not monitorable"
    if not str((account or {}).get("mt5_server") or "").strip():
        return False, "account has no mt5_server"
    if bool_false((account or {}).get("monitoring_enabled")):
        return False, "account monitoring_enabled is false"
    if bool_true((account or {}).get("mt5_access_disabled")) and not is_funded_cap_lock(account):
        return False, "account mt5_access_disabled is true"
    if (account or {}).get("superseded_at") or (account or {}).get("replaced_at") or bool_true((account or {}).get("superseded")):
        return False, "account is superseded"

    account_trader_id = str((account or {}).get("trader_id") or "").strip()
    account_login = clean_login((account or {}).get("mt5_login"))
    purchase_id = str((account or {}).get("purchase_id") or "").strip()

    # Never let a mutable purchase child-pointer/lifecycle state silence an exact
    # active trader_account. Different ownership remains a hard block.
    if purchase_id and purchase:
        purchase_trader_id = str((purchase or {}).get("trader_id") or "").strip()
        if purchase_trader_id and account_trader_id and purchase_trader_id != account_trader_id:
            return False, "purchase trader_id mismatch"

    # Pool mismatch to another trader/login is security-sensitive. Other pool
    # mirror disagreement is not allowed to freeze monitoring.
    if mt5_pool:
        pool_trader_id = str((mt5_pool or {}).get("assigned_trader_id") or (mt5_pool or {}).get("trader_id") or "").strip()
        if pool_trader_id and account_trader_id and pool_trader_id != account_trader_id:
            return False, "mt5_pool linked to a different trader"
        pool_login = clean_login((mt5_pool or {}).get("mt5_login"))
        if pool_login and account_login and pool_login != account_login:
            return False, "mt5_pool mt5_login mismatch"

    return True, "eligible_exact_active_trader_account"


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

def _np_fetch_all_active_monitoring_accounts():
    """Fetch the complete active MT5 population with stable pagination.

    The previous discovery query used one `.limit(MONITORABLE_LIMIT)` call.
    Once the active trader_accounts population reached that cap, any active rows
    outside the first response were invisible to the Windows MT5 engine and their
    dashboard metrics remained frozen.

    Pagination is ordered by immutable account id so concurrent metric updates do
    not reshuffle rows between pages. This is READ ONLY and does not modify any
    assignment/lifecycle/Second-Life/reset/payout automation.
    """
    page_size = min(max(int(MONITORABLE_LIMIT or 1000), 100), 1000)
    max_rows = int(os.getenv("NAIRAPIPS_MONITORABLE_MAX_ROWS", "20000") or "20000")
    rows = []
    seen = set()
    offset = 0
    while offset < max_rows:
        batch = (
            supabase.table("trader_accounts")
            .select("*")
            .in_("account_status", sorted(ACTIVE_ACCOUNT_STATUSES))
            .order("id", desc=False)
            .range(offset, min(offset + page_size - 1, max_rows - 1))
            .execute()
            .data
            or []
        )
        if not batch:
            break
        for row in batch:
            key = str((row or {}).get("id") or "").strip()
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            rows.append(row)
        if len(batch) < page_size:
            break
        offset += page_size
    if offset >= max_rows and len(rows) >= max_rows:
        print(
            "CRITICAL MONITORABLE FEED SAFETY CAP REACHED:",
            {"max_rows": max_rows, "returned": len(rows)},
            flush=True,
        )
    return rows


def _fast_monitorable_feed():
    """Production-critical MT5 discovery path.

    Active trader_accounts are fetched with bounded stable pagination so no live
    account disappears merely because the table contains more than one API page.
    The remaining purchase/pool/trader/plan lookups stay bulk-only; there are no
    per-account monitoring-event writes and no lifecycle reconciliation here.
    """
    rows = _np_fetch_all_active_monitoring_accounts()

    # V21: no MT5-number/age heuristic quarantine.
    # Exclude only by authoritative lifecycle/ownership evidence.
    stale_quarantined = []

    # Cheapest account-level safety first.
    base = []
    for a in rows:
        if not is_active_monitoring_account(a):
            continue
        if not str(a.get("mt5_server") or "").strip():
            continue
        if bool_false(a.get("monitoring_enabled")):
            continue
        if bool_true(a.get("mt5_access_disabled")) and not is_funded_cap_lock(a):
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
            "monitoring_enabled": not bool_false(a.get("monitoring_enabled")),
            "mt5_access_disabled": bool_true(a.get("mt5_access_disabled")),
            "breach_at": a.get("breach_at"),
            "breached_at": a.get("breached_at"),
            "archived_at": a.get("archived_at"),
            "reset_at": a.get("reset_at"),
            "superseded_at": a.get("superseded_at"),
            "replaced_at": a.get("replaced_at"),
            "superseded": a.get("superseded"),
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
            "stale_quarantined": len(stale_quarantined),
        },
        flush=True,
    )
    return out




def _retire_registry_exact(account_id, reason, successor_account_id=None):
    """Idempotently retire one exact MT5 instance from the Monitoring Registry.

    Lifecycle remains authoritative. This helper is called only AFTER the exact
    trader_account terminal state has been successfully persisted.
    """
    if not account_id:
        return False
    try:
        supabase.rpc("np_monitoring_retire", {
            "p_trader_account_id": str(account_id),
            "p_reason": str(reason or "lifecycle_terminal")[:500],
            "p_successor_account_id": (
                str(successor_account_id) if successor_account_id else None
            ),
        }).execute()
        print(
            f"REGISTRY RETIRED account_id={account_id} reason={reason}",
            flush=True,
        )
        return True
    except Exception as exc:
        # Do not undo the lifecycle terminal write. The health/reconciliation
        # endpoint will expose any failed registry retirement for retry.
        print(
            f"REGISTRY RETIRE RETRY REQUIRED account_id={account_id} "
            f"reason={reason} error={repr(exc)}",
            flush=True,
        )
        return False



def _confirmed_terminal_source(row):
    """Authoritative terminal latch for one MT5 lifecycle instance.

    Once a real breach/pass/reset/replacement/supersession is persisted, that MT5
    instance must never re-enter DD policing merely because current equity later
    recovers or because a stale duplicate row looks active.

    Financial watchdog locks are NOT terminal unless a real breach/pass/reset/
    replacement marker also exists.
    """
    if not row:
        return False, None

    status = str(row.get("account_status") or row.get("status") or "").strip().lower()
    stage = str(row.get("stage") or row.get("phase") or "").strip().lower()

    # Hard persisted lifecycle evidence wins permanently.
    for key, reason in (
        ("breach_at", "breach_at"),
        ("breached_at", "breached_at"),
        ("passed_at", "passed_at"),
        ("reset_at", "reset_at"),
        ("superseded_at", "superseded_at"),
        ("replaced_at", "replaced_at"),
    ):
        if row.get(key):
            return True, reason

    if row.get("superseded") is True:
        return True, "superseded"

    # Archived rows are terminal unless they are an explicit live financial lock.
    if row.get("archived_at") and not is_funded_cap_lock(row):
        return True, "archived_at"

    terminal_words = ("archived", "breached", "closed", "disabled", "passed", "reset", "replaced", "superseded")
    blob = f"{status} {stage}"
    if any(word in blob for word in terminal_words) and not is_funded_cap_lock(row):
        return True, f"terminal_status:{status or stage}"

    return False, None


def _retire_registry_row(reg, reason):
    """Best-effort retirement of one confirmed-terminal registry row."""
    try:
        q = (
            supabase.table("monitoring_registry")
            .update({
                "active": False,
                "monitoring_state": "RETIRED",
                "retirement_reason": f"auto_terminal_latch:{reason}",
                "retired_at": now_iso(),
                "updated_at": now_iso(),
            })
            .eq("trader_account_id", reg.get("trader_account_id"))
            .eq("active", True)
        )
        q.execute()
        return True
    except Exception as exc:
        print(
            "REGISTRY AUTO-RETIRE FAILED:",
            {
                "trader_account_id": reg.get("trader_account_id"),
                "mt5_login": reg.get("mt5_login"),
                "reason": reason,
                "error": repr(exc),
            },
            flush=True,
        )
        return False



def _retire_inactive_registry_v39(reg, reason):
    """Atomically retire one exact registry lifecycle via SECURITY DEFINER RPC.

    V38 used a direct table update. In production/RLS that can appear successful
    without giving us a durable, verified retirement. V39 uses the same
    np_monitoring_retire RPC already used by the main lifecycle backend, then
    verifies the exact registry row through np_monitoring_registry_read.

    Return: (verified_retired: bool, verification_payload: dict)
    """
    aid = str((reg or {}).get("trader_account_id") or "").strip()
    login = clean_login((reg or {}).get("mt5_login"))
    if not aid:
        return False, {"reason": "missing_trader_account_id"}

    try:
        supabase.rpc("np_monitoring_retire", {
            "p_trader_account_id": aid,
            "p_reason": str(reason or "stale_inactive_expired_7days")[:500],
            "p_successor_account_id": None,
        }).execute()
    except Exception as exc:
        return False, {
            "reason": "retire_rpc_failed",
            "error": repr(exc),
            "trader_account_id": aid,
            "mt5_login": login,
        }

    # Verify against the authoritative registry reader, not the table update response.
    try:
        rows = supabase.rpc("np_monitoring_registry_read", {}).execute().data or []
        exact = next(
            (
                r for r in rows
                if str(r.get("trader_account_id") or "").strip() == aid
            ),
            None,
        )
        if exact is None:
            # Missing from the registry reader after retirement is also safe:
            # it cannot be delivered to the shards.
            return True, {
                "verified": True,
                "verification": "not_present_in_registry_reader",
                "trader_account_id": aid,
                "mt5_login": login,
            }

        active = bool_true(exact.get("active"))
        state = str(exact.get("monitoring_state") or "").strip().upper()
        verified = (not active) or state == "RETIRED"

        # Metadata is useful, but retirement authority is the RPC above.
        if verified:
            try:
                (
                    supabase.table("monitoring_registry")
                    .update({
                        "current_proof_source": "expired_rolling_7day_no_activity",
                        "current_proof_checked_at": now_iso(),
                        "orphaned_at": exact.get("orphaned_at") or now_iso(),
                        "updated_at": now_iso(),
                    })
                    .eq("trader_account_id", aid)
                    .execute()
                )
            except Exception:
                pass

        return verified, {
            "verified": verified,
            "verification": "registry_reader",
            "active": active,
            "monitoring_state": state,
            "retirement_reason": exact.get("retirement_reason"),
            "trader_account_id": aid,
            "mt5_login": login,
        }
    except Exception as exc:
        return False, {
            "reason": "retirement_verification_failed",
            "error": repr(exc),
            "trader_account_id": aid,
            "mt5_login": login,
        }


_LAST_CURRENT_PROOF_QUARANTINE = []
_LAST_7DAY_INACTIVE_EXPIRED = []
_LAST_7DAY_TRADE_HISTORY_AVAILABLE = True


def _monitoring_registry_rows():
    """STRICT EXCHANGE ROSTER.

    The registry row itself must be the CURRENT lifecycle instance.
    No same-login duplicate rebinding is allowed here.

    Authority:
      POSITIVE CURRENT PROOF -> monitoring_registry -> DD Police

    A row is admitted only when NairaPips can positively prove that the exact
    trader_account is current NOW. Historical rows that merely "look active"
    are quarantined from the feed and NEVER consume a shard slot.

    OLD parent rows are retired when:
      * registry says they have a successor,
      * the purchase points to another trader_account,
      * exact source is terminal/disabled,
      * or the exact source no longer satisfies live/watchdog law.
    """
    registry = supabase.rpc("np_monitoring_registry_read", {}).execute().data or []
    registry = [
        r for r in registry
        if bool_true(r.get("active"))
        and str(r.get("monitoring_state") or "").strip().upper() in {"LIVE", "WATCHDOG"}
    ]
    if not registry:
        return [], []

    account_map = _bulk_rows(
        "trader_accounts",
        [r.get("trader_account_id") for r in registry],
        select="*",
    )
    purchase_map = _bulk_rows(
        "challenge_purchases",
        [r.get("purchase_id") for r in registry],
        select="*",
    )
    trader_map = _bulk_rows(
        "traders",
        [r.get("trader_id") for r in registry],
        select="*",
    )
    pool_map = _bulk_rows(
        "mt5_pool",
        [r.get("mt5_pool_id") for r in registry],
        select="*",
    )

    registry_account_ids = [
        str(r.get("trader_account_id") or "").strip()
        for r in registry
        if str(r.get("trader_account_id") or "").strip()
    ]
    registry_trader_ids = sorted({
        str(r.get("trader_id") or "").strip()
        for r in registry
        if str(r.get("trader_id") or "").strip()
    })

    # Reverse purchase lookup is essential because some legitimate production
    # accounts have trader_accounts.purchase_id / registry.purchase_id = NULL
    # even though challenge_purchases.trader_account_id points to them.
    reverse_purchase_rows = []
    try:
        if registry_trader_ids:
            reverse_purchase_rows = (
                supabase.table("challenge_purchases")
                .select("*")
                .in_("trader_id", registry_trader_ids)
                .limit(5000)
                .execute().data or []
            )
    except Exception as exc:
        print("CURRENT PROOF PURCHASE LOOKUP ERROR:", repr(exc), flush=True)

    purchases_by_account = {}
    purchases_by_trader = {}
    for p in reverse_purchase_rows:
        tid = str(p.get("trader_id") or "").strip()
        aid = str(p.get("trader_account_id") or "").strip()
        if tid:
            purchases_by_trader.setdefault(tid, []).append(p)
        if aid:
            purchases_by_account.setdefault(aid, []).append(p)

    payout_rows = []
    try:
        if registry_account_ids:
            payout_rows = (
                supabase.table("payouts")
                .select("*")
                .in_("trader_account_id", registry_account_ids)
                .limit(5000)
                .execute().data or []
            )
    except Exception as exc:
        print("CURRENT PROOF PAYOUT LOOKUP ERROR:", repr(exc), flush=True)

    payouts_by_account = {}
    for po in payout_rows:
        aid = str(po.get("trader_account_id") or "").strip()
        if aid:
            payouts_by_account.setdefault(aid, []).append(po)

    activate_rows = []
    try:
        if registry_account_ids:
            activate_rows = (
                supabase.table("monitoring_registry_events")
                .select("*")
                .eq("event_type", "ACTIVATE")
                .in_("trader_account_id", registry_account_ids)
                .limit(5000)
                .execute().data or []
            )
    except Exception as exc:
        print("CURRENT PROOF EVENT LOOKUP ERROR:", repr(exc), flush=True)

    direct_activation_accounts = {
        str(ev.get("trader_account_id") or "").strip()
        for ev in activate_rows
        if str(ev.get("trader_account_id") or "").strip()
        and "bootstrap" not in str(ev.get("reason") or "").lower()
    }

    # V45 FAST ROSTER ACTIVITY PROOF
    # The old V38 path downloaded up to 10,000 trader_trades rows TWICE on every
    # /monitorable_accounts refresh. Production contains duplicate-heavy trade
    # history, so that hot-path read could exceed the roster watcher's HTTP timeout.
    #
    # The 7-day business law does not require all historical trades. For the live
    # roster we only need evidence that activity happened inside the rolling 7-day
    # window. Accounts with no recent evidence are handled by the exact fallback
    # probe below before any irreversible retirement decision. This keeps the same
    # inactivity law while removing the unbounded history transfer from the DD feed.
    trade_history_available = True
    trade_rows_by_account = {}
    trade_rows_by_login = {}

    def _remember_trade_row(tr):
        aid = str((tr or {}).get("trader_account_id") or "").strip()
        lg = clean_login((tr or {}).get("mt5_login"))
        if aid:
            trade_rows_by_account.setdefault(aid, []).append(tr)
        if lg:
            trade_rows_by_login.setdefault(lg, []).append(tr)

    registry_logins = sorted({
        clean_login(r.get("mt5_login"))
        for r in registry
        if clean_login(r.get("mt5_login"))
    })
    activity_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    trade_select = "trader_account_id,mt5_login,status,opened_at,closed_at,synced_at,updated_at"

    # Recent OPEN timestamps. This is bounded to the only window that can reset the
    # inactivity clock; it replaces the former 10k all-history account read.
    try:
        if registry_account_ids:
            rows = (
                supabase.table("trader_trades")
                .select(trade_select)
                .in_("trader_account_id", registry_account_ids)
                .gte("opened_at", activity_cutoff)
                .order("opened_at", desc=True)
                .limit(5000)
                .execute().data or []
            )
            for tr in rows:
                _remember_trade_row(tr)
    except Exception as exc:
        trade_history_available = False
        print("V45 RECENT TRADE ACCOUNT LOOKUP ERROR:", repr(exc), flush=True)

    # Recent CLOSE timestamps catch positions opened earlier but closed inside the
    # last seven days. Dedupe happens in _trade_rows_for_account().
    try:
        if registry_account_ids:
            rows = (
                supabase.table("trader_trades")
                .select(trade_select)
                .in_("trader_account_id", registry_account_ids)
                .gte("closed_at", activity_cutoff)
                .order("closed_at", desc=True)
                .limit(5000)
                .execute().data or []
            )
            for tr in rows:
                _remember_trade_row(tr)
    except Exception as exc:
        trade_history_available = False
        print("V45 RECENT TRADE CLOSE LOOKUP ERROR:", repr(exc), flush=True)

    # Legacy rows may have login but no trader_account_id. Keep that compatibility,
    # but only inside the same seven-day window instead of downloading all history.
    try:
        if registry_logins:
            rows = (
                supabase.table("trader_trades")
                .select(trade_select)
                .in_("mt5_login", registry_logins)
                .gte("opened_at", activity_cutoff)
                .order("opened_at", desc=True)
                .limit(5000)
                .execute().data or []
            )
            for tr in rows:
                _remember_trade_row(tr)
    except Exception as exc:
        trade_history_available = False
        print("V45 RECENT TRADE LOGIN LOOKUP ERROR:", repr(exc), flush=True)

    live = []
    rejected = []
    quarantined = []
    stale_inactive = []

    def _retire(reg, reason, successor=None):
        try:
            payload = {
                "active": False,
                "monitoring_state": "RETIRED",
                "retirement_reason": "strict_exchange:" + str(reason),
                "retired_at": now_iso(),
                "updated_at": now_iso(),
            }
            if successor:
                payload["successor_account_id"] = successor
            (
                supabase.table("monitoring_registry")
                .update(payload)
                .eq("trader_account_id", reg.get("trader_account_id"))
                .eq("active", True)
                .execute()
            )
        except Exception as exc:
            print("STRICT EXCHANGE RETIRE FAILED:", repr(exc), flush=True)

    def _payout_is_open(po):
        status = str((po or {}).get("status") or "").strip().lower()
        if status not in {
            "pending", "requested", "submitted", "pending_review",
            "awaiting_review", "under_review", "approved",
            "processing", "payment_processing",
        }:
            return False
        return not str((po or {}).get("paid_at") or "").strip()

    def _positive_current_proof(reg, account, trader, purchase):
        rid = str((account or {}).get("id") or reg.get("trader_account_id") or "").strip()
        trader_id = str((account or {}).get("trader_id") or reg.get("trader_id") or "").strip()
        login = clean_login((account or {}).get("mt5_login") or reg.get("mt5_login"))

        stored = str(reg.get("current_proof_source") or "").strip().lower()
        accepted_stored = {
            "purchase_current_pointer",
            "direct_assignment_event",
            "open_payout_exact_account",
            "purchase_current_watchdog",
            "purchase_exact_account_pointer",
            "purchase_exact_mt5_pointer",
            "trader_current_pointer",
        }
        if stored in accepted_stored:
            return True, stored, purchase

        # Exact purchase account pointer.
        exact = list(purchases_by_account.get(rid) or [])
        if exact:
            exact.sort(key=lambda p: str(p.get("updated_at") or p.get("created_at") or ""), reverse=True)
            return True, "purchase_exact_account_pointer", exact[0]

        # Exact purchase MT5 pointer + same trader. This repairs legacy rows whose
        # purchase_id was never copied into trader_accounts/registry.
        for p in purchases_by_trader.get(trader_id) or []:
            current_login = clean_login(p.get("current_mt5_login"))
            purchase_login = clean_login(p.get("mt5_login"))
            if login and login in {current_login, purchase_login}:
                return True, "purchase_exact_mt5_pointer", p

        # Exact open payout is live financial responsibility and therefore proof.
        for po in payouts_by_account.get(rid) or []:
            if _payout_is_open(po):
                return True, "open_payout_exact_account", purchase

        # Trader pointer is positive proof only. A mismatch never kills another
        # legitimate parallel account.
        if str((trader or {}).get("current_account_id") or "").strip() == rid:
            return True, "trader_current_pointer", purchase

        # All new/manual assignments through V119+ generate ACTIVATE events.
        if rid in direct_activation_accounts:
            return True, "direct_assignment_event", purchase

        return False, "no_positive_current_assignment_proof", purchase

    def _parse_dt(value):
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            # Supabase timestamps commonly end in Z or +00.
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _assignment_time(reg, account, purchase, pool):
        # Strongest assignment-time authorities first.
        candidates = [
            ("account.assigned_at", (account or {}).get("assigned_at")),
            ("account.started_at", (account or {}).get("started_at")),
            ("pool.assigned_at", (pool or {}).get("assigned_at")),
            ("purchase.assigned_at", (purchase or {}).get("assigned_at")),
            ("account.created_at", (account or {}).get("created_at")),
            ("registry.activated_at", (reg or {}).get("activated_at")),
        ]
        for source, value in candidates:
            dt = _parse_dt(value)
            if dt is not None:
                return dt, source
        return None, "assignment_time_missing"

    def _trade_rows_for_account(account):
        rid = str((account or {}).get("id") or "").strip()
        login = clean_login((account or {}).get("mt5_login"))
        rows = []
        seen = set()

        for tr in (trade_rows_by_account.get(rid) or []):
            key = (
                str(tr.get("opened_at") or ""),
                str(tr.get("closed_at") or ""),
                str(tr.get("status") or ""),
                str(tr.get("mt5_login") or ""),
            )
            if key not in seen:
                seen.add(key)
                rows.append(tr)

        for tr in (trade_rows_by_login.get(login) or []):
            key = (
                str(tr.get("opened_at") or ""),
                str(tr.get("closed_at") or ""),
                str(tr.get("status") or ""),
                str(tr.get("mt5_login") or ""),
            )
            if key not in seen:
                seen.add(key)
                rows.append(tr)

        return rows

    def _rolling_trade_activity(account):
        rows = _trade_rows_for_account(account)
        if not rows:
            return {
                "has_ever_traded": False,
                "has_open_trade": False,
                "last_activity_at": None,
                "last_activity_source": None,
                "trade_rows_seen": 0,
            }

        has_open = False
        last_dt = None
        last_source = None

        for tr in rows:
            status = str((tr or {}).get("status") or "").strip().lower()
            closed_raw = str((tr or {}).get("closed_at") or "").strip()

            # Exact OPEN exposure always remains under DD policing.
            if status in {"open", "opened", "active", "position_open"} and not closed_raw:
                has_open = True

            for source in ("closed_at", "opened_at"):
                dt = _parse_dt((tr or {}).get(source))
                if dt is not None and (last_dt is None or dt > last_dt):
                    last_dt = dt
                    last_source = source

        return {
            "has_ever_traded": True,
            "has_open_trade": has_open,
            "last_activity_at": last_dt,
            "last_activity_source": last_source,
            "trade_rows_seen": len(rows),
        }

    def _seven_day_inactivity_status(reg, account, purchase, pool):
        # Never make an irreversible inactivity decision when trade-history
        # infrastructure is unavailable.
        if not trade_history_available:
            return False, {
                "rule_applied": False,
                "reason": "trade_history_unavailable",
            }

        activity = _rolling_trade_activity(account)
        assigned_at, assignment_source = _assignment_time(reg, account, purchase, pool)

        # V43 SAFETY REPAIR — never classify an aged account as "never traded"
        # merely because the broad bulk trader_trades read did not return its rows.
        # The broad read is intentionally capped and, with duplicate/history-heavy
        # accounts, a valid account can be absent from that result set.
        #
        # We only pay for an exact fallback read when ALL of these are true:
        #   1) bulk history says no trade rows, and
        #   2) the account is old enough that assignment-based retirement is possible.
        # Young accounts therefore add no extra DB traffic. A genuinely never-traded
        # aged account is probed once on the cycle that can retire it, then disappears
        # from the live registry after verified retirement.
        if (
            not activity["has_ever_traded"]
            and assigned_at is not None
            and (datetime.now(timezone.utc) - assigned_at).total_seconds() >= (7.0 * 86400.0)
        ):
            exact_rows = []
            rid = str((account or {}).get("id") or "").strip()
            login = clean_login((account or {}).get("mt5_login"))

            try:
                if rid:
                    exact_rows = (
                        supabase.table("trader_trades")
                        .select("trader_account_id,mt5_login,status,opened_at,closed_at,synced_at,updated_at")
                        .eq("trader_account_id", rid)
                        .order("opened_at", desc=True)
                        .limit(25)
                        .execute().data or []
                    )
            except Exception as exc:
                # Fail closed: an inability to prove "never traded" must never
                # retire a live account.
                return False, {
                    "rule_applied": False,
                    "has_ever_traded": False,
                    "reason": "exact_trade_history_probe_failed",
                    "error": repr(exc),
                }

            # Legacy safety: only use login fallback if exact account linkage returned
            # nothing. This preserves current-account identity as the first authority.
            if not exact_rows and login:
                try:
                    exact_rows = (
                        supabase.table("trader_trades")
                        .select("trader_account_id,mt5_login,status,opened_at,closed_at,synced_at,updated_at")
                        .eq("mt5_login", login)
                        .order("opened_at", desc=True)
                        .limit(25)
                        .execute().data or []
                    )
                except Exception as exc:
                    return False, {
                        "rule_applied": False,
                        "has_ever_traded": False,
                        "reason": "exact_trade_history_login_probe_failed",
                        "error": repr(exc),
                    }

            if exact_rows:
                # Feed the exact proof into the same established rolling activity
                # evaluator. No DD, lifecycle, target, or shard logic is changed.
                for tr in exact_rows:
                    _remember_trade_row(tr)
                activity = _rolling_trade_activity(account)

                # Defensive invariant: if exact rows exist but cannot be interpreted,
                # do NOT fall through to the never-traded retirement branch.
                if not activity["has_ever_traded"]:
                    return False, {
                        "rule_applied": False,
                        "has_ever_traded": True,
                        "reason": "exact_trade_rows_uninterpretable_keep_monitoring",
                        "exact_trade_rows_seen": len(exact_rows),
                    }

        # Open exposure always stays in the 3-second DD fleet.
        if activity["has_open_trade"]:
            return False, {
                "rule_applied": True,
                "has_ever_traded": True,
                "has_open_trade": True,
                "last_activity_at": (
                    activity["last_activity_at"].isoformat()
                    if activity["last_activity_at"] else None
                ),
                "last_activity_source": activity["last_activity_source"],
                "trade_rows_seen": activity["trade_rows_seen"],
                "reason": "open_trade_keep_monitoring",
            }

        # Trader has traded before: rolling 7-day clock starts from the most
        # recent OPEN/CLOSE trade timestamp.
        if activity["has_ever_traded"]:
            last_dt = activity["last_activity_at"]
            if last_dt is None:
                # Trade row exists but no reliable timestamp: fail safe, keep it.
                return False, {
                    "rule_applied": False,
                    "has_ever_traded": True,
                    "has_open_trade": False,
                    "trade_rows_seen": activity["trade_rows_seen"],
                    "reason": "trade_timestamp_missing",
                }

            idle_days = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400.0
            if idle_days >= 7.0:
                return True, {
                    "rule_applied": True,
                    "has_ever_traded": True,
                    "has_open_trade": False,
                    "last_activity_at": last_dt.isoformat(),
                    "last_activity_source": activity["last_activity_source"],
                    "inactive_days": round(idle_days, 3),
                    "trade_rows_seen": activity["trade_rows_seen"],
                    "reason": "stale_inactive_expired_7days_since_last_trade",
                }

            return False, {
                "rule_applied": True,
                "has_ever_traded": True,
                "has_open_trade": False,
                "last_activity_at": last_dt.isoformat(),
                "last_activity_source": activity["last_activity_source"],
                "inactive_days": round(idle_days, 3),
                "trade_rows_seen": activity["trade_rows_seen"],
                "reason": "recent_trade_within_7days",
            }

        # Never traded: clock starts at assignment.
        if assigned_at is None:
            return False, {
                "rule_applied": False,
                "has_ever_traded": False,
                "reason": "assignment_time_missing",
            }

        idle_days = (datetime.now(timezone.utc) - assigned_at).total_seconds() / 86400.0
        if idle_days >= 7.0:
            return True, {
                "rule_applied": True,
                "has_ever_traded": False,
                "has_open_trade": False,
                "assignment_source": assignment_source,
                "assigned_at": assigned_at.isoformat(),
                "inactive_days": round(idle_days, 3),
                "reason": "stale_inactive_expired_7days_since_assignment",
            }

        return False, {
            "rule_applied": True,
            "has_ever_traded": False,
            "has_open_trade": False,
            "assignment_source": assignment_source,
            "assigned_at": assigned_at.isoformat(),
            "inactive_days": round(idle_days, 3),
            "reason": "new_account_within_7day_grace",
        }

    for reg in registry:
        rid = str(reg.get("trader_account_id") or "").strip()
        account = account_map.get(rid) or {}
        purchase_id = str(reg.get("purchase_id") or account.get("purchase_id") or "").strip()
        trader_id = str(reg.get("trader_id") or account.get("trader_id") or "").strip()
        purchase = purchase_map.get(purchase_id) or {}
        trader = trader_map.get(trader_id) or {}
        pool = pool_map.get(str(reg.get("mt5_pool_id") or account.get("mt5_pool_id") or "")) or {}

        if not account:
            _retire(reg, "missing_exact_source")
            rejected.append({"trader_account_id": rid, "mt5_login": reg.get("mt5_login"), "reason": "missing_exact_source"})
            continue

        successor = str(reg.get("successor_account_id") or "").strip()
        if successor:
            _retire(reg, "has_successor", successor)
            rejected.append({"trader_account_id": rid, "mt5_login": reg.get("mt5_login"), "reason": "parent_has_successor"})
            continue

        is_term, term_reason = _confirmed_terminal_source(account)
        if is_term:
            _retire(reg, term_reason or "terminal_exact_source")
            rejected.append({"trader_account_id": rid, "mt5_login": reg.get("mt5_login"), "reason": f"terminal_exact_source:{term_reason}"})
            continue

        # V35 POSITIVE-PROOF GATE.
        # "Looks active" is not enough. If NairaPips cannot prove this exact
        # account is current, it is excluded from the shard feed WITHOUT mutating
        # trader lifecycle history. This is a quarantine, not a deletion.
        proof_ok, proof_source, proof_purchase = _positive_current_proof(
            reg, account, trader, purchase
        )
        if proof_purchase:
            purchase = proof_purchase
            if not purchase_id:
                purchase_id = str(purchase.get("id") or "").strip()

        if not proof_ok:
            quarantined.append({
                "trader_account_id": rid,
                "trader_id": trader_id,
                "mt5_login": reg.get("mt5_login") or account.get("mt5_login"),
                "stage": account.get("stage") or account.get("phase"),
                "account_status": account.get("account_status"),
                "reason": proof_source,
                "pool_exact_support": bool(
                    pool
                    and str(pool.get("trader_account_id") or "").strip() == rid
                    and clean_login(pool.get("mt5_login")) == clean_login(account.get("mt5_login"))
                ),
            })
            continue

        # V42 WATCHDOG LAW:
        # funded_profit_cap_reached / profit_protected are financial lock states,
        # not stale-unused accounts. They MUST remain in WATCHDOG indefinitely
        # until payout/cycle resolution, even if there has been no trade for 7+ days.
        # Applying the generic inactivity retirement here caused legitimate funded
        # cap locks to oscillate LIVE -> RETIRED and created pointer gaps.
        if is_funded_cap_lock(account):
            expired_7d = False
            inactivity_info = {
                "rule_applied": False,
                "reason": "financial_watchdog_lock_exempt_from_7day_retirement",
            }
        else:
            # V36: 7 DAYS FROM ASSIGNMENT + NO TRADING ACTIVITY = INVALID.
            # This applies only to ordinary live challenge/funded accounts.
            expired_7d, inactivity_info = _seven_day_inactivity_status(
                reg, account, purchase, pool
            )
        if expired_7d:
            stale_row = {
                "trader_account_id": rid,
                "trader_id": trader_id,
                "mt5_login": account.get("mt5_login") or reg.get("mt5_login"),
                "stage": account.get("stage") or account.get("phase"),
                "account_status": account.get("account_status"),
                **inactivity_info,
            }
            stale_inactive.append(stale_row)

            # V39 ATOMIC LAW:
            # Use the same SECURITY DEFINER retirement RPC as the main lifecycle
            # backend and verify the exact registry row before calling the write
            # durable. This prevents one endpoint seeing 81 while another sees 82
            # because a direct RLS-protected table update did not actually stick.
            verified_retired, retire_verification = _retire_inactive_registry_v39(
                reg,
                inactivity_info.get("reason") or "stale_inactive_expired_7days",
            )
            stale_row["persisted_retirement"] = bool(verified_retired)
            stale_row["retirement_verification"] = retire_verification

            if not verified_retired:
                print(
                    "V39 7DAY RETIREMENT NOT VERIFIED:",
                    {
                        "trader_account_id": rid,
                        "mt5_login": stale_row.get("mt5_login"),
                        "verification": retire_verification,
                    },
                    flush=True,
                )

            # Safety invariant: even when persistence verification fails, the stale
            # account is excluded from THIS feed cycle. It cannot consume a DD slot.
            continue

        # Purchase pointer is the journey-slot authority. If NEW exists, OLD is out.
        if purchase_id:
            current_id = str(purchase.get("trader_account_id") or "").strip()
            if current_id and current_id != rid:
                _retire(reg, "not_current_purchase_pointer", current_id)
                rejected.append({
                    "trader_account_id": rid,
                    "mt5_login": reg.get("mt5_login"),
                    "reason": "not_current_purchase_pointer",
                    "successor_account_id": current_id,
                })
                continue
        else:
            # V34 MULTI-JOURNEY SAFETY:
            # A trader may legitimately own several simultaneous challenge/funded
            # accounts. Therefore traders.current_account_id is NOT a valid global
            # authority for a registry row that has no purchase_id.
            #
            # For legacy/manual rows without purchase linkage, keep the exact
            # registry lifecycle instance only when:
            #   - it has no successor,
            #   - the exact trader_account row is still monitorable,
            #   - it has no terminal latch,
            #   - monitoring_enabled is not false.
            #
            # This avoids falsely ejecting legitimate parallel accounts merely
            # because another account is the trader's dashboard "current" pointer.
            pass

        if not is_active_monitoring_account(account):
            _retire(reg, "exact_source_not_monitorable")
            rejected.append({"trader_account_id": rid, "mt5_login": reg.get("mt5_login"), "reason": "exact_source_not_monitorable"})
            continue

        if bool_false(account.get("monitoring_enabled")):
            _retire(reg, "monitoring_disabled")
            rejected.append({"trader_account_id": rid, "mt5_login": reg.get("mt5_login"), "reason": "monitoring_disabled"})
            continue

        account_login = clean_login(account.get("mt5_login"))
        account_trader_id = str(account.get("trader_id") or "").strip()
        account_id = str(account.get("id") or "").strip()

        if not account_login or not str(account.get("mt5_server") or "").strip():
            rejected.append({"trader_account_id": rid, "mt5_login": account_login, "reason": "missing_login_or_server"})
            continue

        # Credentials may come from exact current account, exact linked pool, or
        # exact current purchase only. Historical duplicate trader_account rows are
        # intentionally excluded so a dead parent cannot resurrect itself.
        credential_rows = [account]

        pool_ok = bool(pool)
        if pool_ok:
            pool_login = clean_login(pool.get("mt5_login"))
            pool_tid = str(pool.get("assigned_trader_id") or pool.get("trader_id") or "").strip()
            if pool_login and pool_login != account_login:
                pool_ok = False
            if pool_tid and account_trader_id and pool_tid != account_trader_id:
                pool_ok = False
        if pool_ok:
            credential_rows.append(pool)

        purchase_ok = bool(purchase)
        if purchase_ok:
            p_tid = str(purchase.get("trader_id") or "").strip()
            p_aid = str(purchase.get("trader_account_id") or "").strip()
            if p_tid and account_trader_id and p_tid != account_trader_id:
                purchase_ok = False
            if p_aid and p_aid != account_id:
                purchase_ok = False
        if purchase_ok:
            credential_rows.append(purchase)

        def _unique_secret_values(rows, keys):
            out = []
            seen = set()
            for row in rows:
                if not row:
                    continue
                for key in keys:
                    value = str(row.get(key) or "").strip()
                    if value and value not in seen:
                        seen.add(value)
                        out.append(value)
            return out

        investor_candidates = _unique_secret_values(
            credential_rows,
            ["mt5_investor_password", "investor_password", "investor"],
        )
        master_candidates = _unique_secret_values(
            credential_rows,
            ["mt5_master_password", "mt5_password", "master_password", "password"],
        )

        rule_values = _fast_rule_values(account, purchase, {})

        live.append({
            "id": account_id,
            "trader_id": account.get("trader_id"),
            "trader_account_id": account_id,
            "current_account_id": account_id,
            "registry_trader_account_id": rid,
            "canonical_rebound": False,
            "name": trader.get("name") or "Trader",
            "full_name": trader.get("name") or "Trader",
            "email": trader.get("email") or account.get("email"),
            "phone": trader.get("phone") or "",
            "phase": account.get("stage") or account.get("phase") or "phase1",
            "stage": account.get("stage") or account.get("phase") or "phase1",
            "status": "active",
            "account_status": account.get("account_status") or "assigned_active",
            "payment_status": "approved",
            "monitoring_enabled": True,
            "mt5_access_disabled": bool_true(account.get("mt5_access_disabled")),
            "mt5_login": account_login,
            "mt5_server": account.get("mt5_server") or "",
            "mt5_master_password": (master_candidates[0] if master_candidates else ""),
            "mt5_password": (master_candidates[0] if master_candidates else ""),
            "master_password": (master_candidates[0] if master_candidates else ""),
            "mt5_investor_password": (investor_candidates[0] if investor_candidates else ""),
            "investor_password": (investor_candidates[0] if investor_candidates else ""),
            "mt5_investor_password_candidates": investor_candidates,
            "mt5_master_password_candidates": master_candidates,
            "credential_pool_match": pool_ok,
            "credential_purchase_match": purchase_ok,
            "credential_candidate_counts": {
                "investor": len(investor_candidates),
                "master": len(master_candidates),
            },
            "account_size": num(account.get("account_size") or account.get("start_balance")),
            "dd_limit_percent": rule_values["dd_limit_percent"],
            "dd_authority_present": rule_values["dd_authority_present"],
            "dd_authority_source": rule_values.get("dd_authority_source") or "authority_missing",
            "target_percent": rule_values["target_percent"],
            "target_authority_present": rule_values["target_authority_present"],
            "target_authority_source": rule_values.get("target_authority_source") or "authority_missing",
            "balance": num(account.get("current_balance") or account.get("start_balance") or account.get("account_size")),
            "current_balance": num(account.get("current_balance") or account.get("start_balance") or account.get("account_size")),
            "equity": num(account.get("current_equity") or account.get("current_balance") or account.get("start_balance") or account.get("account_size")),
            "current_equity": num(account.get("current_equity") or account.get("current_balance") or account.get("start_balance") or account.get("account_size")),
            "highest_equity": num(account.get("highest_equity") or account.get("current_equity") or account.get("start_balance") or account.get("account_size")),
            "lowest_equity": num(account.get("lowest_equity") or account.get("start_balance") or account.get("account_size")),
            "profit_percent": num(account.get("profit_percent")),
            "risk_zone": account.get("risk_zone") or "safe",
            "monitoring_state": reg.get("monitoring_state"),
            "registry_version": reg.get("version"),
            "current_proof_source": proof_source,
            "seven_day_inactivity_rule": inactivity_info,
            "_source_of_truth": "monitoring_registry_positive_current_proof_7day_gate",
        })

    global _LAST_CURRENT_PROOF_QUARANTINE
    global _LAST_7DAY_INACTIVE_EXPIRED
    global _LAST_7DAY_TRADE_HISTORY_AVAILABLE
    _LAST_CURRENT_PROOF_QUARANTINE = quarantined
    _LAST_7DAY_INACTIVE_EXPIRED = stale_inactive
    _LAST_7DAY_TRADE_HISTORY_AVAILABLE = trade_history_available
    return live, rejected



@app.route("/monitoring_exchange_health", methods=["GET"])
def monitoring_exchange_health():
    """Read-only proof of OLD OUT / NEW IN enforcement."""
    try:
        live, rejected = _monitoring_registry_rows()
        reasons = {}
        for row in rejected:
            reason = str(row.get("reason") or "unknown")
            reasons[reason] = reasons.get(reason, 0) + 1
        return ok({
            "release": NAIRAPIPS_MONITORING_RELEASE,
            "strict_exchange_mode": True,
            "multi_journey_safe": True,
            "live_current_instances": len(live),
            "positive_proof_gate": True,
            "seven_day_inactivity_gate": True,
            "seven_day_rule_mode": "rolling_7_days_no_trading_activity_atomic_auto_retire",
            "seven_day_retirement_authority": "np_monitoring_retire_rpc_verified",
            "seven_day_rule_mode": "rolling_7_days_no_trading_activity_atomic_auto_retire",
            "seven_day_retirement_authority": "np_monitoring_retire_rpc_verified",
            "seven_day_trade_history_available": _LAST_7DAY_TRADE_HISTORY_AVAILABLE,
            "stale_inactive_expired_count": len(_LAST_7DAY_INACTIVE_EXPIRED),
            "stale_inactive_expired": _LAST_7DAY_INACTIVE_EXPIRED[:100],
            "quarantined_unproven_count": len(_LAST_CURRENT_PROOF_QUARANTINE),
            "quarantined_unproven": _LAST_CURRENT_PROOF_QUARANTINE[:100],
            "rejected_or_retired_this_read": len(rejected),
            "rejection_reasons": reasons,
            "sample_rejected": rejected[:100],
            "exchange_feed_ready": len(live) > 0 and len(rejected) == 0,
        }, "monitoring exchange health")
    except Exception as e:
        return bad(e, 500)


@app.route("/monitoring_registry_canonical_health", methods=["GET"])
def monitoring_registry_canonical_health():
    """Read-only proof that duplicate trader_account rows no longer drop an MT5."""
    try:
        live, rejected = _monitoring_registry_rows()
        rebound = [
            {
                "mt5_login": r.get("mt5_login"),
                "registry_trader_account_id": r.get("registry_trader_account_id"),
                "canonical_trader_account_id": r.get("trader_account_id"),
                "account_status": r.get("account_status"),
            }
            for r in live
            if r.get("canonical_rebound")
        ]
        return ok({
            "release": NAIRAPIPS_MONITORING_RELEASE,
            "live_count": len(live),
            "rejected_count": len(rejected),
            "canonical_rebound_count": len(rebound),
            "canonical_rebounds": rebound[:100],
            "rejected": rejected[:100],
            "canonical_feed_ready": len(live) > 0 and len(rejected) == 0,
            "strict_exchange_mode": True,
            "multi_journey_safe": True,
        }, "monitoring registry canonical health")
    except Exception as e:
        return bad(e, 500)


@app.route("/monitoring_registry_accounts", methods=["GET"])
def monitoring_registry_accounts():
    """SHADOW endpoint. Not used by production engine until coverage is proven."""
    try:
        live, rejected = _monitoring_registry_rows()
        return ok({
            "accounts": live,
            "count": len(live),
            "rejected_registry_rows": rejected,
            "release": NAIRAPIPS_MONITORING_RELEASE,
        }, f"{len(live)} registry account(s)")
    except Exception as e:
        return bad(e, 500)



@app.route("/monitoring_registry_raw_health", methods=["GET"])
def monitoring_registry_raw_health():
    """Direct database read of the registry before trader-account revalidation."""
    try:
        rows = (
            supabase.rpc("np_monitoring_registry_read", {}).execute().data or []
        )
        active_rows = [r for r in rows if bool_true(r.get("active"))]
        live_rows = [
            r for r in active_rows
            if str(r.get("monitoring_state") or "").strip().upper() in {"LIVE", "WATCHDOG"}
        ]
        states = {}
        for r in rows:
            key = str(r.get("monitoring_state") or "NULL").strip() or "NULL"
            states[key] = states.get(key, 0) + 1
        return ok({
            "release": NAIRAPIPS_MONITORING_RELEASE,
            "raw_registry_total": len(rows),
            "raw_registry_active": len(active_rows),
            "raw_registry_live_watchdog": len(live_rows),
            "states": states,
            "sample": rows[:20],
        }, "monitoring registry raw health")
    except Exception as e:
        return bad(f"monitoring_registry raw read failed: {repr(e)}", 500)


@app.route("/monitoring_registry_health", methods=["GET"])
def monitoring_registry_health():
    """Compare current production discovery with the new exact registry.

    This is the cutover gate. Production should not switch to registry-only until
    every legitimate active account is either in registry or explicitly explained.
    """
    try:
        legacy = _fast_monitorable_feed()
        registry, rejected = _monitoring_registry_rows()

        legacy_by_id = {
            str(r.get("trader_account_id") or r.get("id") or "").strip(): r
            for r in legacy
            if str(r.get("trader_account_id") or r.get("id") or "").strip()
        }
        registry_by_id = {
            str(r.get("trader_account_id") or r.get("id") or "").strip(): r
            for r in registry
            if str(r.get("trader_account_id") or r.get("id") or "").strip()
        }

        missing_from_registry = []
        for aid, row in legacy_by_id.items():
            if aid not in registry_by_id:
                missing_from_registry.append({
                    "trader_account_id": aid,
                    "mt5_login": row.get("mt5_login"),
                    "stage": row.get("stage"),
                    "account_status": row.get("account_status"),
                    "risk_zone": row.get("risk_zone"),
                    "reason": "legacy_feed_live_but_not_in_registry",
                })

        registry_only = []
        for aid, row in registry_by_id.items():
            if aid not in legacy_by_id:
                registry_only.append({
                    "trader_account_id": aid,
                    "mt5_login": row.get("mt5_login"),
                    "stage": row.get("stage"),
                    "account_status": row.get("account_status"),
                    "reason": "registry_live_but_not_in_legacy_feed",
                })

        return ok({
            "release": NAIRAPIPS_MONITORING_RELEASE,
            "legacy_feed_count": len(legacy_by_id),
            "registry_live_count": len(registry_by_id),
            "missing_from_registry_count": len(missing_from_registry),
            "registry_only_count": len(registry_only),
            "rejected_registry_count": len(rejected),
            "missing_from_registry": missing_from_registry[:1000],
            "registry_only": registry_only[:1000],
            "rejected_registry": rejected[:1000],
            "cutover_ready": (
                len(missing_from_registry) == 0
                and len(rejected) == 0
            ),
        }, "monitoring registry health")
    except Exception as e:
        print("REGISTRY HEALTH ERROR:", repr(e), flush=True)
        return bad(e, 500)


# ============================================================================
# V42 — LIGHTWEIGHT ROSTER TOKEN + POINTER-GAP HEALTH
# ============================================================================
@app.route("/monitoring_roster_version", methods=["GET"])
def monitoring_roster_version():
    try:
        import hashlib as _hashlib
        rows = supabase.rpc("np_monitoring_registry_read", {}).execute().data or []
        live = [
            r for r in rows
            if bool_true(r.get("active"))
            and str(r.get("monitoring_state") or "").strip().upper() in {"LIVE", "WATCHDOG"}
        ]
        parts = []
        for r in live:
            parts.append("|".join([
                str(r.get("trader_account_id") or "").strip(),
                str(r.get("mt5_login") or "").strip(),
                str(r.get("monitoring_state") or "").strip().upper(),
                str(r.get("version") or "0").strip(),
            ]))
        parts.sort()
        token = _hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
        return ok({
            "release": NAIRAPIPS_MONITORING_RELEASE,
            "token": token,
            "count": len(live),
        }, "monitoring roster version")
    except Exception as exc:
        return bad({"release": NAIRAPIPS_MONITORING_RELEASE, "error": repr(exc)}, 500)


@app.route("/monitoring_pointer_gap_health", methods=["GET"])
def monitoring_pointer_gap_health():
    try:
        rows = (
            supabase.table("np_monitoring_pointer_gap_health")
            .select("*")
            .limit(1000)
            .execute().data or []
        )
        return ok({
            "release": NAIRAPIPS_MONITORING_RELEASE,
            "gap_count": len(rows),
            "gaps": rows,
            "healthy": len(rows) == 0,
        }, "monitoring pointer gap health")
    except Exception as exc:
        return bad({"release": NAIRAPIPS_MONITORING_RELEASE, "error": repr(exc)}, 500)


@app.route("/monitorable_accounts")
def monitorable_accounts():
    """DD POLICE AUTHORITY FEED — exact current Monitoring Registry only.

    IMPORTANT:
    The four stable DD Police V3.6 shard programs remain unchanged. They already
    consume /monitorable_accounts. V29 changes only the server-side source behind
    that established contract:

        Monitoring Registry -> exact trader_account revalidation -> DD shards

    Fail-closed law:
      * never fall back to the historical/broad discovery population;
      * if registry rows cannot be read/revalidated, return an error;
      * the V3.6 shards retain their last good in-memory population when refresh
        fails, rather than replacing it with guessed/historical accounts.
    """
    try:
        out, rejected = _monitoring_registry_rows()

        # V44 ROSTER SELF-HEALING:
        # Rejected registry rows have ALREADY been excluded by
        # _monitoring_registry_rows(). They must never poison the accepted roster.
        #
        # Previous behaviour returned HTTP 503 when even ONE unrelated registry
        # row was rejected. The unchanged V3.6 DD shards correctly retained their
        # last-good roster on that 503, but that also meant a newly assigned or
        # repaired LIVE account could never enter the fleet until every unrelated
        # inconsistency was cleared. This is a roster-refresh deadlock.
        #
        # Safety law:
        #   - rejected rows remain OUT of the DD feed;
        #   - accepted, positively-proven rows continue to refresh normally;
        #   - zero accepted rows still fails closed below;
        #   - DD calculation/shard/breach logic is untouched.
        if rejected:
            print(
                "DD REGISTRY FEED DEGRADED: rejected rows excluded; accepted roster continues",
                {"accepted": len(out), "rejected": len(rejected), "sample": rejected[:20]},
                flush=True,
            )

        if not out:
            print("DD REGISTRY FEED BLOCKED: zero current registry accounts", flush=True)
            return bad({
                "error": "DD registry returned zero current accounts",
                "release": NAIRAPIPS_MONITORING_RELEASE,
            }, 503)

        for row in out:
            row["_source_of_truth"] = "monitoring_registry_dd_authority"
            row["_dd_feed_release"] = NAIRAPIPS_MONITORING_RELEASE

        print(
            "DD REGISTRY FEED COMPLETE:",
            {"monitorable": len(out), "rejected": 0},
            flush=True,
        )
        # Preserve the exact legacy response envelope expected by V3.6:
        # ok(list) => {"data":[...], ...}; its unpack_rows() reads data directly.
        return ok(out, f"{len(out)} registry-authoritative DD account(s)")
    except Exception as e:
        print("DD REGISTRY FEED FATAL ERROR:", repr(e), flush=True)
        return bad(e, 500)


def _dd_owner_shard(login, total_shards=4):
    """Mirror the unchanged V3.6 rendezvous-hash ownership law."""
    login = clean_login(login)
    n = max(int(total_shards or 1), 1)
    if n <= 1:
        return 1
    best_shard = 1
    best_score = -1
    key = str(login).encode("utf-8")
    import hashlib
    for shard in range(1, n + 1):
        digest = hashlib.sha256(key + b"|" + str(shard).encode("ascii")).digest()
        score = int.from_bytes(digest[:8], "big", signed=False)
        if score > best_score:
            best_score = score
            best_shard = shard
    return best_shard


@app.route("/dd_registry_coverage_health", methods=["GET"])
def dd_registry_coverage_health():
    """Read-only proof of the exact roster the four unchanged DD shards should receive."""
    try:
        out, rejected = _monitoring_registry_rows()
        expected = {1: 0, 2: 0, 3: 0, 4: 0}
        invalid = []
        for row in out:
            login = clean_login(row.get("mt5_login"))
            server = str(row.get("mt5_server") or "").strip()
            if not login or not login.isdigit() or not server:
                invalid.append({
                    "trader_account_id": row.get("trader_account_id") or row.get("id"),
                    "mt5_login": login,
                    "reason": "invalid_login_or_server",
                })
                continue
            expected[_dd_owner_shard(login, 4)] += 1

        ready = (len(rejected) == 0 and len(invalid) == 0 and len(out) > 0)
        return ok({
            "release": NAIRAPIPS_MONITORING_RELEASE,
            "dd_feed_source": "monitoring_registry_positive_current_proof_atomic_rolling_7day_gate",
            "registry_dd_population": len(out),
            "positive_proof_gate": True,
            "seven_day_inactivity_gate": True,
            "seven_day_trade_history_available": _LAST_7DAY_TRADE_HISTORY_AVAILABLE,
            "stale_inactive_expired_count": len(_LAST_7DAY_INACTIVE_EXPIRED),
            "stale_inactive_expired": _LAST_7DAY_INACTIVE_EXPIRED[:100],
            "quarantined_unproven_count": len(_LAST_CURRENT_PROOF_QUARANTINE),
            "quarantined_unproven": _LAST_CURRENT_PROOF_QUARANTINE[:100],
            "expected_shard_counts": {
                "shard_1": expected[1],
                "shard_2": expected[2],
                "shard_3": expected[3],
                "shard_4": expected[4],
            },
            "expected_total": sum(expected.values()),
            "rejected_registry_count": len(rejected),
            "invalid_routing_count": len(invalid),
            "rejected_registry": rejected[:100],
            "invalid_routing": invalid[:100],
            "dd_registry_feed_ready": ready,
        }, "DD registry coverage health")
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
    # V12 EXACT SNAPSHOT TERMINAL-STATE BRIDGE:
    # Snapshot requests are already bound to an immutable trader_account_id.
    # During a target/pass/profit-cap transition the same account may have been
    # archived/locked milliseconds before the final evidence snapshot arrives.
    # Resolve that exact account across statuses, while retaining MT5 ownership
    # verification. This does NOT broaden discovery or reactivate archived rows.
    account = get_account_by_id_any_status(account_id, data.get("mt5_login"))
    if not account:
        return bad("Exact trader account not found or MT5 ownership evidence mismatched", 404)

    # Normal live intelligence is only valid while the account is active.
    # For a terminal account, accept the exact-account request as an idempotent
    # late snapshot instead of returning a false 404. Terminal persistence has
    # already been performed by the pass/cap/lock action.
    if not is_active_monitoring_account(account):
        return ok({
            "account_id": account.get("id"),
            "mt5_login": account.get("mt5_login"),
            "ignored": True,
            "reason": "terminal_account_snapshot_already_persisted",
            "persisted_account_status": account.get("account_status"),
        }, "terminal account snapshot acknowledged")

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
    # The immediately preceding /monitoring_snapshot may already have changed
    # this exact row to breached_archived.  A terminal lock is therefore an
    # idempotent exact-account write, not an active-account discovery operation.
    # Resolve by immutable trader_account_id and verify the supplied MT5 login.
    account = get_account_by_id_any_status(account_id, data.get("mt5_login"))
    if not account:
        return bad("Exact trader account not found or MT5 ownership evidence mismatched", 404)
    status = str(data.get("status") or "breached").lower()
    reason = data.get("reason") or "MT5 access disabled by monitoring engine"

    # V11 PASS-LOCK COMPATIBILITY.
    # Engine event names are evidence, not trader_accounts.account_status values.
    pass_status_map = {
        "phase1_passed": "archived_phase1",
        "phase2_passed": "archived_phase2",
    }
    persisted_account_status = (
        "breached_archived" if "breach" in status
        else pass_status_map.get(status, status)
    )

    # V14: final lock path uses the same reason-only first transaction.
    if "breach" in status:
        _guard_breach_at = account.get("breach_at") or account.get("breached_at") or now_iso()
        _guard_breach_level = (
            data.get("breach_equity_level")
            if data.get("breach_equity_level") not in (None, "")
            else data.get("equity")
            if data.get("equity") not in (None, "")
            else account.get("breach_equity_level")
            if account.get("breach_equity_level") not in (None, "")
            else account.get("current_equity")
        )
        preflight_ok, preflight_row, preflight_mode = verified_account_update(
            account.get("id"), {
                "breach_reason": reason,
                "breach_at": _guard_breach_at,
                "breach_equity_level": _guard_breach_level,
            }
        )
        stored_reason = str((preflight_row or {}).get("breach_reason") or "").strip()
        if not preflight_ok or not stored_reason:
            return bad(
                f"V14 breach_reason-only preflight failed: "
                f"account_write_ok={preflight_ok}, mode={preflight_mode}",
                500,
            )
        print(f"V14 BREACH_REASON PREFLIGHT VERIFIED MT5={account.get('mt5_login')}", flush=True)

    payload = {
        "account_status": persisted_account_status,
        "monitoring_enabled": False,
        "risk_zone": "breached" if "breach" in status else ("passed" if status in pass_status_map else status),
        "archive_reason": reason,
        "archived_at": now_iso(),
        "updated_at": now_iso(),
    }
    if status in pass_status_map:
        payload["phase_pass_status"] = status
        payload["passed_at"] = account.get("passed_at") or now_iso()

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
        # Production DB guard requires breach_reason + breach_at + breach_equity_level
        # in the same terminal transition. Keep breached_at too for compatibility
        # with older readers, but breach_at is the constraint-authoritative field.
        "breach_at": (account.get("breach_at") or account.get("breached_at") or now_iso()) if "breach" in status else account.get("breach_at"),
        "breached_at": (account.get("breached_at") or account.get("breach_at") or now_iso()) if "breach" in status else account.get("breached_at"),
        "breach_equity_level": (
            data.get("breach_equity_level")
            if data.get("breach_equity_level") not in (None, "")
            else data.get("equity")
            if data.get("equity") not in (None, "")
            else account.get("breach_equity_level")
            if account.get("breach_equity_level") not in (None, "")
            else account.get("current_equity")
        ) if "breach" in status else account.get("breach_equity_level"),
        "breach_reason": reason if "breach" in status else account.get("breach_reason"),
    }
    for k, v in evidence_map.items():
        if v not in (None, ""):
            payload[k] = v

    account_write_ok, persisted, write_mode = verified_account_update(account.get("id"), payload)
    if "breach" in status:
        trader_lock_update = {
            "status": "breached",
            "challenge_state": status,
            "mt5_access_disabled": True,
            "monitoring_enabled": False,
            "updated_at": now_iso(),
        }
    elif status in pass_status_map:
        # Preserve lifecycle state already written by /monitoring_snapshot.
        trader_lock_update = {
            "mt5_access_disabled": True,
            "monitoring_enabled": False,
            "phase_pass_status": status,
            "updated_at": now_iso(),
        }
    else:
        trader_lock_update = {
            "status": status,
            "challenge_state": status,
            "mt5_access_disabled": True,
            "monitoring_enabled": False,
            "updated_at": now_iso(),
        }
    trader_write_ok = verified_trader_update(account.get("trader_id"), trader_lock_update)
    safe_insert("monitoring_events", {"trader_id": account.get("trader_id"), "trader_account_id": account.get("id"), "mt5_login": account.get("mt5_login"), "event_type": status, "risk_zone": "breached" if "breach" in status else status, "message": reason, "balance": payload.get("current_balance"), "equity": payload.get("current_equity"), "drawdown_percent": payload.get("drawdown_percent"), "dd_used_percent": payload.get("dd_used_percent"), "created_at": now_iso()})
    alert_once(account, status, status.upper(), reason, "critical", data)
    expected_status = persisted_account_status
    persisted_status = str((persisted or {}).get("account_status") or "").lower()
    if not account_write_ok or persisted_status != str(expected_status).lower():
        return bad(f"MT5 lock persistence failed: account_write_ok={account_write_ok}, mode={write_mode}, persisted_status={persisted_status}, expected={expected_status}", 500)

    # V27: exact terminal account leaves the live Monitoring Registry immediately.
    # This makes the production handoff structural instead of requiring later cleanup.
    registry_retired = _retire_registry_exact(
        account.get("id"),
        "breach_completed" if "breach" in status else (
            f"{status}_completed" if status in pass_status_map else f"terminal_{status}"
        ),
    )

    return ok({
        "account_id": account.get("id"),
        "status": status,
        "persisted_account_status": persisted_status,
        "persisted_balance": (persisted or {}).get("current_balance"),
        "persisted_equity": (persisted or {}).get("current_equity"),
        "account_write_mode": write_mode,
        "trader_write_ok": trader_write_ok,
        "registry_retired": registry_retired,
    }, "access disabled, verified and registry retired")



# ============================================================================
# V9 MAGIC BLACK BOX — immutable historical breach reconstruction
# Uses already-persisted monitoring_snapshots + monitoring_events.
# It does NOT recalculate away a historical breach when the account later recovers.
# ============================================================================
@app.route("/breach_black_box", methods=["GET", "OPTIONS"])
def breach_black_box():
    if request.method == "OPTIONS":
        return ok({})

    _admin, auth_error = require_main_api_admin()
    if auth_error:
        return auth_error

    account_id = str(request.args.get("account_id") or "").strip()
    trader_id = str(request.args.get("trader_id") or "").strip()
    if not account_id:
        return bad("Exact trader_account_id is required", 400)

    account = get_account_by_id_any_status(account_id)
    if not account:
        return bad("Exact trader account not found", 404)
    if trader_id and str(account.get("trader_id") or "") != trader_id:
        return bad("Trader/account ownership mismatch", 409)

    def _rows(table):
        try:
            return (
                supabase.table(table)
                .select("*")
                .eq("trader_account_id", account_id)
                .order("created_at", desc=False)
                .execute().data or []
            )
        except Exception as exc:
            print("MAGIC BLACK BOX READ ERROR", table, exc, flush=True)
            return []

    snapshots = _rows("monitoring_snapshots")
    events = _rows("monitoring_events")

    rules = resolve_account_rules(account, account.get("stage"))
    dd_limit = num(rules.get("dd_limit_percent"), 0.0)
    start = num(
        account.get("start_balance")
        or account.get("account_size")
        or account.get("initial_balance")
        or account.get("starting_balance")
        or account.get("challenge_balance")
        or account.get("original_balance")
        or 0
    )
    breach_level = round(start * (1 - dd_limit / 100.0), 2) if start and dd_limit > 0 else num(account.get("breach_equity_level"), 0.0)

    def snap_time(row):
        return str(row.get("created_at") or row.get("timestamp") or row.get("last_sync_at") or "")

    def row_dd(row):
        return num(row.get("drawdown_percent"), 0.0)

    def row_equity(row):
        return num(row.get("current_equity") if row.get("current_equity") not in (None, "") else row.get("equity"), 0.0)

    def row_balance(row):
        return num(row.get("current_balance") if row.get("current_balance") not in (None, "") else row.get("balance"), 0.0)

    # FIRST CROSSING is immutable historical evidence: earliest stored live observation
    # at/beyond the exact DD rule. Later recovery cannot replace this row.
    first_crossing = None
    first_index = None
    for i, row in enumerate(snapshots):
        limit = num(row.get("dd_limit_percent"), dd_limit)
        level = num(row.get("breach_equity_level"), breach_level)
        eq = row_equity(row)
        bal = row_balance(row)
        dd = row_dd(row)
        crossed = bool(
            (limit > 0 and dd >= limit)
            or (level > 0 and eq > 0 and eq <= level)
            or (level > 0 and bal > 0 and bal <= level)
            or str(row.get("risk_zone") or row.get("zone") or "").lower() == "breached"
            or str(row.get("event_type") or "").lower() == "breached"
        )
        if crossed:
            first_crossing = row
            first_index = i
            break

    # If the historical snapshot table predates the breach capture, preserve that fact
    # instead of inventing an exact crossing that was never stored.
    evidence_quality = "EXACT_STORED_FIRST_CROSSING" if first_crossing else "LEGACY_TERMINAL_RECORD_ONLY"
    if not first_crossing:
        first_crossing = next((e for e in events if str(e.get("event_type") or "").lower() == "breached"), None)

    lowest_row = None
    if snapshots:
        positive = [r for r in snapshots if row_equity(r) > 0]
        if positive:
            lowest_row = min(positive, key=row_equity)

    latest = snapshots[-1] if snapshots else {}
    fc_eq = row_equity(first_crossing or {})
    fc_bal = row_balance(first_crossing or {})
    fc_dd = row_dd(first_crossing or {})
    fc_limit = num((first_crossing or {}).get("dd_limit_percent"), dd_limit)
    fc_level = num((first_crossing or {}).get("breach_equity_level"), breach_level)
    fc_float = num((first_crossing or {}).get("floating_profit"), (fc_eq - fc_bal) if fc_eq and fc_bal else 0.0)
    exceeded = max(0.0, fc_level - min(x for x in [fc_eq, fc_bal] if x > 0)) if fc_level and (fc_eq > 0 or fc_bal > 0) else 0.0

    post = snapshots[(first_index + 1):] if first_index is not None else []
    recovered_rows = [
        r for r in post
        if fc_level > 0 and row_equity(r) > fc_level
    ]
    recovered = bool(recovered_rows)
    recovery_row = recovered_rows[0] if recovered_rows else None

    # Compact immutable timeline around the breach plus all explicit breach/lock events.
    timeline = []
    if first_index is not None:
        lo = max(0, first_index - 5)
        hi = min(len(snapshots), first_index + 11)
        for r in snapshots[lo:hi]:
            timeline.append({
                "kind": "snapshot",
                "timestamp": snap_time(r),
                "balance": row_balance(r),
                "equity": row_equity(r),
                "floating_profit": num(r.get("floating_profit"), row_equity(r) - row_balance(r)),
                "drawdown_percent": row_dd(r),
                "dd_limit_percent": num(r.get("dd_limit_percent"), dd_limit),
                "risk_zone": r.get("risk_zone") or r.get("zone"),
                "event_type": r.get("event_type"),
                "message": r.get("message"),
            })
    for e in events:
        et = str(e.get("event_type") or "").lower()
        if et in {"breached", "breach", "disabled", "locked"} or "breach" in et:
            timeline.append({
                "kind": "event",
                "timestamp": snap_time(e),
                "balance": row_balance(e),
                "equity": row_equity(e),
                "drawdown_percent": row_dd(e),
                "dd_limit_percent": num(e.get("dd_limit_percent"), dd_limit),
                "risk_zone": e.get("risk_zone"),
                "event_type": e.get("event_type"),
                "message": e.get("message"),
            })
    timeline.sort(key=lambda x: str(x.get("timestamp") or ""))

    event_id = None
    if first_crossing:
        event_id = first_crossing.get("intelligence_event_id")
    if not event_id:
        event_id = f"NP-BREACH-{account_id}-{str(account.get('breached_at') or snap_time(first_crossing or {}) or 'legacy').replace(':','').replace('-','')}"

    payload = {
        "event_id": event_id,
        "evidence_quality": evidence_quality,
        "immutable": True,
        "account": {
            "id": account.get("id"),
            "trader_id": account.get("trader_id"),
            "mt5_login": account.get("mt5_login"),
            "stage": account.get("stage"),
            "account_status": account.get("account_status"),
            "account_size": num(account.get("account_size"), start),
            "breached_at": account.get("breached_at"),
            "breach_reason": account.get("breach_reason"),
            "current_balance": num(account.get("current_balance"), 0.0),
            "current_equity": num(account.get("current_equity"), 0.0),
        },
        "rule": {
            "start_balance": start,
            "dd_limit_percent": dd_limit,
            "breach_equity_level": breach_level,
            "authority_present": bool(rules.get("dd_authority_present")),
            "authority": "exact trader_account / linked purchase / linked plan",
        },
        "first_crossing": {
            "timestamp": snap_time(first_crossing or {}) or account.get("breached_at"),
            "balance": fc_bal,
            "equity": fc_eq,
            "floating_profit": fc_float,
            "drawdown_percent": fc_dd,
            "dd_limit_percent": fc_limit,
            "breach_equity_level": fc_level,
            "exceeded_amount": round(exceeded, 2),
            "risk_zone": (first_crossing or {}).get("risk_zone") or (first_crossing or {}).get("zone"),
            "breach_source": (first_crossing or {}).get("breach_source"),
            "message": (first_crossing or {}).get("message"),
        },
        "lowest_observed": {
            "timestamp": snap_time(lowest_row or {}),
            "equity": row_equity(lowest_row or {}),
            "balance": row_balance(lowest_row or {}),
            "drawdown_percent": row_dd(lowest_row or {}),
        },
        "recovery_after_breach": {
            "recovered": recovered,
            "first_recovery_timestamp": snap_time(recovery_row or {}),
            "recovery_equity": row_equity(recovery_row or {}),
            "latest_timestamp": snap_time(latest),
            "latest_balance": row_balance(latest),
            "latest_equity": row_equity(latest),
        },
        "timeline": timeline,
        "snapshot_count": len(snapshots),
        "event_count": len(events),
        "generated_at": now_iso(),
    }
    return ok(payload, "immutable breach black box loaded")


@app.route("/sync_trades", methods=["POST", "OPTIONS"])
def sync_trades():
    """Fast trade-history sync.

    The previous route performed one Supabase network upsert PER trade. An account
    with 200-250 historical deals could therefore hold this request for tens of
    seconds and hit the engine's 60-second timeout/Gunicorn timeout, delaying the
    rest of the monitoring round.

    V7 validates rows exactly as before, deduplicates ticket+login, then writes in
    bulk chunks. Live balance/equity snapshots are never made dependent on hundreds
    of sequential trade-history writes.
    """
    if request.method == "OPTIONS":
        return ok({})

    try:
        data = request.get_json(silent=True) or {}
        trades = data.get("trades") or []
        if not isinstance(trades, list):
            return bad("trades must be a list")

        skipped = 0
        account_cache = {}
        validated = []

        for trade in trades[:500]:
            if not isinstance(trade, dict):
                continue
            try:
                row = dict(trade)
                lookup_id = (
                    row.get("trader_account_id")
                    or row.get("current_account_id")
                    or data.get("trader_account_id")
                    or data.get("current_account_id")
                )
                lookup_login = row.get("mt5_login") or data.get("mt5_login")

                if not lookup_id:
                    skipped += 1
                    print("TRADE SYNC SKIPPED WITHOUT EXACT ACCOUNT ID:", {"mt5_login": clean_login(lookup_login)}, flush=True)
                    continue

                cache_key = f"{lookup_id or ''}:{clean_login(lookup_login)}"
                if cache_key not in account_cache:
                    account_cache[cache_key] = get_account_by_id_or_login(lookup_id, lookup_login)
                account = account_cache.get(cache_key)

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
                validated.append(row)
            except Exception as row_error:
                skipped += 1
                print("TRADE SYNC ROW VALIDATION ERROR:", str(row_error)[:400], flush=True)

        # The DB conflict authority is ticket+mt5_login, so collapse duplicate rows
        # inside this same request before sending them to PostgREST.
        deduped = {}
        no_ticket = []
        for row in validated:
            ticket = str(row.get("ticket") or "").strip()
            login = clean_login(row.get("mt5_login"))
            if ticket and login:
                deduped[(ticket, login)] = row
            else:
                no_ticket.append(row)
        rows_to_save = list(deduped.values()) + no_ticket

        saved = 0
        failed = 0
        chunk_size = 100

        for i in range(0, len(rows_to_save), chunk_size):
            chunk = rows_to_save[i:i + chunk_size]
            if not chunk:
                continue
            try:
                # One request per 100 trades instead of one request per trade.
                supabase.table("trader_trades").upsert(
                    chunk, on_conflict="ticket,mt5_login"
                ).execute()
                saved += len(chunk)
                continue
            except Exception as upsert_error:
                print(
                    "TRADE BATCH UPSERT FALLBACK:",
                    {"rows": len(chunk), "error": str(upsert_error)[:350]},
                    flush=True,
                )

            # Bulk insert fallback keeps the route bounded. Do not fall back to
            # hundreds of sequential network calls, because that recreates the
            # production timeout that caused multi-hour monitoring delays.
            try:
                supabase.table("trader_trades").insert(chunk).execute()
                saved += len(chunk)
            except Exception as insert_error:
                failed += len(chunk)
                print(
                    "TRADE BATCH SAVE SKIPPED:",
                    {"rows": len(chunk), "error": str(insert_error)[:500]},
                    flush=True,
                )

        result = {
            "received": len(trades),
            "validated": len(validated),
            "deduplicated": len(rows_to_save),
            "saved": saved,
            "skipped_non_active": skipped,
            "failed": failed,
            "write_mode": "bulk_chunks_100",
        }

        # Always return JSON. A trade-history problem must not produce an HTML 500
        # page that stalls the VPS engine. Failed history rows will be retried by
        # the engine's periodic history sync.
        return ok(result, "trades synced")
    except Exception as e:
        print("SYNC_TRADES FATAL JSON-SAFE ERROR:", repr(e), flush=True)
        return bad("sync_trades failed safely: " + str(e), 500)


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


@app.route("/monitoring_account_diagnostic", methods=["GET"])
def monitoring_account_diagnostic():
    """Read-only explanation of why one MT5 is or is not in the live engine feed."""
    login = clean_login(request.args.get("mt5_login"))
    if not login:
        return bad("mt5_login is required", 400)
    try:
        rows = (
            supabase.table("trader_accounts")
            .select("*")
            .eq("mt5_login", login)
            .order("updated_at", desc=True)
            .limit(20)
            .execute().data or []
        )
        active_rows = _np_fetch_all_active_monitoring_accounts()
        active_same_login = [r for r in active_rows if clean_login(r.get("mt5_login")) == login]
        feed = _fast_monitorable_feed()
        in_feed = [r for r in feed if clean_login(r.get("mt5_login")) == login]
        reasons = []
        for r in rows:
            why = []
            if not is_active_monitoring_account(r): why.append("account_not_active_for_monitoring")
            if r.get("breach_at") or r.get("breached_at"): why.append("confirmed_breach_timestamp")
            if str(r.get("risk_zone") or "").strip().lower() == "breached" and not (r.get("breach_at") or r.get("breached_at")):
                why.append("stale_or_unconfirmed_breached_label_does_not_block_monitoring")
            if not str(r.get("mt5_server") or "").strip(): why.append("missing_mt5_server")
            if bool_false(r.get("monitoring_enabled")): why.append("monitoring_enabled_false")
            if bool_true(r.get("mt5_access_disabled")) and not is_funded_cap_lock(r): why.append("mt5_access_disabled")
            if r.get("superseded_at") or r.get("replaced_at") or bool_true(r.get("superseded")): why.append("superseded_or_replaced")
            reasons.append({
                "id": r.get("id"),
                "account_status": r.get("account_status"),
                "stage": r.get("stage"),
                "last_sync_at": r.get("last_sync_at"),
                "updated_at": r.get("updated_at"),
                "current_balance": r.get("current_balance"),
                "current_equity": r.get("current_equity"),
                "eligible_before_ambiguity_check": len(why) == 0,
                "reasons": why,
            })
        return ok({
            "mt5_login": login,
            "database_rows": len(rows),
            "active_status_rows": len(active_same_login),
            "live_feed_rows": len(in_feed),
            "duplicate_active_login": len(active_same_login) > 1,
            "rows": reasons,
            "release": NAIRAPIPS_MONITORING_RELEASE,
        }, "monitoring account diagnostic")
    except Exception as e:
        return bad(e, 500)


@app.route("/monitoring_coverage_health", methods=["GET"])
def monitoring_coverage_health():
    """Read-only proof that every active DB account is either in the live feed or explicitly excluded."""
    try:
        rows = _np_fetch_all_active_monitoring_accounts()
        feed = _fast_monitorable_feed()
        feed_ids = {str(r.get("trader_account_id") or r.get("id") or "").strip() for r in feed if str(r.get("trader_account_id") or r.get("id") or "").strip()}
        excluded = []
        for a in rows:
            aid = str(a.get("id") or "").strip()
            if aid in feed_ids:
                continue
            why = []
            if not is_active_monitoring_account(a): why.append("not_active_or_confirmed_terminal")
            if not str(a.get("mt5_server") or "").strip(): why.append("missing_mt5_server")
            if bool_false(a.get("monitoring_enabled")): why.append("monitoring_enabled_false")
            if bool_true(a.get("mt5_access_disabled")) and not is_funded_cap_lock(a): why.append("mt5_access_disabled")
            if a.get("superseded_at") or a.get("replaced_at") or bool_true(a.get("superseded")): why.append("superseded_or_replaced")
            excluded.append({
                "trader_account_id": aid,
                "mt5_login": clean_login(a.get("mt5_login")),
                "stage": a.get("stage"),
                "account_status": a.get("account_status"),
                "risk_zone": a.get("risk_zone"),
                "last_sync_at": a.get("last_sync_at"),
                "reasons": why or ["ownership_or_duplicate_guard"],
            })
        return ok({
            "release": NAIRAPIPS_MONITORING_RELEASE,
            "active_db_rows": len(rows),
            "live_feed_rows": len(feed),
            "excluded_rows": len(excluded),
            "coverage_percent": round((len(feed) / len(rows) * 100.0), 2) if rows else 100.0,
            "excluded": excluded[:1000],
        }, "monitoring coverage health")
    except Exception as e:
        return bad(e, 500)


@app.route("/account_intelligence_scan")
def account_intelligence_scan():
    try:
        rows = _np_fetch_all_active_monitoring_accounts()
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

# NP_FIX: RECALL_REOPENS_SAME_SECOND_LIFE_MT5_ENTITLEMENT_2026_09_08


# ============================================================================
# NAIRAPIPS MONITORING V9 — EXACT RECALL AUTO-REPLACE KICK
# 22 SEP 2026
#
# Recall remains a two-authority operation:
#   Monitoring proves/voids the exact unused bad MT5.
#   Main API Journey Authority proves the SAME entitlement reappeared.
# Only AFTER both proofs succeed may this helper wake an exact replacement path.
# Payout renewal is especially important because its broad DB sweep is disabled;
# the immutable payout_id is therefore used to wake only that renewal.
# ============================================================================

NAIRAPIPS_MONITORING_RECALL_RELEASE_V9 = "V9_EXACT_RECALL_AUTO_REPLACE_2026_09_22"


def _np_recall_post_main_v9(auth_header, path, body, timeout=25):
    try:
        payload = json.dumps(body or {}).encode("utf-8")
        req = urlrequest.Request(
            MAIN_API_URL + path,
            data=payload,
            headers={
                "Authorization": auth_header,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8") or "{}"
            data = json.loads(raw)
            return {
                "requested": True,
                "http_status": int(getattr(response, "status", 200) or 200),
                "success": data.get("success") is not False,
                "response": data,
            }
    except Exception as exc:
        # This is deliberately non-destructive. The exact entitlement is already
        # restored and remains waiting if the immediate kick cannot complete.
        return {
            "requested": True,
            "success": False,
            "deferred": True,
            "error": str(exc),
        }


def _np_recall_kick_exact_replacement_v9(auth_header, trader_id, journey_id, entitlement_type, evidence_id):
    ent_type = str(entitlement_type or "").strip().lower()
    evidence = str(evidence_id or "").strip()

    if ent_type == "payout_renewal":
        if not evidence:
            return {
                "requested": False,
                "success": False,
                "deferred": True,
                "reason": "payout renewal restored but exact payout_id evidence is missing",
            }
        result = _np_recall_post_main_v9(
            auth_header,
            "/admin/retry_exact_payout_renewal",
            {"payout_id": evidence},
            timeout=25,
        )
        response = (result or {}).get("response") or {}
        data = response.get("data") if isinstance(response.get("data"), dict) else response
        if isinstance(data, dict):
            # Endpoint is often async. Expose what it actually reported without
            # claiming a replacement was assigned when it merely started processing.
            result["state"] = data.get("state") or data.get("status")
            result["assigned"] = bool(data.get("assigned") or data.get("replacement_account_id") or data.get("mt5_login"))
            result["mt5_login"] = data.get("mt5_login")
        result["entitlement_type"] = ent_type
        result["evidence_id"] = evidence
        return result

    # Other entitlement classes keep their already-protected stage-specific retry
    # routes. V8/V79 have restored the exact right; we do not invent a generic
    # cross-stage auto-fire route here.
    return {
        "requested": False,
        "success": True,
        "deferred_to_existing_exact_automation": True,
        "entitlement_type": ent_type,
        "evidence_id": evidence or None,
        "journey_id": str(journey_id or "") or None,
        "trader_id": str(trader_id or "") or None,
    }
