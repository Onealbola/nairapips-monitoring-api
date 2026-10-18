"""Real worker saturation tests, without importing the production app/workers."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from werkzeug.test import Client
from werkzeug.wrappers import Response

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from monitoring_http import MonitoringAdmission


class AdmissionTests(unittest.TestCase):
    def test_busy_requests_do_not_reach_application_and_probes_keep_working(self):
        calls = []
        def app(env, start):
            calls.append(env['PATH_INFO'])
            start('200 OK', [])
            return [b'ok']
        gate = MonitoringAdmission(app, capacity=1)
        gate.slots.acquire()
        try:
            client = Client(gate, Response)
            result = client.post('/disable_mt5_access', json={'mt5_login':'fixture'})
            self.assertEqual(result.status_code,503)
            self.assertFalse(result.json['operation_started'])
            self.assertNotIn('data',result.json)
            self.assertEqual(result.headers['Retry-After'],'1')
            self.assertEqual(client.get('/livez').status_code,200)
            self.assertEqual(client.get('/').status_code,200)
            self.assertEqual(client.post('/livez').status_code,503)
            self.assertEqual(calls,['/livez','/'])
        finally:gate.slots.release()

    def test_roster_aliases_share_one_slot_and_release_on_close(self):
        def app(env,start):
            start('200 OK',[])
            yield b'first'
            yield b'last'
        gate = MonitoringAdmission(app)
        first=gate({'PATH_INFO':'/monitorable_accounts'},lambda *args:None)
        self.assertEqual(next(first),b'first')
        try:
            for path in gate.ROSTERS:
                result=Client(gate,Response).get(path)
                self.assertEqual(result.status_code,503)
                self.assertFalse(result.json['success'])
        finally:first.close()
        self.assertEqual(Client(gate,Response).get('/traders').status_code,200)

    def test_handler_and_iterator_failures_release_capacity(self):
        for streaming in [False,True]:
            def app(env,start):
                if not streaming:raise RuntimeError('failure')
                def output():
                    yield b'first'
                    raise RuntimeError('failure')
                return output()
            gate=MonitoringAdmission(app,capacity=1)
            with self.assertRaises(RuntimeError):
                list(gate({'PATH_INFO':'/monitorable_accounts'},lambda *args:None))
            self.assertTrue(gate.slots.acquire(blocking=False));gate.slots.release()
            self.assertTrue(gate.roster_slot.acquire(blocking=False));gate.roster_slot.release()

    def test_large_responses_are_chunked_and_socket_wait_is_bounded(self):
        from unittest.mock import Mock
        connection=Mock()
        gate=MonitoringAdmission(lambda env,start:[b'x'*200000])
        blocks=list(gate({'PATH_INFO':'/ordinary','gunicorn.socket':connection},lambda *args:None))
        self.assertEqual(sum(map(len,blocks)),200000)
        self.assertTrue(all(len(block)<=65536 for block in blocks))
        connection.settimeout.assert_called_once_with(10)


class DatabaseTransportTests(unittest.TestCase):
    def test_sdk_preserves_auth_identity_and_bounded_request_timeouts(self):
        import httpx
        from monitoring_http import create_monitoring_database_client
        seen=[]
        def handler(request):
            seen.append(request)
            return httpx.Response(200,json=[{'id':'exact-account'}])
        db=create_monitoring_database_client('https://fixture.supabase.co','fixture',
                                             transport=httpx.MockTransport(handler))
        try:
            result=db.table('trader_accounts').select('id').eq('id','exact-account').limit(1).execute()
            self.assertEqual(result.data,[{'id':'exact-account'}])
            self.assertEqual(len(seen),1)
            req=seen[0]
            self.assertEqual(req.url.path,'/rest/v1/trader_accounts')
            self.assertEqual(req.url.params['id'],'eq.exact-account')
            self.assertEqual(req.headers['apikey'],'fixture')
            self.assertEqual(req.headers['authorization'],'Bearer fixture')
            self.assertEqual(req.extensions['timeout'],{'connect':2,'read':6,'write':6,'pool':2})
        finally:db.postgrest.session.close()

    def test_disconnected_write_is_not_automatically_replayed(self):
        import httpx
        from monitoring_http import create_monitoring_database_client
        seen=[]
        def handler(request):
            seen.append(request)
            raise httpx.RemoteProtocolError('Server disconnected')
        db=create_monitoring_database_client('https://fixture.supabase.co','fixture',
                                             transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(Exception):
                db.table('trader_accounts').update({'monitoring_enabled':True}).eq('id','fixture').execute()
            self.assertEqual(len(seen),1)
            self.assertEqual(seen[0].method,'PATCH')
        finally:db.postgrest.session.close()


class GunicornAvailabilityTests(unittest.TestCase):
    def test_slow_dependencies_cannot_starve_real_health_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture=Path(directory)/'monitor_fixture.py'
            fixture.write_text('''import threading
from monitoring_http import MonitoringAdmission
release=threading.Event()
lock=threading.Lock()
active=0
def handler(env,start):
 global active
 path=env.get('PATH_INFO')
 if path=='/slow':
  with lock:active+=1
  try:release.wait(15)
  finally:
   with lock:active-=1
 if path=='/livez' and env.get('QUERY_STRING')=='release':release.set()
 with lock:body=str(active).encode()
 start('200 OK',[('Content-Type','text/plain')])
 return [body]
app=MonitoringAdmission(handler)
''')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            logfile=Path(directory)/'gunicorn.log'
            with logfile.open('w') as log:
                process=subprocess.Popen([sys.executable,'-m','gunicorn','monitor_fixture:app',
                    '--config',str(ROOT/'gunicorn.conf.py'),'--pythonpath',directory,
                    '--bind',f'127.0.0.1:{port}','--workers','1','--worker-class','sync','--threads','1'],
                    cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
                import requests
                url=f'http://127.0.0.1:{port}'
                pool=concurrent.futures.ThreadPoolExecutor(max_workers=6)
                futures=[]
                try:
                    deadline=time.monotonic()+8
                    while True:
                        if process.poll() is not None:self.fail(logfile.read_text())
                        try:
                            if requests.get(url+'/livez',timeout=.3).status_code==200:break
                        except requests.RequestException:pass
                        if time.monotonic()>deadline:self.fail('Gunicorn did not start: '+logfile.read_text())
                        time.sleep(.05)
                    futures=[pool.submit(requests.get,url+'/slow',timeout=12) for _ in range(6)]
                    deadline=time.monotonic()+5
                    while requests.get(url+'/livez',timeout=1).text!='6':
                        if time.monotonic()>deadline:self.fail('Six slow requests did not enter the fixture')
                        time.sleep(.02)
                    overloaded=requests.post(url+'/monitoring_snapshot',json={},timeout=1)
                    self.assertEqual(overloaded.status_code,503)
                    self.assertFalse(overloaded.json()['operation_started'])
                    for _ in range(10):
                        self.assertEqual(requests.get(url+'/livez',timeout=1).status_code,200)
                    self.assertIsNone(process.poll())
                    requests.get(url+'/livez?release',timeout=1)
                    self.assertTrue(all(f.result(timeout=3).status_code==200 for f in futures))
                    self.assertEqual(requests.get(url+'/ordinary',timeout=1).status_code,200)
                    self.assertIn('ThreadWorker threads=8',logfile.read_text())
                finally:
                    try:requests.get(url+'/livez?release',timeout=.5)
                    except requests.RequestException:pass
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
                    pool.shutdown(wait=True)


if __name__=='__main__':unittest.main()
