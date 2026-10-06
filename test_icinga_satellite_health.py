import contextlib
import io
import json
import re
import unittest
from unittest.mock import patch

import icinga_satellite_health as h


class HealthTests(unittest.TestCase):
    def test_status_matrix(self):
        cases = [(True, None, True, 'OK'), (True, 0, False, 'OK'),
                 (True, 9.99, False, 'OK'), (True, 10, False, 'WARNING'),
                 (True, 30, False, 'CRITICAL'), (False, 0, True, 'WARNING'),
                 (False, 0, False, 'CRITICAL'), (False, None, None, 'CRITICAL')]
        for connected, lag, tcp, expected in cases:
            with self.subTest(connected=connected, lag=lag, tcp=tcp):
                self.assertEqual(h.calculate_status(connected, lag, tcp, 10, 30)[0], expected)

    def test_native_lag(self):
        attrs = {'connected': True, 'syncing': False, 'remote_log_position': 20}
        self.assertEqual(h.cluster_lag(attrs, 100), 0)
        attrs['syncing'] = True
        self.assertEqual(h.cluster_lag(attrs, 100), 80)
        attrs['remote_log_position'] = 0
        self.assertEqual(h.cluster_lag(attrs, 100), 0)
        attrs['remote_log_position'] = float('nan')
        self.assertIsNone(h.cluster_lag(attrs, 100))
        self.assertIsNone(h.cluster_lag({}, 100))

    def test_aggregation(self):
        self.assertEqual(h.aggregate_status(['OK', 'WARNING']), 'WARNING')
        self.assertEqual(h.aggregate_status(['WARNING', 'CRITICAL']), 'CRITICAL')
        self.assertEqual(h.aggregate_status(['CRITICAL', 'UNKNOWN']), 'UNKNOWN')

    def run_mock(self, endpoints, zones, extra=None):
        with patch.object(h, 'credentials', return_value=('user', 'secret')), \
             patch.object(h, 'IcingaAPI') as api, patch.object(h, 'test_tcp') as tcp:
            api.return_value.endpoints.return_value = endpoints
            api.return_value.zones.return_value = zones
            result = h.run_check(h.parse_args(['--no-tcp'] + (extra or [])))
            tcp.assert_not_called()
            return result

    def fixtures(self):
        endpoints, zones = {}, {}
        for satellites in h.SATELLITES.values():
            for s in satellites:
                endpoints[s['endpoint']] = {'connected': True}
                zones.setdefault(s['zone'], {'endpoints': []})['endpoints'].append(s['endpoint'])
        return endpoints, zones

    def test_all_connected_without_lag_and_tcp(self):
        report = self.run_mock(*self.fixtures())
        self.assertEqual(report['status'], 'OK')
        self.assertEqual(set(report['regions']), {'EMEA', 'APAC', 'AMER'})
        self.assertIn('ok=6', h.format_icinga(report, 10, 30))
        for r in report['regions'].values():
            for s in r['satellites']:
                self.assertIsNone(s['cluster_lag'])
                self.assertIsNone(s['tcp_reachable'])

    def test_missing_or_malformed_runtime_is_unknown(self):
        for value in (None, 'false', 0):
            endpoints, zones = self.fixtures()
            endpoints['emea-sat-01']['connected'] = value
            self.assertEqual(self.run_mock(endpoints, zones)['status'], 'UNKNOWN')

    def test_missing_zone_or_membership_is_unknown(self):
        endpoints, zones = self.fixtures()
        zones['backup-emea']['endpoints'] = []
        self.assertEqual(self.run_mock(endpoints, zones)['status'], 'UNKNOWN')
        del zones['backup-emea']
        self.assertEqual(self.run_mock(endpoints, zones)['status'], 'UNKNOWN')

    def test_api_failure_json(self):
        with patch.object(h, 'run_check', side_effect=h.CheckError('Master API unavailable')), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(h.main(['--json']), 3)
        report = json.loads(out.getvalue())
        self.assertEqual(report['regions'], {})
        self.assertEqual(report['status'], 'UNKNOWN')

    def test_bad_arguments_json(self):
        for args in (['--tcp-timeout', 'nan'], ['--api-url', 'http://localhost'],
                     ['--lag-warning', '40'], ['--bad-argument']):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(h.main(['--json'] + args), 3)
            self.assertEqual(json.loads(out.getvalue())['status'], 'UNKNOWN')

    def test_web_output_preserves_metrics(self):
        report = self.run_mock(*self.fixtures())
        web = h.format_icinga(report, 10, 30)
        self.assertIn("🟢 OK", web)
        self.assertNotIn("\033", web)
        self.assertEqual(web.splitlines()[0].split(" | ")[1],
                         "ok=6 warning=0 critical=0 unknown=0")
        self.assertEqual(h.web_status('WARNING'), '🟡 WARNING')
        self.assertEqual(h.web_status('CRITICAL'), '🔴 CRITICAL')
        self.assertEqual(h.web_status('UNKNOWN'), '🟣 UNKNOWN')

    def test_regional_tables_escape_names_and_keep_unknown_values(self):
        report = self.run_mock(*self.fixtures())
        satellite = report['regions']['EMEA']['satellites'][0]
        satellite['hostname'] = '<script>alert(1)</script>'
        output = h.format_icinga(report, 10, 30)
        output = re.sub(r' style="[^"]*"', "", output)
        self.assertEqual(output.count('<table>'), 3)
        self.assertEqual(output.count('</table>'), 3)
        self.assertEqual(output.count('<td>Non testé</td>'), 6)
        self.assertEqual(output.count('<td>N/D</td>'), 6)
        self.assertNotIn('<script>', output)
        self.assertIn('&lt;script&gt;', output)
        self.assertNotIn('<table>', output.splitlines()[0])
        self.assertEqual(output.count('|'), 1)
        satellite['tcp_reachable'] = False
        satellite['cluster_connected'] = False
        satellite['tcp_latency_ms'] = 0.0
        satellite['cluster_lag'] = 0.0
        output = h.format_icinga(report, 10, 30)
        output = re.sub(r' style="[^"]*"', "", output)
        self.assertIn('<td>Déconnecté</td>', output)
        self.assertIn('<td>Indisponible</td>', output)
        self.assertIn('<td>0.00 ms</td>', output)
        self.assertIn('<td>0.00 s</td>', output)

    def test_web_output_error_and_json(self):
        for args in ([], ['--json']):
            with patch.object(h, 'run_check', side_effect=h.CheckError('API unavailable')), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(h.main(args), 3)
            if '--json' in args:
                self.assertEqual(json.loads(out.getvalue())['status'], 'UNKNOWN')
                self.assertNotIn('🟣', out.getvalue())
            else:
                self.assertTrue(out.getvalue().startswith('🟣 UNKNOWN'))

    def test_tcp_failure_is_not_fatal(self):
        with patch.object(h.socket, 'create_connection', side_effect=OSError):
            self.assertEqual(h.test_tcp('example', 3), (False, None))

    def test_exception_secret_not_exposed(self):
        with patch.object(h, 'run_check', side_effect=ValueError('secret-password')), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(h.main(['--json']), 3)
        self.assertNotIn('secret-password', out.getvalue())


if __name__ == '__main__':
    unittest.main()
