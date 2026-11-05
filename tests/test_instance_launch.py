import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from imagegen.instance import InstanceLock
from imagegen.launch import reuse_running


class InstanceLaunchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.response = {'version': 2, 'scoped_scene_batches': True}
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.assertEqual(self.path, '/api/editorial-capabilities')
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps(fixture.response).encode())
            def log_message(self, *args): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.browser = patch('imagegen.launch.webbrowser.open').start()
        self.addCleanup(patch.stopall)

    def hold(self, record=True):
        lock = InstanceLock(self.root / 'worker.lock')
        self.addCleanup(lock.close)
        if record: lock.record_port(self.server.server_port)
        return lock

    def test_second_start_opens_existing_port_without_replacing_worker(self):
        self.hold()
        self.assertEqual(reuse_running(self.root, 8765, True), 0)
        self.browser.assert_called_once_with(f'http://127.0.0.1:{self.server.server_port}')

    def test_no_browser_reuses_instance_without_opening_tab(self):
        self.hold()
        self.assertEqual(reuse_running(self.root, 8765, False), 0)
        self.browser.assert_not_called()

    def test_legacy_one_byte_lock_reuses_requested_port(self):
        self.hold(record=False)
        self.assertEqual(reuse_running(self.root, self.server.server_port, True), 0)

    def test_unlocked_or_missing_lock_allows_normal_start(self):
        self.assertEqual(reuse_running(self.root, self.server.server_port, True), 10)
        with InstanceLock(self.root / 'worker.lock') as lock: lock.record_port(self.server.server_port)
        self.assertEqual(reuse_running(self.root, self.server.server_port, True), 10)
        self.browser.assert_not_called()

    def test_unrelated_http_service_does_not_open_as_program(self):
        self.hold()
        self.response = {'other_service': True}
        self.assertEqual(reuse_running(self.root, 8765, True), 1)
        self.browser.assert_not_called()

    def test_locked_instance_not_listening_reports_failure_without_traceback(self):
        lock = self.hold()
        lock.record_port(1)
        self.assertEqual(reuse_running(self.root, 8765, True), 1)
        self.browser.assert_not_called()


if __name__ == '__main__': unittest.main()
