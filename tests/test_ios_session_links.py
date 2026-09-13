from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def test_session_links_decode_the_daemon_reply(tmp_path):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("Swift/macOS required")
    root = Path(__file__).resolve().parents[1]
    source = (root / "ios/SidePulse/SidePulse/SessionLinks.swift").read_text()
    wire_format = source.split("// MARK: - Wire format", 1)[1].split("// MARK: - Client", 1)[0]
    harness = tmp_path / "links.swift"
    harness.write_text(
        "import Foundation\n"
        + wire_format
        + '''
struct Reply: Decodable { var links: [NewSessionLink] }
let json = """
{"links": [{"provider": "claude", "label": "Claude", "urls": ["https://claude.ai/code/", "claude://"]},
           {"provider": "paseo", "label": "Paseo", "urls": ["paseo://h/srv_1", "paseo://"]}]}
""".data(using: .utf8)!
let reply = try! JSONDecoder().decode(Reply.self, from: json)
guard reply.links.map(\\.id) == ["claude", "paseo"],
      reply.links[0].candidates.map(\\.absoluteString) == ["https://claude.ai/code/", "claude://"],
      reply.links[1].candidates.first?.absoluteString == "paseo://h/srv_1"
else {
    print("FAILED: \\(reply)")
    exit(1)
}
print("Session link tests passed")
'''
    )
    binary = tmp_path / "links"
    subprocess.run(
        ["swiftc", "-j", "1", "-num-threads", "1", "-swift-version", "5",
         "-module-cache-path", str(tmp_path / "ModuleCache"), "-o", str(binary), str(harness)],
        check=True, capture_output=True, text=True, timeout=120,
    )
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Session link tests passed" in result.stdout
