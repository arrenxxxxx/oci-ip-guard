"""Mode/permission integration contracts; every Cloudflare request is mocked."""
import io
import json
import os
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from guard.mode import resolve_mode, CONFIRMATION
from guard.cloudflare import Cloudflare, DNSWriteBlocked
from guard.common import IPS, DOMAIN
from guard.preflight import check
import test_guard as fixtures

LIVE = {'RUN_MODE':'live', 'ENABLE_DNS_WRITES':CONFIRMATION,
        'CF_API_TOKEN':'test-only', 'CF_ZONE_ID':'a'*32, 'CF_RECORD_ID':'b'*32}


class ModeTest(unittest.TestCase):
    def test_explicit_modes_and_legacy_preservation(self):
        for env, name, dry, error in [
            ({}, 'unconfigured', True, True),
            ({'RUN_MODE':''}, 'unconfigured', True, True),
            ({'RUN_MODE':'unexpected'}, 'unconfigured', True, True),
            ({'RUN_MODE':'simulate'}, 'simulate', True, False),
            ({'RUN_MODE':'live'}, 'live', True, True),
            ({'RUN_MODE':'live','ENABLE_DNS_WRITES':'yes'}, 'live', True, True),
            (LIVE, 'live', False, False),
            ({'DRY_RUN':'true','ENABLE_DNS_WRITES':CONFIRMATION}, 'simulate', True, False),
            ({'DRY_RUN':'false','ENABLE_DNS_WRITES':CONFIRMATION}, 'live', False, False),
            ({'DRY_RUN':'false'}, 'live', True, True),
            ({'DRY_RUN':'invalid'}, 'unconfigured', True, True),
            (dict(LIVE, DRY_RUN='true'), 'live', True, True),
            ({'RUN_MODE':'simulate','DRY_RUN':'false'}, 'simulate', True, True),
            ({'RUN_MODE':'','DRY_RUN':'true'}, 'unconfigured', True, True),
        ]:
            with self.subTest(env=env):
                mode = resolve_mode(env)
                self.assertEqual((mode.name, mode.dry_run, bool(mode.error)), (name,dry,error))

    def test_missing_confirmation_blocks_cloudflare_itself(self):
        with patch.dict(os.environ, dict(LIVE, ENABLE_DNS_WRITES=''), clear=True):
            cf = Cloudflare(False)
        with patch.object(cf, 'api') as api:
            with self.assertRaises(DNSWriteBlocked): cf.switch(IPS[0], IPS[1])
            api.assert_not_called()


class CloudflarePreflightTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, LIVE, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cf = Cloudflare(False)
        self.record = dict(id='b'*32, name=DOMAIN, type='A', proxied=False, content=IPS[0])
        self.details = dict(id='c'*32, status='active', policies=[dict(effect='allow',
            permission_groups=[{'id':'test-dns-write-group','name':'DNS Write'}],
            resources={'com.cloudflare.api.account.zone.'+'a'*32:'*'})])
        self.calls=[]
        self.fail_patch=False
        self.readback_wrong=False
        self.account_token=False
        self.no_policy=False
        self.zone=dict(id='a'*32,status='active',account={'id':'d'*32})

    def api(self, path, payload=None, method=None):
        self.calls.append((path,payload,method))
        if method == 'PATCH':
            if self.fail_patch: raise TimeoutError('do not expose credentials')
            if not self.readback_wrong: self.record['content']=payload['content']
            return self.record
        if path == '/zones/'+'a'*32: return self.zone
        if '/dns_records?' in path: return [self.record]
        if self.account_token and path.startswith('/user/'): raise RuntimeError('unsupported type')
        if path.endswith('/verify'): return {'id':'c'*32,'status':'active'}
        if path.endswith('/'+'c'*32):
            if self.no_policy: raise RuntimeError('denied')
            return self.details
        raise AssertionError(path)

    def test_readonly_success_and_no_patch(self):
        with patch.object(self.cf,'api',side_effect=self.api):
            self.assertEqual(self.cf.preflight(), IPS[0])
        self.assertTrue(self.cf.write_capable)
        self.assertTrue(all(payload is None and method is None for _,payload,method in self.calls))

    def test_account_owned_token_supported(self):
        self.account_token=True
        with patch.object(self.cf,'api',side_effect=self.api): self.cf.preflight()
        self.assertTrue(any(p.startswith('/accounts/'+'d'*32+'/tokens/') for p,_,_ in self.calls))

    def test_policy_unreadable_is_unknown_not_enabled(self):
        self.no_policy=True
        with patch.object(self.cf,'api',side_effect=self.api):
            with self.assertRaisesRegex(ValueError,'未知'): self.cf.preflight()
        self.assertFalse(self.cf.write_capable)

    def test_readonly_scope_wrong_zone_deny_and_unknown_do_not_pass(self):
        variants=[
            dict(effect='allow', permission_groups=[{'name':'DNS Read'}], resources={'com.cloudflare.api.account.zone.'+'a'*32:'*'}),
            dict(effect='allow', permission_groups=[{'name':'DNS Write'}], resources={'com.cloudflare.api.account.zone.'+'e'*32:'*'}),
            dict(effect='deny', permission_groups=[{'name':'DNS Write'}], resources={'com.cloudflare.api.account.zone.*':'*'}),
            dict(effect='allow', permission_groups=[{'id':'unknown'}], resources={'com.cloudflare.api.account.zone.*':'*'}),
        ]
        for policy in variants:
            with self.subTest(policy=policy):
                self.details['policies']=[policy]
                with patch.object(self.cf,'api',side_effect=self.api):
                    with self.assertRaises(ValueError): self.cf.preflight()
                self.assertFalse(self.cf.write_capable)

    def test_missing_credentials_and_malformed_ids(self):
        for env in ({}, dict(LIVE,CF_ZONE_ID='wrong'),dict(LIVE,CF_RECORD_ID='')):
            with patch.dict(os.environ,env,clear=True): cf=Cloudflare(False)
            with patch.object(cf,'api') as api:
                with self.assertRaises(ValueError): cf.preflight()
                api.assert_not_called()

    def test_invalid_record_and_inactive_zone(self):
        for update in ({'proxied':True},{'type':'AAAA'},{'content':'8.8.8.8'},{'id':'e'*32}):
            with self.subTest(update=update):
                old=dict(self.record); self.record.update(update)
                with patch.object(self.cf,'api',side_effect=self.api):
                    with self.assertRaises(ValueError): self.cf.preflight()
                self.record=old
        self.zone['status']='pending'
        with patch.object(self.cf,'api',side_effect=self.api):
            with self.assertRaises(ValueError): self.cf.preflight()

    def test_live_switch_and_readback(self):
        with patch.object(self.cf,'api',side_effect=self.api): self.cf.switch(IPS[0],IPS[1])
        self.assertEqual([x[1] for x in self.calls if x[2]=='PATCH'],[{'content':IPS[1]}])
        self.assertEqual(self.record['content'],IPS[1])

    def test_last_boundary_callback_blocks_before_patch(self):
        def expired(): raise DNSWriteBlocked('样本已过期')
        with patch.object(self.cf,'api',side_effect=self.api):
            with self.assertRaisesRegex(DNSWriteBlocked,'过期'):
                self.cf.switch(IPS[0],IPS[1],before_write=expired)
        self.assertFalse(any(method=='PATCH' for _,_,method in self.calls))

    def test_account_details_fallback_when_user_verify_accepts_token(self):
        original=self.api
        def api(path, payload=None, method=None):
            if path == '/user/tokens/'+'c'*32: raise RuntimeError('details only account scoped')
            return original(path,payload,method)
        with patch.object(self.cf,'api',side_effect=api): self.cf.preflight()
        self.assertTrue(self.cf.write_capable)
        self.assertTrue(any(p.startswith('/accounts/'+'d'*32+'/tokens/') for p,_,_ in self.calls))

    def test_failure_and_wrong_readback_are_not_success(self):
        for fail,wrong in ((True,False),(False,True)):
            with self.subTest(fail=fail,wrong=wrong):
                self.fail_patch,self.readback_wrong=fail,wrong
                with patch.object(self.cf,'api',side_effect=self.api):
                    with self.assertRaises((RuntimeError,TimeoutError)): self.cf.switch(IPS[0],IPS[1])

    def test_prewrite_permission_failure_is_definitely_not_attempted(self):
        self.no_policy=True
        with patch.object(self.cf,'api',side_effect=self.api):
            with self.assertRaises(DNSWriteBlocked): self.cf.switch(IPS[0],IPS[1])
        self.assertFalse(any(method=='PATCH' for _,_,method in self.calls))


