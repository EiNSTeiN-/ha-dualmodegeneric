```markdown
# Home Assistant - Dual Mode Thermostat for AC unit

This component allows adding a heat/cool mode to any climate entity which has both heat and cool but not heat/cool. Switching the underlying climate entity between the two modes is done automatically based on the temperature reported by the entity. All other climate modes are reported as-is and should work the same as they do for the underlying climate entity.

This component is based on [ha-dualmodegeneric](https://github.com/EiNSTeiN-/ha-dualmodegeneric), which is a [fork of](https://github.com/zacs/ha-dualmodegeneric) which is itself a fork of the mainline `generic_thermostat`.

## Installation (HACS) - Recommended
0. Have [HACS](https://custom-components.github.io/hacs/installation/manual/) installed, this will allow you to easily update.
1. Add `https://github.com/jack3308/ha-proxy-climate` as a [custom repository](https://custom-components.github.io/hacs/usage/settings/#add-custom-repositories) as Type: Integration.
2. Click install under "Dual-Mode Thermostat for AC unit" and restart your Home Assistant instance.

## Installation (Manual)
1. Download this repository as a ZIP (green button, top right) and unzip the archive.
2. Copy `/custom_components/proxy_climate` to your `<config_dir>/custom_components/` directory.
   - You will need to create the `custom_components` folder if it does not exist.
   - On Hassio, the final location will be `/config/custom_components/proxy_climate`.
   - On Hassbian, the final location will be `/home/homeassistant/.homeassistant/custom_components/proxy_climate`.

## Configuration
Add the following to your configuration file:

### Example Config
```yaml
climate:
  - platform: dualmode_ac
    name: My Thermostat
    unique_id: climate.my_thermostat
    climate_entity_id: climate.my_disappointing_ac_unit
    custom_temp_entity_id: sensor.where_people_actually_are
    target_temp_high: 21
    target_temp_low: 19
    min_cycle_duration:
      minutes: 20
    cold_tolerance: 1
    hot_tolerance: 1
    initial_hvac_mode: heat_cool
```

Leave `target_temp_high` and `target_temp_low` empty to use the previously set value when the entity is reloaded. Otherwise, the configured value will be used, and the previous value will be ignored.

## Reporting an Issue
1. Set up your logger to print debug messages for this component using:
```yaml
logger:
  default: info
  logs:
    custom_components.proxy_climate: debug
```
2. Restart HA.
3. Verify you're still having the issue.
4. File an issue in this GitHub Repository containing your HA log (Developer section > Info > Load Full Home Assistant Log):
   - You can paste your log file at [pastebin](https://pastebin.com/) and submit a link.
   - Please include details about your setup (Pi, NUC, etc., Docker?, HASSOS?).
   - The log file can also be found at `/<config_dir>/home-assistant.log`.
```