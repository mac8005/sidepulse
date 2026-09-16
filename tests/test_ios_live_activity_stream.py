from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_completion_reaches_list_and_live_activity(tmp_path):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("Swift/macOS required")
    binary = tmp_path / "stream-test"
    subprocess.run([
        "swiftc", "-swift-version", "5", "-module-cache-path", str(tmp_path / "modules"),
        "-o", str(binary),
        str(ROOT / "ios/SidePulse/SidePulse/AgentStreamClient.swift"),
        str(ROOT / "tests/test_ios_live_activity_stream.swift"),
    ], check=True, capture_output=True, text=True, timeout=120)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/stream"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for count, mode, stamp in [(2, "working", 100), (1, "completed", 101)]:
                snapshot = {
                    "aggregateMode": "working", "activeCount": count, "updatedAt": stamp,
                    "agents": [
                        {"id": "aura", "name": "Aura", "mode": "working"},
                        {"id": "kleido", "name": "Kleido", "mode": mode},
                    ],
                }
                self.wfile.write(("data: " + json.dumps(snapshot) + "\n\n").encode())
                self.wfile.flush()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [str(binary), f"http://127.0.0.1:{server.server_port}"],
            check=True, capture_output=True, text=True, timeout=20,
        )
        assert "Stream completion regression passed" in result.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
