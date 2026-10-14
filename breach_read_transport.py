"""Bounded read-only transport for the forensic view; no engine writes."""
import time
from functools import wraps
import requests
from flask import jsonify

class BreachReadUnavailable(RuntimeError):
    pass

class BreachReader:
    def __init__(self,base,key,seconds=15):
        self.base=base.rstrip('/')
        self.headers={'apikey':key,'Authorization':'Bearer '+key}
        self.deadline=time.monotonic()+seconds
    def __call__(self,table,filters,limit=None):
        if table not in {'trader_accounts','challenge_purchases','challenge_plans','monitoring_snapshots','monitoring_events'}:
            raise BreachReadUnavailable('Unsupported evidence table')
        rows=[]
        while True:
            remaining=self.deadline-time.monotonic()
            if remaining<=0:raise BreachReadUnavailable('Evidence deadline')
            count=min(1000,limit) if limit else 1000
            params={'select':'*','order':'created_at.asc,id.asc','limit':str(count),'offset':str(len(rows)),**filters}
            try:
                response=requests.get(self.base+'/rest/v1/'+table,headers=self.headers,
                    params=params,timeout=(min(1,remaining/2),min(3,remaining/2)))
                response.raise_for_status()
                page=response.json()
                if not isinstance(page,list):raise ValueError('Invalid evidence response')
            except Exception as exc:
                raise BreachReadUnavailable('Evidence read failed') from exc
            rows.extend(page)
            if limit or len(page)<count:return rows
            if len(rows)>=20000:raise BreachReadUnavailable('Evidence history exceeded safe complete-read limit')

def complete_breach_view(function):
    @wraps(function)
    def view(*args,**kwargs):
        try:return function(*args,**kwargs)
        except BreachReadUnavailable:
            return jsonify(success=False,error='Complete breach evidence could not be loaded. Please retry.'),503
    return view
