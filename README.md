# ha-bticino-c100x

Home Assistant custom integration for **BTicino Classe 100X** video door entry
(Door Entry / Legrand Eliot cloud).

This is a **community continuation** of the original
[`alpichlabs/ha-bticino-c100x`](https://github.com/alpichlabs/ha-bticino-c100x)
project (`0.2.0-beta.28`). That GitHub repository is no longer available (404).
Credit and thanks to the original alpichlabs work — this repo preserves the
protocol and behaviour, with media/doorbell/card fixes described in
[CHANGELOG.md](CHANGELOG.md). Maintainer: [@bordeo](https://github.com/bordeo).

> **Not affiliated** with BTicino, Legrand, or Netatmo. Door Entry is a trademark
> of their respective owners. Use at your own risk.

**Licensing:** The licence of the original alpichlabs repository is unknown. No
`LICENSE` file is published here so as not to invent one. Treat reuse accordingly.

## Features

- Live **video + audio** from the outdoor unit (WebRTC via Home Assistant)
- **Two-way microphone** through the bundled Lovelace card (HTTPS required)
- **Doorbell** event entity (`device_class: doorbell`) for the Doorbell trigger
  and HomeKit linked doorbell, with 5 s ring debounce
- **Door release** button for the electric strike
- SIP registration diagnostics, monitoring session state, certificate expiry

## Install (HACS)

1. In HACS → Integrations → ⋮ → **Custom repositories**, add:

   `https://github.com/bordeo/ha-bticino-c100x` as category **Integration**.

2. Or use My Home Assistant:

   [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bordeo&repository=ha-bticino-c100x&category=integration)

3. Install **BTicino C100X**, then restart Home Assistant.

## Setup

1. Settings → Devices & services → **Add Integration** → **BTicino C100X**.
2. Sign in with the same **email / password** as the official Door Entry app.
3. Pick the installation (home) and the connected door release(s).

Requirements: Home Assistant with `ffmpeg`, outbound HTTPS to Legrand/Eliot, and
UDP reachability for SIP/media (same network expectations as the mobile app).

## Entities

| Type | Name | Notes |
| --- | --- | --- |
| Camera | Front door / outdoor unit | WebRTC stream after Start monitoring |
| Button | Start monitoring | Begins a view session |
| Button | End session | Ends the shared session |
| Button | Release door | Pulses the electric strike |
| Event | Doorbell | `ring` — Doorbell trigger / HomeKit |
| Binary sensor | Ringing | Active during the debounce window |
| Binary sensor | Monitoring session | `session_state` attribute |
| Binary sensor | Microphone | Mic uplink active |
| Sensor | SIP registration | Diagnostic |
| Sensor | SIP certificate expiry | Diagnostic |
| Sensor | Last ring | Diagnostic |
| Sensor | Last media error | Diagnostic |

Exact entity IDs follow your config entry title (e.g. `camera.demo_front_door`).

## Lovelace card

The integration registers `custom:bticino-c100x-card`. Example:

```yaml
type: custom:bticino-c100x-card
camera_entity: camera.YOUR_TITLE_front_door
start_entity: button.YOUR_TITLE_start_monitoring
end_entity: button.YOUR_TITLE_end_session
release_entity: button.YOUR_TITLE_release_door
entry_id: YOUR_CONFIG_ENTRY_ID
# optional:
# session_entity: binary_sensor.YOUR_TITLE_monitoring_session
```

Find `entry_id` under Developer tools → States (any entity from this integration)
or in `.storage/core.config_entries`.

**Microphone requires HTTPS** (or localhost). The iOS Home Assistant app over
plain HTTP cannot capture audio; the card shows that clearly and disables mic.

## Development / tests

```bash
pip install -r tests/requirements-test.txt
pytest tests/ --ignore=tests/card
```

Card Playwright harness: see [tests/card/README.md](tests/card/README.md).

## Disclaimer

Unofficial community software. Not affiliated with, endorsed by, or supported by
BTicino, Legrand, or Netatmo. Cloud API and SIP details may change without notice.
