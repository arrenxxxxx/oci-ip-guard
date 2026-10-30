import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from .cloudflare import Cloudflare
from .common import secret, INTERVAL
from .probe import Prober
from .engine import Engine
from .notify import Notifier
from .mode import resolve_mode


def handler_for(engine, report_key, admin_key, worker_health, probe_health=None):
    def worker_alive():
        return time.monotonic()-worker_health[0] < 90
    def probe_alive():
        return probe_health is None or (probe_health[0] > 0 and
                time.monotonic()-probe_health[0] < INTERVAL*2+30)
    def healthy():
        return worker_alive() and probe_alive()
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass

        def respond(self, status, value, content_type='application/json'):
            body = (json.dumps(value) if content_type=='application/json' else value).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def admin(self):
            return hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+admin_key)

        def do_GET(self):
            if self.path == '/healthz':
                ok = healthy()
                self.respond(200 if ok else 503, {'healthy':ok, 'worker_healthy':worker_alive(), 'probe_worker_healthy':probe_alive(),
                    'probe_valid':engine.valid(), 'dry_run':engine.dry_run,
                    'auto_switch':engine.availability()})
            elif self.path == '/metrics':
                ok = int(worker_alive())
                self.respond(200, engine.metrics()+f'oci_guard_worker_healthy {ok}\noci_guard_probe_worker_healthy {int(probe_alive())}\n', 'text/plain; version=0.0.4')
            elif self.path in ['/status','/events'] and self.admin():
                with engine.lock:
                    value = engine.status() if self.path == '/status' else [dict(zip(['id','ts','kind','message'], row))
                        for row in engine.db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')]
                    self.respond(200, value)
            else:
                self.respond(404, {'error':'not found'})

        def do_POST(self):
            try:
                size = int(self.headers.get('Content-Length','0'))
                if not 0 < size <= 16384:
                    self.respond(413, {'error':'invalid body size'})
                    return
                body = self.rfile.read(size)
                if self.path == '/report':
                    self.respond(410, {'error':'Remote reports disabled; probes run locally'})
                    return
                elif self.path in ['/switch','/reconcile','/test-notification'] and self.admin():
                    value = json.loads(body)
                    if self.path == '/switch':
                        result = engine.manual(value['ip'])
                    elif self.path == '/reconcile':
                        result = engine.reconcile()
                    else:
                        with engine.lock:
                            engine.event('notification_test','Operator requested notification test')
                        result = 'queued'
                else:
                    self.respond(401, {'error':'unauthorized'})
                    return
                self.respond(200, {'result':result})
            except (ValueError, KeyError, TypeError):
                self.respond(400, {'error':'invalid, stale, or replayed report/request'})
            except Exception as error:
                print('request_error type='+type(error).__name__, flush=True)
                self.respond(503, {'error':'system error'})
    return Handler


def main():
    admin_key = secret('ADMIN_TOKEN')
    prober = Prober()
    mode = resolve_mode(os.environ)
    dry = mode.dry_run
    notifier = Notifier()
    engine = Engine(os.environ.get('STATE_DB','/data/guard.db'),Cloudflare(dry),dry,notifier.channels,mode=mode)
    engine.event('startup',engine.availability()['message'] +
                 ('；' + mode.error if mode.error else '') +
                 f'; mode={mode.name}; legacy_config={mode.legacy}; notification_channels={list(notifier.channels)}')
    engine.sync_dns()
    engine.publish_status()
    health = [time.monotonic()]
    probe_health = [0.0]
    def worker():
        while True:
            try:
                engine.tick()
                notifier.drain(engine)
                health[0] = time.monotonic()
            except Exception as error:
                print('worker_error type='+type(error).__name__,flush=True)
            time.sleep(5)
    threading.Thread(target=worker,daemon=True).start()
    def probe_worker():
        while True:
            started = time.monotonic()
            try:
                report = prober.cycle()
                result = engine.report(report)
                with engine.lock:
                    engine.alarm('probe_error', False, '')
                    engine.save()
                probe_health[0] = time.monotonic()
                print(json.dumps({'event':'probe_cycle', 'result':result,
                    'mapping_ok':report['direct_ok'], 'internet_ok':report['internet_ok'],
                    'ratios':{ip:r['success_ratio'] for ip,r in report['samples'].items()}}),flush=True)
            except Exception as error:
                with engine.lock:
                    engine.alarm('probe_error', True, 'Local probe failed: '+type(error).__name__)
                    engine.save()
                print('probe_error type='+type(error).__name__,flush=True)
            time.sleep(max(1, INTERVAL-(time.monotonic()-started)))
    threading.Thread(target=probe_worker,daemon=True).start()
    handler = handler_for(engine,None,admin_key,health,probe_health)
    bind = os.environ.get('CONTROLLER_BIND_IP','127.0.0.1')
    from .install import private_bind
    private_bind(bind)
    if bind != '127.0.0.1':
        local = HTTPServer(('127.0.0.1',8765),handler)
        threading.Thread(target=local.serve_forever,daemon=True).start()
    HTTPServer((bind,8765),handler).serve_forever()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(json.dumps({'event':'startup_failed','message':'自动切换不可用', 'reason':detail}, ensure_ascii=False), flush=True)
        raise SystemExit(1)
