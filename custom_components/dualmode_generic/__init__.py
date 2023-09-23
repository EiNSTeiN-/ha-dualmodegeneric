"""The dualmode_ac thermostat component."""

DOMAIN = "proxy_climate"
PLATFORMS = ["climate"]

async def async_setup(hass, config):
    """Set up the dualmode_ac climate component."""
    # No additional setup is required in this file
    return True
