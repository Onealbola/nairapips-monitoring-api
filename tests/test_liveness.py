import ast
import unittest
from pathlib import Path
from flask import Flask

class LivenessTests(unittest.TestCase):
    def test_probe_needs_no_database(self):
        source=Path(__file__).resolve().parents[1]/'monitoring_api_GLOBAL_FEED_AUTHORITY.py'
        tree=ast.parse(source.read_text())
        chosen=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('ok','process_liveness')]
        app=Flask(__name__)
        from flask import jsonify
        namespace={'app':app,'jsonify':jsonify,'NAIRAPIPS_MONITORING_RELEASE':'test'}
        exec(compile(ast.Module(body=chosen,type_ignores=[]),str(source),'exec'),namespace)
        result=app.test_client().get('/livez')
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json['data']['status'],'alive')
        self.assertTrue(result.json['success'])
