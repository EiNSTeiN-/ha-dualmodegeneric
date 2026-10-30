"""
Transforms a heat pump AC entity which has separate cooling and heating into dual mode thermostat
that have both heating and cooling.

Originally based on the script at this thread:
https://community.home-assistant.io/t/heat-cool-generic-thermostat/76443/2

Modified to better conform to modern Home Assistant custom_component style.
"""

import asyncio
import logging
import math

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.climate import (
    PLATFORM_SCHEMA,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate.const import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_PRESET_MODE,
    ATTR_PRESET_MODES,
    ATTR_SWING_MODE,
    ATTR_SWING_MODES,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_SWING_MODE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_NAME,
    CONF_UNIQUE_ID,
    EVENT_HOMEASSISTANT_START,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

DEFAULT_TOLERANCE = 0.3
DEFAULT_NAME = "Generic Thermostat"

CONF_CLIMATE_ENTITY_ID = "climate_entity_id"
CONF_TARGET_TEMP_HIGH = "target_temp_high"
CONF_TARGET_TEMP_LOW = "target_temp_low"
CONF_MIN_DUR = "min_cycle_duration"
CONF_COLD_TOLERANCE = "cold_tolerance"
CONF_HOT_TOLERANCE = "hot_tolerance"
CONF_INITIAL_HVAC_MODE = "initial_hvac_mode"
SUPPORT_FLAGS = ClimateEntityFeature.TARGET_TEMPERATURE


def _finite_float(value):
    """Reject non-numeric and non-finite temperatures without treating zero as missing."""
    try:
        number = float(value)
    except (TypeError, ValueError) as err:
        raise vol.Invalid("Expected a finite number") from err
    if not math.isfinite(number):
        raise vol.Invalid("Expected a finite number")
    return number


def _number(value):
    """Read optional attributes, including null or temporarily invalid readings."""
    try:
        return _finite_float(value)
    except vol.Invalid:
        return None


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_CLIMATE_ENTITY_ID): cv.entity_id,
        vol.Optional(CONF_MIN_DUR): vol.All(cv.time_period, cv.positive_timedelta),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_COLD_TOLERANCE, default=DEFAULT_TOLERANCE): vol.All(
            _finite_float, vol.Range(min=0)
        ),
        vol.Optional(CONF_HOT_TOLERANCE, default=DEFAULT_TOLERANCE): vol.All(
            _finite_float, vol.Range(min=0)
        ),
        vol.Optional(CONF_TARGET_TEMP_HIGH): _finite_float,
        vol.Optional(CONF_TARGET_TEMP_LOW): _finite_float,
        vol.Optional(CONF_INITIAL_HVAC_MODE): vol.In(list(HVACMode)),
        vol.Optional(CONF_UNIQUE_ID): cv.string,
    }
)


def _validate_range(config):
    """Reject a configured range whose comfort interval is inverted."""
    low = config.get(CONF_TARGET_TEMP_LOW)
    high = config.get(CONF_TARGET_TEMP_HIGH)
    if low is not None and high is not None:
        gap = config[CONF_COLD_TOLERANCE] + config[CONF_HOT_TOLERANCE]
        if high <= low or high - low < gap - 1e-9:
            raise vol.Invalid("Target range must contain the heating and cooling tolerances")
    return config


