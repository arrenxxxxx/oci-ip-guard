import json
import math
import os
import sqlite3
import threading
import time
from .common import (IPS, PORT, MONITOR_ID, FRESH, MAX_SAMPLE_AGE, COOLDOWN,
                     INTERVAL, FAILURE_THRESHOLD, HEALTHY_THRESHOLD, FAILURE_RATIO, HEALTHY_RATIO)


class Engine:
    def __init__(self, path, dns, dry_run=True, channels=(), clock=time.time):
        self.clock, self.dns, self.dry_run = clock, dns, dry_run
        self.channels = channels
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=20)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, message TEXT);
            CREATE TABLE IF NOT EXISTS outbox (event_id INTEGER, channel TEXT, attempts INTEGER DEFAULT 0,
                next_at REAL DEFAULT 0, delivered INTEGER DEFAULT 0, PRIMARY KEY(event_id, channel));
        ''')
        row = self.db.execute('SELECT body FROM state WHERE id=1').fetchone()
        self.s = json.loads(row[0]) if row else dict(current=None, last_report=0, observed=0,
            last_switch=0, last_plan=0, pending=None, blocked_pending=False, dns_ok=False,
            internet_ok=False, direct_ok=False, alarm={}, samples={}, switches=0, failures={ip: 0 for ip in IPS})
        # A restart or mode change must acquire fresh consecutive observations.
        self.s['samples'] = {}
        self.s['observed'] = self.s['last_report'] = 0
        for old,new in [('nas_offline','monitor_offline'),('nas_internet','monitor_internet')]:
            if old in self.s['alarm']:
                self.s['alarm'][new] = self.s['alarm'].pop(old)
        self.s.setdefault('probe_total', {ip:0 for ip in IPS})
        self.s.setdefault('probe_success_total', {ip:0 for ip in IPS})
        for key in ('failures','probe_total','probe_success_total'):
            for ip in IPS:
                self.s[key].setdefault(ip,0)
        self.s['dns_ok'] = False
        self.s['started'] = clock()
        self.save()

    def save(self):
        self.db.execute('INSERT OR REPLACE INTO state VALUES(1,?)', (json.dumps(self.s),))
        self.db.commit()

    def event(self, kind, message):
        cur = self.db.execute('INSERT INTO events(ts,kind,message) VALUES(?,?,?)',
                              (self.clock(), kind, message))
        for channel in self.channels:
            self.db.execute('INSERT INTO outbox(event_id,channel) VALUES(?,?)', (cur.lastrowid, channel))
        self.db.commit()
        print(json.dumps(dict(ts=self.clock(), event=kind, message=message)), flush=True)

    def alarm(self, name, active, message):
        previous = self.s['alarm'].get(name, False)
        if active != previous:
            self.s['alarm'][name] = active
            self.event(name if active else 'recovery', message if active else name + ' recovered')

    def sync_dns(self):
        try:
            actual = self.dns.current()
            if actual not in IPS:
                raise ValueError('unknown DNS target')
            pending = self.s['pending']
            if pending:
                if actual == pending['to']:
                    self.s['switches'] += 1
                    self.s['last_switch'] = max(self.clock(), pending['ts'])
                    self.event('dns_switch', f"{pending['from']} -> {actual}; API read-back confirmed")
                    self.s['pending'] = None
                    self.s['blocked_pending'] = False
                else:
                    self.s['blocked_pending'] = True
                    self.alarm('uncertain_dns_write', True, 'Unconfirmed DNS write; operator reconciliation required')
            elif self.s['current'] and actual != self.s['current']:
                self.s['last_switch'] = self.clock()
                self.event('external_dns_change', f"{self.s['current']} -> {actual}; cooldown started")
            self.s['current'] = actual
            self.s['dns_ok'] = True
            self.alarm('dns_error', False, '')
        except Exception as error:
            self.s['dns_ok'] = False
            self.alarm('dns_error', True, 'DNS read failed: ' + type(error).__name__)
        self.save()

    def validate(self, report):
        now = self.clock()
        if report.get('agent') != MONITOR_ID:
            raise ValueError('Unexpected agent')
        ts = report.get('observed_at')
        if type(ts) not in (int, float) or not math.isfinite(ts) or not -10 <= now-ts <= MAX_SAMPLE_AGE:
            raise ValueError('Stale or future sample')
        if ts <= self.s['observed']:
            raise ValueError('Replay or out-of-order sample')
        if self.s['observed'] and ts-self.s['observed'] < INTERVAL * 0.75:
            raise ValueError('Samples arrived faster than allowed')
        for field in ['internet_ok', 'direct_ok']:
            if type(report.get(field)) is not bool:
                raise ValueError('Invalid flag')
        samples = report.get('samples')
        if not isinstance(samples, dict) or set(samples) != set(IPS):
            raise ValueError('Incomplete target set')
        for v in samples.values():
            if not isinstance(v, dict) or type(v.get('up')) is not bool or v.get('port') != PORT:
                raise ValueError('Invalid target result')
            ratio = v.get('success_ratio')
            if type(ratio) not in (int,float) or not math.isfinite(ratio) or not 0 <= ratio <= 1:
                raise ValueError('Invalid success ratio')
            if v['up'] != (ratio >= HEALTHY_RATIO):
                raise ValueError('Inconsistent health ratio')
            latency = v.get('latency_ms')
            if v['up'] and (type(latency) not in (int,float) or not math.isfinite(latency) or not 0 <= latency <= 10000):
                raise ValueError('Invalid latency')

    def report(self, report):
        with self.lock:
            self.validate(report)
            now = self.clock()
            gap = report['observed_at'] - self.s['observed']
            old_valid = self.valid()
            self.s.update(last_report=now, observed=report['observed_at'],
                          internet_ok=report['internet_ok'], direct_ok=report['direct_ok'])
            valid = report['internet_ok'] and report['direct_ok']
            self.alarm('monitor_offline', False, '')
            self.alarm('monitor_internet', not report['internet_ok'], 'Default-egress internet controls unavailable; switching inhibited')
            self.alarm('unsafe_route', not report['direct_ok'], 'Source public-IP mapping not verified; switching inhibited')
            for ip, result in report['samples'].items():
                previous = self.s['samples'].get(ip, {})
                consecutive = valid and old_valid and INTERVAL * 0.75 <= gap <= INTERVAL * 1.5
                failed = result['success_ratio'] < FAILURE_RATIO
                fail = (previous.get('fail', 0) if consecutive else 0) + 1 if valid and failed else 0
                good = (previous.get('good', 0) if consecutive else 0) + 1 if valid and result['up'] else 0
                self.s['samples'][ip] = dict(result, fail=fail, good=good)
                if valid:
                    self.s['probe_total'][ip] += 1
                    self.s['probe_success_total'][ip] += int(result['up'])
                    if fail == FAILURE_THRESHOLD and not self.s['alarm'].get('ip_' + ip):
                        self.s['failures'][ip] += 1
                    if result['up'] or fail >= FAILURE_THRESHOLD:
                        self.alarm('ip_' + ip, not result['up'],
                                   ip + f' regional success ratio {result["success_ratio"]:.0%} below {FAILURE_RATIO:.0%} for {FAILURE_THRESHOLD} rounds')
            self.alarm('all_down', valid and all(v['success_ratio'] < FAILURE_RATIO for v in report['samples'].values()),
                       'All entry IPs unavailable; DNS unchanged')
            self.save()  # Persist observation/replay boundary before any external side effect.
            self.sync_dns()
            result = self.decide()
            self.save()
            return result

    def valid(self):
        return (set(self.s['samples']) == set(IPS) and self.s['observed'] > 0 and self.clock()-self.s['observed'] <= FRESH and
                self.s['internet_ok'] and self.s['direct_ok'])

    def decide(self, target=None):
        if not self.valid() or not self.s['dns_ok'] or self.s['pending']:
            return 'inhibited'
        if self.clock()-self.s['last_switch'] < COOLDOWN:
            return 'cooldown'
        current = self.s['current']
        if target is not None and target not in IPS:
            raise ValueError('Unknown target')
        if target == current:
            return 'already_current'
        if target is None:
            if self.s['samples'].get(current, {}).get('fail', 0) < FAILURE_THRESHOLD:
                return 'healthy_or_waiting'
            target = next((ip for ip in IPS if ip != current and
                           self.s['samples'].get(ip, {}).get('good', 0) >= HEALTHY_THRESHOLD), None)
        if target is None or self.s['samples'].get(target, {}).get('good', 0) < HEALTHY_THRESHOLD:
            return 'no_healthy_backup'
        if self.dry_run:
            if self.clock()-self.s['last_plan'] >= COOLDOWN:
                self.event('dry_run_switch', f'{current} -> {target}; DNS NOT modified')
                self.s['last_plan'] = self.clock()
            return 'dry_run'
        self.s['pending'] = dict(ts=self.clock(), **{'from': current, 'to': target})
        self.save()  # Crash-safe intent; never blindly repeat an uncertain PATCH.
        try:
            self.dns.switch(current, target)
        except Exception as error:
            self.event('system_error', 'DNS update uncertain: ' + type(error).__name__)
        self.sync_dns()
        return 'switch_confirmed' if self.s['pending'] is None else 'pending_reconciliation'

    def manual(self, target):
        with self.lock:
            self.sync_dns()
            result = self.decide(target)
            self.event('manual_request', f'{target}: {result}')
            self.save()
            return result

    def reconcile(self):
        with self.lock:
            self.sync_dns()
            if not self.s['dns_ok']:
                return 'inhibited'
            self.s['pending'] = None
            self.s['blocked_pending'] = False
            self.s['last_switch'] = self.clock()
            self.s['samples'] = {}
            self.alarm('uncertain_dns_write', False, '')
            self.event('reconcile', 'Operator accepted current DNS; fresh samples and cooldown required')
            self.save()
            return 'reconciled'

    def tick(self):
        with self.lock:
            baseline = self.s['observed'] or self.s['started']
            offline = self.clock()-baseline > FRESH
            self.alarm('monitor_offline', offline, 'Local probe heartbeat/sample expired; DNS unchanged')
            if offline:
                for v in self.s['samples'].values():
                    v['good'] = v['fail'] = 0
            self.save()

    def metrics(self):
        with self.lock:
            s = self.s
            metrics = dict(dry_run=int(self.dry_run), sample_valid=int(self.valid()),
                monitor_heartbeat_timestamp_seconds=s['last_report'], sample_timestamp_seconds=s['observed'],
                monitor_online=int(s['observed'] > 0 and self.clock()-s['observed'] <= FRESH),
                direct_route_verified=int(s['direct_ok']), internet_up=int(s['internet_ok']),
                dns_read_ok=int(s['dns_ok']), dns_api_configured=int(self.dns.configured),
                pending_dns_write=int(s['pending'] is not None), switches_total=s['switches'],
                last_switch_timestamp_seconds=s['last_switch'])
            metrics['nas_online'] = metrics['monitor_online']
            metrics['nas_heartbeat_timestamp_seconds'] = metrics['monitor_heartbeat_timestamp_seconds']
            metrics['source_mapping_verified'] = metrics['direct_route_verified']
            lines = [f'oci_guard_{k} {v}' for k,v in metrics.items()]
            for ip in IPS:
                v = s['samples'].get(ip, {})
                for name,value in [('ip_up',int(v.get('up',False))),
                    ('ip_fault',int(s['alarm'].get('ip_'+ip,False))),
                    ('regional_success_ratio',v.get('success_ratio','NaN')),
                    ('probe_total',s['probe_total'][ip]),
                    ('probe_success_total',s['probe_success_total'][ip]),
                    ('tcp_latency_seconds',v.get('latency_ms',0)/1000 if v.get('up') else 'NaN'),
                    ('consecutive_failures',v.get('fail',0)), ('failures_total',s['failures'][ip]),
                    ('current_ip',int(s['current']==ip))]:
                    lines.append(f'oci_guard_{name}{{ip="{ip}"}} {value}')
            for ip,v in s['samples'].items():
                for r in v.get('regions',[]):
                    labels = f'ip="{ip}",region="{r["name"]}"'
                    lines.append(f'oci_guard_region_up{{{labels}}} {int(r["up"])}')
                    latency = r['latency_ms']/1000 if r['up'] else 'NaN'
                    lines.append(f'oci_guard_region_tcp_latency_seconds{{{labels}}} {latency}')
            for kind in ['dns_switch','dry_run_switch']:
                count = self.db.execute('SELECT COUNT(*) FROM events WHERE kind=?',(kind,)).fetchone()[0]
                lines.append(f'oci_guard_events_total{{kind="{kind}"}} {count}')
            for channel in ['feishu','telegram']:
                configured = channel in self.channels or (channel == 'telegram' and os.environ.get('TELEGRAM_VIA_GRAFANA') == 'true')
                lines.append(f'oci_guard_notification_configured{{channel="{channel}"}} {int(configured)}')
                row = self.db.execute('SELECT COUNT(*),COALESCE(SUM(attempts),0) FROM outbox WHERE channel=? AND delivered=0',(channel,)).fetchone()
                lines.append(f'oci_guard_notification_pending{{channel="{channel}"}} {row[0]}')
                lines.append(f'oci_guard_notification_retry_attempts{{channel="{channel}"}} {row[1]}')
            return '\n'.join(lines)+'\n'
