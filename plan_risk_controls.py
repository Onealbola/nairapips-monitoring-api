"""Optional future-plan daily limits and payout calendars, frozen at purchase."""
import base64
import calendar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
import time

RELEASE = 'NP_PLAN_RISK_CONTROLS_V1_20261108'
MARKER = '|np_risk_controls_v1:'
FREQUENCIES = ('Daily', 'Weekly', 'Biweekly', 'Monthly', 'On Demand')
WAT = timezone(timedelta(hours=1))


def reset_terms(package):
    result = {}
    for stage in ('phase1', 'phase2', 'funded'):
        enabled = package.get(stage+'_reset_enabled', False)
        if type(enabled) is not bool:
            raise ValueError('Use the reset availability checkboxes')
        raw = package.get(stage+'_reset_fee', 0)
        if isinstance(raw, bool):
            raise ValueError('Enter a valid reset price')
        try:
            amount = Decimal(str(raw))
        except (ValueError, InvalidOperation):
            raise ValueError('Enter a valid reset price')
        if not amount.is_finite() or not 0 <= amount <= 100000000 or amount != amount.quantize(Decimal('.01')):
            raise ValueError('Reset prices must be finite naira amounts with at most two decimals')
        if enabled:
            if amount <= 0:
                raise ValueError('Enter a positive reset price or turn that stage off')
            result[stage] = str(amount.normalize())
    return result


def percentage(value):
    if isinstance(value, bool):
        raise ValueError('Enter a daily drawdown percentage between 0 and 100')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Enter a daily drawdown percentage between 0 and 100')
    if not result.is_finite() or not 0 <= result <= 100:
        raise ValueError('Enter a daily drawdown percentage between 0 and 100')
    return result


def selected_terms(package):
    daily = percentage(package.get('daily_drawdown', 0))
    enabled = package.get('daily_drawdown_enabled', daily > 0)
    scheduled = package.get('payout_schedule_enabled', False)
    if type(enabled) is not bool or type(scheduled) is not bool:
        raise ValueError('Use the daily drawdown and payout schedule checkboxes')
    if enabled and daily <= 0:
        raise ValueError('Enter a positive daily drawdown percentage or turn its checkbox off')
    frequency = package.get('payout_frequency', 'Daily')
    if frequency not in FREQUENCIES:
        raise ValueError('Choose a valid payout schedule')
    result = dict(daily_drawdown=str(daily.normalize()) if enabled else '0',
                  payout_frequency=frequency if scheduled else 'On Demand')
    resets = reset_terms(package)
    if resets:
        result['resets'] = resets
    return result


def freeze(source, terms):
    if MARKER in str(source or ''):
        raise ValueError('Risk terms are already frozen')
    payload = dict(daily_drawdown=str(percentage(terms['daily_drawdown']).normalize()),
                   payout_frequency=terms['payout_frequency'])
    if payload['payout_frequency'] not in FREQUENCIES:
        raise ValueError('Invalid payout schedule')
    if terms.get('resets'):
        payload['resets'] = terms['resets']
    if terms.get('trailing_formula') is not None:
        from balance_trailing import FORMULA
        if terms['trailing_formula']!=FORMULA:raise ValueError('Unsupported frozen trailing formula')
        payload['trailing_formula']=FORMULA
    encoded = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).decode().rstrip('=')
    return str(source or '') + MARKER + encoded


def frozen_terms(row):
    source = str((row or {}).get('journey_source') or '')
    if MARKER not in source:
        return None
    try:
        encoded = source.rsplit(MARKER, 1)[1]
        result = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        if not {'daily_drawdown', 'payout_frequency'} <= set(result) or set(result)-{'daily_drawdown','payout_frequency','resets','trailing_formula'}:
            raise ValueError()
        if 'trailing_formula' in result and result['trailing_formula']!=__import__('balance_trailing').FORMULA:raise ValueError()
        percentage(result['daily_drawdown'])
        if result['payout_frequency'] not in FREQUENCIES:
            raise ValueError()
        if 'resets' in result:
            if not isinstance(result['resets'],dict) or not result['resets'] or set(result['resets'])-{'phase1','phase2','funded'}:
                raise ValueError()
            reset_terms({field:value for stage,price in result['resets'].items()
                         for field,value in ((stage+'_reset_enabled',True),(stage+'_reset_fee',price))})
        return result
    except Exception as exc:
        raise ValueError('Frozen account risk terms are invalid') from exc


def daily_percent(row):
    terms = frozen_terms(row)
    return percentage(terms['daily_drawdown']) if terms else Decimal(0)


def inherit(source, row):
    terms = frozen_terms(row)
    source = __import__('withdrawal_controls').inherit(source,row)
    return freeze(source, terms) if terms else source


