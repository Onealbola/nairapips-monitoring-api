import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

class BrokenQuery:
    def table(self,*args):return self
    def select(self,*args):return self
    def in_(self,*args):return self
    def execute(self):raise TimeoutError('database unavailable')

class LookupTests(unittest.TestCase):
    def setUp(self):
        source=Path(__file__).resolve().parents[1]/'monitoring_api_GLOBAL_FEED_AUTHORITY.py'
        self.tree=ast.parse(source.read_text())
        f=next(n for n in self.tree.body if isinstance(n,ast.FunctionDef) and n.name=='_bulk_rows')
        self.namespace={'supabase':BrokenQuery()}
        exec(compile(ast.Module(body=[f],type_ignores=[]),str(source),'exec'),self.namespace)
    def test_authority_timeout_aborts_instead_of_inventing_missing_account(self):
        with self.assertRaisesRegex(RuntimeError,'Authoritative roster lookup failed'):
            self.namespace['_bulk_rows']('trader_accounts',['account'],fail_on_error=True)
    def test_no_ids_needs_no_query(self):
        self.assertEqual(self.namespace['_bulk_rows']('trader_accounts',[],fail_on_error=True),{})
    def test_every_roster_bulk_read_aborts_on_failure(self):
        f=next(n for n in self.tree.body if isinstance(n,ast.FunctionDef) and n.name=='_monitoring_registry_rows')
        calls=[n for n in ast.walk(f) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='_bulk_rows']
        self.assertEqual(len(calls),4)
        for call in calls:
            self.assertTrue(any(k.arg=='fail_on_error' and isinstance(k.value,ast.Constant) and k.value.value is True for k in call.keywords))
