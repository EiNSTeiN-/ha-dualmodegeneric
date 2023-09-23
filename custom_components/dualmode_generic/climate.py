# ... (previous code)

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
            [HVAC_MODE_COOL, HVAC_MODE_HEAT, HVAC_MODE_FAN_ONLY, HVAC_MODE_DRY, HVAC_MODE_OFF, HVAC_MODE_HEAT_COOL]
        ),
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Optional(CONF_TEMPERATURE_SENSOR_ENTITY_ID): cv.entity_id,  # New option for temperature sensor
    }
)

# ... (previous code)

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
            temperature_sensor_entity_id,  # New parameter for temperature sensor entity ID
    ):
        """Initialize the thermostat."""
        self._name = name
        self.climate_entity_id = climate_entity_id
        self.temperature_sensor_entity_id = temperature_sensor_entity_id  # Store the temperature sensor entity ID

        # ... (previous code)

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        # Add listener for climate entity state
        self.async_on_remove(
            async_track_state_change(
                self.hass, self.climate_entity_id, self._async_climate_state_changed
            )
        )

        # Add listener for temperature sensor entity state (new)
        self.async_on_remove(
            async_track_state_change(
                self.hass, self.temperature_sensor_entity_id, self._async_temperature_sensor_state_changed
            )
        )

        # ... (previous code)

    # ... (previous code)

    @callback
    def _async_update_temp(self, temp):
        """Update thermostat with latest state from sensor."""
        try:
            _LOGGER.debug("New current temperature from entity: %s", temp)
            self._cur_temp = float(temp)
        except ValueError as ex:
            _LOGGER.error("Unable to update from sensor: %s", ex)

    # New method to handle temperature sensor state changes
    async def _async_temperature_sensor_state_changed(self, entity_id, old_state, new_state):
        """Handle temperature sensor changes."""
        if not self._temp_lock.locked():
            _LOGGER.info("Received state change callback from temperature sensor entity")
            if new_state is not None and new_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                self._async_update_temp(new_state.state)
                await self._async_control_heating()
                self.async_write_ha_state()
        else:
            _LOGGER.debug("Not processing temperature sensor state change")

    def _get_current_temperature(self):
        """Get the current temperature from the sensor."""
        state = self.hass.states.get(self.temperature_sensor_entity_id)
        if state and (temp := state.state) is not None:
            try:
                return float(temp)
            except ValueError as ex:
                _LOGGER.error("Unable to get current temperature from sensor: %s", ex)
        return None

    # ... (previous code)
