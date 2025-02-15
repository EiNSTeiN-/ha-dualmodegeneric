"""
Transforms a heat pump AC entity which has separate cooling and heating into dual mode thermostat 
that have both heating and cooling.

Originally based on the script at this thread:
https://community.home-assistant.io/t/heat-cool-generic-thermostat/76443/2

Modified to better conform to modern Home Assistant custom_component style.
"""
import asyncio
import logging

import voluptuous as vol

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    UnitOfTemperature,
)
from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity, ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.components.climate.const import (
    ATTR_PRESET_MODE,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_STEP,
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_PRESET_MODES,
    ATTR_CURRENT_TEMPERATURE,
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_SWING_MODE,
    ATTR_SWING_MODES,
    ATTR_MIN_TEMP,
    ATTR_MAX_TEMP,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_SWING_MODE,
    SERVICE_SET_TEMPERATURE,
    DOMAIN as CLIMATE_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_NAME,
    CONF_UNIQUE_ID,
    EVENT_HOMEASSISTANT_START,
    STATE_UNKNOWN,
    STATE_UNAVAILABLE,
)
from homeassistant.core import CoreState, callback
from homeassistant.helpers import condition
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import (
    async_track_state_change,
    async_track_time_interval,
)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity

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
SUPPORT_FLAGS = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.SWING_MODE | ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.PRESET_MODE | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_CLIMATE_ENTITY_ID): cv.entity_id,
        vol.Optional(CONF_MIN_DUR): vol.All(cv.time_period, cv.positive_timedelta),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_COLD_TOLERANCE, default=DEFAULT_TOLERANCE): vol.Coerce(float),
        vol.Optional(CONF_HOT_TOLERANCE, default=DEFAULT_TOLERANCE): vol.Coerce(float),
        vol.Optional(CONF_TARGET_TEMP_HIGH): vol.Coerce(float),
        vol.Optional(CONF_TARGET_TEMP_LOW): vol.Coerce(float),
        vol.Optional(CONF_INITIAL_HVAC_MODE): vol.In(
            [HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY, HVACMode.DRY, HVACMode.OFF, HVACMode.HEAT_COOL]
        ),
        vol.Optional(CONF_UNIQUE_ID): cv.string,
    }
)


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

        self._support_flags = SUPPORT_FLAGS

        self._active = False
        self._cur_temp = None
        self._temp_lock = asyncio.Lock()
        self._target_temp_high = target_temp_high
        self._target_temp_low = target_temp_low
        self._unit = unit
        self._unique_id = unique_id

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        # Add listener for climate entity state
        self.async_on_remove(
            async_track_state_change(
                self.hass, self.climate_entity_id, self._async_climate_state_changed
            )
        )

        # Check If we have an old state
        old_state = await self.async_get_last_state()
        if old_state is not None:
            if self._target_temp_low is None:
                if old_state.attributes.get(ATTR_TARGET_TEMP_LOW) is not None:
                    self._target_temp_low = float(old_state.attributes[ATTR_TARGET_TEMP_LOW])
            if self._target_temp_high is None:
                if old_state.attributes.get(ATTR_TARGET_TEMP_HIGH) is not None:
                    self._target_temp_high = float(old_state.attributes[ATTR_TARGET_TEMP_HIGH])
            if not self._hvac_mode and old_state.state:
                self._hvac_mode = old_state.state

        if self._target_temp_low is None:
            self._target_temp_low = self.min_temp
        if self._target_temp_high is None:
            self._target_temp_high = self.max_temp
        if not self._hvac_mode:
            # Set default state to _initial_hvac_mode, or Off if not specified
            self._hvac_mode = self._initial_hvac_mode or HVACMode.OFF

        @callback
        def _async_startup(event=None):
            """Init on startup."""
            state = self.hass.states.get(self.climate_entity_id)
            if state:
                _LOGGER.info("Updating internal state from climate entity on startup")
                self._state_changed(state)
                self.async_write_ha_state()
            else:
                _LOGGER.info("Failed to update internal state from climate entity on startup because entity is not available")

        if self.hass.state == CoreState.running:
            _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

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
        state = self.hass.states.get(self.climate_entity_id)
        if state:
            return state.attributes[ATTR_TARGET_TEMP_STEP]
    
    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        # Since this integration does not yet have a step size parameter
        # we have to re-use the precision as the step size for now.
        return self.precision
    
    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        state = self.hass.states.get(self.climate_entity_id)
        if not state or ATTR_UNIT_OF_MEASUREMENT not in state.attributes:
            return UnitOfTemperature.CELSIUS
        return state.attributes[ATTR_UNIT_OF_MEASUREMENT]

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
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if self._hvac_mode == HVACMode.COOL:
            return HVACAction.COOLING if self._cur_temp and self._target_temp_high and self._cur_temp > self._target_temp_high else HVACAction.IDLE
        if self._hvac_mode == HVACMode.HEAT:
            return HVACAction.HEATING if self._cur_temp and self._target_temp_low and self._cur_temp < self._target_temp_low else HVACAction.IDLE
        if self._hvac_mode == HVACMode.FAN_ONLY:
            return HVACAction.FAN
        if self._hvac_mode == HVACMode.DRY:
            return HVACAction.DRYING
        if self._hvac_mode == HVACMode.HEAT_COOL:
            mode = self._climate_entity_hvac_mode()
            if mode == HVACMode.HEAT:
                return HVACAction.HEATING if self._cur_temp and self._target_temp_low and self._cur_temp < self._target_temp_low else HVACAction.IDLE
            elif mode == HVACMode.COOL:
                return HVACAction.COOLING if self._cur_temp and self._target_temp_high and self._cur_temp > self._target_temp_high else HVACAction.IDLE
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
        state = self.hass.states.get(self.climate_entity_id)
        if state and ATTR_HVAC_MODES in state.attributes:
            return [HVACMode.HEAT_COOL] + state.attributes[ATTR_HVAC_MODES]

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
        if hvac_mode in self.hvac_modes:
            self._hvac_mode = hvac_mode
            await self._async_control_heating(force=True)
        else:
            _LOGGER.error("Unrecognized hvac mode: %s", hvac_mode)
            return
        # Ensure we update the current operation after changing the mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        temp_low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        temp_high = kwargs.get(ATTR_TARGET_TEMP_HIGH)
        if temperature is not None:
            if self._hvac_mode == HVACMode.COOL:
                self._target_temp_high = temperature
            if self._hvac_mode == HVACMode.HEAT:
                self._target_temp_low = temperature

            mode = self._climate_entity_hvac_mode()
            if mode == self._hvac_mode and mode in (HVACMode.HEAT, HVACMode.COOL):
                await self._async_internal_set_temperature(temperature)
        if temp_low is not None:
            self._target_temp_low = temp_low
            if self._climate_entity_hvac_mode() == HVACMode.HEAT:
                await self._async_internal_set_temperature(temp_low)
        if temp_high is not None:
            self._target_temp_high = temp_high
            if self._climate_entity_hvac_mode() == HVACMode.COOL:
                await self._async_internal_set_temperature(temp_high)

        difference = max(self._cold_tolerance, self._hot_tolerance, 1)
        if self._target_temp_low > self._target_temp_high or (self._target_temp_high - self._target_temp_low) < difference:
                # If user adjusted the low temp, then adjust high temp by at least the expected difference.
                # And vice versa if they adjusted the high temp instead.
            if temp_low is not None:
                self._target_temp_high = self._target_temp_low + difference
            else:
                self._target_temp_low = self._target_temp_high - difference

        await self._async_control_heating(force=True)
        self.async_write_ha_state()

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        state = self.hass.states.get(self.climate_entity_id)
        if state and (value := state.attributes[ATTR_MIN_TEMP]) is not None:
            return value

        # Get default temp from super class
        return super().min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        state = self.hass.states.get(self.climate_entity_id)
        if state and (value := state.attributes[ATTR_MAX_TEMP]) is not None:
            return value

        # Get default temp from super class
        return super().max_temp

    async def _async_climate_state_changed(self, entity_id, old_state, new_state):
        """Handle temperature changes."""
        if not self._temp_lock.locked():
            _LOGGER.info("Received state change callback from climate entity")
            await self._state_changed(new_state)
            await self._async_control_heating()
            self.async_write_ha_state()
        else:
            _LOGGER.debug("Not processing climate state change")


    async def _state_changed(self, new_state):
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.debug("New entity state is not available")
            return

        if ATTR_CURRENT_TEMPERATURE in new_state.attributes and (temp := new_state.attributes[ATTR_CURRENT_TEMPERATURE]) is not None:
            self._async_update_temp(temp)
        else:
            _LOGGER.debug("Current temperature is not present in climate entity state")

        if ATTR_TEMPERATURE in new_state.attributes and (temp := new_state.attributes[ATTR_TEMPERATURE]) is not None:
            self._async_update_target_temp(new_state.state, temp)
        else:
            _LOGGER.debug("Current temperature is not present in climate entity state")

        if self._hvac_mode == HVACMode.HEAT_COOL:
            if new_state.state not in [HVACMode.HEAT, HVACMode.COOL]:
                _LOGGER.info("Climate entity state change to %s while thermostat was set to %s", new_state.state, self._hvac_mode)
                self._hvac_mode = new_state.state
        elif self._hvac_mode != new_state.state:
            _LOGGER.info("Climate entity state change to %s while thermostat was set to %s", new_state.state, self._hvac_mode)
            self._hvac_mode = new_state.state

        if ATTR_MIN_TEMP in new_state.attributes and (min_temp := new_state.attributes[ATTR_MIN_TEMP]) is not None:
            if self._target_temp_low is None or self._target_temp_low < min_temp:
                self._target_temp_low = min_temp

        if ATTR_MAX_TEMP in new_state.attributes and (max_temp := new_state.attributes[ATTR_MAX_TEMP]) is not None:
            if self._target_temp_high is None or self._target_temp_high > max_temp:
                self._target_temp_high = max_temp

    @callback
    def _async_update_temp(self, temp):
        """Update thermostat with latest state from sensor."""
        try:
            _LOGGER.debug("New current temperature from entity: %s", temp)
            self._cur_temp = float(temp)
        except ValueError as ex:
            _LOGGER.error("Unable to update from sensor: %s", ex)

    @callback
    def _async_update_target_temp(self, new_state, temp):
        """Update thermostat with latest state from climate entity."""
        try:
            if new_state == HVACMode.HEAT:
                if self._hvac_mode in (HVACMode.HEAT, HVACMode.HEAT_COOL) and self._target_temp_low != float(temp):
                    _LOGGER.info("New target_temp_low %s changed from %s for %s mode", temp, self._target_temp_low, new_state)
                    self._target_temp_low = float(temp)
            elif new_state == HVACMode.COOL:
                if self._hvac_mode in (HVACMode.COOL, HVACMode.HEAT_COOL) and self._target_temp_high != float(temp):
                    _LOGGER.info("New target_temp_high %s changed from %s for %s mode", temp, self._target_temp_high, new_state)
                    self._target_temp_high = float(temp)

        except ValueError as ex:
            _LOGGER.error("Temperature updated in : %s", ex)

    def _climate_entity_hvac_mode(self):
        """List of available operation modes."""
        state = self.hass.states.get(self.climate_entity_id)
        if state:
            return state.state

    async def _async_control_heating(self, force=False):
        """Check if we need to turn heating on or off."""
        async with self._temp_lock:
            if not self._active and None not in (self._cur_temp, self._target_temp_high, self._target_temp_low):
                self._active = True
                _LOGGER.info("Obtained current and target temperature. Generic Dual-mode thermostat active.")

            if not self._active or self._hvac_mode == HVACMode.OFF:
                return

            # This variable is used for the long_enough condition and for the LOG Messages
            if not force:
                # If the `force` argument is True, we
                # ignore `min_cycle_duration`.
                # If the `time` argument is not none, we were invoked for
                # keep-alive purposes, and `min_cycle_duration` is irrelevant.
                if self.min_cycle_duration:
                    long_enough = condition.state(
                        self.hass,
                        self.climate_entity_id,
                        self._climate_entity_hvac_mode(),
                        self.min_cycle_duration,
                    )
                    if not long_enough:
                        return

            if self._hvac_mode == HVACMode.HEAT_COOL:
                if self._is_comfortable_temperature():
                    # Do nothing except prevent the other two conditions from triggering
                    pass
                elif self._is_hot_enough():
                    _LOGGER.info("Turning on cooling mode with target=%s", self._target_temp_high)
                    if await self._async_internal_set_hvac_mode(HVACMode.COOL):
                        await self._async_internal_set_temperature(self._target_temp_high)

                elif self._is_cold_enough():
                    _LOGGER.info("Turning on heating mode with target=%s", self._target_temp_low)
                    if await self._async_internal_set_hvac_mode(HVACMode.HEAT):
                        await self._async_internal_set_temperature(self._target_temp_low)
            else:
                _LOGGER.info("Turning on %s mode", self._hvac_mode)
                if await self._async_internal_set_hvac_mode(self._hvac_mode):
                    if self._hvac_mode == HVACMode.COOL:
                        await self._async_internal_set_temperature(self._target_temp_high)
                    if self._hvac_mode == HVACMode.HEAT:
                        await self._async_internal_set_temperature(self._target_temp_low)

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._support_flags

    # checks whether it is cold enough to switch to heating mode
    def _is_cold_enough(self):
        # Use the midpoint in the set range as our target temp when in range mode
        # return ((self._target_temp_low + self._target_temp_high)/2) >= self._cur_temp + self._cold_tolerance
        too_cold = self._cur_temp <= self._target_temp_high - self._cold_tolerance
        _LOGGER.info(
            "_is_too_cold: %s| target high=%s,cur=%s,tolerance=%s",
            too_cold, self._target_temp_high, self._cur_temp, self._cold_tolerance
        )
        return too_cold

    # checks whether it is hot enough to switch to cooling mode
    def _is_hot_enough(self):
        too_hot = self._cur_temp >= self._target_temp_low + self._hot_tolerance
        _LOGGER.info(
            "_is_too_hot: %s| cur=%s,target low=%s,tolerance=%s",
            too_hot, self._cur_temp, self._target_temp_low, self._hot_tolerance
        )
        return too_hot

    def _is_comfortable_temperature(self):
        return self._is_cold_enough() and self._is_hot_enough()

    async def _async_internal_set_hvac_mode(self, hvac_mode: str):
        """Set new hvac mode."""
        if hvac_mode not in self.hvac_modes:
            return

        data = {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_HVAC_MODE: hvac_mode}
        result = await self.hass.services.async_call(CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, data, blocking=True)
        if not result:
            _LOGGER.debug("%s service call failed", SERVICE_SET_HVAC_MODE)
        self.async_write_ha_state()
        return result

    async def _async_internal_set_temperature(self, temperature: float):
        """Set new hvac mode."""
        data = {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_TEMPERATURE: temperature}
        result = await self.hass.services.async_call(CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE, data, blocking=True)
        if not result:
            _LOGGER.debug("%s service call failed", SERVICE_SET_TEMPERATURE)
        self.async_write_ha_state()
        return result

    async def async_set_preset_mode(self, preset_mode: str):
        """Set new preset mode."""
        data = {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_PRESET_MODE: preset_mode}
        result = await self.hass.services.async_call(CLIMATE_DOMAIN, SERVICE_SET_PRESET_MODE, data, blocking=True)
        if not result:
            _LOGGER.debug("%s service call failed", SERVICE_SET_PRESET_MODE)
        self.async_write_ha_state()
        return result

    async def async_set_fan_mode(self, fan_mode: str):
        """Set new preset mode."""
        data = {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_FAN_MODE: fan_mode}
        result = await self.hass.services.async_call(CLIMATE_DOMAIN, SERVICE_SET_FAN_MODE, data, blocking=True)
        if not result:
            _LOGGER.debug("%s service call failed", SERVICE_SET_FAN_MODE)
        self.async_write_ha_state()
        return result

    async def async_set_swing_mode(self, swing_mode: str):
        """Set new preset mode."""
        data = {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_SWING_MODE: swing_mode}
        result = await self.hass.services.async_call(CLIMATE_DOMAIN, SERVICE_SET_SWING_MODE, data, blocking=True)
        if not result:
            _LOGGER.debug("%s service call failed", SERVICE_SET_SWING_MODE)
        self.async_write_ha_state()
        return result
