"""Publication reconciliation against immutable snapshots in a real local Git fixture."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'pipeline_publication_check', ROOT / '.github/scripts/check_downstream_publication.py'
)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)
RUN = subprocess.run


class PipelinePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'state.git'
        self.git('init', '--bare', str(self.repo), before_init=True)
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.source = 'a' * 40
        self.record = {'config': self.source, 'sha256': 'b' * 64,
                       'fingerprint': 'c' * 64, 'source_commit': 'd' * 40}
        self.publication = '.build-state/publication.json'
        self.receipt = '.build-state/notified-fingerprint'
        self.main = self.snapshot({self.publication: '{"config":"stale"}'})
        self.git('update-ref', 'refs/heads/main', self.main)
        self.state = self.snapshot({self.publication: json.dumps(self.record),
                                   self.receipt: 'c' * 64 + '\n'})
        self.git('update-ref', 'refs/heads/pipeline-state', self.state)
        self.requests = []
        self.move_after_publication = False

    def git(self, *args, before_init=False, input=None, check=True):
        command = ['git'] if before_init else ['git', '--git-dir=' + str(self.repo)]
        result = RUN(command + list(args), input=input, text=True, capture_output=True,
                     check=check)
        return result.stdout.strip()

    def snapshot(self, files):
        env = dict(os.environ, GIT_INDEX_FILE=str(self.root / 'fixture-index'))
        empty = self.git('mktree', input='')
        RUN(['git', '--git-dir=' + str(self.repo), 'read-tree', empty], env=env,
            check=True, capture_output=True)
        for path, content in files.items():
            blob = self.git('hash-object', '-w', '--stdin', input=content)
            RUN(['git', '--git-dir=' + str(self.repo), 'update-index', '--add',
                 '--cacheinfo', '100644', blob, path], env=env, check=True,
                capture_output=True)
        result = RUN(['git', '--git-dir=' + str(self.repo), 'write-tree'], env=env,
                     text=True, capture_output=True, check=True)
        return self.git('commit-tree', result.stdout.strip(), input='fixture\n')

    def gh(self, args, **kwargs):
        self.assertEqual(args[:2], ['gh', 'api'])
        endpoint = args[2]
        self.requests.append(endpoint)
        if endpoint.endswith('/git/ref/heads/pipeline-state'):
            result = self.git('rev-parse', '--verify', 'refs/heads/pipeline-state',
                              check=False)
            if not result:
                return subprocess.CompletedProcess(args, 1, '', 'HTTP 404')
            payload = {'object': {'sha': result}}
        else:
            path, revision = endpoint.split('/contents/', 1)[1].split('?ref=', 1)
            self.assertEqual(revision, self.state)
            content = self.git('show', revision + ':' + path, check=False)
            if not content:
                return subprocess.CompletedProcess(args, 1, '', 'HTTP 404')
            payload = {'content': base64.b64encode(content.encode()).decode()}
            if self.move_after_publication and path == self.publication:
                newer = self.snapshot({self.publication: json.dumps({**self.record,
                    'fingerprint': 'e' * 64}), self.receipt: 'e' * 64})
                self.git('update-ref', 'refs/heads/pipeline-state', newer)
                self.move_after_publication = False
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')

    def reconcile(self):
        output = self.root / 'output.env'
        with patch.object(CHECK.subprocess, 'run', side_effect=self.gh), \
                patch.object(CHECK.subprocess, 'check_output', return_value=self.source), \
                patch.object(sys, 'argv', ['check', '--github-output', str(output)]):
            CHECK.main()
        return output.read_text()

    def test_acknowledged_state_skips_despite_stale_main(self):
        self.assertIn('required=false', self.reconcile())
        self.assertEqual(len(self.requests), 3)
        self.assertFalse(any('ref=main' in x for x in self.requests))

    def test_concurrent_state_advance_does_not_mix_receipts(self):
        self.move_after_publication = True
        self.assertIn('required=false', self.reconcile())
        self.assertNotEqual(self.git('rev-parse', 'refs/heads/pipeline-state'), self.state)
        self.assertTrue(all(x.endswith('?ref=' + self.state)
                            for x in self.requests if '/contents/' in x))

    def test_missing_receipt_requires_notification(self):
        self.state = self.snapshot({self.publication: json.dumps(self.record)})
        self.git('update-ref', 'refs/heads/pipeline-state', self.state)
        self.assertIn('required=true', self.reconcile())

    def test_missing_publication_requires_generation(self):
        self.state = self.snapshot({self.receipt: 'c' * 64})
        self.git('update-ref', 'refs/heads/pipeline-state', self.state)
        self.assertIn('required=true', self.reconcile())

    def test_new_source_requires_generation(self):
        self.source = 'f' * 40
        self.assertIn('required=true', self.reconcile())

    def test_missing_state_seed_stops_before_dispatch_output(self):
        self.git('update-ref', '-d', 'refs/heads/pipeline-state')
        with self.assertRaisesRegex(RuntimeError, 'verified seed'):
            self.reconcile()
        self.assertFalse((self.root / 'output.env').exists())
        self.assertEqual(len(self.requests), 1)

    def test_state_server_failure_does_not_create_success_output(self):
        failed = subprocess.CompletedProcess([], 1, '', 'HTTP 503 unavailable')
        with patch.object(CHECK.subprocess, 'run', return_value=failed):
            with self.assertRaisesRegex(RuntimeError, 'state lookup failed'):
                CHECK.read_state_revision()

    def test_invalid_state_reference_rejected(self):
        for sha in ('main', '', 'a' * 39, 'z' * 40):
            with self.subTest(sha=sha), patch.object(CHECK.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0,
                        json.dumps({'object': {'sha': sha}}), '')):
                with self.assertRaises(ValueError):
                    CHECK.read_state_revision()

    def test_bad_publication_json_stops_without_output(self):
        self.state = self.snapshot({self.publication: '{', self.receipt: 'c' * 64})
        self.git('update-ref', 'refs/heads/pipeline-state', self.state)
        with self.assertRaises(json.JSONDecodeError):
            self.reconcile()
        self.assertFalse((self.root / 'output.env').exists())


if __name__ == '__main__':
    unittest.main()
