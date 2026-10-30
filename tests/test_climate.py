"""Exercise the wrapper against real Home Assistant state, service, and event APIs."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.dualmode_generic.climate import PLATFORM_SCHEMA, DualModeGenericThermostat

SOURCE = "climate.heat_pump"


@pytest.fixture
async def setup_wrapper(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    hass.set_state(CoreState.running)
    calls = []
    attributes = {
        "hvac_modes": ["off", "heat", "cool", "dry", "auto"],
        "current_temperature": 21,
        "temperature": 27,
        "min_temp": 8,
        "max_temp": 30,
        "target_temp_step": 0.5,
        "supported_features": int(ClimateEntityFeature.TARGET_TEMPERATURE),
    }
    hass.states.async_set(SOURCE, "cool", attributes)

    async def handle(call):
        calls.append((call.service, dict(call.data)))
        state = hass.states.get(SOURCE)
        attrs = dict(state.attributes)
        if call.service == "set_temperature":
            attrs["temperature"] = call.data["temperature"]
        if call.service == "set_fan_mode":
            attrs["fan_mode"] = call.data["fan_mode"]
        hass.states.async_set(SOURCE, call.data.get("hvac_mode", state.state), attrs)
        # Intentionally no return: the real service registry returns None.

    for service in (
        "set_hvac_mode",
        "set_temperature",
        "set_fan_mode",
        "set_swing_mode",
        "set_preset_mode",
    ):
        hass.services.async_register("climate", service, handle)

    entity = DualModeGenericThermostat(
        "Wrapper",
        SOURCE,
        27,
        16,
        None,
        0.3,
        0.3,
        HVACMode.HEAT_COOL,
        UnitOfTemperature.CELSIUS,
        "wrapper",
    )
    entity.hass = hass
    entity.entity_id = "climate.wrapper"
    monkeypatch.setattr(entity, "async_write_ha_state", Mock())
    monkeypatch.setattr(entity, "async_get_last_state", AsyncMock(return_value=None))
    yield hass, entity, calls
    entity._cancel_cycle()
    if entity._on_remove:
        for remove in entity._on_remove:
            remove()
    await hass.async_block_till_done()


async def test_startup_reads_temperature_and_preserves_configured_range(setup_wrapper):
    hass, entity, calls = setup_wrapper
    await entity.async_added_to_hass()
    await hass.async_block_till_done()
    assert entity.current_temperature == 21
    assert entity.hvac_mode == HVACMode.HEAT_COOL
    assert entity.target_temperature_low == 16
    assert entity.target_temperature_high == 27
    assert calls == []


async def test_off_reaches_source_even_without_current_temperature(setup_wrapper):
    hass, entity, calls = setup_wrapper
    await entity.async_set_hvac_mode(HVACMode.OFF)
    assert calls == [("set_hvac_mode", {"entity_id": SOURCE, "hvac_mode": "off"})]
    assert hass.states.get(SOURCE).state == "off"


@pytest.mark.parametrize(("temperature", "mode", "target"), [(10, "heat", 16), (29, "cool", 27)])
async def test_mode_switch_always_sends_target_after_none_service_response(
    setup_wrapper, temperature, mode, target
):
    hass, entity, calls = setup_wrapper
    source_mode = "cool" if mode == "heat" else "heat"
    hass.states.async_set(
        SOURCE,
        source_mode,
        {
            **hass.states.get(SOURCE).attributes,
            "current_temperature": temperature,
            "temperature": 23,
        },
    )
    entity._state_changed(hass.states.get(SOURCE), sync_target=False)
    entity._hvac_mode = HVACMode.HEAT_COOL
    await entity._async_control_heating()
    assert [service for service, _ in calls] == ["set_hvac_mode", "set_temperature"]
    assert calls[-1][1]["temperature"] == target
    assert hass.states.get(SOURCE).state == mode


async def test_unavailable_and_removed_source_clear_temperature(setup_wrapper):
    hass, entity, calls = setup_wrapper
    await entity.async_added_to_hass()
    for state in ("unavailable", "unknown", None):
        if state is None:
            hass.states.async_remove(SOURCE)
        else:
            hass.states.async_set(SOURCE, state)
        await hass.async_block_till_done()
        assert not entity.available
        assert entity.current_temperature is None
        assert entity.min_temp == 8
        assert entity.max_temp == 30
        assert entity.hvac_modes
    assert calls == []


async def test_only_source_supported_optional_features_are_advertised(setup_wrapper):
    hass, entity, _ = setup_wrapper
    assert not entity.supported_features & ClimateEntityFeature.FAN_MODE
    hass.states.async_set(
        SOURCE,
        "cool",
        {
            **hass.states.get(SOURCE).attributes,
            "supported_features": int(ClimateEntityFeature.FAN_MODE),
        },
    )
    assert entity.supported_features & ClimateEntityFeature.FAN_MODE


async def test_heat_only_source_does_not_advertise_range_or_off(setup_wrapper):
    hass, entity, _ = setup_wrapper
    hass.states.async_set(SOURCE, "heat", {"hvac_modes": ["heat"]})
    assert entity.hvac_modes == ["heat"]
    assert not entity.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    assert not entity.supported_features & ClimateEntityFeature.TURN_OFF


async def test_missing_optional_attributes_and_fahrenheit(setup_wrapper):
    hass, entity, _ = setup_wrapper
    entity._unit = UnitOfTemperature.FAHRENHEIT
    hass.states.async_set(SOURCE, "off", {"hvac_modes": ["off", "heat", "cool", "heat_cool"]})
    assert entity.temperature_unit == UnitOfTemperature.FAHRENHEIT
    assert entity.target_temperature_step > 0
    assert entity.min_temp is not None
    assert entity.max_temp is not None
    assert entity.hvac_modes.count("heat_cool") == 1


async def test_event_during_service_is_not_dropped(setup_wrapper):
    hass, entity, _ = setup_wrapper
    await entity.async_added_to_hass()
    async with entity._temp_lock:
        hass.states.async_set(
            SOURCE,
            "cool",
            {
                **hass.states.get(SOURCE).attributes,
                "current_temperature": 22,
            },
        )
        await asyncio.sleep(0)
    await hass.async_block_till_done()
    assert entity.current_temperature == 22


async def test_minimum_cycle_retries_without_another_sensor_event(setup_wrapper, monkeypatch):
    hass, entity, calls = setup_wrapper
    entity.min_cycle_duration = timedelta(minutes=20)
    entity._hvac_mode = HVACMode.HEAT_COOL
    attrs = {**hass.states.get(SOURCE).attributes, "current_temperature": 10}
    hass.states.async_set(SOURCE, "cool", attrs)
    entity._state_changed(hass.states.get(SOURCE), sync_target=False)
    timer = Mock()
    cancel = Mock()
    timer.return_value = cancel
    monkeypatch.setattr("custom_components.dualmode_generic.climate.async_call_later", timer)
    await entity._async_control_heating()
    assert calls == []
    delay, retry = timer.call_args.args[1:]
    assert 1199 <= delay <= 1200
    now = dt_util.utcnow() + timedelta(minutes=21)
    monkeypatch.setattr("custom_components.dualmode_generic.climate.dt_util.utcnow", lambda: now)
    await retry(now)
    assert [name for name, _ in calls] == ["set_hvac_mode", "set_temperature"]
    assert calls[-1][1]["temperature"] == 16


async def test_service_failure_propagates_and_does_not_send_target(setup_wrapper):
    hass, entity, calls = setup_wrapper

    async def fail(call):
        raise HomeAssistantError("Device rejected mode")

    hass.services.async_register("climate", "set_hvac_mode", fail)
    with pytest.raises(HomeAssistantError, match="rejected"):
        await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert calls == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), None, "bad", -5, 99])
async def test_invalid_setpoints_never_reach_hardware(setup_wrapper, value):
    _, entity, calls = setup_wrapper
    entity._hvac_mode = HVACMode.HEAT
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=value)
    assert calls == []
    assert entity._target_temp_low == 16


async def test_range_is_validated_before_any_service_call(setup_wrapper):
    _, entity, calls = setup_wrapper
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(target_temp_low=25, target_temp_high=20)
    assert calls == []
    assert entity._target_temp_low == 16
    assert entity._target_temp_high == 27


async def test_single_setpoint_updates_correct_boundary_and_source(setup_wrapper):
    hass, entity, calls = setup_wrapper
    entity._hvac_mode = HVACMode.COOL
    await entity.async_set_temperature(temperature=24)
    assert entity.target_temperature == 24
    assert hass.states.get(SOURCE).attributes["temperature"] == 24
    assert [service for service, _ in calls] == ["set_temperature"]


async def test_explicit_initial_mode_and_targets_override_source_on_startup(setup_wrapper):
    hass, entity, calls = setup_wrapper
    entity._initial_hvac_mode = HVACMode.HEAT
    entity._target_temp_low = 20
    await entity.async_added_to_hass()
    await hass.async_block_till_done()
    assert entity.hvac_mode == HVACMode.HEAT
    assert entity.target_temperature == 20
    assert [name for name, _ in calls] == ["set_hvac_mode", "set_temperature"]


async def test_actual_state_and_setpoints_settle_after_mode_change(setup_wrapper):
    hass, entity, _ = setup_wrapper
    await entity.async_added_to_hass()
    await entity.async_set_temperature(hvac_mode="heat", temperature=20)
    await hass.async_block_till_done()
    assert entity.hvac_mode == HVACMode.HEAT
    assert entity.target_temperature == 20
    assert entity.current_temperature == 21
    assert hass.states.get(SOURCE).attributes["temperature"] == 20


async def test_auto_mode_temperature_is_forwarded(setup_wrapper):
    hass, entity, calls = setup_wrapper
    entity._hvac_mode = HVACMode.AUTO
    await entity.async_set_temperature(temperature=22)
    assert hass.states.get(SOURCE).state == "auto"
    assert hass.states.get(SOURCE).attributes["temperature"] == 22
    assert [name for name, _ in calls] == ["set_hvac_mode", "set_temperature"]


@pytest.mark.parametrize("temperature", [0, -10])
async def test_zero_and_negative_readings_are_real_temperatures(setup_wrapper, temperature):
    hass, entity, _ = setup_wrapper
    hass.states.async_set(
        SOURCE,
        "heat",
        {
            **hass.states.get(SOURCE).attributes,
            "current_temperature": temperature,
            "temperature": 16,
        },
    )
    entity._state_changed(hass.states.get(SOURCE))
    entity._hvac_mode = HVACMode.HEAT
    assert entity.current_temperature == temperature
    assert entity.hvac_action == "heating"


@pytest.mark.parametrize("temperature", [None, float("nan"), "unavailable"])
async def test_invalid_current_temperature_prevents_automatic_mode_switch(
    setup_wrapper, temperature
):
    hass, entity, calls = setup_wrapper
    hass.states.async_set(
        SOURCE,
        "cool",
        {
            **hass.states.get(SOURCE).attributes,
            "current_temperature": temperature,
        },
    )
    entity._state_changed(hass.states.get(SOURCE))
    entity._hvac_mode = HVACMode.HEAT_COOL
    await entity._async_control_heating()
    assert calls == []
    assert entity.current_temperature is None


async def test_source_removed_while_changing_mode_does_not_send_a_target(setup_wrapper):
    hass, entity, calls = setup_wrapper

    async def disconnect(call):
        hass.states.async_remove(SOURCE)

    hass.services.async_register("climate", "set_hvac_mode", disconnect)
    await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert calls == []
    assert not entity.available


async def test_fan_mode_forwarding_accepts_a_none_response(setup_wrapper):
    hass, entity, calls = setup_wrapper
    await entity.async_set_fan_mode("low")
    assert calls == [("set_fan_mode", {"entity_id": SOURCE, "fan_mode": "low"})]
    assert hass.states.get(SOURCE).attributes["fan_mode"] == "low"


async def test_deferred_startup_initializes_when_home_assistant_starts(setup_wrapper):
    hass, entity, _ = setup_wrapper
    hass.set_state(CoreState.starting)
    await entity.async_added_to_hass()
    assert entity.current_temperature is None
    hass.set_state(CoreState.running)
    hass.bus.async_fire("homeassistant_start")
    await hass.async_block_till_done()
    assert entity.current_temperature == 21


async def test_removal_cancels_startup_listener(setup_wrapper):
    hass, entity, calls = setup_wrapper
    hass.set_state(CoreState.starting)
    await entity.async_added_to_hass()
    for remove in entity._on_remove:
        remove()
    entity._on_remove = []
    hass.bus.async_fire("homeassistant_start")
    await hass.async_block_till_done()
    assert entity.current_temperature is None
    assert calls == []


@pytest.mark.parametrize(
    "config",
    [
        {"platform": "dualmode_ac"},
        {"platform": "dualmode_ac", "climate_entity_id": SOURCE, "cold_tolerance": -1},
        {"platform": "dualmode_ac", "climate_entity_id": SOURCE, "hot_tolerance": float("nan")},
        {
            "platform": "dualmode_ac",
            "climate_entity_id": SOURCE,
            "target_temp_low": 25,
            "target_temp_high": 20,
        },
        {
            "platform": "dualmode_ac",
            "climate_entity_id": SOURCE,
            "target_temp_low": 20,
            "target_temp_high": 21,
            "cold_tolerance": 1,
            "hot_tolerance": 1,
        },
    ],
)
def test_invalid_configuration_is_rejected(config):
    with pytest.raises(vol.Invalid):
        PLATFORM_SCHEMA(config)
