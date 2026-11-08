"""Static and uncapped equity-trailing risk, with exact-account durable state.

The allowance is a fixed percentage of starting capital, never of peak equity.
This module has no network, broker, or worker side effects on import.
"""
import copy
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid4

RELEASE = 'NP_EQUITY_TRAILING_V1_20261107'


def mode(value):
    result = str(value or 'STATIC').strip().upper()
    if result not in ('STATIC', 'TRAILING'):
        raise ValueError('Choose STATIC or TRAILING')
    return result


def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError('A finite number is required')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('A finite number is required') from exc
    if not result.is_finite():
        raise ValueError('A finite number is required')
    return result


def timestamp(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('A timezone is required')
    return result.astimezone(timezone.utc)


def resolve_mode(account, purchase=None):
    """Issued accounts never inherit later edits to a mutable plan."""
    account, purchase = account or {}, purchase or {}
    if purchase:
        if str(purchase.get('id')) != str(account.get('purchase_id') or account.get('challenge_purchase_id')):
            raise ValueError('Purchase does not belong to this account')
        if str(purchase.get('trader_id')) != str(account.get('trader_id')):
            raise ValueError('Purchase owner differs from account owner')
    frozen = 'TRAILING' if str(account.get('journey_source') or '').startswith('equity_trailing_v1:') else None
    return mode(frozen or account.get('drawdown_type') or purchase.get('drawdown_type') or 'STATIC')


def spec(account, purchase=None):
    result = dict(account_id=str(UUID(str(account.get('trader_account_id') or account['id']))),
        trader_id=str(UUID(str(account['trader_id']))),
        purchase_id=str(account.get('purchase_id') or account.get('challenge_purchase_id') or ''),
        mt5_login=str(account['mt5_login']).strip(), stage=str(account['stage']).lower(),
        capital=format(number(account.get('start_balance') or account.get('account_size')).normalize(), 'f'),
        limit=format(number(account['dd_limit_percent']).normalize(), 'f'), drawdown_type=resolve_mode(account, purchase))
    if (not result['mt5_login'] or result['stage'] not in ('phase1', 'phase2', 'funded')
            or number(result['capital']) <= 0 or not 0 < number(result['limit']) < 100):
        raise ValueError('Exact account risk terms are required')
    from plan_risk_controls import daily_percent
    daily = daily_percent(account)
    if daily > 0:
        result['daily_drawdown'] = str(daily.normalize())
    return result


def fingerprint(rules):
    return hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def initial(rules):
    return dict(rules_hash=fingerprint(rules), highest_equity=rules['capital'],
        last_observed_at=None, breached=False, first_breach=None, worst_used_percent='0')


def evaluate(rules, previous, equity, observed_at, peak_equity=None, balance=None, daily_realised=None):
    """Evaluate in observation order; a low before a high cannot breach later."""
    if previous['rules_hash'] != fingerprint(rules):
        raise ValueError('Account identity or frozen rules changed')
    if type(previous['breached']) is not bool:
        raise ValueError('Invalid saved breach state')
    capital, limit, equity = number(rules['capital']), number(rules['limit']), number(equity)
    observed = timestamp(observed_at)
    high = number(previous['highest_equity'])
    if high < capital or number(previous['worst_used_percent']) < 0:
        raise ValueError('Saved watermark or risk usage is invalid')
    if previous['breached']:
        first = previous.get('first_breach')
        if not isinstance(first, dict) or (number(first['equity']) if mode(rules['drawdown_type']) == 'TRAILING' else min(number(first['equity']), number(first.get('balance',first['equity'])))) > number(first['floor']):
            raise ValueError('Saved terminal breach evidence is invalid')
        timestamp(first['observed_at'])
    state = copy.deepcopy(previous)
    if previous['last_observed_at'] and observed <= timestamp(previous['last_observed_at']):
        return state
    if previous['breached']:
        return state
    high = max(capital, high, equity, number(peak_equity) if peak_equity is not None else capital)
    allowance = capital * limit / 100
    reference = high if mode(rules['drawdown_type']) == 'TRAILING' else capital
    floor = reference - allowance
    risk_value = equity if mode(rules['drawdown_type']) == 'TRAILING' or balance is None else min(equity, number(balance))
    damage = max(Decimal(0), reference - risk_value)
    used = damage / allowance * 100
    overall_breach = risk_value <= floor
    daily_breach = False
    daily_enabled = number(rules.get('daily_drawdown', 0)) > 0
    if daily_enabled:
        state['overall_floor'] = str(floor)
    if daily_enabled and (balance is None or daily_realised is None) and risk_value > floor:
        raise ValueError('Verified daily trading history and balance are required')
    if daily_enabled and balance is not None and daily_realised is not None:
        from plan_risk_controls import WAT
        daily_allowance = capital * number(rules['daily_drawdown']) / 100
        realised = number(daily_realised)
        daily_floor = number(balance) - realised - daily_allowance
        daily_loss = max(Decimal(0), realised * -1 + number(balance) - equity)
        daily_used = daily_loss / daily_allowance * 100
        daily_breach = equity <= daily_floor
        state.update(daily_floor=str(daily_floor), daily_realised=str(realised),
            daily_day=observed.astimezone(WAT).date().isoformat(),
            daily_loss_percent=str(daily_loss / capital * 100), daily_used_percent=str(daily_used))
        if daily_floor > floor:
            floor = daily_floor
        used = max(used, daily_used)
    breach = overall_breach or daily_breach
    state.update(highest_equity=str(high), equity=str(equity), drawdown_floor=str(floor),
        drawdown_amount=str(damage), drawdown_percent=str(damage / capital * 100),
        used_percent=str(used), worst_used_percent=str(max(number(previous['worst_used_percent']), used)),
        breached=breach, last_observed_at=observed.isoformat())
    if breach:
        state['first_breach'] = dict(observed_at=observed.isoformat(), equity=str(equity), balance=str(number(balance)) if balance is not None else str(equity), floor=str(floor))
    return state


@contextmanager
def account_lock(path):
    with open(path, 'a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'0'); stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class EquityDrawdownJournal:
    """Broker-local atomic journal. Missing history on an old account fails closed."""
    def __init__(self, directory):
        self.directory = Path(directory)

    def assess(self, account, equity, observed_at, now=None, balance=None, daily_realised=None):
        rules = account['drawdown_rules']
        if (str(account.get('trader_account_id') or account.get('id')) != rules['account_id']
                or str(account.get('mt5_login')) != rules['mt5_login']
                or str(account.get('trader_id')) != rules['trader_id']):
            raise ValueError('Roster identity differs from frozen risk rules')
        now = now or datetime.now(timezone.utc)
        observed = timestamp(observed_at)
        if not -5 <= (now - observed).total_seconds() <= 30:
            raise ValueError('A current broker observation is required')
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (str(UUID(rules['account_id'])) + '.json')
        with account_lock(path.with_suffix('.lock')):
            if path.exists():
                if path.stat().st_size > 65536:
                    raise ValueError('Invalid drawdown journal')
                state = json.loads(path.read_text())
            else:
                started = timestamp(account.get('account_started_at') or account.get('started_at') or account.get('created_at'))
                if not 0 <= (now - started).total_seconds() <= 300:
                    raise ValueError('Existing account watermark must be recovered; it cannot be reset')
                state = initial(rules)
            updated = evaluate(rules, state, equity, observed_at, balance=balance, daily_realised=daily_realised)
            if updated != state or not path.exists():
                temporary = path.with_suffix('.' + str(uuid4()) + '.tmp')
                try:
                    with open(temporary, 'x', encoding='utf-8') as stream:
                        json.dump(updated, stream, sort_keys=True)
                        stream.flush(); os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    if temporary.exists():
                        temporary.unlink()
            return dict(updated, new_breach=updated["breached"] and not state["breached"])


class DrawdownStateStore:
    """Cross-worker CAS journal in the existing service-only private settings table."""
    def __init__(self, transport, url, key):
        self.http = transport
        self.url = url.rstrip('/') + '/rest/v1/package_studio_private_settings'
        self.headers = {'apikey': key, 'Authorization': 'Bearer ' + key,
            'Prefer': 'return=representation'}

    def assess(self, account, purchase, equity, observed_at, now=None, peak_equity=None, balance=None, daily_realised=None):
        rules = spec(account, purchase)
        if rules['drawdown_type'] != 'TRAILING' and not rules.get('daily_drawdown'):
            raise ValueError('Durable trailing journal requires trailing terms')
        now = now or datetime.now(timezone.utc)
        observed = timestamp(observed_at)
        if not -5 <= (now-observed).total_seconds() <= 180:
            raise ValueError('A current broker observation is required')
        key = 'equity_drawdown_v1:' + rules['account_id']
        for _ in range(3):
            response = self.http.get(self.url, headers=self.headers,
                params={'key': 'eq.'+key, 'select': 'value', 'limit': '1'}, timeout=(2, 3))
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or len(rows) > 1:
                raise ValueError('Invalid trailing state response')
            raw = rows[0]['value'] if rows else None
            if raw is None:
                started = timestamp(account.get('started_at') or account.get('created_at'))
                if not 0 <= (now-started).total_seconds() <= 300:
                    raise ValueError('Existing account trailing history is unavailable; recovery required')
                previous = initial(rules)
            else:
                previous = json.loads(raw)
            state = evaluate(rules, previous, equity, observed_at, peak_equity=peak_equity, balance=balance, daily_realised=daily_realised)
            if raw is not None and state == previous:
                return state
            value = json.dumps(state, sort_keys=True, separators=(',', ':'))
            if raw is None:
                response = self.http.post(self.url, headers=self.headers,
                    json={'key': key, 'value': value, 'updated_at': now.isoformat(), 'updated_by': RELEASE}, timeout=(2, 3))
                if response.status_code == 409:
                    continue
            else:
                response = self.http.patch(self.url, headers=self.headers,
                    params={'key': 'eq.'+key, 'value': 'eq.'+raw},
                    json={'value': value, 'updated_at': now.isoformat(), 'updated_by': RELEASE}, timeout=(2, 3))
            response.raise_for_status()
            confirmed = response.json()
            if isinstance(confirmed, list) and len(confirmed) == 1 and confirmed[0].get('value') == value:
                return state
        raise ValueError('Trailing state changed concurrently; retry this observation')


def runtime_ready():
    """An operator can enable sales after installing and verifying every DD shard."""
    return bool(os.environ.get('NP_EQUITY_TRAILING_INGEST_KEY') or os.environ.get('NP_LIVE_STATE_INGEST_KEY')) and (os.environ.get('NP_EQUITY_TRAILING_VERIFIED', '') == RELEASE or __import__('plan_risk_controls').daily_runtime_ready())


def account_mode(account, purchase=None):
    """Cheap legacy default; strict purchase identity for explicitly typed accounts."""
    if not (account or {}).get('drawdown_type') and not (purchase or {}).get('drawdown_type') and not str((account or {}).get('journey_source') or '').startswith('equity_trailing_v1:'):
        return 'STATIC'
    return resolve_mode(account, purchase)


def read_saved_state(transport, url, key, account, purchase):
    rules = spec(account, purchase)
    store = DrawdownStateStore(transport, url, key)
    response = transport.get(store.url, headers=store.headers,
        params={'key':'eq.equity_drawdown_v1:'+rules['account_id'], 'select':'value', 'limit':'1'}, timeout=(2,3))
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows,list) or len(rows)!=1:
        raise ValueError('Saved trailing watermark is unavailable')
    state = json.loads(rows[0]['value'])
    if state['rules_hash'] != fingerprint(rules):
        raise ValueError('Saved trailing terms differ from this account')
    return state


def verify_ingest(request):
    """Trailing state cannot be advanced by browser-supplied observations."""
    import hmac
    expected = os.environ.get('NP_EQUITY_TRAILING_INGEST_KEY') or os.environ.get('NP_LIVE_STATE_INGEST_KEY')
    supplied = request.headers.get('X-NP-Trailing-Key', '')
    if not expected or not hmac.compare_digest(expected, supplied):
        raise ValueError('Authenticated trailing broker observation required')
