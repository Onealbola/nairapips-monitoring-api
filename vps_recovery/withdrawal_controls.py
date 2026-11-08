"""Immutable, optional per-plan withdrawal rules; legacy accounts keep 15% cap."""
import base64,json,os
from decimal import Decimal,InvalidOperation
MARKER='|np_withdrawal_v1:'
RISK_MARKER='|np_risk_controls_v1:'
RELEASE='NP_WITHDRAWAL_RULES_V1_20261108'
FIELDS={'minimum_withdrawal_enabled','minimum_withdrawal','minimum_profit_enabled','minimum_profit_percent','profit_cap_enabled','profit_cap'}
def number(value,maximum=100000000):
    if isinstance(value,bool):raise ValueError('Enter a valid withdrawal rule value')
    try:x=Decimal(str(value))
    except (InvalidOperation,ValueError):raise ValueError('Enter a valid withdrawal rule value')
    if not x.is_finite() or not 0<=x<=maximum or x!=x.quantize(Decimal('.01')):raise ValueError('Withdrawal values must be finite, non-negative and have at most two decimals')
    return x

def selected(row):
    if not FIELDS.intersection(row):return None
    flags={k:row.get(k,True if k=='profit_cap_enabled' else False) for k in ('minimum_withdrawal_enabled','minimum_profit_enabled','profit_cap_enabled')}
    if any(type(v) is not bool for v in flags.values()):raise ValueError('Use the withdrawal rule checkboxes')
    values={'minimum_withdrawal':number(row.get('minimum_withdrawal',0)),
            'minimum_profit_percent':number(row.get('minimum_profit_percent',0),100),
            'profit_cap':number(row.get('profit_cap',15),100)}
    for flag,key in (('minimum_withdrawal_enabled','minimum_withdrawal'),('minimum_profit_enabled','minimum_profit_percent'),('profit_cap_enabled','profit_cap')):
        if flags[flag] and values[key]<=0:raise ValueError('Enter a positive value or turn its withdrawal checkbox off')
    if flags['profit_cap_enabled'] and flags['minimum_profit_enabled'] and values['minimum_profit_percent']>values['profit_cap']:raise ValueError('Minimum profit target cannot exceed the profit cap')
    if flags['minimum_withdrawal_enabled'] and flags['profit_cap_enabled'] and row.get('account_size') and row.get('payout_split'):
        maximum=number(row['account_size'],1000000000)*values['profit_cap']/100*number(row['payout_split'],100)/100
        if values['minimum_withdrawal']>maximum:raise ValueError('Minimum withdrawal exceeds the maximum trader payout under this profit cap and split')
    return {**flags,**{k:str(v.normalize()) for k,v in values.items()}}

def read(row):
    source=str((row or {}).get('journey_source') or '')
    if MARKER not in source:return None
    try:
        raw=source.split(MARKER,1)[1].split(RISK_MARKER,1)[0]
        value=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
        if set(value)!=FIELDS:raise ValueError()
        return selected(value)
    except Exception as e:raise ValueError('Purchased withdrawal rules could not be verified') from e

def strip(source):
    source=str(source or '')
    if MARKER not in source:return source
    before,after=source.split(MARKER,1)
    tail=RISK_MARKER+after.split(RISK_MARKER,1)[1] if RISK_MARKER in after else ''
    return before+tail

def freeze(source,terms):
    if terms is None:return source
    terms=selected(terms)
    source=strip(source)
    parts=source.split(RISK_MARKER,1)
    encoded=base64.urlsafe_b64encode(json.dumps(terms,sort_keys=True,separators=(',',':')).encode()).decode().rstrip('=')
    return parts[0]+MARKER+encoded+(RISK_MARKER+parts[1] if len(parts)==2 else '')

def inherit(source,row):return freeze(source,read(row))
def cap_percent(row):
    terms=read(row)
    return (Decimal(terms['profit_cap']) if terms['profit_cap_enabled'] else None) if terms else Decimal(15)
def ready():return os.environ.get('NP_PLAN_WITHDRAWAL_VERIFIED')==RELEASE

def edit(row,body):
    if not FIELDS.intersection(body):return row
    current=read(row) or dict(minimum_withdrawal_enabled=False,minimum_withdrawal=0,minimum_profit_enabled=False,minimum_profit_percent=0,profit_cap_enabled=True,profit_cap=15)
    current.update({k:v for k,v in body.items() if k in FIELDS})
    return dict(row,journey_source=freeze(row.get('journey_source') or 'plan_edit',selected(dict(row,**current))))
def view(row):
    policy=read(row) or dict(minimum_withdrawal_enabled=False,minimum_withdrawal=0,minimum_profit_enabled=False,minimum_profit_percent=0,profit_cap_enabled=True,profit_cap=15)
    return dict(row,**{k:(float(v) if k in {'minimum_withdrawal','minimum_profit_percent','profit_cap'} else v) for k,v in policy.items()})
def needs_broker_update(row):return cap_percent(row)!=Decimal(15)
def validate_request(row,amount,quote=None,require_verified=False):
    terms=read(row)
    if not terms:return
    amount=number(amount)
    if terms['minimum_withdrawal_enabled'] and amount<Decimal(terms['minimum_withdrawal']):
        raise ValueError('Minimum withdrawal for this plan is ₦'+format(Decimal(terms['minimum_withdrawal']),',.2f'))
    if require_verified and not (quote or {}).get('verified'):raise ValueError('A fresh verified account update is required before this payout can be approved or paid')
    if quote and quote.get('verified'):
        if terms['minimum_profit_enabled']:
            target=Decimal(str(quote['start_balance']))*Decimal(terms['minimum_profit_percent'])/100
            if Decimal(str(quote['verified_profit']))<target:raise ValueError('This plan requires '+terms['minimum_profit_percent']+'% profit before withdrawal')
        if amount>Decimal(str(quote.get('available_payout') or 0)):raise ValueError('Requested withdrawal exceeds the verified plan allowance')
