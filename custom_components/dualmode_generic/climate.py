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
    TEMP_CELSIUS,
)
from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity
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
    CURRENT_HVAC_COOL,
    CURRENT_HVAC_HEAT,
    CURRENT_HVAC_FAN,
    CURRENT_HVAC_DRY,
    CURRENT_HVAC_IDLE,
    CURRENT_HVAC_OFF,
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_DRY,
    HVAC_MODE_OFF,
    HVAC_MODE_HEAT_COOL,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_SWING_MODE,
    SERVICE_SET_TEMPERATURE,
    SUPPORT_PRESET_MODE,
    SUPPORT_TARGET_TEMPERATURE,
    SUPPORT_TARGET_TEMPERATURE_RANGE,
    SUPPORT_SWING_MODE,
    SUPPORT_FAN_MODE,
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
DEFAULT_NAME = "Proxy Climate"

CONF_SENSOR = "target_sensor"
CONF_CLIMATE_ENTITY_ID = "climate_entity_id"
CONF_SENSOR_ENTITY_ID = "sensor_entity_id"
CONF_TARGET_TEMP_HIGH = "target_temp_high"
CONF_TARGET_TEMP_LOW = "target_temp_low"
CONF_MIN_DUR = "min_cycle_duration"
CONF_COLD_TOLERANCE = "cold_tolerance"
CONF_HOT_TOLERANCE = "hot_tolerance"
CONF_INITIAL_HVAC_MODE = "initial_hvac_mode"
CONF_USE_CLIMATE_ENTITY_TEMP = "use_climate_entity_temp"  # Add this line for the new parameter
SUPPORT_FLAGS = SUPPORT_TARGET_TEMPERATURE | SUPPORT_SWING_MODE | SUPPORT_FAN_MODE | SUPPORT_PRESET_MODE | SUPPORT_TARGET_TEMPERATURE_RANGE

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_CLIMATE_ENTITY_ID): cv.entity_id,
        vol.Optional(CONF_SENSOR): cv.entity_id,
        vol.Optional(CONF_SENSOR_ENTITY_ID): cv.entity_id,
        vol.Optional(CONF_USE_CLIMATE_ENTITY_TEMP, default=False): cv.boolean,  # Add this line for the new parameter
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_COLD_TOLERANCE, default=DEFAULT_TOLERANCE): vol.Coerce(float),
        vol.Optional(CONF_HOT_TOLERANCE, default=DEFAULT_TOLERANCE): vol.Coerce(float),
        vol.Optional(CONF_TARGET_TEMP_HIGH): vol.Coerce(float),
        vol.Optional(CONF_TARGET_TEMP_LOW): vol.Coerce(float),
        vol.Optional(CONF_INITIAL_HVAC_MODE): vol.In(
            [HVAC_MODE_COOL, HVAC_MODE_HEAT, HVAC_MODE_FAN_ONLY, HVAC_MODE_DRY, HVAC_MODE_OFF, HVAC_MODE_HEAT_COOL]
        ),
        vol.Optional(CONF_UNIQUE_ID): cv.string,
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the proxy_climate platform."""

    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)

    name = config.get(CONF_NAME)
    climate_entity_id = config.get(CONF_CLIMATE_ENTITY_ID)
    sensor_entity_id = config.get(CONF_SENSOR)
    use_climate_entity_temp = config.get(CONF_USE_CLIMATE_ENTITY_TEMP)  # Add this line for the new parameter
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
            ProxyClimateThermostat(  # Change the class name here
                name,
                climate_entity_id,
                sensor_entity_id,
                target_temp_high,
                target_temp_low,
                min_cycle_duration,
                cold_tolerance,
                hot_tolerance,
                initial_hvac_mode,
                unit,
                unique_id,
                use_climate_entity_temp,  # Add this line
            )
        ]
    )

class ProxyClimateThermostat(ClimateEntity, RestoreEntity):
    """Representation of a Proxy Climate Thermostat device."""

    def __init__(
        self,
        name,
        climate_entity_id,
        sensor_entity_id,
        target_temp_high,
        target_temp_low,
        min_cycle_duration,
        cold_tolerance,
        hot_tolerance,
        initial_hvac_mode,
        unit,
        unique_id,
        use_climate_entity_temp,
    ):
        """Initialize the thermostat."""
        self._name = name
        self.climate_entity_id = climate_entity_id
        self.sensor_entity_id = sensor_entity_id
        self.min_cycle_duration = min_cycle_duration
        self._cold_tolerance = cold_tolerance
        self._hot_tolerance = hot_tolerance
        self._hvac_mode = None
        self._initial_hvac_mode = initial_hvac_mode
        self.use_climate_entity_temp = use_climate_entity_temp  # Add this line for the new parameter

        self._unit = unit
        self._current_temp = None
        self._target_temp = None
        self._target_temp_high = target_temp_high
        self._target_temp_low = target_temp_low
        self._swing_mode = None
        self._fan_mode = None
        self._operation_list = None
        self._away = None
        self._preset_mode = None
        self._operation = None
        self._unique_id = unique_id
        self._hvac_list = None

        # Set by async_added_to_hass
        self._async_remove_listener = None
        self._async_remove_state_listener = None

    async def async_added_to_hass(self):
        """Register callbacks."""
        # Restore state
        if self._target_temp is None:
            state = await self.async_get_last_state()
            if state:
                self._target_temp = float(state.attributes.get(ATTR_TARGET_TEMP))
                self._fan_mode = state.attributes.get(ATTR_FAN_MODE)
                self._swing_mode = state.attributes.get(ATTR_SWING_MODE)
                self._hvac_mode = state.attributes.get(ATTR_HVAC_MODE)
                self._operation = state.attributes.get(ATTR_HVAC_MODE)
                self._preset_mode = state.attributes.get(ATTR_PRESET_MODE)

        # Add listeners
        async_track_state_change(
            self.hass, [self.climate_entity_id, self.sensor_entity_id], self._async_sensor_changed
        )

        if self.min_cycle_duration:
            async_track_time_interval(
                self.hass, self._async_control_heating, self.min_cycle_duration
            )

        # Update initial state
        await self._async_update_temp()

        # Entity ID's for on and off states.
        self._hvac_list = await self.hass.helpers.condition.async_process_condition(
            self.climate_entity_id, True, "{% if states('" + self.climate_entity_id + "') in ['" + HVAC_MODE_HEAT + "', '" + HVAC_MODE_COOL + "'] %}on{% else %}off{% endif %}"
        )
        # Return True if the sensor is in a relevant state
        if self._hvac_list:
            _LOGGER.info(self._name + " enabled.")
        else:
            _LOGGER.info(self._name + " disabled.")
            self._hvac_mode = HVAC_MODE_OFF

        @callback
        def async_startup(event):
            """Update the state on startup."""
            if self._target_temp is None:
                return
            if self._operation is None:
                return

            _LOGGER.debug("Restoring state to target temp %s", self._target_temp)
            _LOGGER.debug("Restoring state to operation mode %s", self._operation)
            _LOGGER.debug("Restoring state to HVAC mode %s", self._hvac_mode)

            self._async_restore_state()
            self.async_schedule_update_ha_state(True)

        if self.hass.state == CoreState.running:
            async_startup(None)
        else:
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_START, async_startup)

    @property
    def name(self):
        """Return the name of the climate device."""
        return self._name

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    @property
    def state(self):
        """Return the current state."""
        if self.is_away_mode_on:
            return HVAC_MODE_OFF
        return self._hvac_mode

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        if self.use_climate_entity_temp:  # Use the climate entity's temperature
            return self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        return self._unit

    @property
    def current_temperature(self):
        """Return the current temperature."""
        if self.use_climate_entity_temp:  # Use the climate entity's temperature
            return self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_CURRENT_TEMPERATURE)
        return self._current_temp

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temp

    @property
    def target_temperature_high(self):
        """Return the highbound target temperature we try to reach."""
        return self._target_temp_high

    @property
    def target_temperature_low(self):
        """Return the lowbound target temperature we try to reach."""
        return self._target_temp_low

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        return self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_MIN_TEMP)

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        return self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_MAX_TEMP)

    @property
    def fan_mode(self):
        """Return the current fan mode."""
        return self._fan_mode

    @property
    def fan_modes(self):
        """Return the list of available fan modes."""
        return self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_FAN_MODES)

    @property
    def swing_mode(self):
        """Return the current swing mode."""
        return self._swing_mode

    @property
    def swing_modes(self):
        """Return the list of available swing modes."""
        return self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_SWING_MODES)

    @property
    def hvac_mode(self):
        """Return the current operation mode."""
        return self._operation

    @property
    def hvac_modes(self):
        """Return the list of available operation modes."""
        if self._hvac_list:
            return self._hvac_list
        return self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_HVAC_MODES)

    @property
    def preset_mode(self):
        """Return the current preset mode, e.g., home, away, temp."""
        return self._preset_mode

    @property
    def preset_modes(self):
        """Return a list of available preset modes."""
        return self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_PRESET_MODES)

    @property
    def is_away_mode_on(self):
        """Return if away mode is on."""
        return self.hass.states.get(self.climate_entity_id).state == HVAC_MODE_OFF

    async def async_set_temperature(self, **kwargs):
        """Set new target temperatures."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        target_temp_high = kwargs.get(ATTR_TARGET_TEMP_HIGH)
        target_temp_low = kwargs.get(ATTR_TARGET_TEMP_LOW)

        if temperature is None:
            return

        if self._target_temp_low is not None and self._target_temp_high is not None:
            if temperature < self._target_temp_low:
                self._target_temp_low = temperature
            elif temperature > self._target_temp_high:
                self._target_temp_high = temperature
            else:
                self._target_temp_low = temperature
                self._target_temp_high = temperature
        else:
            self._target_temp = temperature
            self._target_temp_low = None
            self._target_temp_high = None

        if self._hvac_mode == HVAC_MODE_HEAT_COOL:
            if self._target_temp_low is not None and self._target_temp_high is not None:
                await self.hass.services.async_call(
                    CLIMATE_DOMAIN,
                    SERVICE_SET_TEMPERATURE,
                    {
                        ATTR_ENTITY_ID: self.climate_entity_id,
                        ATTR_TARGET_TEMP_LOW: self._target_temp_low,
                        ATTR_TARGET_TEMP_HIGH: self._target_temp_high,
                    },
                )
            else:
                await self.hass.services.async_call(
                    CLIMATE_DOMAIN,
                    SERVICE_SET_TEMPERATURE,
                    {
                        ATTR_ENTITY_ID: self.climate_entity_id,
                        ATTR_TEMPERATURE: self._target_temp,
                    },
                )

        else:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_TEMPERATURE,
                {
                    ATTR_ENTITY_ID: self.climate_entity_id,
                    ATTR_TEMPERATURE: temperature,
                },
            )

    async def async_set_fan_mode(self, fan_mode):
        """Set new target fan mode."""
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_FAN_MODE,
            {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_FAN_MODE: fan_mode},
        )

    async def async_set_swing_mode(self, swing_mode):
        """Set new target swing operation."""
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_SWING_MODE,
            {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_SWING_MODE: swing_mode},
        )

    async def async_set_hvac_mode(self, hvac_mode):
        """Set new target operation mode."""
        if self._hvac_mode != HVAC_MODE_HEAT_COOL:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_HVAC_MODE,
                {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_HVAC_MODE: hvac_mode},
            )

    async def async_set_preset_mode(self, preset_mode):
        """Set new target operation mode."""
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_PRESET_MODE: preset_mode},
        )

    async def async_turn_away_mode_on(self):
        """Turn away mode on."""
        await self.hass.services.async_call(
            CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_HVAC_MODE: HVAC_MODE_OFF}
        )

    async def async_turn_away_mode_off(self):
        """Turn away mode off."""
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_HVAC_MODE: self._initial_hvac_mode},
        )

    async def async_turn_on(self):
        """Turn on."""
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_HVAC_MODE: self._initial_hvac_mode},
        )

    async def async_turn_off(self):
        """Turn off."""
        await self.hass.services.async_call(
            CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_HVAC_MODE: HVAC_MODE_OFF}
        )

    async def async_added_to_hass(self):
        """Register callbacks."""

    async def async_sensor_changed(self, entity_id, old_state, new_state):
        """Handle temperature changes."""
        if new_state is None or new_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return

        if entity_id == self.climate_entity_id:
            self._hvac_mode = new_state.state
            await self._async_update_temp()

        elif entity_id == self.sensor_entity_id:
            self._current_temp = new_state.state
            await self._async_update_temp()
            self.async_schedule_update_ha_state()

    async def _async_sensor_changed(self, entity_id, old_state, new_state):
        """Handle temperature changes."""
        if new_state is None or new_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return

        if entity_id == self.climate_entity_id:
            self._hvac_mode = new_state.state
            await self._async_update_temp()

        elif entity_id == self.sensor_entity_id:
            self._current_temp = new_state.state
            await self._async_update_temp()
            self.async_schedule_update_ha_state()

    async def _async_control_heating(self, _):
        """Call turn_on or turn_off based on hysteresis."""
        hvac_mode = self._hvac_mode
        climate_entity = self.hass.states.get(self.climate_entity_id)
        if climate_entity.attributes.get(ATTR_SWING_MODE) == self._swing_mode:
            swing = False
        else:
            swing = True

        hvac_modes = self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_HVAC_MODES)
        if self._operation == HVAC_MODE_HEAT_COOL:
            hvac_modes = [HVAC_MODE_HEAT, HVAC_MODE_COOL, HVAC_MODE_OFF]

        if self._swing_mode is not None:
            if (
                self._target_temp - self._hot_tolerance
                < self._current_temp
                and hvac_mode != HVAC_MODE_COOL
                and self._fan_mode != "on"
                and self._swing_mode == swing
            ):
                if (
                    self._hvac_mode != HVAC_MODE_HEAT
                    and hvac_modes.index(HVAC_MODE_HEAT)
                    < hvac_modes.index(hvac_mode)
                ):
                    await self.async_set_hvac_mode(HVAC_MODE_HEAT)
                elif (
                    self._hvac_mode != HVAC_MODE_COOL
                    and hvac_modes.index(HVAC_MODE_COOL)
                    < hvac_modes.index(hvac_mode)
                ):
                    await self.async_set_hvac_mode(HVAC_MODE_COOL)
                elif (
                    self._hvac_mode != HVAC_MODE_OFF
                    and hvac_modes.index(HVAC_MODE_OFF)
                    < hvac_modes.index(hvac_mode)
                ):
                    await self.async_set_hvac_mode(HVAC_MODE_OFF)
            elif (
                self._target_temp + self._cold_tolerance
                > self._current_temp
                and hvac_mode != HVAC_MODE_HEAT
                and self._fan_mode != "on"
                and self._swing_mode == swing
            ):
                if (
                    self._hvac_mode != HVAC_MODE_COOL
                    and hvac_modes.index(HVAC_MODE_COOL)
                    < hvac_modes.index(hvac_mode)
                ):
                    await self.async_set_hvac_mode(HVAC_MODE_COOL)
                elif (
                    self._hvac_mode != HVAC_MODE_HEAT
                    and hvac_modes.index(HVAC_MODE_HEAT)
                    < hvac_modes.index(hvac_mode)
                ):
                    await self.async_set_hvac_mode(HVAC_MODE_HEAT)
                elif (
                    self._hvac_mode != HVAC_MODE_OFF
                    and hvac_modes.index(HVAC_MODE_OFF)
                    < hvac_modes.index(hvac_mode)
                ):
                    await self.async_set_hvac_mode(HVAC_MODE_OFF)
            else:
                if self._hvac_mode != self._operation:
                    await self.async_set_hvac_mode(self._operation)
        else:
            if (
                self._target_temp - self._hot_tolerance
                < self._current_temp
                and hvac_mode != HVAC_MODE_COOL
                and self._fan_mode != "on"
            ):
                if (
                    self._hvac_mode != HVAC_MODE_HEAT
                    and hvac_modes.index(HVAC_MODE_HEAT)
                    < hvac_modes.index(hvac_mode)
                ):
                    await self.async_set_hvac_mode(HVAC_MODE_HEAT)
                elif (
                    self._hvac_mode != HVAC_MODE_COOL
                    and hvac_modes.index(HVAC_MODE_COOL)
                    < hvac_modes.index(hvac_mode)
                ):
                    await self.async_set_hvac_mode(HVAC_MODE_COOL)
                elif (
                    self._hvac_mode != HVAC_MODE_OFF
                    and hvac_modes.index(HVAC_MODE_OFF)
                    < hvac_modes.index(hvac_mode)
                ):
                    await self.async_set_hvac_mode(HVAC_MODE_OFF)
            elif (
                self._target_temp + self._cold_tolerance
                > self._current_temp
                and hvac_mode != HVAC_MODE_HEAT
                and self._fan_mode != "on"
            ):
                if (
                    self._hvac_mode != HVAC_MODE_COOL
                    and hvac_modes.index(HVAC_MODE_COOL)
                    < hvac_modes.index(hvac_mode)
                ):
                    await self.async_set_hvac_mode(HVAC_MODE_COOL)
                elif (
                    self._hvac_mode != HVAC_MODE_HEAT
                    and hvac_modes.index(HVAC_MODE_HEAT)
                    < hvac_modes.index(hvac_mode)
                ):
                    await self.async_set_hvac_mode(HVAC_MODE_HEAT)
                elif (
                    self._hvac_mode != HVAC_MODE_OFF
                    and hvac_modes.index(HVAC_MODE_OFF)
                    < hvac_modes.index(hvac_mode)
                ):
                    await self.async_set_hvac_mode(HVAC_MODE_OFF)
            else:
                if self._hvac_mode != self._operation:
                    await self.async_set_hvac_mode(self._operation)

    async def _async_update_temp(self):
        """Update thermostat with latest state from sensor."""
        if self.use_climate_entity_temp:  # Use the climate entity's temperature
            climate_entity = self.hass.states.get(self.climate_entity_id)
            self._current_temp = climate_entity.attributes.get(ATTR_CURRENT_TEMPERATURE)
        else:
            sensor_entity = self.hass.states.get(self.sensor_entity_id)
            if sensor_entity:
                if sensor_entity.state is not None and sensor_entity.state != STATE_UNKNOWN:
                    self._current_temp = float(sensor_entity.state)
                else:
                    self._current_temp = None
                    return
            else:
                self._current_temp = None
                return

        if self._current_temp is None:
            self._current_temp = self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_CURRENT_TEMPERATURE)
        if self._hvac_mode is None:
            self._hvac_mode = self.hass.states.get(self.climate_entity_id).state
        if self._unit is None:
            self._unit = self.hass.config.units.temperature_unit

        if self._target_temp is None:
            self._target_temp = float(
                self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_TEMPERATURE)
            )

        if self._operation is None:
            self._operation = self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_HVAC_ACTION)
        if self._preset_mode is None:
            self._preset_mode = self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_PRESET_MODE)
        if self._fan_mode is None:
            self._fan_mode = self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_FAN_MODE)
        if self._swing_mode is None:
            self._swing_mode = self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_SWING_MODE)
        if self._operation_list is None:
            self._operation_list = self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_HVAC_MODES)
        if self._hvac_list is None:
            self._hvac_list = self.hass.states.get(self.climate_entity_id).attributes.get(ATTR_HVAC_MODES)

    @callback
    def _async_restore_state(self):
        """Restore previous state."""
        self.async_schedule_update_ha_state(True)

    async def async_turn_on(self):
        """Turn on."""
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_HVAC_MODE: self._initial_hvac_mode},
        )

    async def async_turn_off(self):
        """Turn off."""
        await self.hass.services.async_call(
            CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_HVAC_MODE: HVAC_MODE_OFF}
        )

    async def async_update(self):
        """Update the state."""
        await self._async_update_temp()
        self.async_schedule_update_ha_state(True)
