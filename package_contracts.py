"""Canonical immutable package rules shared by API, monitoring and DD consumers.

No database connections, workers, broker calls, email or account assignments run
on import. A consumer must load the exact contract and persist state per account.
"""
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID, uuid4

RULES_VERSION = 'NP_PACKAGE_RULES_1'
CENT = Decimal('.01')


def dec(value):
    if value is None or isinstance(value, bool):
        raise ValueError('A finite numeric value is required')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('A finite numeric value is required') from exc
    if not result.is_finite():
        raise ValueError('A finite numeric value is required')
    return result


def money(value):
    return format(dec(value).quantize(CENT, rounding=ROUND_HALF_UP), '.2f')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def identity(value):
    return str(UUID(str(value)))


def timestamp(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Observation timestamps need a timezone')
    return result.astimezone(timezone.utc)


def make_contract(priced, trader_id, contract_id=None):
    """Input is a validated server quote; never accept a browser-supplied price."""
    s = priced['selection']
    capital = dec(s['capital'])
    if capital <= 0:
        raise ValueError('Capital must be positive')
    stages = ['phase1', 'phase2', 'funded'] if s['phases'] == 'two' else ['phase1', 'funded']
    rules = {}
    for stage in stages:
        target = None if stage == 'funded' else dec(s['phase1_target' if stage == 'phase1' else 'phase2_target'])
        rules[stage] = dict(target_percent=str(target) if target is not None else None,
                            target_basis='equity', require_flat_before_pass=True,
                            drawdown_percent=str(dec(s['drawdown'])), drawdown_type=s['drawdown_type'],
                            trailing_formula='highest_observed_equity_minus_initial_capital_allowance' if s['drawdown_type']=='trailing' else None)
    document = dict(contract_id=identity(contract_id or uuid4()), trader_id=identity(trader_id),
                    rules_version=RULES_VERSION, catalog_revision=priced['catalog_revision'],
                    currency='NGN', capital=money(capital), purchase_price=money(priced['total']),
                    stages=stages, stage_rules=rules, profit_share_percent=str(dec(s['profit_share'])),
                    gross_profit_cap_percent=None if s['profit_cap']=='none' else str(dec(s['profit_cap'])),
                    payout_cycles=None if s['payout_cycles']=='unlimited' else int(s['payout_cycles']),
                    phase_resets=int(s['phase_resets']), funded_resets=int(s['funded_resets']))
    validate_contract(document)
    return {'document': document, 'rules_hash': digest(document)}


def validate_contract(doc, expected_hash=None):
    required={'contract_id','trader_id','rules_version','catalog_revision','currency','capital','purchase_price','stages','stage_rules','profit_share_percent','gross_profit_cap_percent','payout_cycles','phase_resets','funded_resets'}
    if not isinstance(doc,dict) or set(doc)!=required:
        raise ValueError('Incomplete package contract')
    identity(doc['contract_id']);identity(doc['trader_id'])
    if doc['rules_version']!=RULES_VERSION or doc['currency']!='NGN':
        raise ValueError('Unsupported package rules or currency')
    if type(doc['catalog_revision']) is not int or doc['catalog_revision']<0:
        raise ValueError('Invalid catalog revision')
    if dec(doc['capital'])<=0 or dec(doc['purchase_price'])<=0:
        raise ValueError('Invalid package capital or price')
    if doc['stages'] not in (['phase1','funded'],['phase1','phase2','funded']):
        raise ValueError('Invalid purchased stage sequence')
    if set(doc['stage_rules'])!=set(doc['stages']):
        raise ValueError('Missing stage rules')
    for stage,rule in doc['stage_rules'].items():
        if set(rule)!={'target_percent','target_basis','require_flat_before_pass','drawdown_percent','drawdown_type','trailing_formula'}:
            raise ValueError('Incomplete stage rules')
        if not 0<dec(rule['drawdown_percent'])<100 or rule['drawdown_type'] not in ('static','trailing'):
            raise ValueError('Invalid drawdown rules')
        if rule['target_basis']!='equity' or rule['require_flat_before_pass'] is not True:
            raise ValueError('Unsupported pass qualification')
        if stage=='funded':
            if rule['target_percent'] is not None:raise ValueError('Funded stages do not have evaluation targets')
        elif not 0<dec(rule['target_percent'])<=100:
            raise ValueError('Invalid stage target')
        expected='highest_observed_equity_minus_initial_capital_allowance' if rule['drawdown_type']=='trailing' else None
        if rule['trailing_formula']!=expected:raise ValueError('Unsupported trailing formula')
    if not 0<dec(doc['profit_share_percent'])<=100:raise ValueError('Invalid profit share')
    if doc['gross_profit_cap_percent'] is not None and not 0<dec(doc['gross_profit_cap_percent'])<=1000:raise ValueError('Invalid gross profit cap')
    for key in ('phase_resets','funded_resets'):
        if type(doc[key]) is not int or not 0<=doc[key]<=100:raise ValueError('Invalid reset allowance')
    if doc['payout_cycles'] is not None and (type(doc['payout_cycles']) is not int or not 1<=doc['payout_cycles']<=1000):raise ValueError('Invalid paid cycle allowance')
    if expected_hash is not None and digest(doc)!=expected_hash:
        raise ValueError('Package contract hash mismatch')
    return doc


def initial_state(contract, account_id, stage):
    validate_contract(contract)
    if stage not in contract['stages']:raise ValueError('This stage was not purchased')
    return dict(account_id=identity(account_id),contract_id=contract['contract_id'],trader_id=contract['trader_id'],
                rules_hash=digest(contract),stage=stage,highest_equity=contract['capital'],
                lowest_equity=None,last_observed_at=None,sequence=-1,decision='active')


def evaluate(contract, state, observation):
    """Pure evaluation of one authenticated broker observation for one account.

    Observation identity and contract hash must be supplied by the collector
    binding, not inferred from MT5 login or the trader's latest account pointer.
    Equal-time and lower-sequence observations cannot overwrite durable state.
    """
    validate_contract(contract, state['rules_hash'])
    for key in ('account_id','contract_id','trader_id','rules_hash'):
        if observation.get(key)!=state[key]:raise ValueError('Observation identity does not match exact account contract')
    if state['contract_id']!=contract['contract_id'] or state['trader_id']!=contract['trader_id']:
        raise ValueError('State belongs to another contract')
    seq=observation.get('sequence')
    if type(seq) is not int or seq<0:raise ValueError('A non-negative broker sequence is required')
    observed=timestamp(observation['observed_at'])
    if seq<=state['sequence'] or (state['last_observed_at'] and observed<=timestamp(state['last_observed_at'])):
        return deepcopy(state)
    if state['decision'] in ('passed','breached','paid_closed'):return deepcopy(state)
    balance,equity=dec(observation['balance']),dec(observation['equity'])
    opened=observation.get('open_positions')
    if type(opened) is not int or opened<0:raise ValueError('Invalid broker balance, equity or open position count')
    result=deepcopy(state);capital=dec(contract['capital']);rule=contract['stage_rules'][state['stage']]
    high=max(dec(state['highest_equity']),equity)
    low=min(dec(state['lowest_equity']),equity) if state['lowest_equity'] is not None else equity
    allowance=capital*dec(rule['drawdown_percent'])/100
    floor=(high if rule['drawdown_type']=='trailing' else capital)-allowance
    decision='profit_cap_reached' if state['decision']=='profit_cap_reached' else 'active'
    # A simultaneous target and DD observation must never manufacture a pass.
    if equity<=floor:decision='breached'
    elif state['stage']!='funded' and opened==0 and equity>=capital*(1+dec(rule['target_percent'])/100):decision='passed'
    elif state['stage']=='funded' and contract['gross_profit_cap_percent'] is not None and equity>=capital*(1+dec(contract['gross_profit_cap_percent'])/100):decision='profit_cap_reached'
    result.update(highest_equity=money(high),lowest_equity=money(low),balance=money(balance),equity=money(equity),
                  drawdown_floor=money(floor),sequence=seq,last_observed_at=observed.isoformat(),decision=decision)
    return result


def payout_amount(contract, eligible_closed_profit):
    """Caller must prove exact-account closed net profit from trusted broker data."""
    validate_contract(contract)
    gross=max(Decimal(0),dec(eligible_closed_profit))
    if contract['gross_profit_cap_percent'] is not None:
        gross=min(gross,dec(contract['capital'])*dec(contract['gross_profit_cap_percent'])/100)
    return money(gross*dec(contract['profit_share_percent'])/100)
