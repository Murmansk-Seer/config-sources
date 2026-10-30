import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'check_downstream_publication', ROOT / '.github/scripts/check_downstream_publication.py'
)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class DownstreamPublicationTests(unittest.TestCase):
    def setUp(self):
        self.source = 'a' * 40
        self.state = {
            'config': self.source, 'sha256': 'b' * 64,
            'fingerprint': 'c' * 64, 'source_commit': 'd' * 40,
        }

    def test_upstream_merge_requires_dispatch_even_if_refresh_is_unchanged(self):
        state = {**self.state, 'config': 'e' * 40}
        self.assertTrue(CHECK.notification_required(self.source, state, 'c' * 64))

    def test_failed_dispatch_is_retried_after_sync_reports_current(self):
        self.assertTrue(CHECK.notification_required(self.source, self.state, 'e' * 64))

    def test_verified_and_acknowledged_source_does_not_dispatch(self):
        self.assertFalse(CHECK.notification_required(self.source, self.state, 'c' * 64 + '\n'))

    def test_missing_publication_or_receipt_retries(self):
        self.assertTrue(CHECK.notification_required(self.source, None, None))
        self.assertTrue(CHECK.notification_required(self.source, self.state, None))

    def test_incomplete_publication_is_not_a_success(self):
        for field in ('sha256', 'source_commit', 'fingerprint'):
            with self.subTest(field=field):
                self.assertTrue(CHECK.notification_required(
                    self.source, {**self.state, field: ''}, 'c' * 64
                ))

    def test_http_failure_is_not_treated_as_unchanged(self):
        failed = subprocess.CompletedProcess([], 1, '', 'HTTP 503 unavailable')
        with patch.object(CHECK.subprocess, 'run', return_value=failed):
            with self.assertRaises(RuntimeError):
                CHECK.read_remote_file('publication')

    def test_http_404_is_an_unpublished_source(self):
        missing = subprocess.CompletedProcess([], 1, '', 'gh: Not Found (HTTP 404)')
        with patch.object(CHECK.subprocess, 'run', return_value=missing):
            self.assertIsNone(CHECK.read_remote_file('publication'))

    def test_remote_content_is_decoded(self):
        value = json.dumps({'content': base64.b64encode(b'publication').decode()})
        with patch.object(CHECK.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, value, '')):
            self.assertEqual(CHECK.read_remote_file('publication'), 'publication')

    def test_workflow_dispatch_is_not_gated_by_gems_refresh(self):
        workflow = (ROOT / '.github/workflows/sync-upstream.yml').read_text()
        step = workflow.split('- name: Dispatch api-data build', 1)[1].split('needs: sync', 1)[0]
        self.assertIn("if: steps.downstream.outputs.required == 'true'", step)
        self.assertNotIn('steps.check.outputs.skip', step)
        reconcile = workflow.split('- name: Reconcile downstream publication', 1)[1].split('- name:', 1)[0]
        self.assertNotIn('if:', reconcile)
        self.assertIn('!inputs.dry_run', workflow)


if __name__ == '__main__':
    unittest.main()
