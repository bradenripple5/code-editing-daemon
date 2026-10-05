import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from patch_daemon import Worker, digest, parse_directions, replace_bytes


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'a.html').write_bytes(b'old\r\n')
        (self.root / 'b.js').write_bytes(b'old')
        self.worker = Worker(self.root)

    def request(self, dry_run=False):
        return {'dry_run': dry_run, 'files': [
            {'path': name, 'operations': [{'type': 'REPLACE', 'target': 'old', 'value': 'new'}]}
            for name in ['a.html', 'b.js']]}

    def test_batch_and_line_endings(self):
        self.worker.apply(self.request(True))
        self.assertEqual((self.root / 'b.js').read_bytes(), b'old')
        self.worker.apply(self.request())
        self.assertEqual((self.root / 'a.html').read_bytes(), b'new\r\n')
        self.assertEqual((self.root / 'b.js').read_bytes(), b'new')

    def test_failed_validation_writes_nothing(self):
        request = self.request()
        request['files'][1]['operations'][0]['target'] = 'missing'
        with self.assertRaises(ValueError):
            self.worker.apply(request)
        self.assertEqual((self.root / 'a.html').read_bytes(), b'old\r\n')

    def test_write_failure_rolls_back(self):
        def fail_second(path, data):
            if path.name == 'b.js':
                raise OSError('simulated failure')
            replace_bytes(path, data)
        with patch('patch_daemon.replace_bytes', side_effect=fail_second):
            with self.assertRaises(OSError):
                self.worker.apply(self.request())
        self.assertEqual((self.root / 'a.html').read_bytes(), b'old\r\n')

    def test_stale_and_escaping_paths(self):
        for changes in [{'sha256': digest(b'wrong')}, {'path': '../outside'}, {'path': '.git/config'}]:
            request = self.request()
            request['files'][0].update(changes)
            with self.assertRaises(ValueError):
                self.worker.apply(request)

    def test_strict_legacy_parser(self):
        self.assertEqual(parse_directions('REMOVE\n<<<\nold\n>>>')[0]['type'], 'REMOVE')
        with self.assertRaises(ValueError):
            parse_directions('garbage\nREMOVE\n<<<\nold\n>>>')

    def test_real_daemon_client(self):
        script = str(Path(__file__).with_name('patch_daemon.py'))
        state = self.root / 'connection.json'
        process = subprocess.Popen([sys.executable, script, 'serve', '--root', str(self.root), '--state', str(state)])
        try:
            deadline = time.monotonic() + 10
            while not state.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail('Daemon failed to start')
                time.sleep(0.05)
            request = self.root / 'request.json'
            request.write_text(json.dumps(self.request()), encoding='utf-8')
            result = subprocess.run([sys.executable, script, 'submit', str(request), '--state', str(state)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['ok'])
            self.assertEqual((self.root / 'b.js').read_bytes(), b'new')
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
