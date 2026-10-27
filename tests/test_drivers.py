"""Test device driver layer, factories, and telemetry parsing across all categories."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.robam_roki.devices import (
    DishwasherDriver,
    GenericDriver,
    HoodDriver,
    MicrowaveDriver,
    OvenDriver,
    SteamOvenDriver,
    SterilizerDriver,
    StoveDriver,
    WaterPurifierDriver,
    get_driver_for_device,
)


def test_driver_factory_resolution():
    coordinator = MagicMock()

    assert isinstance(get_driver_for_device(coordinator, {"dc": "RYYJ"}), HoodDriver)
    assert isinstance(get_driver_for_device(coordinator, {"dc": "RRQZ"}), StoveDriver)
    assert isinstance(get_driver_for_device(coordinator, {"dc": "RXWJ"}), DishwasherDriver)
    assert isinstance(get_driver_for_device(coordinator, {"dc": "RJSQ"}), WaterPurifierDriver)
    assert isinstance(get_driver_for_device(coordinator, {"dc": "RZQL"}), SteamOvenDriver)
    assert isinstance(get_driver_for_device(coordinator, {"dc": "RZKY"}), SteamOvenDriver)
    assert isinstance(get_driver_for_device(coordinator, {"dc": "RDKX"}), OvenDriver)
    assert isinstance(get_driver_for_device(coordinator, {"dc": "RWBL"}), MicrowaveDriver)
    assert isinstance(get_driver_for_device(coordinator, {"dc": "RXDG"}), SterilizerDriver)
    assert isinstance(get_driver_for_device(coordinator, {"dc": "UNKNOWN"}), GenericDriver)
    assert isinstance(get_driver_for_device(coordinator, {}), HoodDriver)


def test_stove_driver_telemetry():
    coordinator = MagicMock()
    guid = "RRQZ000000000001"
    driver = StoveDriver(coordinator, {"guid": guid, "dc": "RRQZ", "dt": "9B39"})

    coordinator.data = {
        guid: {
            "stoveHeadLeftPower": 5,
            "stoveHeadRightPower": 9,
            "stoveHeadLeftRemainingTime": 15,
            "stoveHeadRightRemainingTime": 0,
            "stoveHeadLeftWorkStatus": 2,
            "stoveHeadRightWorkStatus": 3,
            "stoveHeadLeftAlarmStatus": 0,
            "stoveHeadRightAlarmStatus": 1,
        }
    }

    sensor_map = {s.key: s for s in driver.sensors()}
    assert sensor_map["left_head_level"].value_fn(coordinator.data[guid]) == 5
    assert sensor_map["right_head_level"].value_fn(coordinator.data[guid]) == 9
    assert sensor_map["left_head_remaining_time"].value_fn(coordinator.data[guid]) == 15
    assert sensor_map["right_head_status"].value_fn(coordinator.data[guid]) == "定时中"

    bsensor_map = {b.key: b for b in driver.binary_sensors()}
    assert bsensor_map["left_head_alarm"].is_on_fn(coordinator.data[guid]) is False
    assert bsensor_map["right_head_alarm"].is_on_fn(coordinator.data[guid]) is True


def test_dishwasher_driver_telemetry():
    coordinator = MagicMock()
    guid = "RXWJ000000000001"
    driver = DishwasherDriver(coordinator, {"guid": guid, "dc": "RXWJ"})

    coordinator.data = {
        guid: {
            "workStatus": 2,
            "remainTime": 45,
            "workMode": "强力洗",
            "doorOpen": 0,
            "lackSalt": 1,
            "lackRinse": 0,
        }
    }

    sensor_map = {s.key: s for s in driver.sensors()}
    assert sensor_map["work_status"].value_fn(coordinator.data[guid]) == "工作中"
    assert sensor_map["remaining_time"].value_fn(coordinator.data[guid]) == 45
    assert sensor_map["work_mode"].value_fn(coordinator.data[guid]) == "强力洗"

    bsensor_map = {b.key: b for b in driver.binary_sensors()}
    assert bsensor_map["door"].is_on_fn(coordinator.data[guid]) is False
    assert bsensor_map["lack_salt"].is_on_fn(coordinator.data[guid]) is True
    assert bsensor_map["lack_rinse"].is_on_fn(coordinator.data[guid]) is False


def test_water_purifier_telemetry():
    coordinator = MagicMock()
    guid = "RJSQ000000000001"
    driver = WaterPurifierDriver(coordinator, {"guid": guid, "dc": "RJSQ"})

    coordinator.data = {
        guid: {
            "input_tds": 250,
            "output_tds": 12,
            "filter1": 95,
            "filter2": 78,
            "purifying": 1,
            "leakAlarm": 0,
        }
    }

    sensor_map = {s.key: s for s in driver.sensors()}
    assert sensor_map["input_tds"].value_fn(coordinator.data[guid]) == 250
    assert sensor_map["output_tds"].value_fn(coordinator.data[guid]) == 12
    assert sensor_map["filter_life_1"].value_fn(coordinator.data[guid]) == 95

    bsensor_map = {b.key: b for b in driver.binary_sensors()}
    assert bsensor_map["purifying"].is_on_fn(coordinator.data[guid]) is True
    assert bsensor_map["leak_alarm"].is_on_fn(coordinator.data[guid]) is False


def test_thermal_cooker_telemetry():
    coordinator = MagicMock()
    guid = "RZQL000000000001"
    driver = SteamOvenDriver(coordinator, {"guid": guid, "dc": "RZQL"})

    coordinator.data = {
        guid: {
            "workStatus": 3,
            "temp": 100,
            "setTemp": 100,
            "remainTime": 25,
            "doorOpen": 0,
            "alarm": 0,
        }
    }

    sensor_map = {s.key: s for s in driver.sensors()}
    assert sensor_map["work_status"].value_fn(coordinator.data[guid]) == "工作中"
    assert sensor_map["current_temperature"].value_fn(coordinator.data[guid]) == 100
    assert sensor_map["target_temperature"].value_fn(coordinator.data[guid]) == 100
    assert sensor_map["remaining_time"].value_fn(coordinator.data[guid]) == 25

    bsensor_map = {b.key: b for b in driver.binary_sensors()}
    assert bsensor_map["door"].is_on_fn(coordinator.data[guid]) is False
    assert bsensor_map["alarm"].is_on_fn(coordinator.data[guid]) is False


def test_generic_driver_fallback():
    coordinator = MagicMock()
    guid = "MYSTERY000000001"
    driver = GenericDriver(coordinator, {"guid": guid, "dc": "SPECIAL_ROBOT"})

    coordinator.data = {guid: {"foo": "bar", "voltage": 220}}

    sensors = driver.sensors()
    assert len(sensors) == 1
    assert sensors[0].key == "raw_state"
    assert sensors[0].value_fn(coordinator.data[guid]) == "available"
    assert sensors[0].attrs_fn(coordinator.data[guid]) == {"foo": "bar", "voltage": 220}
