"""Offline unit and CLI integration tests; run sequentially with unittest."""

from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import speedtest


PAYLOAD = b"x" * (128 * 1024 + 123)
SCRIPT = Path(__file__).with_name("speedtest.py")


class Handler(BaseHTTPRequestHandler):
    events = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/missing":
            self.send_error(404)
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/file")
            self.end_headers()
            return
        if self.path == "/slow":
            time.sleep(0.15)
        self.send_response(200)
        if self.path != "/no-length":
            length = len(PAYLOAD) + (100 if self.path == "/truncated" else 0)
            self.send_header("Content-Length", str(length))
        self.end_headers()
        try:
            if self.path == "/file":
                Handler.events.append("start")
                self.wfile.write(PAYLOAD[:65536])
                self.wfile.flush()
                time.sleep(0.02)
                Handler.events.append("finish")
                self.wfile.write(PAYLOAD[65536:])
            else:
                self.wfile.write(PAYLOAD)
        except OSError:
            pass  # The timeout test deliberately closes the client connection.


class SpeedtestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proxy_env = patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        cls.proxy_env.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.proxy_env.stop()

    def run_cli(self, path, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), self.url + path, *args],
            text=True, capture_output=True, timeout=10,
        )

    def test_ten_sequential_complete_downloads_and_summary(self):
        Handler.events = []
        result = self.run_cli("/file")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Handler.events, ["start", "finish"] * 10)
        rows = re.findall(r"\[(\d+)/10\] (\d+) bytes in ([\d.]+) s", result.stdout)
        self.assertEqual([int(row[0]) for row in rows], list(range(1, 11)))
        self.assertTrue(all(int(row[1]) == len(PAYLOAD) for row in rows))
        self.assertTrue(all(float(row[2]) >= 0.015 for row in rows))
        self.assertIn(f"Total downloaded: {len(PAYLOAD) * 10} bytes", result.stdout)
        mean = float(re.search(r"Average request time: ([\d.]+) s", result.stdout)[1])
        self.assertAlmostEqual(mean, sum(float(row[2]) for row in rows) / 10, places=5)
        rate = float(re.search(r"Download speed: ([\d.]+) MiB/s", result.stdout)[1])
        self.assertAlmostEqual(rate, len(PAYLOAD) / mean / 1024**2, delta=0.005)

    def test_speed_uses_total_bytes_over_total_time(self):
        samples = [speedtest.Measurement(1, 1_000_000), speedtest.Measurement(3, 9_000_000)] * 5
        output = io.StringIO()
        with patch.object(speedtest, "download", side_effect=samples), redirect_stdout(output):
            self.assertEqual(speedtest.main([self.url + "/file"]), 0)
        self.assertIn("Average request time: 2.000000 s", output.getvalue())
        self.assertIn("Total downloaded: 50000000 bytes", output.getvalue())
        self.assertIn("Download speed: 2.384 MiB/s (20.000 Mbit/s)", output.getvalue())

    def test_http_error_stops_without_summary(self):
        result = self.run_cli("/missing")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Request 1/10 failed", result.stderr)
        self.assertIn("404", result.stderr)
        self.assertNotIn("Download speed:", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_truncated_body_is_an_error(self):
        result = self.run_cli("/truncated")
        self.assertEqual(result.returncode, 1)
        self.assertIn("IncompleteRead", result.stderr)
        self.assertNotIn("Download speed:", result.stdout)

    def test_socket_timeout_is_an_error(self):
        result = self.run_cli("/slow", "--timeout", "0.03")
        self.assertEqual(result.returncode, 1)
        self.assertIn("timed out", result.stderr)
        self.assertNotIn("Download speed:", result.stdout)

    def test_redirects_and_unknown_content_length(self):
        for path in ("/redirect", "/no-length"):
            with self.subTest(path=path):
                result = speedtest.download(self.url + path, 2)
                self.assertEqual(result.size_bytes, len(PAYLOAD))
                self.assertGreater(result.seconds, 0)

    def test_invalid_arguments(self):
        for args in (
            [], ["file:///etc/hosts"], ["https://"], ["http://localhost:bad"],
            ["http://local host/file"], [self.url, "--timeout", "0"],
            [self.url, "--timeout", "-1"], [self.url, "--timeout", "nan"],
            [self.url, "--timeout", "inf"], [self.url, "--timeout", "abc"],
        ):
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    speedtest.main(args)
                self.assertEqual(error.exception.code, 2)

    def test_interruption_has_clear_exit_code(self):
        with patch.object(speedtest, "download", side_effect=KeyboardInterrupt), redirect_stderr(io.StringIO()):
            self.assertEqual(speedtest.main([self.url]), 130)


if __name__ == "__main__":
    unittest.main()
