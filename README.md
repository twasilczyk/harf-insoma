# Insoma RF

> [!WARNING]
> This project is mostly AI generated and not well reviewed.
>
> USE AT YOUR OWN RISK

Control an Insoma sprinkler or garden hose valve from Home Assistant through a
433.92 MHz radio transmitter. Each valve has an **on/off switch** and one
**watering duration setting**.

Turning on always sends a timed command: **5, 10, 30, 60, 90, or 120 minutes**,
with **120 minutes** as the default. The valve uses its own timer to close even
if Home Assistant stops running. This integration never sends the remote's
permanent-on command. Turning off sends an explicit off command.

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=twasilczyk&repository=harf-insoma&category=integration)

## Requirements

- Home Assistant **2026.9.2 or newer**.
- An RF transmitter supporting **433.92 MHz OOK**, exposed as a configured
  `radio_frequency` entity.
- An Insoma valve using the 16-bit protocol described below, and its remote's
  captured **OFF code**.

The integration uses Home Assistant's `radio_frequency` integration. An ordinary
ESPHome remote transmitter without a compatible `radio_frequency` entity is not
sufficient.

## Install with HACS

With [HACS installed](https://www.hacs.xyz/docs/use/download/download/), use the
button above or:

1. Open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/twasilczyk/harf-insoma` with type **Integration**.
3. Find **Insoma RF** in HACS and download it.
4. Restart Home Assistant.
5. Open **Settings → Devices & services → Add integration → Insoma RF**.

### Manual installation

Copy `custom_components/harf_insoma` from this repository into your Home Assistant
configuration directory's `custom_components` folder. Restart Home Assistant,
then add **Insoma RF** through **Settings → Devices & services**.

## Configure a valve

During setup, select the transmitter and enter the full
16-bit **OFF code** captured from that valve's paired remote. Use hexadecimal
with a `0x` prefix or decimal, for example:

| Remote | Hexadecimal OFF code | Decimal OFF code |
| --- | --- | --- |
| Example 1 | `0xB750` | `46928` |
| Example 2 | `0x24E0` | `9440` |

These are example captures; use the code from your own remote. A hexadecimal OFF
code ends in `0`. Setup rejects codes whose lowest four bits contain a command.
The integration does not pair a new remote with the valve.

Add the integration again for another valve. Valves can share the same
transmitter.

## Watering duration

The valve's device page contains its on/off switch and a duration dropdown under
**Configuration**. The duration is stored locally in Home Assistant and survives
restarts. Changing it sends no radio command and does not change watering already
in progress; it takes effect the next time you turn the switch on.

For example, choose **30 minutes** and turn on: Home Assistant sends the remote's
30-minute command. Turn off at any time to send the off command.

## State and shutoff

The integration does not receive acknowledgements or state reports. The switch shows
an **assumed state** based on commands sent by this integration. Home Assistant
marks it off when the expected watering time expires; that local deadline does
not control the valve's hardware timer. After a restart, a saved run with a valid
future deadline resumes its remaining countdown without transmitting. An expired
run is shown as off; without a trustworthy saved state, the switch starts as
unknown. Home Assistant restarts never resend an on command.

Using the physical remote, radio interference, or an out-of-range transmitter
can make the displayed state differ from the valve. The integration cannot
confirm water flow or that a command arrived.

## Protocol

The remote uses a 16-bit frame: the upper 12 bits identify the remote and the
lower four bits select a command.

| Command | Low four bits | Example frame |
| --- | --- | --- |
| Off | `0x0` | `0xB750` |
| 5 minutes | `0x2` | `0xB752` |
| 10 minutes | `0x4` | `0xB754` |
| 30 minutes | `0x6` | `0xB756` |
| 60 minutes | `0x8` | `0xB758` |
| 90 minutes | `0xA` | `0xB75A` |
| 120 minutes | `0xE` | `0xB75E` |

The permanent-on command (`0xC`) is intentionally not available.

Transmission uses a 300 µs pulse unit, a `1:2` high/low zero, a `2:1` high/low
one, and a `1:7` sync pulse, repeated 180 times without an additional inter-frame
gap.

## Development and releases

Use Python **3.14.2 or newer in the 3.14 series**:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest -q
```

GitHub Actions run the tests, Hassfest, HACS validation, and a tag/manifest version
check. See [RELEASING.md](RELEASING.md) for publishing and hardware verification.

Without GitHub releases, HACS uses the repository's default branch. Published
GitHub releases provide versioned updates; pushing a tag alone does not publish
a release. See [HACS versioning](https://www.hacs.xyz/docs/publish/start/#versions).

Report problems in the [issue tracker](https://github.com/twasilczyk/harf-insoma/issues).
