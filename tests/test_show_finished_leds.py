from __future__ import annotations

import json

import pytest

from sidepulse.device_writer import validate_led_text
from sidepulse.led_wasm import LedWasmUnavailableError, SdLedWasmController
from sidepulse.led_status import (
    ASK_AMBER,
    DONE_GREEN,
    WORKING_CYAN,
    AgentLedController,
    LedDisplayState,
    program_for_display_state,
)
from sidepulse.models import AgentMode
from sidepulse.settings import AgentMonitorSettings, load_settings, save_settings


def test_show_finished_setting_defaults_off_and_round_trips(tmp_path):
    settings = AgentMonitorSettings()
    assert settings.show_finished_enabled is False

    path = tmp_path / "settings.json"
    save_settings(settings.with_show_finished(True), path)

    assert load_settings(path).show_finished_enabled is True
    assert json.loads(path.read_text())["show_finished_enabled"] is True


def test_dot_keeps_one_led_green_and_moves_cyan_on_the_other():
    program = program_for_display_state(
        LedDisplayState.WORKING,
        led_count=2,
        show_finished=True,
    )

    validate_led_text(program)
    assert f"0:{DONE_GREEN}" in program
    assert f"1:{DONE_GREEN}" not in program
    assert f"0:{WORKING_CYAN}" not in program
    assert f"1:{WORKING_CYAN}" in program


def test_pro_keeps_first_half_green_and_moves_cyan_on_second_half():
    program = program_for_display_state(
        LedDisplayState.WORKING,
        led_count=8,
        show_finished=True,
    )

    validate_led_text(program)
    for index in range(4):
        assert f"{index}:{DONE_GREEN}" in program
        assert f"{index}:{WORKING_CYAN}" not in program
    for index in range(4, 8):
        assert f"{index}:{DONE_GREEN}" not in program
        assert f"{index}:{WORKING_CYAN}" in program


def test_idle_keeps_first_half_green_and_second_half_off():
    program = program_for_display_state(
        LedDisplayState.IDLE,
        led_count=8,
        brightness=64,
        show_finished=True,
    )

    validate_led_text(program)
    assert program.startswith("brightness 64\n")
    for index in range(4):
        assert f"{index}:{DONE_GREEN}" in program
    for index in range(4, 8):
        assert f"{index}:{DONE_GREEN}" not in program
        assert f"{index}:#000000" in program


def test_ask_keeps_first_half_green_and_pulses_amber_on_second_half():
    program = program_for_display_state(
        LedDisplayState.ASK,
        led_count=8,
        show_finished=True,
    )

    validate_led_text(program)
    for index in range(4):
        assert f"{index}:{DONE_GREEN}" in program
        assert f"{index}:{ASK_AMBER}" not in program
    for index in range(4, 8):
        assert f"{index}:{DONE_GREEN}" not in program
        assert f"{index}:{ASK_AMBER} 1.6s pulse" in program


def test_done_remains_solid_green_with_finished_indicator():
    program = program_for_display_state(
        LedDisplayState.DONE,
        led_count=8,
        show_finished=True,
    )

    validate_led_text(program)
    assert program == DONE_GREEN


@pytest.mark.parametrize("led_count", (2, 8))
def test_all_show_finished_programs_parse_with_firmware_wasm(led_count):
    try:
        controller = SdLedWasmController(led_count=led_count)
    except LedWasmUnavailableError as exc:
        pytest.skip(str(exc))

    cases = (
        (LedDisplayState.IDLE, False),
        (LedDisplayState.ASK, False),
        (LedDisplayState.DONE, False),
        (LedDisplayState.WORKING, False),
        (LedDisplayState.WORKING, True),
    )
    for state, kitt_mode in cases:
        program = program_for_display_state(
            state,
            led_count=led_count,
            kitt_mode=kitt_mode,
            show_finished=True,
        )

        controller.reset(0)
        result = controller.parse(program, 0)

        assert result.ok, (
            state,
            kitt_mode,
            result.error_name,
            result.line,
            result.column,
            program,
        )


def test_kitt_show_finished_scans_only_the_moving_half():
    program = program_for_display_state(
        LedDisplayState.WORKING,
        led_count=8,
        kitt_mode=True,
        show_finished=True,
    )

    validate_led_text(program)
    motion = "\n".join(program.splitlines()[1:-1])
    for index in range(4):
        assert f"{index}:{WORKING_CYAN}" not in motion
    for index in range(4, 8):
        assert f"{index}:{WORKING_CYAN}" in motion


def test_led_controller_rewrites_when_finished_indicator_changes(tmp_path):
    device = tmp_path / "SidePulsePro"
    device.mkdir()
    controller = AgentLedController(device_path=device)

    standard = controller.sync_mode(AgentMode.WORKING)
    unchanged = controller.sync_mode(AgentMode.WORKING)
    mixed = controller.sync_mode(AgentMode.WORKING, show_finished=True)

    assert standard.changed is True
    assert unchanged.changed is False
    assert mixed.changed is True
    assert f"0:{DONE_GREEN}" in (device / "LEDS.LED").read_text()


def test_led_controller_rewrites_nonworking_states_when_finished_indicator_changes(
    tmp_path,
):
    for mode in (AgentMode.IDLE_READY, AgentMode.WAITING_FOR_INPUT):
        device = tmp_path / mode.value
        device.mkdir()
        controller = AgentLedController(device_path=device)

        standard = controller.sync_mode(mode)
        unchanged = controller.sync_mode(mode)
        mixed = controller.sync_mode(mode, show_finished=True)

        assert standard.changed is True
        assert unchanged.changed is False
        assert mixed.changed is True
        assert f"0:{DONE_GREEN}" in (device / "LEDS.LED").read_text()
