from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def test_foreground_dot_notification_presentation(tmp_path):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("Swift/macOS required")
    root = Path(__file__).resolve().parents[1]
    source = (root / "ios/SidePulse/SidePulse/AppDelegate.swift").read_text()
    body = source.split(") async -> UNNotificationPresentationOptions {", 1)[1].split("\n    }", 1)[0]
    harness = tmp_path / "presentation.swift"
    # UNNotification has no public initializer. Wrap real notification content
    # to execute the production callback body without launching the iOS app.
    harness.write_text("""
import Foundation
import UserNotifications

struct TestRequest { let content: UNNotificationContent }
struct TestNotification { let request: TestRequest }

func presentationOptions(for content: UNNotificationContent) -> UNNotificationPresentationOptions {
    let notification = TestNotification(request: TestRequest(content: content))
""" + body + """
}

let cases: [(String, String?, UNNotificationInterruptionLevel, UNNotificationPresentationOptions)] = [
    ("passive Dot resume", "working", .passive, [.list]),
    ("ordinary Dot completion", "completed", .active, [.banner]),
    ("time-sensitive Dot", "completed", .timeSensitive, [.banner]),
    ("ordinary notification", nil, .active, [.banner, .sound]),
    ("non-Dot passive notification", nil, .passive, [.banner, .sound]),
]
for (name, mode, level, expected) in cases {
    let content = UNMutableNotificationContent()
    if let mode { content.userInfo = ["dot": ["aggregateMode": mode]] }
    content.interruptionLevel = level
    let actual = presentationOptions(for: content)
    guard actual == expected else {
        print("FAILED: \\(name): expected \\(expected.rawValue), received \\(actual.rawValue)")
        exit(1)
    }
}
print("Dot presentation tests passed")
""")
    binary = tmp_path / "presentation"
    subprocess.run(
        ["swiftc", "-j", "1", "-num-threads", "1", "-swift-version", "5",
         "-module-cache-path", str(tmp_path / "ModuleCache"), "-o", str(binary), str(harness)],
        check=True, capture_output=True, text=True, timeout=120,
    )
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Dot presentation tests passed" in result.stdout
