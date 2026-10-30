import os
import subprocess
import sys
import unittest


class ConfigTest(unittest.TestCase):
    def test_fixed_egress_cannot_be_reused_as_entry(self):
        for extra in [
            {'FIXED_EGRESS_PUBLIC_IP':'192.0.2.10','FIXED_EGRESS_PRIVATE_IP':'10.0.0.70'},
            {'FIXED_EGRESS_PUBLIC_IP':'192.0.2.90'},
            {'FIXED_EGRESS_PUBLIC_IP':'192.0.2.90','FIXED_EGRESS_PRIVATE_IP':'10.0.0.10',
             'SOURCE_IPS':'10.0.0.10,10.0.0.20,10.0.0.30'},
        ]:
            result=self.run_config(extra, 'from guard.probe import Prober; Prober()')
            self.assertNotEqual(result.returncode,0)

    def run_config(self, values, code='import guard.common'):
        env = {k:v for k,v in os.environ.items() if k not in (
            'ENTRY_IPS','DNS_NAME','MONITOR_ID','PROBE_PORT','PROBE_INTERVAL_SECONDS',
            'STALE_AFTER_SECONDS','MAX_SAMPLE_AGE_SECONDS','FAILURE_THRESHOLD',
            'HEALTHY_THRESHOLD','COOLDOWN_SECONDS','PROBE_TIMEOUT_SECONDS')}
        env.update(values)
        return subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True)

    def test_reject_invalid_targets_ports_and_timings(self):
        for values in ({'ENTRY_IPS':'192.0.2.1'}, {'ENTRY_IPS':'192.0.2.1,192.0.2.1'},
                       {'ENTRY_IPS':'example.com,192.0.2.1'}, {'PROBE_PORT':'0'},
                       {'PROBE_PORT':'65536'}, {'STALE_AFTER_SECONDS':'20'},
                       {'MAX_SAMPLE_AGE_SECONDS':'200'}, {'FAILURE_THRESHOLD':'0'}):
            with self.subTest(values=values):
                self.assertNotEqual(self.run_config(values).returncode, 0)

    def test_nondefault_two_target_failover(self):
        result=self.run_config({'ENTRY_IPS':'192.0.2.44,192.0.2.45','DNS_NAME':'test.example.org',
            'MONITOR_ID':'branch-a','PROBE_PORT':'8443','PROBE_INTERVAL_SECONDS':'30',
            'FAILURE_THRESHOLD':'2','HEALTHY_THRESHOLD':'2','COOLDOWN_SECONDS':'600'}, '''
import tempfile
from guard.common import IPS,PORT,MONITOR_ID,DOMAIN
from guard.engine import Engine
class DNS:
    configured=True
    record_ready=True
    def preflight(self): return self.current()
    ip=IPS[0]
    writes=[]
    def current(self): return self.ip
    def switch(self, old, new, before_write=None):
        if before_write: before_write()
        self.writes.append((old,new)); self.ip=new
dns=DNS()
now=[2000000000.0]
with tempfile.TemporaryDirectory() as tmp:
    e=Engine(tmp+'/state.db',dns,False,clock=lambda:now[0])
    assert PORT==8443 and MONITOR_ID=='branch-a' and DOMAIN=='test.example.org'
    for i in range(2):
        now[0]+=30
        e.report(dict(agent=MONITOR_ID,observed_at=now[0],direct_ok=True,internet_ok=True,
            samples={ip:dict(up=(ip==IPS[1]),success_ratio=1.0 if ip==IPS[1] else 0.0,port=PORT,latency_ms=10) for ip in IPS}))
        assert len(dns.writes)==i
    assert dns.writes==[(IPS[0],IPS[1])]
    e.db.close()
''')
        self.assertEqual(result.returncode,0,result.stderr)


if __name__ == '__main__': unittest.main()