class RuntimeModeTest(unittest.TestCase):
    setUp=fixtures.GuardTest.setUp
    tearDown=fixtures.GuardTest.tearDown
    sample=fixtures.GuardTest.sample
    step=fixtures.GuardTest.step
    three=fixtures.GuardTest.three

    def events(self, kind):
        return [r[0] for r in self.e.db.execute('SELECT message FROM events WHERE kind=?',(kind,))]

    def test_simulation_event_is_explicit_and_queued_to_both_channels(self):
        self.e.dry_run=True
        self.e.channels=('telegram','feishu')
        self.three()
        self.assertIn('检测到切换条件，但当前为模拟模式，DNS 未修改',self.events('dry_run_switch')[0])
        self.assertEqual(self.dns.writes,[])
        self.assertEqual(self.e.availability()['state'],'simulate')
        self.assertFalse(self.e.availability()['available'])
        self.assertIn('oci_guard_mode_simulate 1',self.e.metrics())
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM outbox JOIN events ON outbox.event_id=events.id WHERE events.kind=?',('dry_run_switch',)).fetchone()[0],2)

    def test_confirmation_error_blocks_even_manual_requests(self):
        self.e.mode=resolve_mode({'RUN_MODE':'live'})
        self.e.config_error=self.e.mode.error
        self.three()
        self.assertEqual(self.e.manual(IPS[1]),'inhibited')
        self.assertEqual(self.dns.writes,[])
        self.assertEqual(self.e.availability()['state'],'unavailable')
        self.assertIn('configuration',{r['code'] for r in self.e.availability()['reasons']})

    def test_permissions_fail_closed_with_actionable_status(self):
        with patch.object(self.dns,'preflight',side_effect=ValueError('权限未知：请检查 Token policy')):
            self.assertEqual(self.three()[-1],'inhibited')
        self.assertEqual(self.dns.writes,[])
        self.assertIn('权限未知',self.events('switch_blocked')[-1])
        self.assertIn('oci_guard_dns_write_capable 0',self.e.metrics())

    def test_known_prewrite_block_clears_intent_never_claims_switch(self):
        def blocked(old,new,before_write=None):
            self.dns.ip=new  # unrelated external edit must not become our success
            raise DNSWriteBlocked('权限检查拒绝，未发送 PATCH')
        with patch.object(self.dns,'switch',side_effect=blocked):
            self.assertEqual(self.three()[-1],'inhibited')
        self.assertIsNone(self.e.s['pending'])
        self.assertEqual(self.events('dns_switch'),[])
        self.assertEqual(self.dns.writes,[])
        self.assertTrue(self.events('switch_blocked'))

    def test_slow_prewrite_validation_cannot_use_expired_samples(self):
        def slow(old,new,before_write=None):
            self.now+=151
            before_write()
            self.dns.writes.append((old,new))
        with patch.object(self.dns,'switch',side_effect=slow):
            self.assertEqual(self.three()[-1],'inhibited')
        self.assertEqual(self.dns.writes,[])
        self.assertIsNone(self.e.s['pending'])
        self.assertIn('过期',self.events('switch_blocked')[-1])

    def test_available_only_after_fresh_samples_and_healthy_backup(self):
        self.assertFalse(self.e.availability()['available'])
        self.three(up=(True,True,True))
        self.assertTrue(self.e.availability()['available'])
        self.assertEqual(self.e.availability()['message'],'DNS 自动切换已启用')
        self.assertIn('oci_guard_auto_switch_available 1',self.e.metrics())
        self.now+=151
        self.assertFalse(self.e.availability()['available'])
        self.assertIn('sample_stale',{r['code'] for r in self.e.availability()['reasons']})

    def test_dns_failure_notification_and_pending_protection(self):
        self.dns.uncertain=True
        self.assertEqual(self.three()[-1],'pending_reconciliation')
        self.assertIn('DNS 写入失败或结果不确定',self.events('dns_write_failed')[0])
        self.assertIn('pending_write',{r['code'] for r in self.e.availability()['reasons']})
        self.assertEqual(self.events('dns_switch'),[])
        self.now+=901;self.three()
        self.assertEqual(len(self.dns.writes),1)

    def test_success_event_contains_old_new_and_readback(self):
        self.three()
        event=self.events('dns_switch')[0]
        self.assertIn(IPS[0],event);self.assertIn(IPS[1],event);self.assertIn('读回已确认',event)

    def test_readonly_full_preflight_checks_mapping_internet_and_backup(self):
        class Prober:
            def __init__(self,report): self.report=report
            def cycle(self): return self.report
        for kwargs,reason in (({'direct':False},'映射'),({'internet':False},'出口'),({'up':(True,False,False)},'备用')):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError,reason): check(self.dns,Prober(self.sample(**kwargs)))
        result=check(self.dns,Prober(self.sample()))
        self.assertTrue(result['ok']);self.assertEqual(self.dns.writes,[])

    def test_health_separates_process_from_switch_readiness(self):
        from guard.server import handler_for
        class Socket:
            def __init__(self):self.data=io.BytesIO(b'GET /healthz HTTP/1.0\r\n\r\n');self.output=b''
            def makefile(self,*args):return self.data
            def settimeout(self,*args):pass
            def sendall(self,data):self.output+=data
        sock=Socket()
        handler_for(self.e,None,'a'*32,[time.monotonic()],[0.0])(sock,('127.0.0.1',1),None)
        response=json.loads(sock.output.split(b'\r\n\r\n')[1])
        self.assertTrue(response['worker_healthy'])
        self.assertFalse(response['probe_worker_healthy'])
        self.assertFalse(response['auto_switch']['available'])
        self.assertEqual(response['auto_switch']['message'],'自动切换不可用')


if __name__=='__main__':unittest.main()
