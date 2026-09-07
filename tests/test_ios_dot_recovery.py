from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from sidepulse.device_writer import validate_led_text
from sidepulse.led_wasm import LedWasmUnavailableError, SdLedWasmController


ROOT = Path(__file__).resolve().parents[1]
IOS = ROOT / "ios/SidePulse/SidePulse"


@pytest.fixture(scope="module")
def swift_binaries(tmp_path_factory):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("Swift/macOS required for the production iOS mirror tests")
    build = tmp_path_factory.mktemp("ios-dot")
    binaries = {}
    for name, sources in {
        "mirror": [
            IOS / "DotStatusMirror.swift",
            IOS / "EventLog.swift",
            ROOT / "tests/ios_dot_test_support.swift",
            ROOT / "tests/test_ios_dot_recovery.swift",
        ],
        "writer": [
            IOS / "DriveWriter.swift",
            IOS / "EventLog.swift",
            ROOT / "tests/test_ios_drive_writer.swift",
        ],
    }.items():
        binary = build / name
        subprocess.run(
            ["swiftc", "-j", "1", "-num-threads", "1", "-swift-version", "5",
             "-module-cache-path", str(build / "ModuleCache"), "-o", str(binary),
             *map(str, sources)],
            check=True, capture_output=True, text=True, timeout=120,
        )
        binaries[name] = binary
    return binaries


def test_production_mirror_recovery_and_write_ordering(swift_binaries):
    result = subprocess.run(
        [str(swift_binaries["mirror"])], check=True,
        capture_output=True, text=True, timeout=30,
    )
    assert "Dot recovery tests passed" in result.stdout


def test_coordinated_usb_writes_and_validation(swift_binaries):
    result = subprocess.run(
        [str(swift_binaries["writer"])], check=True,
        capture_output=True, text=True, timeout=30,
    )
    assert "Drive writer tests passed" in result.stdout


def _firmware_window(program, end_seconds):
    try:
        controller = SdLedWasmController(led_count=2)
    except LedWasmUnavailableError as exc:
        pytest.skip(str(exc))
    controller.reset(0)
    parsed = controller.parse("brightness 3\n" + program, 0)
    assert parsed.ok, (parsed, program)
    # Step the actual firmware engine, including repeat termination. Run the
    # loop inside JavaScriptCore to avoid thousands of Python/ObjC round trips.
    value = controller.context.evaluateScript_(f"""
        (function () {{
            var movingAt52Minutes = false;
            var last = [];
            for (var ms = 0; ms <= {end_seconds * 1000}; ms += 100) {{
                last = JSON.parse(sdledStep(2, ms));
                if (ms >= 3143000 && ms < 3153000 && last[5] > 0) {{
                    movingAt52Minutes = true;
                }}
            }}
            return JSON.stringify({{moving: movingAt52Minutes, last: last}});
        }})()
    """)
    assert controller.context.exception() is None
    return json.loads(value.toString())


def test_old_thirty_minute_flow_reproduces_reported_blackout():
    old = "off 160ms cosine\n0:#4DA3FF 760ms pulse 0ms; 1:#4DA3FF 760ms pulse 260ms\nrepeat 1526\noff"
    observed = _firmware_window(old, 3153)
    assert observed == {"moving": False, "last": [0, 0, 0, 0, 0, 0]}


def test_all_animations_survive_missed_refresh_and_still_expire(swift_binaries):
    result = subprocess.run(
        [str(swift_binaries["mirror"]), "--programs"], check=True,
        capture_output=True, text=True, timeout=30,
    )
    programs = json.loads(result.stdout)
    assert len(programs) == 6
    for case in programs:
        validate_led_text("brightness 3\n" + case["program"])
        observed = _firmware_window(case["program"], 7215)
        assert observed["moving"], case
        assert observed["last"][3:] == [0, 0, 0], case
        if case["unread"] == "true":
            assert observed["last"][1] > 0, case
        else:
            assert observed["last"] == [0, 0, 0, 0, 0, 0], case
