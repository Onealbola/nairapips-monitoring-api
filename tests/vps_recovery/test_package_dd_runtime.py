import sys,unittest,copy,json,tempfile
from pathlib import Path
from datetime import datetime,timezone,timedelta
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'vps_recovery'))
from package_contracts import make_contract
from package_dd_runtime import PackageDDJournal,PackageDDUnavailable

class PackageDDTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.journal=PackageDDJournal(self.temp.name)
  selection=dict(capital='500000',phases='two',drawdown='10',drawdown_type='trailing',phase1_target='10',phase2_target='8',profit_share='80',profit_cap='none',payout_cycles='3',phase_resets='1',funded_resets='2')
  made=make_contract(dict(selection=selection,catalog_revision=1,total=10000),str(uuid4()));d=made['document']
  c=dict(id=d['contract_id'],trader_id=d['trader_id'],purchase_id=str(uuid4()),rules_hash=made['rules_hash'],document=d)
  self.row=dict(trader_account_id=str(uuid4()),trader_id=d['trader_id'],purchase_id=c['purchase_id'],package_contract_id=c['id'],package_rules_hash=c['rules_hash'],package_contract=c,stage='phase1',mt5_login='123456',account_size=500000,package_account_started_at=datetime.now(timezone.utc).isoformat())
 def read(self,eq,balance=None,row=None):return self.journal.assess(row or self.row,eq if balance is None else balance,eq,datetime.now(timezone.utc).isoformat())
 def test_trailing_watermark_survives_restart_and_never_decreases(self):
  self.assertEqual(self.read(600000)['breach_equity_level'],550000)
  self.journal=PackageDDJournal(self.temp.name)
  r=self.read(560000);self.assertEqual(r['highest_equity'],600000);self.assertFalse(r['breached'])
  self.assertTrue(self.read(550000)['breached'])
 def test_original_breach_survives_recovery_and_journal_restart(self):
  self.read(600000);r=self.read(540000);self.assertTrue(r['new_breach']);first=copy.deepcopy(r['first_breach'])
  self.journal=PackageDDJournal(self.temp.name);r=self.read(700000)
  self.assertTrue(r['breached']);self.assertFalse(r['new_breach']);self.assertEqual(r['first_breach'],first);self.assertEqual(r['breach_equity_level'],550000)
 def test_static_package_uses_equity_not_legacy_lower_of_balance_and_equity(self):
  from package_contracts import digest
  row=copy.deepcopy(self.row);c=row['package_contract'];c['document']['stage_rules']['phase1']['drawdown_type']='static';c['document']['stage_rules']['phase1']['trailing_formula']=None;c['rules_hash']=digest(c['document']);row['package_rules_hash']=c['rules_hash']
  self.assertFalse(self.read(500000,balance=400000,row=row)['breached'])
  self.assertTrue(self.read(450000,balance=500000,row=row)['breached'])
 def test_zero_and_negative_equity_breach_without_fabricating_capital(self):
  self.assertTrue(self.read(0)['breached']);self.assertTrue(self.read(-100)['breached'])
 def test_old_missing_or_corrupt_state_requires_recovery(self):
  old={**self.row,'package_account_started_at':(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()}
  with self.assertRaises(PackageDDUnavailable):self.read(600000,row=old)
  self.read(600000);path=Path(self.temp.name)/(self.row['trader_account_id']+'.json');path.write_text('broken')
  with self.assertRaises(PackageDDUnavailable):self.read(560000)
  self.assertEqual(path.read_text(),'broken')
 def test_wrong_hash_or_identity_never_rewrites_existing_journal(self):
  self.read(600000)
  with self.assertRaises(PackageDDUnavailable):self.read(550000,row={**self.row,'package_rules_hash':'0'*64})
  self.assertFalse(self.read(560000)['breached'])
 def test_freshness_and_nonfinite_broker_data_are_rejected(self):
  for equity in ['NaN','Infinity',None]:
   with self.assertRaises(PackageDDUnavailable):self.read(equity)
  with self.assertRaises(PackageDDUnavailable):self.journal.assess(self.row,500000,500000,(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
