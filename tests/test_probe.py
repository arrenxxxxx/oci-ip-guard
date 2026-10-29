import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from guard.common import IPS, PORT, MONITOR_ID
from guard.engine import Engine
from guard import probe
from test_guard import DNS


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ,{'SOURCE_IPS':'10.0.0.10,10.0.0.20,10.0.0.30'})
        self.env.start()
    def tearDown(self):
        self.env.stop()
    def test_reject_mismatched_duplicate_or_loopback_sources(self):
        for value in ['10.0.0.1','10.0.0.1,10.0.0.1,10.0.0.3','127.0.0.1,10.0.0.2,10.0.0.3']:
            with self.assertRaises(ValueError): probe.source_map(value)
    def test_targets_require_unique_votes_and_safe_metric_labels(self):
        for rows in [probe.DEFAULT_TARGETS[:2],probe.DEFAULT_TARGETS+[probe.DEFAULT_TARGETS[0]],
                     [dict(t,name='bad"label') for t in probe.DEFAULT_TARGETS]]:
            with self.assertRaises(ValueError):probe.targets_config(json.dumps(rows))
    def test_mapping_mismatch_blocks_cycle(self):
        p=probe.Prober()
        with patch.object(probe,'public_ip',return_value='1.2.3.4'),patch.object(probe,'resolve',return_value='1.1.1.1'), \
             patch.object(probe,'tcp',return_value={'up':True,'latency_ms':5}) as tcp:
            report=p.cycle()
        self.assertFalse(report['direct_ok'])
        self.assertTrue(all(v['regions']==[] for v in report['samples'].values()))
        self.assertEqual(tcp.call_count,2)  # Only the default-egress controls ran.
    def test_source_mapping_requires_two_agreeing_services(self):
        p=probe.Prober(); calls=[]
        reverse={v:k for k,v in p.sources.items()}
        def echo(source,endpoint):
            calls.append((source,endpoint))
            return reverse[source] if endpoint==probe.ECHOES[0] else '1.2.3.4'
        with patch.object(probe,'public_ip',side_effect=echo):
            self.assertFalse(p.verify_mapping())
        self.assertEqual(len(calls),6)
    def test_mapping_cache_expires_and_failures_retry(self):
        now=[1000]; p=probe.Prober(clock=lambda:now[0]); reverse={v:k for k,v in p.sources.items()}
        with patch.object(probe,'public_ip',side_effect=lambda src,e:reverse[src]) as echo:
            self.assertTrue(p.verify_mapping());self.assertTrue(p.verify_mapping())
            self.assertEqual(echo.call_count,6)
            now[0]+=301;self.assertTrue(p.verify_mapping());self.assertEqual(echo.call_count,12)
        with patch.object(probe,'public_ip',side_effect=TimeoutError()) as echo:
            now[0]+=301;self.assertFalse(p.verify_mapping());self.assertFalse(p.verify_mapping())
            self.assertEqual(echo.call_count,6)
    def test_fixed_denominator_and_source_bound_calls(self):
        p=probe.Prober()
        hosts={t['host']:'1.1.1.'+str(i+1) for i,t in enumerate(p.targets)}
        def tcp(source,address,port):
            up=address!='1.1.1.2'
            return {'up':up,'latency_ms':10 if up else None}
        with patch.object(p,'verify_mapping',return_value=True),patch.object(probe,'resolve',side_effect=hosts.__getitem__), \
             patch.object(probe,'tcp',side_effect=tcp) as call:
            report=p.cycle()
        self.assertEqual(report['samples'][IPS[0]]['success_ratio'],2/3)
        self.assertFalse(report['samples'][IPS[0]]['up'])
        self.assertEqual({c.args[0] for c in call.call_args_list},{None,*p.sources.values()})
    def test_dns_error_is_unknown_not_failed_vote(self):
        with patch.object(probe.Prober,'verify_mapping',return_value=True),patch.object(probe,'resolve',side_effect=OSError()):
            with self.assertRaises(OSError):probe.Prober().cycle()
    def test_socket_binds_source_and_ignores_proxy_environment(self):
        with patch.dict(os.environ,{'HTTPS_PROXY':'http://127.0.0.1:1'}),patch.object(probe.socket,'socket') as constructor:
            sock=constructor.return_value.__enter__.return_value
            self.assertTrue(probe.tcp('10.0.0.10','1.1.1.1',443)['up'])
            sock.bind.assert_called_once_with(('10.0.0.10',0));sock.connect.assert_called_once_with(('1.1.1.1',443))


class RegionalDecisions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=2000000000.0;self.dns=DNS()
        self.out=redirect_stdout(io.StringIO());self.out.__enter__()
        self.e=Engine(self.temp.name+'/guard.db',self.dns,False,clock=lambda:self.now)
    def tearDown(self):
        self.e.db.close();self.temp.cleanup();self.out.__exit__(None,None,None)
    def step(self,ratios,mapping=True,internet=True):
        self.now+=60
        return self.e.report(dict(agent=MONITOR_ID,observed_at=self.now,direct_ok=mapping,internet_ok=internet,
            samples={ip:dict(up=r>=.8,success_ratio=r,port=PORT,latency_ms=100 if r else None) for ip,r in zip(IPS,ratios)}))
    def test_thresholds_and_degraded_resets_both_streaks(self):
        self.step([.49,1,1]);self.step([.49,1,1]);self.step([.5,1,1])
        self.assertEqual(self.e.s['samples'][IPS[0]]['fail'],0)
        for _ in range(3): self.step([.49,.79,.8])
        self.assertEqual(self.dns.writes,[(IPS[0],IPS[2])])
    def test_degraded_backup_never_qualifies(self):
        for _ in range(4): self.step([0,.79,.79])
        self.assertEqual(self.dns.writes,[])
    def test_all_failed_mapping_failure_and_internet_loss_inhibit(self):
        for ratios,mapping,internet in [([0,0,0],True,True),([0,1,1],False,True),([0,1,1],True,False)]:
            for _ in range(3): self.step(ratios,mapping,internet)
        self.assertEqual(self.dns.writes,[])
    def test_dry_run_ratio_event_without_dns_write(self):
        self.e.dry_run=True
        for _ in range(3): result=self.step([0,1,1])
        self.assertEqual(result,'dry_run');self.assertEqual(self.dns.writes,[])
        self.assertEqual(self.e.s['current'],IPS[0])
    def test_nonfinite_and_inconsistent_ratio_rejected(self):
        for value in [float('nan'),float('inf'),-.1,1.1]:
            with self.assertRaises(ValueError): self.step([value,1,1])
    def test_stale_samples_block_manual_switch(self):
        for _ in range(3):self.step([1,1,1])
        self.now+=151;self.e.tick()
        self.assertEqual(self.e.manual(IPS[1]),'inhibited');self.assertEqual(self.dns.writes,[])
    def test_metrics_include_regional_ratio(self):
        self.step([1,2/3,0])
        self.assertIn('oci_guard_regional_success_ratio{ip="'+IPS[1]+'"} 0.666',self.e.metrics())

if __name__=='__main__': unittest.main()
