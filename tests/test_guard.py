import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from guard.common import IPS, PORT, MONITOR_ID, DOMAIN, signature
from guard.engine import Engine
from guard.cloudflare import Cloudflare
from guard.notify import Notifier


class DNS:
    configured = True
    write_capable = True
    def preflight(self):
        return self.current()
    def __init__(self):
        self.ip, self.writes, self.fail, self.uncertain = IPS[0], [], False, False
    def current(self):
        if self.fail:
            raise OSError('network down')
        return self.ip
    def switch(self, expected, target, before_write=None):
        if before_write: before_write()
        self.writes.append((expected,target))
        if self.uncertain:
            raise TimeoutError()
        self.ip = target


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name+'/state.db'
        self.now = 2000000000.0
        self.dns = DNS()
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.e = Engine(self.path,self.dns,False,clock=lambda:self.now)
    def tearDown(self):
        self.e.db.close()
        self.output.__exit__(None,None,None)
        self.temp.cleanup()
    def sample(self, up=(False,True,True), internet=True, direct=True):
        return dict(agent=MONITOR_ID,observed_at=self.now,internet_ok=internet,direct_ok=direct,
                    samples={ip:dict(up=status,success_ratio=1.0 if status else 0.0,port=PORT,latency_ms=100 if status else None)
                             for ip,status in zip(IPS,up)})
    def step(self, **kwargs):
        self.now+=60
        return self.e.report(self.sample(**kwargs))
    def three(self, **kwargs):
        return [self.step(**kwargs) for _ in range(3)]
    def test_switch_only_after_three(self):
        self.assertEqual(self.three(),['healthy_or_waiting','healthy_or_waiting','switch_confirmed'])
        self.assertEqual(self.dns.writes,[(IPS[0],IPS[1])])
    def test_no_auto_failback(self):
        self.three()
        self.now+=901
        self.three(up=(True,True,True))
        self.assertEqual(self.e.s['current'],IPS[1])
        self.assertEqual(len(self.dns.writes),1)
    def test_dry_run_never_writes_or_fakes_current(self):
        self.e.dry_run=True
        self.assertEqual(self.three()[-1],'dry_run')
        self.assertEqual(self.dns.writes,[])
        self.assertEqual(self.e.s['current'],IPS[0])
    def test_all_down_inhibits(self):
        self.three(up=(False,False,False))
        self.assertEqual(self.dns.writes,[])
    def test_internet_loss_resets_streak(self):
        self.step();self.step();self.step(internet=False)
        self.step();self.step()
        self.assertEqual(self.dns.writes,[])
        self.step()
        self.assertEqual(len(self.dns.writes),1)
    def test_route_invalid_inhibits(self):
        self.three(direct=False)
        self.assertEqual(self.dns.writes,[])
    def test_stale_rejected(self):
        report=self.sample();self.now+=151
        with self.assertRaises(ValueError):self.e.report(report)
    def test_future_rejected(self):
        report=self.sample();report['observed_at']+=11
        with self.assertRaises(ValueError):self.e.report(report)
    def test_replay_rejected(self):
        report=self.sample();self.e.report(report)
        with self.assertRaises(ValueError):self.e.report(report)
    def test_fast_samples_rejected(self):
        self.step();self.now+=1
        with self.assertRaises(ValueError):self.e.report(self.sample())
    def test_missing_target_rejected(self):
        report=self.sample();del report['samples'][IPS[2]]
        with self.assertRaises(ValueError):self.e.report(report)
    def test_wrong_port_rejected(self):
        report=self.sample();report['samples'][IPS[0]]['port']=PORT+1
        with self.assertRaises(ValueError):self.e.report(report)
    def test_gap_breaks_consecutive(self):
        self.step();self.step();self.now+=91;self.step();self.step()
        self.assertEqual(self.dns.writes,[])
    def test_backup_needs_three_good(self):
        self.step(up=(False,False,False));self.step(up=(False,False,False));self.step()
        self.assertEqual(self.dns.writes,[])
    def test_cooldown_applies_to_manual(self):
        self.three();self.step(up=(True,False,True))
        self.assertEqual(self.e.manual(IPS[2]),'cooldown')
        self.assertEqual(len(self.dns.writes),1)
    def test_manual_cannot_bypass_health(self):
        self.assertEqual(self.e.manual(IPS[1]),'inhibited')
        self.three(up=(True,False,True))
        self.assertEqual(self.e.manual(IPS[1]),'no_healthy_backup')
    def test_offline_alarm_and_recovery(self):
        self.step();self.now+=151;self.e.tick()
        self.assertTrue(self.e.s['alarm']['monitor_offline'])
        self.assertEqual(self.e.s['samples'][IPS[0]]['fail'],0)
        self.step()
        self.assertFalse(self.e.s['alarm']['monitor_offline'])
    def test_restart_resets_streak_preserves_cooldown(self):
        self.three();last=self.e.s['last_switch'];self.e.db.close()
        self.e=Engine(self.path,self.dns,False,clock=lambda:self.now)
        self.assertEqual(self.e.s['last_switch'],last)
        self.assertEqual(self.e.s['samples'],{})
    def test_uncertain_write_is_not_retried(self):
        self.dns.uncertain=True;self.three();self.now+=901;self.three()
        self.assertEqual(len(self.dns.writes),1)
        self.assertIsNotNone(self.e.s['pending'])
    def test_late_dns_readback_reconciles(self):
        self.dns.uncertain=True;self.three();self.dns.ip=IPS[1];self.step()
        self.assertIsNone(self.e.s['pending'])
        self.assertEqual(self.e.s['switches'],1)
    def test_dns_error_inhibits(self):
        self.dns.fail=True;self.three()
        self.assertEqual(self.dns.writes,[])
    def test_external_change_starts_cooldown(self):
        self.step();self.dns.ip=IPS[2];self.step()
        self.assertEqual(self.e.s['last_switch'],self.now)
    def test_nan_rejected(self):
        report=self.sample();report['samples'][IPS[1]]['latency_ms']=float('nan')
        with self.assertRaises(ValueError):self.e.report(report)
    def test_persistent_notification_outbox(self):
        self.e.channels=('feishu','telegram');self.e.event('test','message')
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],2)
    def test_signature_covers_entire_body(self):
        self.assertNotEqual(signature('a'*32,b'one'),signature('a'*32,b'two'))
    def test_cloudflare_dry_guard(self):
        with patch.dict('os.environ',{},clear=True):cf=Cloudflare(True)
        with patch.object(cf,'api') as api:
            with self.assertRaises(RuntimeError):cf.switch(IPS[0],IPS[1])
            api.assert_not_called()
    def test_cloudflare_refuses_proxy_and_multiple_records(self):
        with patch.dict('os.environ',{'CF_API_TOKEN':'t','CF_ZONE_ID':'z','CF_RECORD_ID':'r'},clear=True):cf=Cloudflare(False)
        record=dict(id='r',type='A',name=DOMAIN,proxied=True,content=IPS[0])
        with patch.object(cf,'api',return_value=[record]):
            with self.assertRaises(RuntimeError):cf.current()
        record['proxied']=False
        with patch.object(cf,'api',return_value=[record,record]):
            with self.assertRaises(RuntimeError):cf.current()
    def test_notification_channels_retry_independently(self):
        with patch.dict('os.environ',{'FEISHU_WEBHOOK':'https://open.feishu.cn/open-apis/bot/v2/hook/test','TELEGRAM_BOT_TOKEN':'test','TELEGRAM_CHAT_ID':'1'},clear=True):n=Notifier()
        self.e.channels=n.channels;self.e.event('test','message')
        def send(channel,message):
            if channel=='feishu':raise OSError()
        with patch.object(n,'send',side_effect=send):n.drain(self.e)
        rows=dict(self.e.db.execute('SELECT channel,delivered FROM outbox'))
        self.assertEqual(rows,{'feishu':0,'telegram':1})



