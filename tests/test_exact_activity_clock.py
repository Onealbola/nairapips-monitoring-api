import ast
import unittest
from datetime import datetime, timezone
from pathlib import Path

class ActivityTests(unittest.TestCase):
    def setUp(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'monitoring_api_GLOBAL_FEED_AUTHORITY.py').read_text())
        outer=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_monitoring_registry_rows')
        funcs=[n for n in outer.body if isinstance(n,ast.FunctionDef) and n.name in ('_parse_dt','_trade_rows_for_account','_rolling_trade_activity')]
        self.env={'datetime':datetime,'timezone':timezone,'clean_login':lambda x:str(x or '').strip(),'trade_rows_by_account':{},'trade_rows_by_login':{}}
        exec(compile(ast.Module(body=funcs,type_ignores=[]),'activity','exec'),self.env)
        self.account={'id':'current','mt5_login':'123'}
    def activity(self):return self.env['_rolling_trade_activity'](self.account)
    def test_closing_restarts_clock_for_swing_traders(self):
        self.env['trade_rows_by_account']['current']=[{'opened_at':'2026-10-01T10:00:00Z','closed_at':'2026-10-10T10:00:00Z','status':'closed','mt5_login':'123'}]
        result=self.activity()
        self.assertEqual(result['last_activity_at'],datetime(2026,10,10,10,tzinfo=timezone.utc))
        self.assertEqual(result['last_activity_source'],'closed_at')
    def test_reused_login_history_does_not_enter_current_account(self):
        self.env['trade_rows_by_login']['123']=[{'trader_account_id':'parent','opened_at':'2026-10-10T10:00:00Z','status':'open'}]
        self.assertFalse(self.activity()['has_ever_traded'])
    def test_open_position_remains_protected(self):
        self.env['trade_rows_by_account']['current']=[{'opened_at':'2026-10-01T10:00:00Z','status':'open'}]
        self.assertTrue(self.activity()['has_open_trade'])
    def test_sync_time_does_not_restart_clock(self):
        self.env['trade_rows_by_account']['current']=[{'synced_at':'2026-10-10T11:00:00Z','status':'closed'}]
        self.assertTrue(self.activity()['has_ever_traded'])
        self.assertIsNone(self.activity()['last_activity_at'])
