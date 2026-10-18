"""Contract-bound DD arithmetic with a durable, per-account equity watermark.

This module never sends broker orders or calls an HTTP endpoint. The worker's
existing verified zero-exposure and breach-relay workflow remains responsible
for policing a result. A missing/corrupt journal is never silently reset for an
old account.
"""
import json,os
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
from package_account_rules import bind_package_account,PackageBindingError
from package_contracts import dec,money,timestamp

class PackageDDUnavailable(RuntimeError):pass

@contextmanager
def account_lock(path):
    stream=open(path,'a+b')
    try:
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            if not stream.read(1):stream.write(b'0');stream.flush()
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        stream.close()

class PackageDDJournal:
    def __init__(self,directory):self.directory=Path(directory)
    def assess(self,row,balance,equity,observed_at):
        try:
            rule=bind_package_account(row,row['package_contract'])
            capital=dec(rule['capital']);balance=dec(balance);equity=dec(equity);now=timestamp(observed_at)
            actual_now=datetime.now(timezone.utc)
            if not -5<=(actual_now-now).total_seconds()<=30:
                raise ValueError('A current broker observation is required')
            self.directory.mkdir(parents=True,exist_ok=True)
            file=self.directory/(rule['account_id']+'.json')
            with account_lock(self.directory/(rule['account_id']+'.lock')):
                if file.exists():
                    if file.stat().st_size>65536:raise ValueError('Invalid journal size')
                    state=json.loads(file.read_text())
                    for key,value in [('account_id',rule['account_id']),('contract_id',rule['package_contract_id']),('rules_hash',rule['package_rules_hash']),('mt5_login',str(row['mt5_login']))]:
                        if state.get(key)!=value:raise ValueError('Journal identity does not match this account contract')
                    high=dec(state['highest_equity'])
                    if high<capital or type(state['breached']) is not bool:raise ValueError('Invalid journal state')
                else:
                    started=timestamp(row.get('package_account_started_at') or row.get('started_at') or row.get('created_at'))
                    if not 0<=(actual_now-started).total_seconds()<=300:
                        raise ValueError('Existing account has no proven DD watermark; recovery is required')
                    high=capital
                    state=dict(account_id=rule['account_id'],contract_id=rule['package_contract_id'],rules_hash=rule['package_rules_hash'],mt5_login=str(row['mt5_login']),highest_equity=money(high),breached=False,first_breach=None)
                high=high if state["breached"] else max(high,equity)
                allowance=capital*dec(rule['dd_limit_percent'])/100
                reference=high if rule['drawdown_type']=='trailing' else capital
                floor=reference-allowance
                new_breach=not state['breached'] and equity<=floor
                breached=state['breached'] or new_breach
                dd=max((reference-equity)/capital*100,dec(0))
                if breached and state['first_breach'] is None:
                    state['first_breach']={'timestamp':now.isoformat(),'balance':money(balance),'equity':money(equity),'breach_equity_level':money(floor),'drawdown_percent':float(dd),'dd_used_percent':float(dd/dec(rule['dd_limit_percent'])*100)}
                changed=not file.exists() or dec(state['highest_equity'])!=high or breached!=state['breached']
                state.update(highest_equity=money(high),breached=breached)
                if changed:
                    temporary=self.directory/(rule['account_id']+'.'+str(uuid4())+'.tmp')
                    try:
                        with open(temporary,'x',encoding='utf-8') as stream:
                            json.dump(state,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
                        os.replace(temporary,file)
                    finally:
                        if temporary.exists():temporary.unlink()
                dd=max((reference-equity)/capital*100,dec(0))
                return dict(capital=float(capital),limit=rule['dd_limit_percent'],drawdown_type=rule['drawdown_type'],highest_equity=float(high),breach_equity_level=float(floor),drawdown_percent=float(dd),used_percent=float(dd/dec(rule['dd_limit_percent'])*100),breached=breached,new_breach=new_breach,first_breach=state['first_breach'])
        except Exception as exc:
            raise PackageDDUnavailable('Exact package DD state is unavailable; no guessed or reset watermark is permitted') from exc
