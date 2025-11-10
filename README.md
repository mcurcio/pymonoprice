## Status
[![Build Status](https://github.com/etsinko/pymonoprice/actions/workflows/ci.yml/badge.svg)](https://github.com/etsinko/pymonoprice/actions/workflows/ci.yml)[![Coverage Status](https://coveralls.io/repos/github/etsinko/pymonoprice/badge.svg)](https://coveralls.io/github/etsinko/pymonoprice)

# pymonoprice
Python3 interface implementation for Monoprice 6 zone amplifier

## Notes
This is for use with [Home-Assistant](http://home-assistant.io)

## Usage
```python
from pymonoprice import get_monoprice

monoprice = get_monoprice('/dev/ttyUSB0')
# Automatically detects Monoprice 10761 (6-zone) and 31028 (12-channel) amplifiers.
# You can force a specific model via the optional "model" argument, e.g.
# monoprice = get_monoprice('/dev/ttyUSB0', model='31028')
# Valid zones are 11-16 for main monoprice amplifier
zone_status = monoprice.zone_status(11)

# Print zone status
print('Zone Number = {}'.format(zone_status.zone))
print('Power is {}'.format('On' if zone_status.power else 'Off'))
print('Mute is {}'.format('On' if zone_status.mute else 'Off'))
print('Public Anouncement Mode is {}'.format('On' if zone_status.pa else 'Off'))
print('Do Not Disturb Mode is {}'.format('On' if zone_status.do_not_disturb else 'Off'))
print('Volume = {}'.format(zone_status.volume))
print('Treble = {}'.format(zone_status.treble))
print('Bass = {}'.format(zone_status.bass))
print('Balance = {}'.format(zone_status.balance))
print('Source = {}'.format(zone_status.source))
print('Keypad is {}'.format('connected' if zone_status.keypad else 'disconnected'))

# Turn off zone #11
monoprice.set_power(11, False)

# Mute zone #12
monoprice.set_mute(12, True)

# Set volume for zone #13
monoprice.set_volume(13, 15)

# Set source 1 for zone #14 
monoprice.set_source(14, 1)

# Set treble for zone #15
monoprice.set_treble(15, 10)

# Set bass for zone #16
monoprice.set_bass(16, 7)

# Set balance for zone #11
monoprice.set_balance(11, 3)

# Restore zone #11 to it's original state
monoprice.restore_zone(zone_status)
```

### Selecting an amplifier protocol

`get_monoprice` and `get_async_monoprice` now auto-detect whether the attached
amplifier speaks the original 10761 protocol or the 12-channel 31028 protocol.
If auto-detection is not possible (for example when using a mocked serial port),
pass `model='10761'` or `model='31028'` explicitly. Note that the 31028 hardware
does not expose treble/bass controls, so the corresponding methods will simply
log a warning when that model is selected.

## Usage with asyncio

With `asyncio` flavor all methods of Monoprice object are coroutines.

```python
import asyncio
from pymonoprice import get_async_monoprice

async def main(loop):
    monoprice = await get_async_monoprice('/dev/ttyUSB0')
    zone_status = await monoprice.zone_status(11)
    if zone_status.power:
        await monoprice.set_power(zone_status.zone, False)

loop = asyncio.get_event_loop()
loop.run_until_complete(main(loop))

```

## Command line tool

Installing `pymonoprice` now exposes the `pymonoctl` helper, a thin wrapper around
the Python API for quick inspections or scripted changes:

```bash
# Read the current status of zones 11 and 12 using a USB serial adapter
pymonoctl --port /dev/ttyUSB0 status --zone 11 --zone 12 --json

# Update power and volume for zone 11
pymonoctl --port /dev/ttyUSB0 set --zone 11 power=on volume=15

# Talk to a TCP serial bridge (PySerial socket:// URL is generated automatically)
pymonoctl --host 192.0.2.10 --tcp-port 5000 status --unit 1
```

### Applying configuration files

`pymonoctl apply-config` consumes a small JSON/YAML mapping. Each zone key maps to
the attributes you would normally pass through the library:

```yaml
zones:
  11:
    power: true
    volume: 15
    source: 3
  12:
    mute: false
```

Save the file (e.g. `amp.yaml`) and run `pymonoctl --port /dev/ttyUSB0 apply-config amp.yaml`.
YAML parsing requires [`PyYAML`](https://pyyaml.org/); JSON works out of the box.
