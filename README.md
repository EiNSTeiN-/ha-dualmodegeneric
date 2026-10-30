# Home Assistant - Dual Mode Thermostat for AC unit

This component allows adding a heat/cool mode to any climate entity which has both heat and cool but not heat/cool. Switching the underlying climate entity between the two modes is done automatically based on the temperature reported by the entity. All other climate modes are reported as-is and should work the same as they do for the underlying climate entity.



This component is based on [ha-dualmodegeneric](https://github.com/zacs/ha-dualmodegeneric) which is itself a fork of the mainline `generic_thermostat`.

## Installation (HACS) - Recommended
0. Have [HACS](https://custom-components.github.io/hacs/installation/manual/) installed, this will allow you to easily update
1. Add `https://github.com/EiNSTeiN-/ha-dualmodegeneric` as a [custom repository](https://custom-components.github.io/hacs/usage/settings/#add-custom-repositories) as Type: Integration
2. Click install under "Dual-Mode Thermostat for AC unit", restart your instance.

## Installation (Manual)
1. Download this repository as a ZIP (green button, top right) and unzip the archive
2. Copy the contents of `/custom_components/dualmode_generic` into `<config_dir>/custom_components/dualmode_ac/`.
   * You will need to create the `custom_components` folder if it does not exist
   * On Home Assistant OS the final location will be `/config/custom_components/dualmode_ac`
   * The installed directory must match the `dualmode_ac` integration domain. The repository retains its historic `dualmode_generic` source folder so existing HACS installations can update without cached-path errors.

Version 0.3.0 targets Home Assistant 2026.10 and later. The configuration platform,
integration domain, and entity unique IDs are unchanged. If an older manual install
used a directory named `dualmode_generic`, keep a copy of it outside
`custom_components`, then replace it with `dualmode_ac` rather than loading both copies.
Restart Home Assistant after updating Python code; the reload action reloads YAML
configuration, not imported Python modules.

## Configuration
Add the following to your configuration file

### Example Config
```yaml
climate:
  - platform: dualmode_ac
    name: My Thermostat
    unique_id: climate.my_thermostat
    climate_entity_id: climate.my_disappointing_ac_unit
    target_temp_high: 21
    target_temp_low: 19
    min_cycle_duration:
        minutes: 20
    cold_tolerance: 1
    hot_tolerance: 1
    initial_hvac_mode: heat_cool
```

Leave `target_temp_high` and `target_temp_low` empty to use the previously set value when the entity is reloaded. Otherwise the configured value will be used and previous value ignored.

The source must support both heating and cooling to expose the synthetic `heat_cool`
mode. Other source modes pass through. Optional fan, preset, and swing controls are
advertised only when the source supports them. When the source disappears or becomes
unavailable, the wrapper also becomes unavailable and clears its current temperature.

The existing tolerance convention is preserved: the comfortable interval runs from
`target_temp_low + hot_tolerance` to `target_temp_high - cold_tolerance`. Below that
interval the source heats to the low target; above it the source cools to the high
target. Within the interval, the source keeps its current mode and receives that mode's
target. The target range must be wide enough to contain both tolerances and at least
one source temperature step. Updating one boundary can move the other to preserve this
gap; explicitly supplying an invalid pair raises an error before sending any command.

`min_cycle_duration` delays automatic mode changes. A timer retries when the duration
expires, even if the temperature does not change again. Explicit mode/setpoint commands
bypass that delay, and switching off is always forwarded immediately.

## Development

The regression suite uses the real Home Assistant 2026.10.4 state, service, and event
APIs with simulated climate hardware. No live heating or cooling equipment is used.
Python 3.14.2 or later and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync --locked --group test
uv run pytest
uv run ruff check custom_components tests
uv run ruff format --check custom_components tests
```

## Reporting an Issue
1. Setup your logger to print debug messages for this component using:
```yaml
logger:
  default: info
  logs:
    custom_components.dualmode_ac: debug
```
2. Restart HA
3. Verify you're still having the issue
4. File an issue in this Github Repository containing your HA log (Developer section > Info > Load Full Home Assistant Log)
   * You can paste your log file at pastebin https://pastebin.com/ and submit a link.
   * Please include details about your setup (Pi, NUC, etc, docker?, HASSOS?)
   * The log file can also be found at `/<config_dir>/home-assistant.log`
