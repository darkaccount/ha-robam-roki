"""Tests for the static fan level ladder.

The ladder is deliberately resolved without talking to the device: sending an
unsupported level would start the motor, so setup must never probe.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

CONST_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "robam_roki"
    / "const.py"
)


def _load_const():
    spec = importlib.util.spec_from_file_location("roki_const_test", CONST_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


const = _load_const()


def test_r8229_ladder_is_not_linear():
    # The App's FanCtr8229View binds gear1/2/3/6 with tags 1, 2, 3, 6, so the
    # top gear is 6 and not the count of buttons.
    assert const.levels_for_model("R8229") == [1, 2, 3, 6]


def test_known_models_come_from_the_app_views():
    assert const.levels_for_model("R9700") == [1, 2, 3, 6]
    assert const.levels_for_model("R66A2") == [2, 6]
    # The app's model list spells this one without the R prefix.
    assert const.levels_for_model("66A2H") == [2, 6]
    assert const.levels_for_model("R5610") == [1, 6]
    assert const.levels_for_model("R8700") == [2, 6]


def test_unknown_model_falls_back_to_the_default_ladder():
    assert const.levels_for_model("R9999") == const.DEFAULT_LEVELS
    assert const.levels_for_model(None) == const.DEFAULT_LEVELS
    assert const.levels_for_model("") == const.DEFAULT_LEVELS


def test_model_lookup_is_case_and_space_insensitive():
    assert const.levels_for_model("  r8229 ") == [1, 2, 3, 6]


def test_levels_are_always_copies():
    first = const.levels_for_model("R8229")
    first.append(99)
    assert const.levels_for_model("R8229") == [1, 2, 3, 6]


def test_every_table_entry_is_ascending_and_unique():
    for model, levels in const.MODEL_LEVELS.items():
        assert levels, model
        assert levels == sorted(set(levels)), model
        assert all(level > 0 for level in levels), model


def test_two_gear_models_have_exactly_two_gears():
    for model in const.TWO_GEAR_MODELS:
        assert len(const.levels_for_model(model)) == 2, model


def test_conf_levels_constant_exists():
    assert const.CONF_LEVELS == "levels"


# -- provenance ----------------------------------------------------------


def test_only_verified_models_are_marked_hardware():
    # "hardware" is a promise that a real hood was seen reporting these
    # levels; it must never be handed out to a model read out of the APK.
    for model, source in const.LEVEL_SOURCES.items():
        assert source == "hardware", model
        assert model in const.MODEL_LEVELS, model


def test_r8229_is_the_only_hardware_verified_model():
    # R8229 is the one hood this was tested against.
    assert const.levels_source_for_model("R8229") == "hardware"


def test_other_models_are_marked_apk():
    for model in ("R9700", "R66A2", "66A2H", "R5610", "R8700"):
        assert const.levels_source_for_model(model) == "apk", model


def test_unknown_models_are_marked_fallback():
    assert const.levels_source_for_model("R9999") == "fallback"
    assert const.levels_source_for_model(None) == "fallback"
    assert const.levels_source_for_model("") == "fallback"


def test_every_table_row_is_a_real_model_code():
    # A row that is not in IPlatRokiFamily.all can never match a device, so it
    # would silently do nothing.
    for model in const.MODEL_LEVELS:
        assert model in const.KNOWN_MODEL_CODES, model


def test_the_model_code_list_matches_the_app():
    assert len(const.KNOWN_MODEL_CODES) >= 40
    assert "R8229" in const.KNOWN_MODEL_CODES
    assert "Q30i1" in const.KNOWN_MODEL_CODES
    # Not every code is prefixed with R.
    assert "66A2H" in const.KNOWN_MODEL_CODES
    assert "90B8Z" in const.KNOWN_MODEL_CODES


# -- percentage mapping (mirrors fan.RokiHoodFan) ------------------------


def _percentage(levels: list[int], level: int) -> int | None:
    if level <= 0 or not levels:
        return None
    if level not in levels:
        level = min(levels, key=lambda item: abs(item - level))
    return round(100 * (levels.index(level) + 1) / len(levels))


def _level_for_percentage(levels: list[int], percentage: int) -> int:
    index = min(
        len(levels) - 1, max(0, round(percentage * len(levels) / 100) - 1)
    )
    return levels[index]


@pytest.mark.parametrize(
    ("level", "expected"),
    [(1, 25), (2, 50), (3, 75), (6, 100)],
)
def test_r8229_level_to_percentage(level: int, expected: int):
    assert _percentage([1, 2, 3, 6], level) == expected


@pytest.mark.parametrize(
    ("percentage", "expected"),
    [(1, 1), (25, 1), (50, 2), (75, 3), (100, 6)],
)
def test_r8229_percentage_to_level(percentage: int, expected: int):
    # HA presents a 4-step speed slider as 25 / 50 / 75 / 100 %.
    assert _level_for_percentage([1, 2, 3, 6], percentage) == expected


def test_r8229_percentage_slider_steps():
    # Every value the speed slider can produce maps back to the same level.
    for percentage in (25, 50, 75, 100):
        level = _level_for_percentage([1, 2, 3, 6], percentage)
        assert _percentage([1, 2, 3, 6], level) == percentage


def test_every_gear_is_reachable_from_the_card():
    # The regression this mapping exists for: dividing by ``len - 1`` collapsed
    # two card positions onto the same level and left the lowest gear
    # unreachable, so a four-gear hood only ever received three commands.
    levels = [1, 2, 3, 6]
    steps = [round(100 * i / len(levels)) for i in range(1, len(levels) + 1)]
    assert [_level_for_percentage(levels, step) for step in steps] == levels


def test_the_lowest_gear_is_not_reported_as_off():
    # 0 % is how Home Assistant reads "off", so the lowest gear must not map
    # to it.
    assert _percentage([1, 2, 3, 6], 1) > 0


def test_two_gear_ladder_maps_to_50_and_100():
    levels = const.levels_for_model("R66A2")
    assert _percentage(levels, 2) == 50
    assert _percentage(levels, 6) == 100
    assert _level_for_percentage(levels, 50) == 2
    assert _level_for_percentage(levels, 100) == 6


def test_percentage_round_trip_is_stable():
    for levels in ([1, 2, 3, 6], [2, 6], [1, 6], const.DEFAULT_LEVELS):
        for level in levels:
            percentage = _percentage(levels, level)
            assert percentage > 0
            assert _level_for_percentage(levels, percentage) == level


def test_off_level_has_no_percentage():
    assert _percentage([1, 2, 3, 6], 0) is None
