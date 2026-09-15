<p align="center">
	<img src="https://github.com/deltaecho07/hass-meteoswiss-rain-radar/blob/2903bedef066b9e1f0c1d4b676f891c9c393cc87/custom_components/meteoswiss_rain_radar/brand/logo.png" width="300">
</p>

# MeteoSwiss Rain Radar for Home Assistant

This fork adds **reporting-only hail detection** to [deltaecho07's rain radar integration](https://github.com/deltaecho07/hass-meteoswiss-rain-radar). Rain detection geometry and thresholds stay unchanged. Rain now reports the same age and health diagnostics as hail, with its own freshness and poll settings. Rain sensor names, default entity IDs and icons identify the product explicitly.

**No tagged hail release is available yet.** The work is on `feature/hail-reporting` in `tma/hass-meteoswiss-rain-radar`. Upstream `v0.1.3` does not include it. Development verification uses isolated Home Assistant tests, not a live installation or storm validation.

## Entity interface

The integration exposes **eleven Home Assistant entities per entry**, grouped under one device: five rain entities and six hail entities. It adds no custom services or device-control actions.

These are typical entity IDs; renaming and multiple entries can change them. Use the entity registry to confirm yours.

| Entity ID | Name | Value | Category |
| --- | --- | --- | --- |
| `binary_sensor.meteoswiss_rain_radar_rain` | Rain | `on` / `off` / unknown | Normal |
| `sensor.meteoswiss_rain_radar_rain_qualifying_distance` | Rain qualifying distance | km; nearest qualifying rain cell | Normal |
| `sensor.meteoswiss_rain_radar_rain_observation` | Rain observation | UTC timestamp | Diagnostic |
| `sensor.meteoswiss_rain_radar_rain_data_age` | Rain data age | minutes | Diagnostic |
| `sensor.meteoswiss_rain_radar_rain_data_health` | Rain data health | [Health enum](docs/hail.md#entities-and-health) | Diagnostic |
| `binary_sensor.meteoswiss_rain_radar_hail` | Hail | `on` / `off` / unknown | Normal |
| `sensor.meteoswiss_rain_radar_hail_maximum_poh` | Hail maximum POH | %; maximum within the radius | Normal |
| `sensor.meteoswiss_rain_radar_hail_qualifying_distance` | Hail qualifying distance | km; nearest qualifying hail cell, unknown if none | Normal |
| `sensor.meteoswiss_rain_radar_hail_observation` | Hail observation | UTC timestamp, not download time | Diagnostic |
| `sensor.meteoswiss_rain_radar_hail_data_age` | Hail data age | minutes | Diagnostic |
| `sensor.meteoswiss_rain_radar_hail_data_health` | Hail data health | [Health enum](docs/hail.md#entities-and-health) | Diagnostic |

Names above follow the device name, **MeteoSwiss Rain Radar**, unless customized. Rain uses `mdi:weather-rainy` and hail uses `mdi:weather-hail`; both distances use `mdi:map-marker-distance` and both observations use `mdi:clock-outline`. Units above are native units; Home Assistant may convert hail distance or duration for display. Rain distance keeps its existing km behavior, including on imperial systems. Every entity exposes `observation`, `data_health` and source attribution as attributes. Only hail entities add `coverage_complete`: the legacy rain reader produces no coverage evidence, so rain health covers the update and the observation age, not radar coverage around your home.

**Breaking entity ID change:** existing default `sensor.meteoswiss_rain_radar_distance` becomes `sensor.meteoswiss_rain_radar_rain_qualifying_distance`, and `sensor.meteoswiss_rain_radar_last_radar_image` becomes `sensor.meteoswiss_rain_radar_rain_observation`. Setup renames those registry entries automatically, including generated numeric duplicates, without replacing their unique IDs. Custom IDs and user metadata are preserved. **Update external automation, script and dashboard references yourself**; this integration doesn't rewrite them. See the [migration rules](docs/hail.md#rain-entity-id-migration) for collisions and rollback.

For hail, `on` requires a fresh qualifying cell; `off` requires fresh, complete coverage below threshold. **Unknown or unavailable is not clear weather.** A qualifying cell can still report `on` with partial coverage, but partial coverage cannot prove `off`. Timestamp, age and health remain diagnostic context when weather values are unknown.

Rain follows the same freshness rule: an observation older than the configured limit, a missing or future timestamp, or a failed update makes rain and rain distance **unknown**, never `off`. A cached frame keeps its own source timestamp; it is never renewed by a poll that found nothing new, and it expires on its own timer without a download. Rain age and health stay readable during outages so automations can see why values are unknown.

Rain and hail both look up the published file in the official MeteoSwiss catalogue instead of building its name. The two-character radar-site suffix in `RZCyyjjjHHMMKK.XYZ.h5` changes in service, and a built name returns 403 when it does. This fixes discovery only. Rain decoding, geometry and the missing rain coverage state are unchanged.

POH estimates hail of any size at the ground. A cell at or above 80% within 10 km qualifies by default; this is **not an 80% chance of hail hitting your property**, a forecast or an arrival time. MESHS is not required and has no entity.

Hail defaults are provisional: **10 km radius, inclusive 80% POH, maximum age 10 minutes, polling every 60 seconds**. Fresh qualifying data is reported immediately on receipt, with no eight-minute delay. These settings are not meteorologically validated and carry no source-latency guarantee. Missing, stale or incomplete data cannot prove clear conditions.

Read the [hail guide](docs/hail.md) for options, coverage and health states. Two disabled examples are for later approved use: a [simple notification](docs/examples/hail-notification.yaml) and a [shadow-hold package](docs/examples/hail-shadow-hold.yaml). The package restores local helpers and notifies after distinct fresh clear observations span 30 minutes, but **never clears its hold or controls devices**. The integration itself has no persisted protection state.

## Later installation through HACS

Installation and any Home Assistant restart need separate approval. A release or merge is **not required** to install an approved public branch or commit:

1. In HACS, confirm that the tracked repository is `https://github.com/tma/hass-meteoswiss-rain-radar`, type **Integration**, not upstream. Add it through **Custom repositories** if needed.
2. After the reviewed changes have been pushed, open **Developer tools → Actions**, choose `update.install`, and target the actual HACS update entity for this fork. Set **Version** to the approved full commit SHA, or `feature/hail-reporting` after confirming its current commit. HACS [supports these Version values](https://www.hacs.xyz/docs/use/entities/update/#install-action) without a release; the normal download dialog isn't an arbitrary branch picker. See the [detailed instructions](docs/hail.md#later-hacs-installation), including the optional release route.
3. Record the installed SHA and disable automatic updates for this integration. This downloads a snapshot, not persistent branch tracking; later normal updates can replace it with `main`.
4. After the download, restart Home Assistant yourself when approved. **Keep your existing integration entry; don't remove and recreate it.** Open its **Options** to see the expanded **Rain** and **Hail** sections, with units and help for all eight settings. New users get the same sections through **Settings → Devices & services → Add Integration → MeteoSwiss Rain Radar**.

Rain settings are radius (**km**, default 5), rain-rate threshold (**mm/h**, default 0.2, strictly above), maximum observation age (**minutes**, default 10, range 1–60) and poll interval (**seconds**, default 60, range 15–300). Hail settings are radius (**km**), inclusive POH threshold (**%**), maximum observation age (**minutes**) and poll interval (**seconds**), with the same age and poll ranges. Initial choices take effect immediately; saving options reloads only that entry. Hail uses the current Home Assistant home location, rain the coordinates saved at setup. Read the [settings and rain-reader limitations](docs/hail.md#options) before changing thresholds.

## Sources and development

**Source: MeteoSwiss.** Hail data is free, needs no registration, and is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Percentages, radius checks and distances are integration calculations, not official warnings or an endorsement. See [official products, attribution and terms](docs/hail.md#sources-and-attribution).

[Development and verification](docs/development.md) records test commands, fixture provenance and remaining limits. The software retains the upstream [MIT license and copyright](LICENSE).
