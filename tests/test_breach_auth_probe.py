import ast,json,unittest
from pathlib import Path
from types import SimpleNamespace
from urllib import request as urllib_request
from urllib.error import HTTPError,URLError
from flask import Flask,request,jsonify

class AuthProbeTests(unittest.TestCase):
    def setUp(self):
        self.app=Flask(__name__);self.calls=[];self.payload={'success':True};self.fail=None
        test=self
        class Reply:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return json.dumps(test.payload).encode()
        def open_probe(probe,timeout):
            test.calls.append((probe,timeout))
            if test.fail:raise test.fail
            return Reply()
        self.env=dict(request=request,MAIN_API_URL='https://main.invalid',urlrequest=SimpleNamespace(Request=urllib_request.Request,urlopen=open_probe),json=json,HTTPError=HTTPError,URLError=URLError,bad=lambda message,status:(jsonify(success=False,error=message),status))
        tree=ast.parse((Path(__file__).resolve().parents[1]/'monitoring_api_GLOBAL_FEED_AUTHORITY.py').read_text())
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='require_main_api_admin')
        exec(compile(ast.Module(body=[node],type_ignores=[]),'probe-fixture','exec'),self.env)
    def call(self,light=True):
        with self.app.test_request_context(headers={'Authorization':'Bearer fixture'}):return self.env['require_main_api_admin'](lightweight=light)
    def test_breach_probe_is_lightweight_and_recall_default_unchanged(self):
        self.assertIsNone(self.call()[1]);self.assertEqual(self.calls[-1][0].full_url,'https://main.invalid/admin/auth_check');self.assertEqual(self.calls[-1][1],5)
        self.assertIsNone(self.call(False)[1]);self.assertEqual(self.calls[-1][0].full_url,'https://main.invalid/admin_bootstrap');self.assertEqual(self.calls[-1][1],20)
    def test_invalid_empty_and_unavailable_fail_closed(self):
        self.payload={};self.assertEqual(self.call()[1][1],401)
        self.fail=URLError('fixture');self.assertEqual(self.call()[1][1],503)
        self.fail=HTTPError('https://main.invalid',401,'unauthorized',None,None);self.assertEqual(self.call()[1][1],401)
