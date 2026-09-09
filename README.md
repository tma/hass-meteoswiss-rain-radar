<p align="center">
	<img src="https://github.com/deltaecho07/hass-meteoswiss-rain-radar/blob/2903bedef066b9e1f0c1d4b676f891c9c393cc87/custom_components/meteoswiss_rain_radar/brand/logo.png" width="300">
</p>

# MeteoSwiss Rain Radar for Home Assistant

This fork adds **reporting-only hail detection** to [deltaecho07's rain radar integration](https://github.com/deltaecho07/hass-meteoswiss-rain-radar). Existing rain entities, configuration entries and rain threshold behavior stay unchanged.

**No tagged hail release is available yet.** The work is on `feature/hail-reporting` in `tma/hass-meteoswiss-rain-radar`. Upstream `v0.1.3` does not include it. No live Home Assistant installation or storm validation has been performed.

## Entity interface

The integration exposes **nine Home Assistant entities per entry**, grouped under one device: three existing rain entities and six new hail entities. It adds no custom services or device-control actions.

These are typical entity IDs; renaming and multiple entries can change them. Use the entity registry to confirm yours.

| Entity ID | Value | Purpose |
| --- | --- | --- |
| `binary_sensor.meteoswiss_rain_radar_rain` | `on` / `off` | Existing rain detection |
| `sensor.meteoswiss_rain_radar_distance` | km | Existing nearest precipitation distance |
| `sensor.meteoswiss_rain_radar_last_radar_image` | Timestamp | Existing rain observation time |
| `binary_sensor.meteoswiss_rain_radar_hail` | `on` / `off` / unknown | Hail threshold met within the radius |
| `sensor.meteoswiss_rain_radar_hail_maximum_poh` | % | Maximum observed POH within the radius |
| `sensor.meteoswiss_rain_radar_hail_qualifying_distance` | km | Nearest cell meeting the POH threshold; unknown if none qualifies |
| `sensor.meteoswiss_rain_radar_hail_observation` | UTC timestamp | Hail observation time, not download time |
| `sensor.meteoswiss_rain_radar_hail_data_age` | minutes | Age of that observation |
| `sensor.meteoswiss_rain_radar_hail_data_health` | Enum | `ok`, `partial_coverage`, `stale`, `missing`, `future`, `error`, and [other health states](docs/hail.md#entities-and-health) |

Units above are native units; Home Assistant may convert hail distance or duration for display. Every hail entity also exposes `observation`, `data_health`, `coverage_complete` and source attribution as attributes.

For hail, `on` requires a fresh qualifying cell; `off` requires fresh, complete coverage below threshold. **Unknown or unavailable is not clear weather.** A qualifying cell can still report `on` with partial coverage, but partial coverage cannot prove `off`. Timestamp, age and health remain diagnostic context when weather values are unknown.

POH estimates hail of any size at the ground. A cell at or above 80% within 10 km qualifies by default; this is **not an 80% chance of hail hitting your property**, a forecast or an arrival time. MESHS is not required and has no entity.

Hail defaults are provisional: **10 km radius, inclusive 80% POH, maximum age 10 minutes, polling every 60 seconds**. Fresh qualifying data is reported immediately on receipt, with no eight-minute delay. These settings are not meteorologically validated and carry no source-latency guarantee. Missing, stale or incomplete data cannot prove clear conditions.

Read the [hail guide](docs/hail.md) for options, coverage and health states. Two disabled examples are for later approved use: a [simple notification](docs/examples/hail-notification.yaml) and a [shadow-hold package](docs/examples/hail-shadow-hold.yaml). The package restores local helpers and notifies after distinct fresh clear observations span 30 minutes, but **never clears its hold or controls devices**. The integration itself has no persisted protection state.

## Later installation through HACS

Installation and any Home Assistant restart need separate approval. Once an approved hail release exists:

1. In HACS, open **Custom repositories** and add `https://github.com/tma/hass-meteoswiss-rain-radar`, type **Integration**.
2. Select **MeteoSwiss Rain Radar** from this fork. **Pin an approved immutable published release/tag and record its commit SHA**, following the [version-selection limits](docs/hail.md#later-hacs-installation). Don't select a moving branch or accept unreviewed updates.
3. After the separately approved download and restart, go to **Settings → Devices & services → Add Integration → MeteoSwiss Rain Radar**. Existing users keep their entry.
4. The initial form has rain settings. Open the entry's options to adjust hail settings. Hail uses the current Home Assistant home location; saving options reloads the entry.

## Sources and development

**Source: MeteoSwiss.** Hail data is free, needs no registration, and is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Percentages, radius checks and distances are integration calculations, not official warnings or an endorsement. See [official products, attribution and terms](docs/hail.md#sources-and-attribution).

[Development and verification](docs/development.md) records test commands, fixture provenance and remaining limits. The software retains the upstream [MIT license and copyright](LICENSE).
