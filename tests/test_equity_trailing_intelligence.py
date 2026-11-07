"""Exercise the monitoring writer's real branch without importing live workers."""
import ast
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch
from flask import Flask, request

class CapturedWrite(Exception): pass

class IntelligenceTests(TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc).isoformat();self.app=Flask(__name__)
        self.account=dict(id='account',trader_id='trader',purchase_id='purchase',mt5_login='123',stage='funded',
            start_balance=1000000,current_balance=1200000,current_equity=1150000,highest_equity=1200000,lowest_equity=950000,account_status='assigned_active')
        self.purchase=dict(id='purchase',trader_id='trader',drawdown_type='TRAILING')
        self.state=dict(highest_equity='1200000',drawdown_floor='1100000',drawdown_percent='5',
            used_percent='50',worst_used_percent='50',breached=False,last_observed_at=self.now)
        self.writes=[]
        def capture(aid,update):
            self.writes.append(update)
            raise CapturedWrite()
        self.env=dict(request=request,SUPABASE_URL='https://fixture.invalid',SUPABASE_KEY='fixture',
            PackageBindingError=ValueError,MAX_DD_PERCENT=20,is_active_monitoring_account=lambda _:True,
            resolve_account_rules=lambda *a:dict(drawdown_type='TRAILING',dd_limit_percent=10,dd_authority_present=True,target_percent=0,target_authority_present=True),
            fetch_purchase_by_id=lambda _:self.purchase,now_iso=lambda:self.now,verified_account_update=capture)
        tree=ast.parse((Path(__file__).resolve().parents[1]/'monitoring_api_GLOBAL_FEED_AUTHORITY.py').read_text())
        names={'num','risk_zone','static_dd_precise','dd_used_from_static','apply_intelligence'}
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'trailing-intelligence','exec'),self.env)
    def run_writer(self):
        with self.app.test_request_context('/monitoring_snapshot',method='POST'):
            return self.env['apply_intelligence'](self.account,dict(balance=1200000,equity=1150000,timestamp=self.now))
    def test_earlier_low_does_not_breach_against_later_trailing_peak(self):
        store=Mock();store.assess.return_value=self.state
        with patch('equity_drawdown.verify_ingest'),patch('equity_drawdown.DrawdownStateStore',return_value=store):
            with self.assertRaises(CapturedWrite):self.run_writer()
        row=self.writes[0]
        self.assertEqual(row['breach_equity_level'],1100000)
        self.assertEqual(row['dd_used_percent'],50)
        self.assertNotIn('breached_at',row)
        self.assertEqual(row['highest_equity'],1200000)
    def test_untrusted_observation_cannot_advance_the_journal(self):
        store=Mock()
        with patch.dict('os.environ',{},clear=True),patch('equity_drawdown.DrawdownStateStore',return_value=store):
            with self.assertRaises(ValueError):self.run_writer()
        store.assess.assert_not_called();self.assertEqual(self.writes,[])
    def test_recorded_trailing_breach_survives_later_equity_recovery(self):
        self.state['breached']=True;store=Mock();store.assess.return_value=self.state
        with patch('equity_drawdown.verify_ingest'),patch('equity_drawdown.DrawdownStateStore',return_value=store):
            with self.assertRaises(CapturedWrite):self.run_writer()
        self.assertIn('breached_at',self.writes[0])
        self.assertEqual(self.writes[0]['breach_equity_level'],1100000)