def assert_assignment_snapshot(account, purchase):
    """Refuse a new assignment that would drop its exact purchased controls.

    This validates the proposed insert only; it never edits existing accounts or
    reconstructs missing broker history from current values.
    """
    if not purchase or not purchase.get('id'):
        return
    if (str(account.get('purchase_id') or '') != str(purchase['id'])
            or str(account.get('trader_id') or '') != str(purchase.get('trader_id') or '')):
        raise ValueError('Assignment risk snapshot belongs to another purchase or owner')
    if frozen_terms(account) != frozen_terms(purchase):
        raise ValueError('Assignment stopped: purchased daily, payout or reset rules were not preserved')
    from withdrawal_controls import read as withdrawal_terms
    if withdrawal_terms(account) != withdrawal_terms(purchase):
        raise ValueError('Assignment stopped: purchased withdrawal rules were not preserved')
    from equity_drawdown import account_mode, mode
    if account_mode(account) != mode(purchase.get('drawdown_type')):
        raise ValueError('Assignment stopped: purchased drawdown mode was not preserved')


def plan_controls_view(row):
    result = dict(row)
    terms = frozen_terms(row) or {}
    result['daily_drawdown_enabled'] = float(terms.get('daily_drawdown', 0)) > 0
    legacy_daily = row.get('daily_drawdown') or 0
    if str(legacy_daily).lower() in ('none','off','disabled'):
        legacy_daily = 0
    result['daily_drawdown'] = float(terms.get('daily_drawdown', legacy_daily))
    result['payout_schedule_enabled'] = terms.get('payout_frequency', 'On Demand') != 'On Demand'
    result['payout_frequency'] = terms.get('payout_frequency', row.get('payout_frequency') or 'On Demand')
    from plan_commercial import reset_price
    for stage in ('phase1', 'phase2', 'funded'):
        price = reset_price(row, stage)
        result[stage+'_reset_enabled'] = float(price or 0) > 0
        result[stage+'_reset_fee'] = float(price or 0)
    return result


def edit_plan_controls(row, body):
    """Only called by authenticated plan writes; never touches issued snapshots."""
    keys = {'daily_drawdown_enabled', 'payout_schedule_enabled'} | {stage+'_reset_enabled' for stage in ('phase1','phase2','funded')}
    legacy_reset_change = bool((frozen_terms(row) or {}).get('resets')) and bool({'reset_fee','funded_reset_fee'}.intersection(body))
    if not keys.intersection(body) and not legacy_reset_change:
        return dict(row, **{k:v for k,v in body.items() if k in {'reset_fee','funded_reset_fee'}})
    controls = plan_controls_view(row)
    controls.update(body)
    for field,stages in (('reset_fee',('phase1','phase2')),('funded_reset_fee',('funded',))):
        if field in body and body[field] != row.get(field):
            for stage in stages:
                if stage+'_reset_enabled' not in body:
                    controls[stage+'_reset_fee'] = body[field] or 0
                    controls[stage+'_reset_enabled'] = float(body[field] or 0) > 0
    terms = selected_terms(controls)
    source = str(row.get('journey_source') or 'plan_edit').split(MARKER, 1)[0]
    result = dict(row, **{k:v for k,v in body.items() if k in {'reset_fee','funded_reset_fee'}})
    result.update(daily_drawdown=float(terms['daily_drawdown']),
                  payout_frequency=terms['payout_frequency'])
    if any(stage+'_reset_enabled' in body for stage in ('phase1','phase2','funded')):
        result['reset_fee'] = float(terms.get('resets', {}).get('phase1', 0))
        result['funded_reset_fee'] = float(terms.get('resets', {}).get('funded', 0))
    # An explicit empty reset map is represented by zero legacy prices.
    result['journey_source'] = freeze(source, terms) if float(terms['daily_drawdown']) > 0 or controls.get('payout_schedule_enabled', False) or terms.get('resets') else source
    return result


