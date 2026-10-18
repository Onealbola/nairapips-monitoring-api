import ast,unittest,copy
from pathlib import Path
from uuid import uuid4
from package_contracts import make_contract
from package_account_rules import load_package_rules,PackageBindingError

class MonitoringBindingTests(unittest.TestCase):
 def setUp(self):
  tree=ast.parse((Path(__file__).resolve().parents[1]/'monitoring_api_GLOBAL_FEED_AUTHORITY.py').read_text())
  selection=dict(capital='500000',phases='two',drawdown='10',drawdown_type='trailing',phase1_target='10',phase2_target='8',profit_share='80',profit_cap='none',payout_cycles='3',phase_resets='1',funded_resets='2')
  made=make_contract(dict(selection=selection,catalog_revision=1,total=10000),str(uuid4()));doc=made['document']
  self.contract=dict(id=doc['contract_id'],trader_id=doc['trader_id'],purchase_id=str(uuid4()),rules_hash=made['rules_hash'],document=doc)
  self.account=dict(id=str(uuid4()),trader_id=doc['trader_id'],purchase_id=self.contract['purchase_id'],package_contract_id=doc['contract_id'],package_rules_hash=made['rules_hash'],stage='phase2',mt5_login='123456',account_size=500000,start_balance=500000,target_percent=90,dd_limit_percent=90)
  self.calls=[]
  def read(table,filters,limit):self.calls.append(table);return [copy.deepcopy(self.contract)]
  self.ns=dict(load_package_rules=load_package_rules,PackageContractReader=lambda *a:read,PackageBindingError=PackageBindingError,SUPABASE_URL='https://fixture.invalid',SUPABASE_KEY='fixture')
  nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'num','resolve_account_rules','_fast_rule_values','apply_intelligence'}]
  exec(compile(ast.Module(body=nodes,type_ignores=[]),'monitoring-binding-fixture','exec'),self.ns)
  self.read=read
 def test_both_rule_paths_use_the_exact_contract_over_mutable_values(self):
  for r in [self.ns['resolve_account_rules'](self.account,reader=self.read),self.ns['_fast_rule_values'](self.account,{'phase2_target':99},{'max_drawdown':99},self.contract)]:
   self.assertEqual(r['target_percent'],8);self.assertEqual(r['dd_limit_percent'],10);self.assertEqual(r['drawdown_type'],'trailing');self.assertEqual(r['profit_share_percent'],80)
  self.assertEqual(self.calls,['np_package_contracts'])
 def test_missing_hash_binding_never_falls_back_to_another_plan(self):
  with self.assertRaises(PackageBindingError):self.ns['_fast_rule_values']({**self.account,'package_rules_hash':'0'*64},{'phase2_target':99},{'max_drawdown':99},self.contract)
 def test_package_target_cannot_enter_unverified_legacy_lifecycle_writer(self):
  with self.assertRaises(PackageBindingError):self.ns['apply_intelligence'](self.account,{'balance':999999,'equity':999999})