PLATFORM_SCHEMA = vol.All(PLATFORM_SCHEMA, _validate_range)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the dual mode generic thermostat platform."""

    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)

    name = config.get(CONF_NAME)
    climate_entity_id = config.get(CONF_CLIMATE_ENTITY_ID)
    target_temp_high = config.get(CONF_TARGET_TEMP_HIGH)
    target_temp_low = config.get(CONF_TARGET_TEMP_LOW)
    min_cycle_duration = config.get(CONF_MIN_DUR)
    cold_tolerance = config.get(CONF_COLD_TOLERANCE)
    hot_tolerance = config.get(CONF_HOT_TOLERANCE)
    initial_hvac_mode = config.get(CONF_INITIAL_HVAC_MODE)
    unit = hass.config.units.temperature_unit
    unique_id = config.get(CONF_UNIQUE_ID)

    async_add_entities(
        [
            DualModeGenericThermostat(
                name,
                climate_entity_id,
                target_temp_high,
                target_temp_low,
                min_cycle_duration,
                cold_tolerance,
                hot_tolerance,
                initial_hvac_mode,
                unit,
                unique_id,
            )
        ]
    )


class DualModeGenericThermostat(ClimateEntity, RestoreEntity):
    """Representation of a Generic Thermostat device."""

    def __init__(
        self,
        name,
        climate_entity_id,
        target_temp_high,
        target_temp_low,
        min_cycle_duration,
        cold_tolerance,
        hot_tolerance,
        initial_hvac_mode,
        unit,
        unique_id,
    ):
        """Initialize the thermostat."""
        self._name = name
        self.climate_entity_id = climate_entity_id

        self.min_cycle_duration = min_cycle_duration
        self._cold_tolerance = cold_tolerance
        self._hot_tolerance = hot_tolerance
        self._hvac_mode = None
        self._initial_hvac_mode = initial_hvac_mode

        self._cur_temp = None
        self._temp_lock = asyncio.Lock()
        self._target_temp_high = target_temp_high
        self._target_temp_low = target_temp_low
        self._unit = unit
        self._unique_id = unique_id
        self._unsub_cycle = None
        self._last_source_attributes = {}

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        # Add listener for climate entity state
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self.climate_entity_id, self._async_climate_state_changed
            )
        )
        self.async_on_remove(self._cancel_cycle)

        # Check If we have an old state
        old_state = await self.async_get_last_state()
        if old_state is not None:
            if self._target_temp_low is None:
                if old_state.attributes.get(ATTR_TARGET_TEMP_LOW) is not None:
                    self._target_temp_low = _number(old_state.attributes[ATTR_TARGET_TEMP_LOW])
            if self._target_temp_high is None:
                if old_state.attributes.get(ATTR_TARGET_TEMP_HIGH) is not None:
                    self._target_temp_high = _number(old_state.attributes[ATTR_TARGET_TEMP_HIGH])
            if old_state.state in self.hvac_modes:
                self._hvac_mode = old_state.state

        if self._target_temp_low is None:
            self._target_temp_low = self.min_temp
        if self._target_temp_high is None:
            self._target_temp_high = self.max_temp
        if self._initial_hvac_mode is not None:
            self._hvac_mode = self._initial_hvac_mode
        if not self._hvac_mode:
            # Set default state to _initial_hvac_mode, or Off if not specified
            self._hvac_mode = self._initial_hvac_mode or HVACMode.OFF

        async def _async_startup(event=None):
            """Init on startup."""
            state = self.hass.states.get(self.climate_entity_id)
            self._state_changed(state, sync_target=False, sync_mode=self._initial_hvac_mode is None)
            await self._async_control_heating()
            self.async_write_ha_state()

        if self.hass.state == CoreState.running:
            await _async_startup()
        else:
            self.async_on_remove(
                self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)
            )

    @property
    def available(self):
        """Do not present a working thermostat while its source is offline."""
        state = self.hass.states.get(self.climate_entity_id)
        return state is not None and state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)

    @property
    def _source_attributes(self):
        state = self.hass.states.get(self.climate_entity_id)
        return state.attributes if self.available else self._last_source_attributes

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def name(self):
        """Return the name of the thermostat."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique id of this thermostat."""
        return self._unique_id

    @property
    def precision(self):
        """Return the precision of the system."""
        return super().precision

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        step = _number(self._source_attributes.get(ATTR_TARGET_TEMP_STEP))
        return step if step is not None and step > 0 else self.precision

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return self._source_attributes.get(ATTR_UNIT_OF_MEASUREMENT) or self._unit

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self._cur_temp

    @property
    def hvac_mode(self):
        """Return current operation."""
        return self._hvac_mode

    @property
    def hvac_action(self):
        """Return the current running hvac operation if supported.

        Need to be a HVACAction.
        """
        if not self.available:
            return None
        if action := self._source_attributes.get(ATTR_HVAC_ACTION):
            return action
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if self._hvac_mode == HVACMode.COOL:
            return (
                HVACAction.COOLING
                if self._cur_temp is not None
                and self._target_temp_high is not None
                and self._cur_temp > self._target_temp_high
                else HVACAction.IDLE
            )
        if self._hvac_mode == HVACMode.HEAT:
            return (
                HVACAction.HEATING
                if self._cur_temp is not None
                and self._target_temp_low is not None
                and self._cur_temp < self._target_temp_low
                else HVACAction.IDLE
            )
        if self._hvac_mode == HVACMode.FAN_ONLY:
            return HVACAction.FAN
        if self._hvac_mode == HVACMode.DRY:
            return HVACAction.DRYING
        if self._hvac_mode == HVACMode.HEAT_COOL:
            mode = self._climate_entity_hvac_mode()
            if mode == HVACMode.HEAT:
                return (
                    HVACAction.HEATING
                    if self._cur_temp is not None
                    and self._target_temp_low is not None
                    and self._cur_temp < self._target_temp_low
                    else HVACAction.IDLE
                )
            elif mode == HVACMode.COOL:
                return (
                    HVACAction.COOLING
                    if self._cur_temp is not None
                    and self._target_temp_high is not None
                    and self._cur_temp > self._target_temp_high
                    else HVACAction.IDLE
                )
            else:
                _LOGGER.info("Climate entity returned unexpected state: %s, assuming idle", mode)
                return HVACAction.IDLE
        return HVACAction.IDLE

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        if self._hvac_mode == HVACMode.HEAT:
            return self._target_temp_low
        if self._hvac_mode == HVACMode.COOL:
            return self._target_temp_high
        if self._hvac_mode != HVACMode.HEAT_COOL:
            return _number(self._source_attributes.get(ATTR_TEMPERATURE))
        return None

    @property
    def target_temperature_high(self):
        """Return the upper temperature we try to reach when in range mode."""
        if self._hvac_mode == HVACMode.HEAT_COOL:
            return self._target_temp_high
        return None

    @property
    def target_temperature_low(self):
        """Return the lower temperature we try to reach when in range mode."""
        if self._hvac_mode == HVACMode.HEAT_COOL:
            return self._target_temp_low
        return None

    @property
    def hvac_modes(self):
        """List of available operation modes."""
        modes = list(
            self._source_attributes.get(
                ATTR_HVAC_MODES, [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]
            )
        )
        if HVACMode.HEAT in modes and HVACMode.COOL in modes:
            modes.insert(0, HVACMode.HEAT_COOL)
        return list(dict.fromkeys(modes))

    @property
    def preset_mode(self):
        """Return the current preset mode, e.g., home, away, temp."""
        state = self.hass.states.get(self.climate_entity_id)
        if state and ATTR_PRESET_MODE in state.attributes:
            return state.attributes[ATTR_PRESET_MODE]

    @property
    def preset_modes(self):
        """Return a list of available preset modes."""
        state = self.hass.states.get(self.climate_entity_id)
        if state and ATTR_PRESET_MODES in state.attributes:
            return state.attributes[ATTR_PRESET_MODES]

    @property
    def fan_mode(self):
        """Return the fan setting.

        Requires ClimateEntityFeature.FAN_MODE.
        """
        state = self.hass.states.get(self.climate_entity_id)
        if state and ATTR_FAN_MODE in state.attributes:
            return state.attributes[ATTR_FAN_MODE]

    @property
    def fan_modes(self):
        """Return the list of available fan modes.

        Requires ClimateEntityFeature.FAN_MODE.
        """
        state = self.hass.states.get(self.climate_entity_id)
        if state and ATTR_FAN_MODES in state.attributes:
            return state.attributes[ATTR_FAN_MODES]

    @property
    def swing_mode(self):
        """Return the swing setting.

        Requires ClimateEntityFeature.SWING_MODE.
        """
        state = self.hass.states.get(self.climate_entity_id)
        if state and ATTR_SWING_MODE in state.attributes:
            return state.attributes[ATTR_SWING_MODE]

    @property
    def swing_modes(self):
        """Return the list of available swing modes.

        Requires ClimateEntityFeature.SWING_MODE.
        """
        state = self.hass.states.get(self.climate_entity_id)
        if state and ATTR_SWING_MODES in state.attributes:
            return state.attributes[ATTR_SWING_MODES]

    async def async_set_hvac_mode(self, hvac_mode):
        """Set hvac mode."""
        if hvac_mode not in self.hvac_modes:
            raise ServiceValidationError(f"Unsupported HVAC mode: {hvac_mode}")
        async with self._temp_lock:
            self._hvac_mode = hvac_mode
        await self._async_control_heating(force=True)
        # Ensure we update the current operation after changing the mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        passthrough_temperature = None
        async with self._temp_lock:
            mode = kwargs.get(ATTR_HVAC_MODE, self._hvac_mode)
            if mode not in self.hvac_modes:
                raise ServiceValidationError(f"Unsupported HVAC mode: {mode}")
            low, high = self._target_temp_low, self._target_temp_high
            low_changed = ATTR_TARGET_TEMP_LOW in kwargs
            high_changed = ATTR_TARGET_TEMP_HIGH in kwargs
            try:
                if low_changed:
                    low = _finite_float(kwargs[ATTR_TARGET_TEMP_LOW])
                if high_changed:
                    high = _finite_float(kwargs[ATTR_TARGET_TEMP_HIGH])
                if ATTR_TEMPERATURE in kwargs:
                    temperature = _finite_float(kwargs[ATTR_TEMPERATURE])
                    if mode == HVACMode.HEAT:
                        low, low_changed = temperature, True
                    elif mode == HVACMode.COOL:
                        high, high_changed = temperature, True
                    elif mode == HVACMode.HEAT_COOL:
                        raise ServiceValidationError("Use a temperature range in heat_cool mode")
                    else:
                        if not self.min_temp <= temperature <= self.max_temp:
                            raise ServiceValidationError("Temperature exceeds the source limits")
                        passthrough_temperature = temperature
            except vol.Invalid as err:
                raise ServiceValidationError(str(err)) from err
            # Preserve the existing tolerance convention, but never allow its
            # comfort interval to invert and alternate heating and cooling.
            difference = max(
                self._cold_tolerance + self._hot_tolerance, self.target_temperature_step
            )
            if high - low < difference - 1e-9:
                if low_changed and high_changed:
                    raise ServiceValidationError(
                        "Temperature range is narrower than the tolerances"
                    )
                if low_changed:
                    high = low + difference
                else:
                    low = high - difference
            if not self.min_temp <= low <= high <= self.max_temp:
                raise ServiceValidationError("Temperature range exceeds the source limits")
            self._hvac_mode = mode
            self._target_temp_low, self._target_temp_high = low, high
        await self._async_control_heating(force=True)
        if passthrough_temperature is not None and self.available:
            await self._async_internal_set_temperature(passthrough_temperature)
        self.async_write_ha_state()

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        if (value := _number(self._source_attributes.get(ATTR_MIN_TEMP))) is not None:
            return value

        # Get default temp from super class
        return super().min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        if (value := _number(self._source_attributes.get(ATTR_MAX_TEMP))) is not None:
            return value

        # Get default temp from super class
        return super().max_temp

    async def _async_climate_state_changed(self, event):
        """Handle temperature changes."""
        # A service call can publish intermediate states while this lock is held.
        # Wait, then consume the latest source state instead of dropping events or
        # overwriting our target with an intermediate mode-change setpoint.
        async with self._temp_lock:
            self._state_changed(self.hass.states.get(self.climate_entity_id))
        await self._async_control_heating()
        self.async_write_ha_state()

    @callback
    def _state_changed(self, new_state, *, sync_target=True, sync_mode=True):
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._cur_temp = None
            self._cancel_cycle()
            return

        self._last_source_attributes = dict(new_state.attributes)
        self._cur_temp = _number(new_state.attributes.get(ATTR_CURRENT_TEMPERATURE))

        if (
            sync_target
            and ATTR_TEMPERATURE in new_state.attributes
            and (temp := new_state.attributes[ATTR_TEMPERATURE]) is not None
        ):
            self._async_update_target_temp(new_state.state, temp)
        if sync_mode and self._hvac_mode == HVACMode.HEAT_COOL:
            if new_state.state not in [HVACMode.HEAT, HVACMode.COOL]:
                _LOGGER.info(
                    "Climate entity state change to %s while thermostat was set to %s",
                    new_state.state,
                    self._hvac_mode,
                )
                self._hvac_mode = new_state.state
        elif sync_mode and self._hvac_mode != new_state.state:
            _LOGGER.info(
                "Climate entity state change to %s while thermostat was set to %s",
                new_state.state,
                self._hvac_mode,
            )
            self._hvac_mode = new_state.state

        if (min_temp := _number(new_state.attributes.get(ATTR_MIN_TEMP))) is not None:
            if self._target_temp_low is None or self._target_temp_low < min_temp:
                self._target_temp_low = min_temp

        if (max_temp := _number(new_state.attributes.get(ATTR_MAX_TEMP))) is not None:
            if self._target_temp_high is None or self._target_temp_high > max_temp:
                self._target_temp_high = max_temp

    @callback
    def _async_update_target_temp(self, new_state, temp):
        """Update thermostat with latest state from climate entity."""
        try:
            temp = _finite_float(temp)
            if new_state == HVACMode.HEAT:
                if self._hvac_mode in (
                    HVACMode.HEAT,
                    HVACMode.HEAT_COOL,
                ) and self._target_temp_low != float(temp):
                    _LOGGER.info(
                        "New target_temp_low %s changed from %s for %s mode",
                        temp,
                        self._target_temp_low,
                        new_state,
                    )
                    self._target_temp_low = temp
            elif new_state == HVACMode.COOL:
                if self._hvac_mode in (
                    HVACMode.COOL,
                    HVACMode.HEAT_COOL,
                ) and self._target_temp_high != float(temp):
                    _LOGGER.info(
                        "New target_temp_high %s changed from %s for %s mode",
                        temp,
                        self._target_temp_high,
                        new_state,
                    )
                    self._target_temp_high = temp

        except vol.Invalid:
            _LOGGER.debug("Ignoring invalid source target temperature: %s", temp)

    def _climate_entity_hvac_mode(self):
        """List of available operation modes."""
        state = self.hass.states.get(self.climate_entity_id)
        if state:
            return state.state

    async def _async_control_heating(self, force=False):
        """Check if we need to turn heating on or off."""
        async with self._temp_lock:
            self._cancel_cycle()
            if not self.available:
                return
            source = self.hass.states.get(self.climate_entity_id)
            mode = self._hvac_mode
            if mode == HVACMode.HEAT_COOL:
                if None in (self._cur_temp, self._target_temp_high, self._target_temp_low):
                    return
                if (
                    self._target_temp_high - self._target_temp_low
                    < max(self._cold_tolerance + self._hot_tolerance, self.target_temperature_step)
                    - 1e-9
                ):
                    _LOGGER.error("Cannot control an inverted or overlapping temperature range")
                    return
                if self._is_comfortable_temperature():
                    mode = source.state
                elif self._is_hot_enough():
                    mode = HVACMode.COOL
                else:
                    mode = HVACMode.HEAT
            if mode != source.state:
                if not force and self.min_cycle_duration and mode != HVACMode.OFF:
                    remaining = (
                        source.last_changed + self.min_cycle_duration - dt_util.utcnow()
                    ).total_seconds()
                    if remaining > 0:
                        self._unsub_cycle = async_call_later(
                            self.hass, remaining, self._async_cycle_elapsed
                        )
                        return
                await self._async_internal_set_hvac_mode(mode)
                if not self.available:
                    return
            # Successful blocking calls return None; failure is an exception.
            # Always send the target after a successful mode change.
            if mode in (HVACMode.COOL, HVACMode.HEAT):
                target = self._target_temp_high if mode == HVACMode.COOL else self._target_temp_low
                current_target = _number(
                    self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_TEMPERATURE)
                )
                if target is not None and target != current_target:
                    await self._async_internal_set_temperature(target)

    @callback
    def _cancel_cycle(self):
        """Cancel pending control on removal, unavailability, or a new decision."""
        if self._unsub_cycle is not None:
            self._unsub_cycle()
            self._unsub_cycle = None

    async def _async_cycle_elapsed(self, now):
        """Retry a deferred mode switch even if the source never changes again."""
        self._unsub_cycle = None
        self._state_changed(self.hass.states.get(self.climate_entity_id), sync_target=False)
        await self._async_control_heating()
        self.async_write_ha_state()

    @property
    def supported_features(self):
        """Return the list of supported features."""
        features = SUPPORT_FLAGS
        modes = self.hvac_modes
        if HVACMode.HEAT_COOL in modes:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        if HVACMode.OFF in modes:
            features |= ClimateEntityFeature.TURN_OFF
        if any(mode != HVACMode.OFF for mode in modes):
            features |= ClimateEntityFeature.TURN_ON
        optional = (
            ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.PRESET_MODE
            | ClimateEntityFeature.SWING_MODE
        )
        return features | (
            ClimateEntityFeature(self._source_attributes.get("supported_features", 0)) & optional
        )

    # checks whether it is cold enough to switch to heating mode
    def _is_cold_enough(self):
        too_cold = self._cur_temp <= self._target_temp_high - self._cold_tolerance
        _LOGGER.debug(
            "_is_too_cold: %s| target high=%s,cur=%s,tolerance=%s",
            too_cold,
            self._target_temp_high,
            self._cur_temp,
            self._cold_tolerance,
        )
        return too_cold

    # checks whether it is hot enough to switch to cooling mode
    def _is_hot_enough(self):
        too_hot = self._cur_temp >= self._target_temp_low + self._hot_tolerance
        _LOGGER.debug(
            "_is_too_hot: %s| cur=%s,target low=%s,tolerance=%s",
            too_hot,
            self._cur_temp,
            self._target_temp_low,
            self._hot_tolerance,
        )
        return too_hot

    def _is_comfortable_temperature(self):
        return self._is_cold_enough() and self._is_hot_enough()

    async def _async_internal_set_hvac_mode(self, hvac_mode: str):
        """Set new hvac mode."""
        if hvac_mode not in self.hvac_modes:
            raise ServiceValidationError(f"Unsupported source HVAC mode: {hvac_mode}")

        data = {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_HVAC_MODE: hvac_mode}
        await self.hass.services.async_call(
            CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, data, blocking=True, context=self._context
        )

    async def _async_internal_set_temperature(self, temperature: float):
        """Set new hvac mode."""
        data = {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_TEMPERATURE: temperature}
        await self.hass.services.async_call(
            CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE, data, blocking=True, context=self._context
        )

    async def async_set_preset_mode(self, preset_mode: str):
        """Set new preset mode."""
        data = {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_PRESET_MODE: preset_mode}
        await self.hass.services.async_call(
            CLIMATE_DOMAIN, SERVICE_SET_PRESET_MODE, data, blocking=True, context=self._context
        )
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str):
        """Set new preset mode."""
        data = {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_FAN_MODE: fan_mode}
        await self.hass.services.async_call(
            CLIMATE_DOMAIN, SERVICE_SET_FAN_MODE, data, blocking=True, context=self._context
        )
        self.async_write_ha_state()

    async def async_set_swing_mode(self, swing_mode: str):
        """Set new preset mode."""
        data = {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_SWING_MODE: swing_mode}
        await self.hass.services.async_call(
            CLIMATE_DOMAIN, SERVICE_SET_SWING_MODE, data, blocking=True, context=self._context
        )
        self.async_write_ha_state()
