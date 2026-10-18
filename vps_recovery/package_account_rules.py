"""Exact, immutable account-contract binding shared by API and monitoring.

No network or worker activity on import. A package account never falls back to
current plan values, trader profile values, or a different purchase's rules.
"""
from package_contracts import validate_contract,identity,dec,RULES_VERSION

class PackageBindingError(ValueError):pass

def has_package_binding(account):
    return bool((account or {}).get('package_contract_id') or (account or {}).get('package_rules_hash'))

def bind_package_account(account,contract):
    try:
        aid=identity(account.get('trader_account_id') or account.get('current_account_id') or account['id'])
        cid=identity(account['package_contract_id']);tid=identity(account['trader_id'])
        pid=identity(account.get('purchase_id') or account.get('challenge_purchase_id'))
        if identity(contract['id'])!=cid or identity(contract['trader_id'])!=tid or identity(contract['purchase_id'])!=pid:
            raise ValueError('Account and purchase contract identities differ')
        doc=contract['document'];hash=account['package_rules_hash']
        if hash!=contract['rules_hash'] or contract.get('rules_version',RULES_VERSION)!=RULES_VERSION:
            raise ValueError('Account contract hash/version differs')
        validate_contract(doc,hash)
        if identity(doc['contract_id'])!=cid or identity(doc['trader_id'])!=tid:
            raise ValueError('Contract document identity differs')
        stage=str(account.get('stage') or account.get('phase') or '').lower()
        if stage not in doc['stages'] or not str(account.get('mt5_login') or '').strip():
            raise ValueError('Exact stage and broker account identity are required')
        capital=dec(doc['capital'])
        for key in ['start_balance','account_size']:
            if account.get(key) not in (None,'') and dec(account[key])!=capital:
                raise ValueError('Account capital differs from its purchased capital')
        rule=doc['stage_rules'][stage]
        return {'account_id':aid,'trader_id':tid,'purchase_id':pid,'package_contract_id':cid,
          'package_rules_hash':hash,'package_contract':{key:contract[key] for key in ('id','trader_id','purchase_id','rules_hash','document')},'package_document':doc,
          'package_account_started_at':account.get('started_at') or account.get('created_at'),
          'stage':stage,'capital':float(capital),'dd_limit_percent':float(dec(rule['drawdown_percent'])),
          'drawdown_type':rule['drawdown_type'],'dd_authority_present':True,
          'dd_authority_source':'immutable_package_contract','target_percent':float(dec(rule['target_percent'])) if rule['target_percent'] is not None else 0.0,
          'target_authority_present':True,'target_authority_source':'immutable_package_contract',
          'one_phase':'phase2' not in doc['stages'],'second_life_enabled':False,
          'profit_share_percent':float(dec(doc['profit_share_percent'])),'gross_profit_cap_percent':doc['gross_profit_cap_percent'],
          'payout_cycles':doc['payout_cycles'],'phase_resets':doc['phase_resets'],'funded_resets':doc['funded_resets']}
    except (KeyError,TypeError,ValueError) as exc:
        raise PackageBindingError('Exact package account contract could not be verified') from exc

def load_package_rules(account,reader,stage=None):
    if not has_package_binding(account):return None
    try:
        cid=identity(account.get('package_contract_id'))
        rows=reader('np_package_contracts',{'id':'eq.'+cid},limit=1)
        if not isinstance(rows,list) or len(rows)!=1:raise ValueError('Exact package contract not found')
        result=bind_package_account(account,rows[0])
        if stage is not None and str(stage).lower()!=result['stage']:raise ValueError('Requested stage differs from exact account stage')
        return result
    except Exception as exc:
        if isinstance(exc,PackageBindingError):raise
        raise PackageBindingError('Exact package contract lookup failed; no fallback rule is permitted') from exc

class PackageContractReader:
    """Bounded read-only access to one contract; no schema/financial mutations."""
    def __init__(self,url,key):
        self.url=url.rstrip('/')+'/rest/v1/np_package_contracts'
        self.headers={'apikey':key,'Authorization':'Bearer '+key}
    def __call__(self,table,filters,limit=1):
        if table!='np_package_contracts' or set(filters)!={'id'} or limit!=1:
            raise PackageBindingError('Only an exact contract lookup is permitted')
        import requests
        response=requests.get(self.url,headers=self.headers,params={'select':'id,trader_id,purchase_id,rules_hash,rules_version,document','limit':'1',**filters},timeout=(2,4))
        response.raise_for_status()
        return response.json()

def package_roster_fields(rules):
    if not rules.get('package_contract_id'):return {}
    return {key:rules[key] for key in ['package_contract_id','package_rules_hash','package_contract','purchase_id','drawdown_type','package_account_started_at']}
