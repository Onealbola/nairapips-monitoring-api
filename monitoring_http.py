"""Bound monitoring HTTP work so slow dependencies cannot occupy every thread."""
import json
import threading
import weakref

_instances = weakref.WeakSet()


def configure_worker_capacity(threads):
    capacity = max(1, int(threads) - 2)
    for instance in list(_instances):
        instance.configure_capacity(capacity)
    return capacity


class MonitoringAdmission:
    PROBES = frozenset({'/', '/livez'})
    ROSTERS = frozenset({'/monitorable_accounts', '/traders', '/traders_raw', '/debug/supabase'})

    def __init__(self, application, capacity=6):
        self.application = application
        self.configure_capacity(capacity)
        _instances.add(self)

    def configure_capacity(self, capacity):
        # Gunicorn calls this once before the worker accepts requests.
        self.capacity = max(1, int(capacity))
        self.slots = threading.BoundedSemaphore(self.capacity)
        self.roster_slot = threading.BoundedSemaphore(1)

    @staticmethod
    def busy(environ, start_response):
        # In particular, never return a successful empty roster. Collectors must
        # preserve their last verified population on an unsuccessful refresh.
        body = json.dumps({'success': False, 'operation_started': False,
            'error': 'Monitoring request capacity is busy. Retry shortly.'}).encode()
        headers = [
            ('Content-Type', 'application/json'), ('Content-Length', str(len(body))),
            ('Retry-After', '1'), ('Cache-Control', 'no-store'), ('Vary', 'Origin')]
        origin = environ.get('HTTP_ORIGIN', '')
        if origin in {'https://nairapips.com', 'https://www.nairapips.com'}:
            headers.append(('Access-Control-Allow-Origin', origin))
        start_response('503 Service Unavailable', headers)
        yield body

    def __call__(self, environ, start_response):
        connection = environ.get('gunicorn.socket')
        if connection is not None:
            # Gunicorn sets accepted sockets back to blocking mode. A stalled
            # upload or reader must not retain an HTTP thread indefinitely.
            connection.settimeout(10)
        path = environ.get('PATH_INFO', '')
        method = environ.get('REQUEST_METHOD', 'GET')
        probe = path in self.PROBES and method in {'GET', 'HEAD'}
        if probe or method == 'OPTIONS':
            yield from self.application(environ, start_response)
            return
        if not self.slots.acquire(blocking=False):
            yield from self.busy(environ, start_response)
            return
        roster_acquired = False
        response = None
        try:
            if path in self.ROSTERS:
                roster_acquired = self.roster_slot.acquire(blocking=False)
                if not roster_acquired:
                    yield from self.busy(environ, start_response)
                    return
            response = self.application(environ, start_response)
            for block in response:
                # Limit each blocking socket write, including large roster data.
                if not block:
                    yield block
                else:
                    for offset in range(0, len(block), 65536):
                        yield block[offset:offset + 65536]
        finally:
            try:
                if response is not None and hasattr(response, 'close'):
                    response.close()
            finally:
                if roster_acquired:
                    self.roster_slot.release()
                self.slots.release()
