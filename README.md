# Purolator Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-purolator.svg)](https://github.com/ha-parcel-integrations/ha-purolator/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-purolator/total.svg)](https://github.com/ha-parcel-integrations/ha-purolator/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Purolator](https://www.purolator.com) parcels in Canada. No account is needed — you enter the tracking code (PIN) yourself, just like on the Purolator website.

> [!WARNING]
> **Pre-1.0.** The integration has been confirmed against delivered parcels only. Purolator can change or restrict access to its public tracking endpoint at any time, in which case updates fail until the integration is updated. Statuses for returns and exceptions have not been observed yet; an unrecognised status shows as `unknown` and logs a warning with a link to report it.

Part of the [ha-parcel-integrations](https://ha-parcel-integrations.github.io/) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Dynamic polling](#dynamic-polling)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Track any number of Purolator parcels by tracking code — no account needed
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), the carrier's own status text, the expected delivery date and a tracking deep-link
- Incoming and outgoing parcels tracked side by side, counted separately
- Summary sensors: incoming parcels, parcels awaiting pickup, next delivery, recently delivered parcels, outgoing parcels, delivered outgoing parcels
- Read-only **Deliveries** calendar with the expected delivery dates
- `purolator.track_parcel` / `purolator.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed — plus status changed and delivered for outgoing parcels)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2024.12 or newer
- A Purolator parcel and its tracking code (PIN, from the shipping
  confirmation email or the notice card) — no account needed

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-purolator` as an **Integration**.
3. Install **Purolator** and restart Home Assistant.

### Manual

Copy `custom_components/purolator` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → Purolator**. There is nothing to fill in: the hub is created immediately (Purolator tracking needs no account).

Then add parcels via the integration's **Configure** dialog, the [`purolator.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). The PIN is on your shipping confirmation email or the notice card.

### Incoming and outgoing parcels

Parcels you are expecting and parcels you sent are kept in two separate lists, each with its own menu entry under **Configure**, and are counted by separate sensors — so a parcel you shipped does not inflate your incoming count.

Which list a code belongs in is your call, not the carrier's: Purolator's tracking data does not say whether you sent or are receiving a parcel, and there is no account to compare against. A code entered in the other list — or re-added through `track_parcel` with the other direction — moves rather than being duplicated, which is how you correct a parcel filed the wrong way.

## Options

**Configure** opens a menu with **Incoming parcels**, **Outgoing parcels** and **Settings**:

| Menu entry | Option | Default | Description |
|---|---|---|---|
| Incoming parcels | Tracking codes | — | The parcels you are expecting. Changes apply immediately, no restart. |
| Outgoing parcels | Tracking codes | — | The parcels you sent. Counted separately from the incoming ones. |
| Settings | Delivered parcels: filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Settings | Include status history | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule with nothing to configure.

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence to
what your tracked parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 30 minutes)** — while any tracked parcel is out for delivery
  today, starting an hour before its estimated delivery date begins (or
  immediately if no date is known yet).
- **Normal (every 60 minutes)** — for anything else still on its way.
- **Fully paused** — once every tracked parcel has been delivered, or nothing
  is tracked at all, polling stops until you add a parcel back (adding one
  always triggers an immediate check, regardless of the pause).
- A small, fixed per-hub offset is added on top, so not every Purolator
  hub out there polls at exactly the same second.

Purolator only reports an estimated delivery *date*, not a time window, so
that date is treated as the start of an expected delivery day. The cadence is
deliberately slower than other carriers, and the integration backs off when
Purolator refuses a request. It never falls back to any other method.

## Removal

Standard HA removal applies: **Settings → Devices & Services → Purolator → ⋮ → Delete**. Nothing is stored on Purolator's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.purolator_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.purolator_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.purolator_awaiting_pickup` | Number of incoming parcels waiting at a pickup point, full list under the `parcels` attribute |
| `sensor.purolator_next_delivery` | Earliest expected delivery moment across all active parcels |
| `sensor.purolator_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.purolator_outgoing_parcels` | Number of active parcels you sent, full list under the `parcels` attribute |
| `sensor.purolator_outgoing_delivered_parcels` | Parcels you sent that have since been delivered (see the retention option) |
| `sensor.purolator_last_successful_update` | Diagnostic: when Purolator was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `registered` | Label information received by Purolator |
| `in_transit` | Picked up or moving through the sort network |
| `out_for_delivery` | On the delivery vehicle |
| `at_pickup_point` | Available for pickup |
| `delivered` | Delivered |
| `problem` | A delivery attempt failed |
| `unknown` | Not found yet, or a status that is not mapped yet |

`returning` is not reported: no return statuses have been observed.

Event times from Purolator are local wall-clock times without a timezone. The province of each scan sets the zone (provinces that span two zones use the larger one); when a scan has no province, Home Assistant's configured timezone is used instead. Weight is converted from pounds to kilograms and is only shown for single-piece shipments.

The carrier's own human-readable text is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the Purolator device):

| Event | When |
|---|---|
| `purolator_parcel_registered` | A new parcel appears in the active list |
| `purolator_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `purolator_parcel_delivered` | A parcel is delivered |
| `purolator_parcel_delivery_time_changed` | The expected delivery window changes |
| `purolator_outgoing_parcel_status_changed` | A parcel you sent changes status, except the final hop to delivered |
| `purolator_outgoing_parcel_delivered` | A parcel you sent is delivered |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up. There is no `registered` event for an outgoing parcel: you already know you handed it over.

## Services

| Service | Fields | Description |
|---|---|---|
| `purolator.track_parcel` | `tracking_code`, `direction` | Start tracking a parcel. `direction` is `incoming` (default) or `outgoing`; calling it again with the other direction moves an already tracked parcel |
| `purolator.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.purolator: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — Purolator has not registered it yet (the tracking site answers "not found" until the first scan), or the code is wrong. It will pick up automatically once scanned.
- **Updates keep failing** — Purolator is refusing or challenging the requests. The integration backs off and retries; there is nothing to configure. If it persists across days, [open an issue](https://github.com/ha-parcel-integrations/ha-purolator/issues/new).
- **A status logs "Unrecognised Purolator status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-purolator/issues/new) with the logged line so the mapping can be extended.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by Purolator, Home Assistant, or any other third party referenced in this project. Please don't contact Purolator for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed without an account. These may change or be withdrawn without notice and may be subject to Purolator's terms. Data is sent only to Purolator's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

This integration uses the public tracking endpoint behind the Purolator tracking page. It does not log in, solve challenges or scrape web pages: if Purolator blocks a request, the update fails and is retried later.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
