"""Static deployment/monitoring contracts. Never connects to Cloudflare."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent


class MonitoringContractTest(unittest.TestCase):
    def test_example_requires_explicit_mode(self):
        values = {}
        for line in (ROOT / 'deploy/controller/.env.example').read_text().splitlines():
            if line and not line.startswith('#'):
                key, value = line.split('=', 1)
                values[key] = value
        self.assertEqual(values['RUN_MODE'], '')
        self.assertEqual(values['ENABLE_DNS_WRITES'], '')
        self.assertNotIn('DRY_RUN', values)

    def test_compose_does_not_override_mode_or_confirmation(self):
        compose = (ROOT / 'deploy/controller/compose.yaml').read_text()
        self.assertIn('env_file: .env', compose)
        self.assertNotIn('RUN_MODE', compose)
        self.assertNotIn('DRY_RUN', compose)
        self.assertNotIn('ENABLE_DNS_WRITES', compose)

    def test_dashboard_prioritizes_actual_availability(self):
        dashboard = json.loads((ROOT / 'monitoring/dashboard.json').read_text())
        panels = dashboard['panels']
        first = panels[0]
        self.assertEqual(first['gridPos']['y'], 0)
        self.assertIn('oci_guard_auto_switch_available', first['targets'][0]['expr'])
        self.assertIn('oci_guard_mode_simulate', first['targets'][0]['expr'])
        self.assertTrue(first['targets'][0]['instant'])
        labels = first['fieldConfig']['defaults']['mappings'][0]['options']
        self.assertEqual(labels['0']['text'], '自动切换不可用')
        self.assertEqual(labels['1']['text'], '模拟模式：故障时不会修改 DNS')
        self.assertEqual(labels['2']['text'], 'DNS 自动切换已启用')
        self.assertIn('无当前数据', first['fieldConfig']['defaults']['noValue'])
        expressions = ' '.join(t['expr'] for p in panels for t in p['targets'])
        for metric in ('oci_guard_worker_healthy', 'oci_guard_sample_valid',
                       'oci_guard_dns_record_ready', 'oci_guard_auto_switch_blocked'):
            self.assertIn(metric, expressions)
        self.assertEqual(len({p['id'] for p in panels}), len(panels))

    def test_alerts_distinguish_simulation_success_and_failure(self):
        alerts = (ROOT / 'monitoring/alerts.yaml').read_text()
        for name in ('OciGuardControllerDown', 'OciGuardNotificationFailure',
                     'OciGuardSimulationMode', 'OciGuardAutoSwitchUnavailable',
                     'OciGuardSimulatedFailover', 'OciGuardDnsSwitchConfirmed',
                     'OciGuardDnsWriteFailed', 'OciGuardFailoverBlocked'):
            self.assertIn('alert: ' + name, alerts)
        for kind in ('dry_run_switch', 'dns_switch', 'dns_write_failed', 'switch_blocked'):
            self.assertIn('kind="' + kind + '"', alerts)
        self.assertIn('DNS 未修改', alerts)


if __name__ == '__main__':
    unittest.main()
