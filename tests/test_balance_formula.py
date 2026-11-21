"""Shared collector formula compatibility, independent of production imports."""
import unittest
from datetime import datetime,timezone
from uuid import uuid4
from balance_trailing import future_plan,FORMULA
from equity_drawdown import spec,initial,evaluate

class BalanceFormulaTests(unittest.TestCase):
 def test_signed_closed_balance_peak_controls_future_formula(self):
  row=dict(id=str(uuid4()),trader_id=str(uuid4()),mt5_login='123',stage='phase1',start_balance=1000000,dd_limit_percent=10,drawdown_type='TRAILING')
  rules=spec(future_plan(row))
  self.assertEqual(rules['trailing_formula'],FORMULA)
  for peak,floor in [(1000000,900000),(1100000,990000),(1200000,1080000),(1500000,1350000)]:
   s=evaluate(rules,initial(rules),2000000,datetime.now(timezone.utc).isoformat(),balance=peak,peak_balance=peak)
   self.assertEqual(float(s['drawdown_floor']),floor)
  old=spec(row)
  self.assertNotIn('trailing_formula',old)
  self.assertEqual(float(evaluate(old,initial(old),1200000,datetime.now(timezone.utc).isoformat())['drawdown_floor']),1100000)

if __name__=='__main__':unittest.main()