def broker_daily_realised(mt5, row, observed_at):
    """Verify the assigned account's balance ledger before accepting daily P/L.

    An empty/stale history after account switching is not evidence of zero loss.
    Reconcile all post-assignment balance entries, then select this WAT day.
    Transfers affect the balance proof but are excluded from trading P/L.
    """
    observed = parse_time(observed_at)
    day_start = observed.astimezone(WAT).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    assigned = row.get('account_started_at') or row.get('started_at') or row.get('created_at')
    if not assigned:
        raise ValueError('Exact account assignment time is required for daily history')
    start = parse_time(assigned)
    if start > observed:
        raise ValueError('Account assignment is in the future')
    expected = str(row.get('mt5_login'))
    def money(value):
        if isinstance(value,bool):raise ValueError('Invalid broker ledger amount')
        amount=Decimal(str(value))
        if not amount.is_finite():raise ValueError('Invalid broker ledger amount')
        return amount
    capital=money(row.get('start_balance') or row.get('account_size') or 0)
    if capital<=0:raise ValueError('Exact initial capital is required for daily history')
    info = mt5.account_info()
    if info is None or str(info.login) != expected:
        raise ValueError('Daily history account identity mismatch')
    before=money(info.balance)
    deals = mt5.history_deals_get(start, observed)
    if deals is None:
        raise ValueError('Daily broker trading history unavailable')
    ledger=Decimal(0);total=Decimal(0);seen=set()
    for deal in deals:
        ticket=getattr(deal,'ticket',None)
        if ticket is None or ticket in seen:raise ValueError('Broker deal identity missing or duplicated')
        seen.add(ticket)
        kind=getattr(deal,'type',None)
        if isinstance(kind,bool) or not isinstance(kind,int):raise ValueError('Broker deal type missing')
        raw_time=getattr(deal,'time_msc',0)
        raw_time=money(raw_time)/1000 if raw_time else money(getattr(deal,'time',0))
        when=datetime.fromtimestamp(float(raw_time),timezone.utc)
        # MT5 ranges have second precision while assignment/observation timestamps
        # can include fractions. Boundary deals belong to this same lifecycle.
        if when < start.replace(microsecond=0) or when > observed:
            raise ValueError('Broker deal lies outside the verified history window')
        net=sum((money(getattr(deal,field,0) or 0) for field in ('profit','commission','swap','fee')),Decimal(0))
        if kind!=3:ledger+=net # MT5 CREDIT changes account credit, not balance.
        if kind in (0,1) and when>=day_start:total+=net
    info = mt5.account_info()
    if info is None or str(info.login) != expected:
        raise ValueError('Terminal account changed during daily history read')
    after=money(info.balance)
    if abs(after-before)>Decimal('.01'):
        raise ValueError('Broker balance changed during daily history read; retry required')
    if not deals or abs(capital+ledger-after)>Decimal('.01'):
        # Some brokers timestamp initial funding after delivery. Verify the
        # complete balance ledger independently instead of counting funding twice.
        full=mt5.history_deals_get(datetime(1970,1,1,tzinfo=timezone.utc),observed)
        if full is None:raise ValueError('Full broker balance ledger unavailable')
        full_net=Decimal(0);initial_funds=Decimal(0);daily_net=Decimal(0);full_seen=set();first_trade=False
        ordered=sorted(full,key=lambda d:(getattr(d,'time_msc',0) or getattr(d,'time',0)*1000,getattr(d,'ticket',0)))
        for deal in ordered:
            ticket=getattr(deal,'ticket',None);kind=getattr(deal,'type',None)
            if ticket is None or ticket in full_seen:raise ValueError('Full broker deal identity missing or duplicated')
            full_seen.add(ticket)
            if isinstance(kind,bool) or not isinstance(kind,int):raise ValueError('Full broker deal type missing')
            raw_time=getattr(deal,'time_msc',0)
            raw_time=money(raw_time)/1000 if raw_time else money(getattr(deal,'time',0))
            when=datetime.fromtimestamp(float(raw_time),timezone.utc)
            if when>observed:raise ValueError('Full broker ledger includes a future deal')
            net=sum((money(getattr(deal,field,0) or 0) for field in ('profit','commission','swap','fee')),Decimal(0))
            if kind in (0,1):first_trade=True
            if kind!=3:
                full_net+=net
                if not first_trade:initial_funds+=net
            if kind in (0,1) and max(day_start,start)<=when<=observed:daily_net+=net
        checked=mt5.account_info()
        if (checked is None or str(checked.login)!=expected or money(checked.balance)!=after):
            raise ValueError('Broker account changed during full balance-ledger verification')
        if abs(full_net-after)>Decimal('.01') or abs(initial_funds-capital)>Decimal('.01'):
            raise ValueError('Broker history does not reconcile to the assigned capital and current balance')
        total=daily_net
    return total

_ready_cache = (0, False)


