"""Regression tests for private content, outgoing history and fail-closed setup."""
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('infra_config', ROOT / 'scripts/infra_config.py')
infra = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(infra)


class PrivacyControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.env = os.environ.copy()
        for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL'):
            self.env.pop(key, None)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'EcommerceRAG Maintainers')
        self.git('config', 'user.email', 'maintainers@example.invalid')
        (self.repo / 'README.md').write_text('Public project\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Initial public code')

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, data=None, check=True):
        return subprocess.run(['git', *args], cwd=self.repo, input=data, env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)

    def guard(self, *args, data=None):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/privacy_guard.py'), *args],
                              cwd=self.repo, input=data, env=self.env, capture_output=True)

    def test_clean_history(self):
        self.assertEqual(self.guard('--history').returncode, 0)

    def test_force_added_private_document(self):
        (self.repo / '.gitignore').write_text('docs/\n')
        (self.repo / 'docs').mkdir()
        (self.repo / 'docs/design.md').write_text('Private design')
        self.git('add', '-f', 'docs/design.md')
        self.assertEqual(self.guard().returncode, 1)

    def test_values_not_printed(self):
        samples = ['sk-'+'x'*32, 'tvly-'+'x'*32, '.'.join(['10', '27', '5', '19']), 'C'+':/Users/example/project']
        for value in samples:
            (self.repo / 'settings.txt').write_text(value)
            self.git('add', 'settings.txt')
            result = self.guard()
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(value.encode(), result.stdout + result.stderr)

    def test_deleted_secret_in_history_blocks_push(self):
        (self.repo / 'settings.txt').write_text('sk-'+'x'*32)
        self.git('add', '.')
        self.git('commit', '-qm', 'Fixture containing private value')
        self.git('rm', '-q', 'settings.txt')
        self.git('commit', '-qm', 'Remove fixture')
        self.assertEqual(self.guard().returncode, 0)
        self.assertEqual(self.guard('--history').returncode, 1)

    def test_unreferenced_outgoing_commit_is_checked(self):
        clean = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        (self.repo / 'settings.txt').write_text('sk-'+'x'*32)
        self.git('add', '.')
        tree = self.git('write-tree').stdout.decode().strip()
        orphan = self.git('commit-tree', tree, data=b'Outgoing fixture\n').stdout.decode().strip()
        self.git('read-tree', clean)
        self.assertEqual(self.guard('--history').returncode, 0)
        update = f'HEAD {orphan} refs/heads/test {"0"*40}\n'.encode()
        self.assertEqual(self.guard('--pre-push', data=update).returncode, 1)

    def test_environment_author_override_blocked(self):
        self.env['GIT_AUTHOR_NAME'] = 'Private author'
        self.assertEqual(self.guard('--identity').returncode, 1)

    @unittest.skipUnless(os.name == 'posix', 'Git Bash and python3 are exercised on the Linux development host')
    def test_real_commit_and_push_hooks(self):
        shutil.copytree(ROOT / '.githooks', self.repo / '.githooks')
        (self.repo / 'scripts').mkdir()
        shutil.copy2(ROOT / 'scripts/privacy_guard.py', self.repo / 'scripts/privacy_guard.py')
        for hook in (self.repo / '.githooks').iterdir():
            hook.chmod(0o755)
        self.git('config', 'core.hooksPath', '.githooks')
        (self.repo / 'AGENTS.md').write_text('Local instructions')
        self.git('add', '.')
        self.assertNotEqual(self.git('commit', '-qm', 'Blocked document', check=False).returncode, 0)
        self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Bypassed commit hook fixture')
        target = self.repo / 'target.git'
        self.git('init', '--bare', '-q', str(target))
        self.assertNotEqual(self.git('push', str(target), 'main', check=False).returncode, 0)
        self.assertEqual(self.git('--git-dir='+str(target), 'show-ref', check=False).returncode, 1)


class PrivateConfig(unittest.TestCase):
    @contextmanager
    def configuration(self, text=None, mode=0o600):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'infra.env'
            if text is not None:
                path.write_text(text)
                path.chmod(mode)
            with patch.dict(os.environ, {'ECR_INFRA_ENV': str(path)}, clear=True):
                yield path

    def test_missing_rejected(self):
        with self.configuration():
            with self.assertRaises(ValueError):
                infra.require('ECR_DB_PRIVATE_IP')

    def test_bad_address_and_uuid_rejected(self):
        for value in ('0.0.0.0', '127.0.0.1', '192.0.2.20', ''):
            with self.configuration('ECR_DB_PRIVATE_IP='+value):
                with self.assertRaises(ValueError):
                    infra.require('ECR_DB_PRIVATE_IP')
        with self.configuration('ECR_DATA_UUID='+'0'*8+'-'+('0'*4+'-')*3+'0'*12):
            with self.assertRaises(ValueError):
                infra.require('ECR_DATA_UUID')

    def test_valid_private_address(self):
        value = '.'.join(['10', '27', '5', '19'])
        with self.configuration('ECR_DB_PRIVATE_IP='+value):
            self.assertEqual(infra.require('ECR_DB_PRIVATE_IP'), value)

    @unittest.skipUnless(os.name == 'posix', 'POSIX configuration ownership and permission checks')
    def test_world_readable_rejected(self):
        with self.configuration('ECR_DB_PRIVATE_IP='+'.'.join(['10', '27', '5', '19']), 0o644):
            with self.assertRaises(ValueError):
                infra.require('ECR_DB_PRIVATE_IP')

    @unittest.skipUnless(os.name == 'posix', 'Shell deployment guards require Linux')
    def test_missing_configuration_stops_shell_actions(self):
        with self.configuration():
            for script in ('bootstrap-dev.sh', 'check-data-mount.sh', 'db-firewall.sh'):
                result = subprocess.run(['bash', str(ROOT / 'scripts' / script)], capture_output=True)
                self.assertNotEqual(result.returncode, 0, script)
                self.assertIn(b'UNVERIFIED infrastructure configuration', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
