"""Prospective percentage trailing from verified closed broker balances."""
import json,time,os
from datetime import datetime,timezone,timedelta
from decimal import Decimal

FORMULA='highest_closed_balance_percent'
RELEASE='NP_BALANCE_PERCENT_TRAILING_V1_20261121'
SOURCE_PREFIX='dd_police_balance_pct_v1_shard_'
_ready_cache=(0,False)

def uses_balance_percent(row):
    from plan_risk_controls import frozen_terms
    return (frozen_terms(row) or {}).get('trailing_formula')==FORMULA

def future_plan(plan,require_ready=False):
    """Copy only: never rewrite a saved plan, purchase or issued account."""
    from equity_drawdown import mode
    if mode(plan.get('drawdown_type'))!='TRAILING':return dict(plan)
    if require_ready and not runtime_ready():raise ValueError('Balance-based percentage trailing is awaiting the verified monitoring update')
    from plan_risk_controls import frozen_terms,freeze,MARKER
    row=dict(plan);terms=dict(frozen_terms(plan) or dict(daily_drawdown='0',payout_frequency=plan.get('payout_frequency') or 'On Demand'))
    terms['trailing_formula']=FORMULA
    row['journey_source']=freeze(str(plan.get('journey_source') or 'future_trailing_snapshot').split(MARKER,1)[0],terms)
    row['trailing_formula']=FORMULA;row['trailing_basis']='highest_closed_balance'
    return row

def runtime_ready():
    global _ready_cache
    if time.monotonic()-_ready_cache[0]<10:return _ready_cache[1]
    ready=False
    try:
        import requests
        url=os.environ['SUPABASE_URL'].rstrip('/')+'/rest/v1/np_live_account_state_health'
        key=os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or os.environ['SUPABASE_KEY']
        cutoff=(datetime.now(timezone.utc)-timedelta(seconds=180)).isoformat()
        r=requests.get(url,headers={'apikey':key,'Authorization':'Bearer '+key},params={'select':'source','observed_at':'gte.'+cutoff,'limit':'1000'},timeout=(2,3));r.raise_for_status()
        sources={row.get('source') for row in r.json()}
        private_url=os.environ['SUPABASE_URL'].rstrip('/')+'/rest/v1/package_studio_private_settings'
        headers={'apikey':key,'Authorization':'Bearer '+key}
        params={'select':'value','key':'eq.balance_percent_trailing_runtime_v1','limit':'1'}
        verified=requests.get(private_url,headers=headers,params=params,timeout=(2,3));verified.raise_for_status()
        records=verified.json()
        confirmed=len(records)==1 and json.loads(records[0]['value']).get('release')==RELEASE
        expected={SOURCE_PREFIX+str(n)+'_of_4' for n in range(1,5)}
        # Fresh reports from old workers prevent a mixed-version release.
        old=any(str(s).startswith('dd_police') and not str(s).startswith(SOURCE_PREFIX) for s in sources)
        if not confirmed and expected<=sources and not old:
            value=json.dumps({'release':RELEASE,'verified_at':datetime.now(timezone.utc).isoformat()},sort_keys=True)
            saved=requests.post(private_url,headers=dict(headers,Prefer='return=representation'),json={'key':'balance_percent_trailing_runtime_v1','value':value,'updated_at':datetime.now(timezone.utc).isoformat(),'updated_by':RELEASE},timeout=(2,3))
            if saved.status_code==409:saved=requests.get(private_url,headers=headers,params=params,timeout=(2,3))
            saved.raise_for_status();records=saved.json()
            confirmed=len(records)==1 and json.loads(records[0]['value']).get('release')==RELEASE
        ready=confirmed and not old
    except Exception:pass
    _ready_cache=(time.monotonic(),ready)
    return ready

def broker_balance_peak(mt5,row,observed_at):
    """Replay exact-account deals; exclude deposits from growth, include withdrawals.

    The complete ledger must reconcile to the current broker balance. A reused
    account, missing history, duplicate deal or switched session cannot seed a peak.
    """
    from equity_drawdown import number,timestamp
    observed=timestamp(observed_at);started=timestamp(row.get('account_started_at') or row.get('started_at') or row.get('created_at'))
    expected=str(row['mt5_login']);capital=number(row.get('start_balance') or row.get('account_size'))
    info=mt5.account_info()
    if info is None or str(info.login)!=expected or started>observed or capital<=0:raise ValueError('Exact balance-history identity unavailable')
    server=str(row.get('mt5_server') or '').strip().lower()
    if server and str(getattr(info,'server','')).strip().lower()!=server:raise ValueError('Broker server identity mismatch')
    before=number(info.balance)
    deals=mt5.history_deals_get(datetime(1970,1,1,tzinfo=timezone.utc),observed)
    if not deals:raise ValueError('Complete broker balance history unavailable')
    ordered=sorted(deals,key=lambda d:(getattr(d,'time_msc',0) or getattr(d,'time',0)*1000,getattr(d,'ticket',0)))
    seen=set();running=Decimal(0);peak=capital;cash_in=Decimal(0);traded=False
    for deal in ordered:
        ticket=getattr(deal,'ticket',None);kind=getattr(deal,'type',None)
        if ticket is None or ticket in seen or isinstance(kind,bool) or not isinstance(kind,int):raise ValueError('Invalid or duplicate broker deal')
        seen.add(ticket)
        seconds=number(getattr(deal,'time_msc',0))/1000 if getattr(deal,'time_msc',0) else number(getattr(deal,'time',0))
        when=datetime.fromtimestamp(float(seconds),timezone.utc)
        if when>observed:raise ValueError('Future broker deal')
        net=sum((number(getattr(deal,f,0) or 0) for f in ('profit','commission','swap','fee')),Decimal(0))
        trading=kind in (0,1)
        if trading and not traded:
            if when<started.replace(microsecond=0) or abs(running-cash_in-capital)>Decimal('.01'):raise ValueError('Account was used before this assignment or funding does not match')
            traded=True
        if kind!=3:running+=net # Credit does not change balance.
        # Positive non-trading cash after assignment is not realised trading profit.
        if not trading and kind!=3 and net>0:
            cash_in=cash_in+net if traded else max(Decimal(0),running-capital)
        if trading or (traded and kind!=3):peak=max(peak,running-cash_in)
    if not traded and abs(running-cash_in-capital)>Decimal('.01'):raise ValueError('Initial funding does not match purchased capital')
    checked=mt5.account_info()
    if checked is None or str(checked.login)!=expected or number(checked.balance)!=before or abs(running-before)>Decimal('.01'):raise ValueError('Broker balance history does not reconcile; retry')
    if server and str(getattr(checked,'server','')).strip().lower()!=server:raise ValueError('Broker server changed during history replay')
    return peak
