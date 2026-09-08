from __future__ import annotations

import pytest

from sidepulse.device_writer import validate_led_text
from sidepulse.led_appearance import ANIMATIONS, PALETTES, working_program
from sidepulse.led_wasm import SdLedWasmController


@pytest.mark.parametrize("led_count", (2, 8))
@pytest.mark.parametrize("animation", ANIMATIONS)
@pytest.mark.parametrize("palette", PALETTES)
@pytest.mark.parametrize("finished", (False, True))
def test_modes_fit_firmware_and_preserve_unread_half(led_count, animation, palette, finished):
    _, working, _, done = PALETTES[palette]
    program = working_program(animation, working, done, led_count=led_count, show_finished=finished)
    validate_led_text("brightness 3\n" + program)
    firmware = SdLedWasmController(led_count=led_count)
    firmware.reset(0)
    assert firmware.parse("brightness 3\n" + program, 0).ok, program
    if finished:
        motion = "\n".join(program.splitlines()[1:])
        for index in range(led_count // 2):
            assert f"{index}:{working}" not in motion


def test_dot_kitt_is_slow_even_with_finished_led():
    for finished in (False, True):
        program = working_program("kitt", "#4DA3FF", "#39D98A", led_count=2, show_finished=finished)
        assert "320ms" not in program
        assert ("2400ms pulse" if finished else "1000ms pulse 800ms") in program


def test_settings_migration_roundtrip_and_validation(tmp_path):
    from sidepulse.settings import AgentMonitorSettings, load_settings, save_settings
    path = tmp_path / "settings.json"
    path.write_text('{"kitt_mode_enabled": true}')
    assert load_settings(path).led_animation == "kitt"
    for animation in ANIMATIONS:
        for palette in PALETTES:
            settings = AgentMonitorSettings().with_led_appearance(animation=animation, palette=palette)
            save_settings(settings, path)
            loaded = load_settings(path)
            assert (loaded.led_animation, loaded.led_palette) == (animation, palette)
            assert loaded.kitt_mode_enabled == (animation == "kitt")
    path.write_text('{"led_animation": "invalid", "led_palette": "invalid"}')
    assert load_settings(path).led_animation == "gentle"
    assert load_settings(path).led_palette == "ocean"
    with pytest.raises(ValueError):
        AgentMonitorSettings().with_led_appearance(animation="invalid")


def test_appearance_changes_invalidate_device_cache(tmp_path):
    from sidepulse.led_status import AgentLedController
    from sidepulse.models import AgentMode
    controller = AgentLedController(device_path=tmp_path / "LEDS.LED", dry_run=True)
    assert controller.sync_mode(AgentMode.WORKING, animation="gentle").changed
    assert not controller.sync_mode(AgentMode.WORKING, animation="gentle").changed
    assert controller.sync_mode(AgentMode.WORKING, animation="tide").changed
    assert controller.sync_mode(AgentMode.WORKING, animation="tide", palette="dusk").changed


@pytest.mark.parametrize("led_count", (2, 8))
@pytest.mark.parametrize("animation", ANIMATIONS)
def test_all_display_states_fit_and_all_read_is_off(led_count, animation):
    from sidepulse.led_status import LedDisplayState, program_for_display_state
    for palette in PALETTES:
        for state in LedDisplayState:
            for finished in (False, True):
                program = program_for_display_state(state, led_count=led_count, animation=animation,
                                                    palette=palette, show_finished=finished, brightness=3)
                validate_led_text(program)
                firmware = SdLedWasmController(led_count=led_count)
                firmware.reset(0)
                assert firmware.parse(program, 0).ok
                if state == LedDisplayState.IDLE and not finished:
                    assert program == "off"
