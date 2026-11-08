SOURCE = 'monitoring_api_GLOBAL_FEED_AUTHORITY.py'
HANDLER = 'drawdown_version'
import ast
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from flask import Flask, request, jsonify

class CollectorAuthCheckTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        node = next(n for n in ast.parse((Path(__file__).resolve().parents[1] / SOURCE).read_bytes()).body if isinstance(n, ast.FunctionDef) and n.name == HANDLER)
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'isolated-collector-check', 'exec'), dict(app=app, request=request, jsonify=jsonify, os=os))
        self.client = app.test_client()

    def test_authentication_and_no_secret_disclosure(self):
        with patch.dict(os.environ, {'NP_EQUITY_TRAILING_INGEST_KEY': 'dedicated-fixture-secret', 'NP_LIVE_STATE_INGEST_KEY': 'fallback-fixture-secret'}):
            for key in ('', 'wrong', 'fallback-fixture-secret', 'dedicated-fixture-secret'):
                response = self.client.post('/drawdown_version', headers={'X-NP-Trailing-Key': key})
                self.assertEqual(response.status_code, 200 if key == 'dedicated-fixture-secret' else 403)
                self.assertEqual(response.json['collector_auth_verified'], key == 'dedicated-fixture-secret')
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertNotIn('fixture-secret', response.get_data(as_text=True))

    def test_fallback_and_missing_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.client.post('/drawdown_version').status_code, 403)
            os.environ['NP_LIVE_STATE_INGEST_KEY'] = 'fallback-fixture-secret'
            self.assertEqual(self.client.post('/drawdown_version', headers={'X-NP-Trailing-Key': 'fallback-fixture-secret'}).status_code, 200)
            self.assertEqual(self.client.get('/drawdown_version').status_code, 200)

if __name__ == '__main__':
    unittest.main()
