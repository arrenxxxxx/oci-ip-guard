import json
import math
import os
import sqlite3
import threading
import time
from .cloudflare import DNSWriteBlocked
from .common import (IPS, PORT, MONITOR_ID, FRESH, MAX_SAMPLE_AGE, COOLDOWN,
                     INTERVAL, FAILURE_THRESHOLD, HEALTHY_THRESHOLD, FAILURE_RATIO, HEALTHY_RATIO)


class Engine:
    def __init__(self, path, dns, dry_run=True, channels=(), clock=time.time, mode=None):
        self.clock, self.dns, self.dry_run = clock, dns, dry_run
        self.mode = mode
        self.config_error = mode.error if mode else ''
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
        self.s['dns_write_capable'] = False
        self.s['preflight_error'] = ''
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
            self.s['dns_write_capable'] = False
            self.s['preflight_error'] = ''
            if not self.dry_run and not self.config_error:
                try:
                    self.dns.preflight()
                    self.s['dns_write_capable'] = self.dns.write_capable is True
                except Exception as error:
                    self.s['preflight_error'] = str(error) if isinstance(error, ValueError) else '只读预检失败；检查 Cloudflare 凭据、权限和 API 连通性。'
            actual = self.dns.current()
            if actual not in IPS:
                raise ValueError('unknown DNS target')
            pending = self.s['pending']
            if pending:
                if actual == pending['to']:
                    self.s['switches'] += 1
                    self.s['last_switch'] = max(self.clock(), pending['ts'])
                    self.event('dns_switch', f"真实 DNS 切换成功：{pending['from']} -> {actual}；API 读回已确认。")
                    self.s['pending'] = None
                    self.s['blocked_pending'] = False
                else:
                    self.s['blocked_pending'] = True
                    self.alarm('uncertain_dns_write', True, 'DNS 写入结果未确认；已禁止继续切换，请核对实际 DNS 后执行 reconcile。')
            elif self.s['current'] and actual != self.s['current']:
                self.s['last_switch'] = self.clock()
                self.event('external_dns_change', f"{self.s['current']} -> {actual}; cooldown started")
            self.s['current'] = actual
            self.s['dns_ok'] = True
            self.alarm('dns_error', False, '')
        except Exception as error:
            self.s['dns_ok'] = False
            self.alarm('dns_error', True, 'DNS 只读检查失败，自动切换不可用；检查凭据和记录配置：' + type(error).__name__)
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
            self.alarm('monitor_internet', not report['internet_ok'], '默认出口控制探测失败，自动切换不可用；DNS 未修改，请检查出口连通性。')
            self.alarm('unsafe_route', not report['direct_ok'], '公网/私网源地址映射未验证，自动切换不可用；DNS 未修改，请检查地址顺序和回显链路。')
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
                                   ip + f' 区域成功率 {result["success_ratio"]:.0%} 连续 {FAILURE_THRESHOLD} 轮低于 {FAILURE_RATIO:.0%}；仅为故障检测，不代表 DNS 已切换。' + (' 模拟模式：故障时不会修改 DNS。' if self.dry_run else ' 请查看后续切换执行结果。'))
            self.alarm('all_down', valid and all(v['success_ratio'] < FAILURE_RATIO for v in report['samples'].values()),
                       '全部入口不可用，没有健康备用 IP；自动切换不可用，DNS 未修改。')
            self.save()  # Persist observation/replay boundary before any external side effect.
            self.sync_dns()
            result = self.decide()
            self.publish_status()
            self.save()
            return result

    def valid(self):
        return (set(self.s['samples']) == set(IPS) and self.s['observed'] > 0 and self.clock()-self.s['observed'] <= FRESH and
                self.s['internet_ok'] and self.s['direct_ok'])

    def blocked(self, result, target=None):
        current = self.s['current']
        fault = self.s['samples'].get(current, {}).get('fail', 0) >= FAILURE_THRESHOLD
        if fault or target is not None:
            reasons = self.availability()['reasons']
            explanation = '; '.join(r['message'] for r in reasons)
            if result == 'no_healthy_backup' and target is not None:
                explanation = f'请求的备用 IP {target} 尚未达到连续健康轮次；检查该入口并等待健康阈值。'
            explanation = explanation or result
            key = result + ':' + explanation
            if self.s.get('last_blocked') != key:
                self.event('switch_blocked', '自动切换受保护条件阻止；本次未发起新的 DNS 写入（如有 pending，实际记录仍需核对）：' + explanation)
                self.s['last_blocked'] = key
        return result

    def before_write(self, current, target):
        pending = self.s['pending']
        if (self.config_error or self.dry_run or not self.valid() or not self.s['dns_ok'] or
                not self.s['dns_write_capable'] or self.clock()-self.s['last_switch'] < COOLDOWN or
                not pending or pending['from'] != current or pending['to'] != target or
                self.s['samples'].get(target, {}).get('good', 0) < HEALTHY_THRESHOLD):
            raise DNSWriteBlocked('写入前样本已过期或保护条件改变；重新探测后再判断，DNS 未修改。')

    def decide(self, target=None):
        if self.config_error or not self.valid() or not self.s['dns_ok'] or self.s['pending']:
            return self.blocked('inhibited', target)
        if not self.dry_run and not self.s['dns_write_capable']:
            return self.blocked('inhibited', target)
        if self.clock()-self.s['last_switch'] < COOLDOWN:
            return self.blocked('cooldown', target)
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
            return self.blocked('no_healthy_backup', target)
        if self.dry_run:
            if self.clock()-self.s['last_plan'] >= COOLDOWN:
                self.event('dry_run_switch', f'检测到切换条件，但当前为模拟模式，DNS 未修改：{current} -> {target}（计划）。')
                self.s['last_plan'] = self.clock()
            return 'dry_run'
        self.s['last_blocked'] = None
        self.s['pending'] = dict(ts=self.clock(), **{'from': current, 'to': target})
        self.save()  # Crash-safe intent; never blindly repeat an uncertain PATCH.
        try:
            self.dns.switch(current, target, before_write=lambda: self.before_write(current, target))
        except DNSWriteBlocked as error:
            self.s['pending'] = None
            self.s['blocked_pending'] = False
            self.s['dns_write_capable'] = False
            self.event('switch_blocked', '自动切换受写入前检查阻止；DNS 未修改：' + str(error))
            self.save()
            self.sync_dns()
            return 'inhibited'
        except Exception as error:
            self.event('dns_write_failed', f'DNS 写入失败或结果不确定：{current} -> {target}；{type(error).__name__}。等待读回验证，禁止盲目重试。')
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
            self.alarm('monitor_offline', offline, '本机探测心跳/样本已过期；自动切换不可用，DNS 未修改，请检查探测线程。')
            if offline:
                for v in self.s['samples'].values():
                    v['good'] = v['fail'] = 0
            self.publish_status()
            self.save()

    def availability(self):
        with self.lock:
            return self._availability()

    def _availability(self):
        reasons = []
        def add(code, message):
            reasons.append({'code': code, 'message': message})
        name = self.mode.name if self.mode else ('simulate' if self.dry_run else 'live')
        if self.config_error:
            add('configuration', self.config_error)
        if not self.s['dns_ok']:
            add('dns_read', 'DNS 记录读取/身份校验失败；检查凭据、唯一 A 记录、DNS-only 与候选 IP。')
        if name == 'live' and not self.s['dns_write_capable']:
            add('write_capability', self.s.get('preflight_error') or 'DNS 写权限尚未完成只读验证。')
        if self.s['pending']:
            add('pending_write', '存在未确认的 DNS 写入；检查事件与实际记录后执行 reconcile。')
        if not self.valid():
            if not self.s['observed'] or self.clock()-self.s['observed'] > FRESH:
                add('sample_stale', '等待新鲜本机探测；检查探测线程和目标连通性。')
            if not self.s['direct_ok']:
                add('source_mapping', '公网/私网源地址映射未验证；检查 ENTRY_IPS、SOURCE_IPS 和回显链路。')
            if not self.s['internet_ok']:
                add('internet', '默认出口控制探测未通过；检查原有出口链路。')
        if self.clock()-self.s['last_switch'] < COOLDOWN:
            add('cooldown', '切换冷却期未结束；等待 COOLDOWN_SECONDS。')
        current = self.s['current']
        if not any(ip != current and self.s['samples'].get(ip, {}).get('good', 0) >= HEALTHY_THRESHOLD for ip in IPS):
            add('healthy_backup', '没有达到连续健康轮次的备用 IP；检查备用入口并等待健康阈值。')
        available = name == 'live' and not self.dry_run and not reasons
        state = 'enabled' if available else 'simulate' if name == 'simulate' and not self.config_error else 'unavailable'
        message = {'enabled':'DNS 自动切换已启用', 'simulate':'模拟模式：故障时不会修改 DNS', 'unavailable':'自动切换不可用'}[state]
        if state == 'simulate' and reasons:
            message += '；自动切换不可用，另有保护条件未满足（见 reasons）。'
        return dict(available=available, state=state, message=message, reasons=reasons, mode=name,
                    legacy_config=bool(self.mode and self.mode.legacy))

    def status(self):
        with self.lock:
            return dict(self.s, dry_run=self.dry_run, auto_switch=self.availability(),
                        probe_valid=self.valid())

    def publish_status(self):
        state = self.availability()
        fingerprint = (state['state'], tuple(r['code'] for r in state['reasons']))
        if fingerprint != getattr(self, '_published_status', None):
            print(json.dumps(dict(event='operating_status', auto_switch=state), ensure_ascii=False), flush=True)
            self._published_status = fingerprint

    def metrics(self):
        with self.lock:
            s = self.s
            availability = self.availability()
            metrics = dict(dry_run=int(self.dry_run), sample_valid=int(self.valid()),
                auto_switch_available=int(availability['available']),
                mode_live=int(availability['mode'] == 'live'),
                mode_simulate=int(availability['state'] == 'simulate'),
                dns_write_capable=int(s['dns_write_capable']),
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
            blocked = {reason['code'] for reason in availability['reasons']}
            for reason in ('configuration','dns_read','write_capability','pending_write','sample_stale','source_mapping','internet','cooldown','healthy_backup'):
                lines.append(f'oci_guard_auto_switch_blocked{{reason="{reason}"}} {int(reason in blocked)}')
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
            for kind in ['dns_switch','dry_run_switch','dns_write_failed','switch_blocked']:
                count = self.db.execute('SELECT COUNT(*) FROM events WHERE kind=?',(kind,)).fetchone()[0]
                lines.append(f'oci_guard_events_total{{kind="{kind}"}} {count}')
            for channel in ['feishu','telegram']:
                configured = channel in self.channels or (channel == 'telegram' and os.environ.get('TELEGRAM_VIA_GRAFANA') == 'true')
                lines.append(f'oci_guard_notification_configured{{channel="{channel}"}} {int(configured)}')
                row = self.db.execute('SELECT COUNT(*),COALESCE(SUM(attempts),0) FROM outbox WHERE channel=? AND delivered=0',(channel,)).fetchone()
                lines.append(f'oci_guard_notification_pending{{channel="{channel}"}} {row[0]}')
                lines.append(f'oci_guard_notification_retry_attempts{{channel="{channel}"}} {row[1]}')
            return '\n'.join(lines)+'\n'
