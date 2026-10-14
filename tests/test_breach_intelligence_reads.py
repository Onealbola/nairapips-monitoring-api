import ast,json,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from flask import Flask,request,jsonify
from breach_read_transport import BreachReader,BreachReadUnavailable,complete_breach_view
SOURCE=Path(__file__).resolve().parents[1]/'monitoring_api_GLOBAL_FEED_AUTHORITY.py'

class TransportTests(unittest.TestCase):
    def test_complete_history_includes_later_pages(self):
        responses=[Mock(json=lambda:[{'id':i} for i in range(1000)]),Mock(json=lambda:[{'id':1000}])]
        with patch('breach_read_transport.requests.get',side_effect=responses) as get:
            rows=BreachReader('https://database.invalid','fixture')('monitoring_snapshots',{'trader_account_id':'eq.exact'})
        self.assertEqual(len(rows),1001)
        self.assertEqual(get.call_args_list[1].kwargs['params']['offset'],'1000')
        self.assertEqual(get.call_args_list[1].kwargs['params']['trader_account_id'],'eq.exact')
    def test_failed_page_never_becomes_empty_legacy_evidence(self):
        with patch('breach_read_transport.requests.get',side_effect=TimeoutError):
            with self.assertRaises(BreachReadUnavailable):BreachReader('https://database.invalid','fixture')('monitoring_events',{'trader_account_id':'eq.exact'})
    def test_expired_deadline_starts_no_database_request(self):
        reader=BreachReader('https://database.invalid','fixture',seconds=-1)
        with patch('breach_read_transport.requests.get') as get:
            with self.assertRaises(BreachReadUnavailable):reader('monitoring_events',{})
            get.assert_not_called()

class ViewTests(unittest.TestCase):
    def setUp(self):
        self.app=Flask(__name__);self.fail=False;self.calls=[]
        self.tables={'trader_accounts':[{'id':'exact','trader_id':'owner','stage':'funded','account_size':1000,'dd_limit_percent':10}],
            'monitoring_snapshots':[{'created_at':'2026-10-01','equity':1000,'balance':1000}, {'created_at':'2026-10-02','equity':890,'balance':890,'drawdown_percent':11}, {'created_at':'2026-10-03','equity':1100,'balance':1100}], 'monitoring_events':[]}
        test=self
        def reader(table,filters,limit=None):
            test.calls.append((table,filters))
            if test.fail:raise BreachReadUnavailable('fixture failure')
            return test.tables.get(table,[])
        self.env=dict(app=self.app,request=request,jsonify=jsonify,complete_breach_view=complete_breach_view,BreachReader=lambda *a:reader,SUPABASE_URL='https://fixture.invalid',SUPABASE_KEY='fixture',
            require_main_api_admin=lambda **kw:({'success':True},None) if request.headers.get('Authorization')=='Bearer fixture' else (None,(jsonify(success=False),401)),
            ok=lambda data=None,message='ok':jsonify(success=True,data=data),bad=lambda message,status:(jsonify(success=False,error=message),status),now_iso=lambda:'2026-10-14',bool_true=lambda value:bool(value),_rule_cache_get=lambda key:None,_rule_cache_set=lambda *a: (_ for _ in ()).throw(AssertionError('Display must not change engine cache')))
        nodes=[n for n in ast.parse(SOURCE.read_text()).body if isinstance(n,ast.FunctionDef) and n.name in {'num','resolve_account_rules','breach_black_box'}]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'forensic-fixture','exec'),self.env)
        self.client=self.app.test_client()
    def read(self,owner='owner'):
        return self.client.get('/breach_black_box?account_id=exact&trader_id='+owner,headers={'Authorization':'Bearer fixture'})
    def test_first_crossing_survives_later_recovery(self):
        r=self.read();self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['data']['first_crossing']['equity'],890)
        self.assertEqual(r.json['data']['first_crossing']['timestamp'],'2026-10-02')
        self.assertTrue(r.json['data']['recovery_after_breach']['recovered'])
    def test_wrong_owner_reads_no_history(self):
        self.assertEqual(self.read('other').status_code,409)
        self.assertEqual([x[0] for x in self.calls],['trader_accounts'])
    def test_missing_auth_reads_nothing(self):
        self.assertEqual(self.client.get('/breach_black_box?account_id=exact').status_code,401)
        self.assertEqual(self.calls,[])
    def test_incomplete_evidence_is_unavailable_not_breach_confirmation(self):
        self.fail=True;r=self.read();self.assertEqual(r.status_code,503)
        self.assertFalse(r.json['success']);self.assertNotIn('data',r.json)
