from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def test_notification_write_safety(tmp_path):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("Swift/macOS required")
    root = Path(__file__).resolve().parents[1]
    ios = root / "ios/SidePulse/SidePulse"
    binary = tmp_path / "notification-safety"
    subprocess.run(
        ["swiftc", "-j", "1", "-num-threads", "1", "-swift-version", "5",
         "-module-cache-path", str(tmp_path / "ModuleCache"), "-o", str(binary),
         str(ios / "DndSchedule.swift"), str(ios / "DotNotificationShared.swift"),
         str(root / "tests/test_ios_dot_notification.swift")],
        check=True, capture_output=True, text=True, timeout=120,
    )
    result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=30)
    assert "Dot notification safety tests passed" in result.stdout
