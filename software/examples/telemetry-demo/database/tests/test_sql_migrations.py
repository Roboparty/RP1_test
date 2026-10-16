import os
import unittest
from unittest.mock import patch

from imports.sql_migrations import read_sql, role_identifier


class PortableRoleTests(unittest.TestCase):
    def test_custom_application_roles_render_for_every_role_bearing_script(self):
        for filename in [
            '003_security_and_logic.sql', '005_security_hardening.sql', '006_mtbf_contract_v1.sql',
            '007_mtbf_contract_v1_projection_fix.sql', '008_campaign_mtbf_contract_v1.sql',
            '010_api_access_status.sql', '012_test_target_parts.sql', '016_account_access_workflow.sql',
            '018_execution_joint_telemetry.sql', '019_execution_history_import.sql',
            '020_edge_collector_ingestion.sql', '024_influx_export_queue.sql',
        ]:
            with self.subTest(filename=filename), patch.dict(os.environ, {
                'RP1_APP_USER': 'ht_app', 'RP1_READONLY_USER': 'ht_readonly',
                'RP1_MIGRATOR_USER': 'ht_admin',
            }):
                rendered = read_sql(filename)
                self.assertNotIn('__RP1_', rendered)
                self.assertIn('"ht_app"', rendered)
                self.assertNotIn('TO rp1_app', rendered)

    def test_role_injection_is_rejected_without_echoing_credential_text(self):
        with patch.dict(os.environ, {'RP1_APP_USER': 'unsafe; DROP ROLE secret'}):
            with self.assertRaisesRegex(RuntimeError, 'RP1_APP_USER must'):
                role_identifier('RP1_APP_USER', 'rp1_app')

    def test_unquoted_default_role_is_not_emitted(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(role_identifier('RP1_APP_USER', 'rp1_app'), '"rp1_app"')


if __name__ == '__main__':
    unittest.main()
