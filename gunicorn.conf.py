"""Keep health probes responsive while monitoring requests wait on external I/O."""
import os

bind = '0.0.0.0:' + os.environ.get('PORT', '10000')
workers = 2
worker_class = 'gthread'
threads = 8
worker_connections = 64
backlog = 128
preload_app = False
timeout = 30
graceful_timeout = 30
accesslog = '-'
errorlog = '-'
access_log_format = '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s duration_us=%(D)s'


def on_starting(server):
    # Render CLI flags override config defaults. Convert the observed sync-worker
    # configuration and enforce the capacity floor before workers are created.
    if server.cfg.worker_class.__name__ == 'SyncWorker':
        server.cfg.set('worker_class', 'gthread')
    if server.cfg.worker_class.__name__ == 'ThreadWorker' and server.cfg.threads < threads:
        server.cfg.set('threads', threads)
    # The arbiter caches this class before on_starting; refresh that cache too.
    server.worker_class = server.cfg.worker_class
    server.log.info('NP MONITOR HTTP V51 worker_class=%s threads=%s workers=%s bind=%s',
                    server.cfg.worker_class.__name__, server.cfg.threads,
                    server.cfg.workers, server.cfg.bind)


def post_worker_init(worker):
    from monitoring_http import configure_worker_capacity
    capacity = configure_worker_capacity(worker.cfg.threads)
    worker.log.info('NP MONITOR HTTP V51 request_slots=%s probe_threads_reserved=2', capacity)