def daily_runtime_ready():
    """All four updated shards must have recently published their broker probes."""
    global _ready_cache
    if time.monotonic() - _ready_cache[0] < 15:
        return _ready_cache[1]
    ready = False
    try:
        import requests
        url = os.environ['SUPABASE_URL'].rstrip('/') + '/rest/v1/np_live_account_state_health'
        key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or os.environ['SUPABASE_KEY']
        headers = {'apikey': key, 'Authorization': 'Bearer ' + key}
        private_url = os.environ['SUPABASE_URL'].rstrip('/') + '/rest/v1/package_studio_private_settings'
        verified_key = 'plan_risk_controls_runtime_v1'
        verified = requests.get(private_url, headers=headers,
            params={'select':'value','key':'eq.'+verified_key,'limit':'1'}, timeout=(2,3))
        verified.raise_for_status()
        rows = verified.json()
        confirmed = len(rows)==1 and json.loads(rows[0]['value']).get('release') == RELEASE
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=180)).isoformat()
        response = requests.get(url, headers=headers,
            params={'select': 'source', 'observed_at': 'gte.' + cutoff, 'limit': '1000'}, timeout=(2, 3))
        response.raise_for_status()
        sources = {row.get('source') for row in response.json()}
        from balance_trailing import SOURCE_PREFIX
        sources={str(s).replace(SOURCE_PREFIX,'dd_police_daily_v1_shard_') if str(s).startswith(SOURCE_PREFIX) else s for s in sources}
        expected = {'dd_police_daily_v1_shard_%d_of_4' % n for n in range(1,5)}
        rollback = any(str(source).startswith('dd_police') and source not in expected for source in sources)
        if not confirmed and expected <= sources:
            value = json.dumps({'release':RELEASE,'verified_at':datetime.now(timezone.utc).isoformat()},sort_keys=True)
            saved = requests.post(private_url, headers=dict(headers,Prefer='return=representation'),
                json={'key':verified_key,'value':value,'updated_at':datetime.now(timezone.utc).isoformat(),'updated_by':RELEASE}, timeout=(2,3))
            if saved.status_code == 409:
                # A different worker won the first verification insert.
                saved = requests.get(private_url, headers=headers,
                    params={'select':'value','key':'eq.'+verified_key,'limit':'1'}, timeout=(2,3))
            saved.raise_for_status()
            records = saved.json()
            confirmed = len(records)==1 and json.loads(records[0]['value']).get('release') == RELEASE
        # Remember a proven installation when there are no active accounts. A
        # recent old-worker report explicitly blocks new opt-in sales on rollback.
        ready = confirmed and not rollback
    except Exception:
        pass
    _ready_cache = (time.monotonic(), ready)
    return ready


def daily_breach_proof(account, data, now=None):
    """For an authenticated collector's durable first-breach outbox evidence."""
    from equity_drawdown import spec, initial, evaluate
    observed = parse_time(data.get('timestamp') or data.get('observed_at'))
    now = now or datetime.now(timezone.utc)
    started = parse_time(account.get('started_at') or account.get('created_at'))
    if observed < started or observed > now + timedelta(seconds=5):
        raise ValueError('Breach observation is outside this account lifecycle')
    rules = spec(account)
    if not rules.get('daily_drawdown'):
        raise ValueError('Account did not purchase daily drawdown')
    state = evaluate(rules, initial(rules), data.get('current_equity',data.get('equity')),
        observed.isoformat(), balance=data.get('current_balance',data.get('balance')),
        daily_realised=data.get('daily_realised'), peak_equity=data.get('trailing_highest_equity'), peak_balance=data.get('trailing_highest_balance'))
    if not state['breached']:
        raise ValueError('Broker evidence does not cross the purchased loss floor')
    return state


def parse_time(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('A timezone is required')
    return parsed.astimezone(timezone.utc)


def payout_due(account, purchase, paid_payouts=(), now=None):
    """Legacy accounts retain their existing policy; only frozen opt-ins wait."""
    terms = frozen_terms(account)
    if not terms or terms['payout_frequency'] == 'On Demand':
        return None
    if (str(purchase.get('id')) != str(account.get('purchase_id')) or
            str(purchase.get('trader_id')) != str(account.get('trader_id'))):
        raise ValueError('Payout schedule purchase identity mismatch')
    if frozen_terms(purchase) != terms:
        raise ValueError('Account schedule differs from the purchased terms')
    start = parse_time(account.get('started_at') or account.get('created_at'))
    anchor = start
    for payout in paid_payouts:
        if (str(payout.get('trader_account_id')) != str(account.get('id')) or
                str(payout.get('trader_id')) != str(account.get('trader_id')) or
                str(payout.get('status') or '').lower() != 'paid'):
            continue
        anchor = max(anchor, parse_time(payout.get('paid_at') or payout.get('updated_at') or payout.get('approved_at') or payout.get('created_at')))
    frequency = terms['payout_frequency']
    if frequency == 'Monthly':
        local = anchor.astimezone(WAT)
        year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
        due = local.replace(year=year, month=month, day=min(local.day, calendar.monthrange(year, month)[1])).astimezone(timezone.utc)
    else:
        due = anchor + timedelta(days={'Daily': 1, 'Weekly': 7, 'Biweekly': 14}[frequency])
    return due