class HttpBoundaryTest(unittest.TestCase):
    setUp = GuardTest.setUp
    tearDown = GuardTest.tearDown
    sample = GuardTest.sample
    def exchange(self,path,body=None,headers=None):
        import time
        from guard.server import handler_for
        class Socket:
            def __init__(self,data):self.data=io.BytesIO(data);self.output=b''
            def makefile(self,*args):return self.data
            def settimeout(self,*args):pass
            def sendall(self,data):self.output+=data
        method='GET' if body is None else 'POST'
        raw=(method+' '+path+' HTTP/1.0\r\n').encode()
        if body is not None:raw+=f'Content-Length: {len(body)}\r\n'.encode()
        for k,v in (headers or {}).items():raw+=(k+': '+v+'\r\n').encode()
        sock=Socket(raw+b'\r\n'+(body or b''))
        handler_for(self.e,'r'*32,'a'*32,[time.monotonic()])(sock,('127.0.0.1',1),None)
        return int(sock.output.split(b' ')[1])
    def test_remote_reports_disabled(self):
        body=json.dumps(self.sample()).encode()
        self.assertEqual(self.exchange('/report',body),410)
        headers={'X-Guard-Signature':signature('r'*32,body)}
        self.assertEqual(self.exchange('/report',body,headers),410)
    def test_http_admin_auth(self):
        self.assertEqual(self.exchange('/switch',b'{}'),401)
        self.assertEqual(self.exchange('/status'),404)
        self.assertEqual(self.exchange('/status',headers={'Authorization':'Bearer '+'a'*32}),200)
    def test_http_body_limit_and_health(self):
        self.assertEqual(self.exchange('/report',b'x'*16385),413)
        self.assertEqual(self.exchange('/healthz'),200)

class FaultPersistenceTest(unittest.TestCase):
    setUp = GuardTest.setUp
    tearDown = GuardTest.tearDown
    sample = GuardTest.sample
    step = GuardTest.step
    three = GuardTest.three
    def test_offline_does_not_claim_ip_recovery(self):
        self.e.dry_run=True;self.three();self.now+=151;self.e.tick()
        self.assertTrue(self.e.s['alarm']['ip_'+IPS[0]])
        self.assertIn('oci_guard_ip_fault{ip="'+IPS[0]+'"} 1',self.e.metrics())
        self.three()
        self.assertEqual(self.e.s['failures'][IPS[0]],1)
        self.step(up=(True,True,True))
        self.assertFalse(self.e.s['alarm']['ip_'+IPS[0]])

    def test_restart_has_no_valid_sample_until_new_report(self):
        self.step(up=(True,True,True));self.e.db.close()
        self.e=Engine(self.path,self.dns,False,clock=lambda:self.now)
        self.assertFalse(self.e.valid())
        self.assertEqual(self.e.s['probe_total'][IPS[0]],1)
        self.step(up=(True,True,True))
        self.assertTrue(self.e.valid())
        self.assertEqual(self.e.s['probe_total'][IPS[0]],2)


if __name__ == '__main__':unittest.main()
