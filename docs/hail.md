# Hail reporting

This is reporting-only work on `feature/hail-reporting` in `tma/hass-meteoswiss-rain-radar`. It adds hail data without replacing rain behavior. It does not move devices, persist a protection hold or decide when protection can be released. Installation, notifications and later protective automation each need approval before live use.

## What POH means

POH is a radar estimate of hail of any size at the ground. It isn't a measured hail report, forecast, arrival time or property-level hit probability. With the default settings, detection means **at least one radar cell center within 10 km has POH ≥ 80%**. It says nothing about when or whether that cell's hail will reach your home.

The ODIM POH quantity is dimensionless `0..1`. The reader masks no-data and nonfinite values, applies gain/offset, then converts the supported fractional encoding to percent. Explicit `%` metadata is also supported. The official fixture contains float32 integer-percent encodings widened to float64; only exact encodings are normalized, so nominal 80% qualifies without broadly rounding nearby values through the threshold.

MESHS estimates maximum severe hail size above 2 cm; smaller hail isn't shown. The STAC description says **cm**, but the product metadata table, ODIM Table 16 and both unit fields in the inspected `MESH` file say **mm**. The reader requires explicit, consistent `mm` for MESH. See the [recorded evidence](../tests/fixtures/hail/provenance.json). MESHS is not downloaded for operational detection, has no entity and never gates POH. Requiring it could miss damaging 1.5 cm hail.

## Geometry and source updates

The five-minute ODIM HDF5 grids use Swiss LV95. The inspected national grids have 710 × 640 cells at 1,000 × 1,000 m, but the reader uses each file's projection, dimensions and separate x/y cell sizes, not a hardcoded 1 km assumption. It derives cell centers from outer corners, with rows north-to-south and columns west-to-east.

The radius is a true projected circle, including boundary cell centers. The location isn't snapped to a cell and fractional radii aren't rounded up. Distance is from the current `hass.config` home location to the nearest qualifying **cell center inside that circle**, not a storm edge, a road distance or an arrival estimate. A cell whose area overlaps the circle but whose center is outside is excluded. A small circle can contain no centers. Rounded geographic corners allow a 10 m metadata consistency tolerance; circle comparisons allow only 1 micrometre of projection round-off. Neither is a claim of meteorological accuracy at that precision.

Discovery selects the latest eligible past five-minute POH observation from official STAC assets. Every poll requests today's complete daily item at `…/items/YYYYMMDD-ch`, using UTC. It requests yesterday only if today has no eligible past asset. ETag/304 responses reuse immutable parsed metadata but recheck time eligibility; a future asset can become eligible without downloading or parsing the JSON again. There is no five-minute pause between checks for source corrections. The official [STAC API specification](https://data.geo.admin.ch/api/stac/static/spec/v1/openapi.yaml) documents direct-item conditional requests.

If both days lack an eligible past asset, the downloader attempts one bounded, paginated archive listing. Later polls use direct daily items, newest first, stopping at the first eligible past asset or the exact 14-day cutoff. They don't repeat the full archive listing. Request, schema or checksum failures don't trigger an archive fallback or certify cached weather. An exhausted daily-item 404 can mean that day is absent; asset-file 404s still use bounded retries and fail if unresolved. Validators and parsed daily metadata are retained by URL only within the calendar window needed for that cutoff.

The UTC filename must match the item date and file timestamps. Daily `2400`/`3000` aggregates are excluded, and future assets cannot replace an eligible past observation. Hail season is April 1–September 30; outside that period the integration reports `off_season` and skips hail discovery (UTC calendar checks). Seasonal files can be empty, which is not clear weather.

Unchanged HDF5 assets are cached by URL plus SHA256; a changed checksum at the same observation time causes reanalysis, not a new observation. Polling discovers source updates; it doesn't produce new observations. Age is rechecked on cached data. The expiry callback notifies entities without postponing the scheduled poll or making an extra request. Async requests have bounded retries and sizes; STAC parsing, asset scanning, file decoding and geometry run off the Home Assistant event loop. Coordinators borrow Home Assistant's shared HTTP client without closing it on entry unload or changing its defaults. Standalone downloaders create their owned clients off-loop. There is no promised publication or notification latency.

## Options

Initial setup and the entry's **Options** both show expanded **Rain** and **Hail** sections. All six fields have units in their labels and help explaining their meaning. Initial hail choices take effect immediately; saving options reloads only that entry. Keep existing version-1 entries: rain identifiers and flat storage keys are unchanged, so no migration or entry recreation is needed. Options take precedence over saved rain data when reopening the form.

### Rain

| Setting / stored key | Unit | Default | Meaning |
| --- | --- | --- | --- |
| Rain radius / `radius` | km | 5 | Search around the saved home location. The legacy detector rounds fractional radii up to whole grid cells, unlike hail's true circle. |
| Rain-rate threshold / `threshold` | mm/h | 0.2 | Rain requires a value **strictly above** the threshold. Lower values include lighter rain; higher values require heavier rain. |

Rain rate is not a probability or five-minute accumulation. The [official precipitation product documentation](https://opendatadocs.meteoswiss.ch/d-radar-data/d1-precipitation-radar-products) describes RZC/PRECIP as instantaneous `Rain_Rate` in mm/h. The inspected [2026-09-09 09:45 UTC RZC file](https://data.geo.admin.ch/ch.meteoschweiz.ogd-radar-precip/20260909-ch/rzc262520945vl.001.h5) confirms `quantity=RATE`, `unit=mm/h`, gain 1, offset 0, NaN no-data and zero undetect in `/dataset1/data1/what` (223,335 bytes; SHA256 `b50340942fd5f8699ba7d344f30744b73b44887bdea0cd61e3027a9fa53d4340`).

The unchanged rain reader compares float32 raw values with `>`; values at 0.2 don't qualify at the default threshold. It **does not apply or validate gain/offset/unit metadata or explicitly mask no-data**. The mm/h label matches the current encoding, not a new parser guarantee. Hail's decoding, geometry and freshness guarantees do not apply to the legacy rain path. Rain inputs must be finite; no new rain ranges were added.

### Hail

Hail uses the current Home Assistant home location, not the coordinates saved at initial setup. No private coordinates belong in examples or reports.

| Setting / stored key | Unit | Default | Range and meaning |
| --- | --- | --- | --- |
| Hail radius / `hail_radius_km` | km | 10 | 0.1–100; a true circle of radar cell centers around the current home location. |
| POH threshold / `hail_poh_threshold` | % | 80 | 0–100; a cell **at or above** this value qualifies. POH estimates hail of any size at the ground in that cell, not a property hit probability or forecast. |
| Maximum observation age / `hail_max_age_minutes` | minutes | 10 | 1–60; older observations make hail values unknown, not clear weather. |
| Poll interval / `hail_poll_seconds` | seconds | 60 | 15–300; discovers five-minute source updates, not new observations on every poll. |

All hail inputs must be finite numbers. An observation exactly at the maximum age is accepted; older data is stale. A threshold of 0 also qualifies valid zero-valued cells, so it is not a useful hail-alert setting. A larger radius can introduce partial coverage.

These defaults are **provisional, not validated safety settings**. Reporting is immediate on receipt of a fresh qualifying observation; there is no eight-minute wait option. The proposed 30-minute clear period is a later automation requirement, not an integration option or implemented timer.

### Evidence behind the provisional settings

[MeteoSwiss hail climatology](https://www.meteoswiss.admin.ch/climate/the-climate-of-switzerland/hail-climatology.html) uses **POH ≥ 80%** to classify hail days at a radar pixel. It cites comparisons with damage data supporting that threshold for observed hail extent and damage occurrence (Nisi et al., 2016). This gives 80% an observed-hail classification precedent, **not validation of a protective alert based on any qualifying cell within 10 km**.

The [official “Hagelschutz – einfach automatisch” FAQ](https://www.hagelschutz-einfach-automatisch.ch/eigentuemer-verwaltungen/fragen-und-antworten.html), linked from Schutz vor Naturgefahren, warns at **5% forecast probability** for hail of at least 1.5 cm at a location. It describes warning roughly 15 minutes before expected hail and releasing control roughly 30 minutes after the all-clear. That forecast probability is not documented as MeteoSwiss POH: don't substitute 5% for this integration's 80%. The reviewed guidance provides no corresponding warning radius or conversion between these probabilities. A fixed radius also cannot guarantee lead time without storm motion and direction.

The guidance supports avoiding a MESHS size gate and delaying release to avoid repeated movements. It does not validate this integration's 10-minute freshness limit or the example's seven distinct clear observations. Defaults remain unchanged and provisional; compare radius/threshold combinations during the later notification-only shadow trial before choosing protective settings.

## Entities and health

All six hail entities share the existing device. Unique IDs are the configuration entry ID followed by the suffix below; Home Assistant entity IDs can differ after renaming or with multiple entries. Check the entity registry rather than constructing IDs from these suffixes.

| Name | Unique ID suffix | State/unit |
| --- | --- | --- |
| Hail | `_hail` | `on`, `off` or unknown |
| Hail maximum POH | `_hail_max_poh` | % |
| Hail qualifying distance | `_hail_distance` | km |
| Hail observation | `_hail_observation` | UTC timestamp |
| Hail data age | `_hail_age` | minutes (`min`) |
| Hail data health | `_hail_health` | enum below |

Every hail entity exposes `data_health`, `coverage_complete` and `observation` attributes, plus `Source: MeteoSwiss` attribution. Timestamp, age and health are diagnostics. On failure, a retained timestamp is only diagnostic context, not evidence that cached weather is usable. Future or absent timestamps have no numeric age. The age entity updates with entity refreshes, not continuously; automations should calculate age from the observation and `now()`, never from `last_changed`, a poll time or a download time.

| Health | Meaning and weather states |
| --- | --- |
| `ok` | Fresh, complete circle coverage. Detection is `on` if any cell qualifies, otherwise `off`. Maximum POH can legitimately be 0%. Distance is unknown when no cell qualifies, never a substitute zero. |
| `partial_coverage` | The circle extends beyond the grid or contains missing cells. A valid qualifying cell can still report `on` and a distance. Without one, detection is unknown, **not `off`**. A positive observed maximum can be shown, but it is incomplete (`coverage_complete: false`); a partial zero maximum is unknown. |
| `outside_grid` | Home location is outside the raster extent, even if the radius overlaps it. No weather values. |
| `no_cells` | The circle contains no cell centers. No weather values. |
| `all_nodata` | Selected cells are all no-data/nonfinite. No weather values. |
| `empty` | Empty bytes or an empty supported HDF5 product. No weather values. Malformed files are `error`, not `empty`. |
| `off_season` | Current UTC date or observation is outside April–September. No weather values; not an assurance of no hail. |
| `missing` | No eligible observation was found, or reporting has no result yet. No weather values. |
| `stale` | Observation age exceeds the configured limit. Timestamp and age can remain visible; detection, POH and distance are unknown. |
| `future` | Only future assets were found, or a clock reversal makes the observation future-dated. Timestamp may remain visible, but age and weather values are unknown. |
| `error` | Request, checksum, metadata, decoding or other update failure. Even a fresh cached result cannot certify weather. No weather values. |

Normal handled failures produce `unknown` weather states with readable health. An unexpected coordinator failure can make weather entities `unavailable`; diagnostics remain readable while loaded. Treat both `unknown` and `unavailable` as no evidence. Don't coerce either to `off` or numeric zero. Only complete, fresh `ok` observations below threshold provide clear samples, and **one clear sample never releases protection**.

## Notification-only example

[Download the example YAML](examples/hail-notification.yaml). It is one automation mapping for the automation editor's YAML view, not an entire `automations.yaml` list. For list-based configuration, add it as a list item. It is disabled by default (`initial_state: false`). Only after installation and notification testing are approved, change that to `true` to enable it at startup.

Replace all three entity IDs with the hail entities from the **same entry**. Set the example's radius, threshold and maximum age to that entry's options. The integration doesn't expose options as entity attributes, so these example values aren't synchronized automatically. Keep the POH sensor in `%` and the distance sensor in `km`; the example refuses other display units rather than labeling converted values incorrectly.

The example checks the observation timestamp, current age, health and matching observation attributes before notifying. It accepts qualifying partial coverage but never treats it as clear. The action only creates or updates a Home Assistant persistent notification containing timestamp, age, radius, threshold, POH, distance and health. Startup and minute checks can repeat the same cached observation; the fixed notification ID avoids a new notification for every check. These are repeated reports, not distinct weather evidence. A same-time source correction can update the report.

There is deliberately **no clear, release, notification-dismissal or device action**. Stale, missing, future, unhealthy or below-threshold data does nothing, and an old notification may remain visible. Read its timestamp. The notification is not a persisted protection hold and isn't guaranteed to survive a restart. This example also isn't an outage monitor or a durable observation log.

## Shadow-hold package

The second [example package](examples/hail-shadow-hold.yaml) implements a local, restored **shadow request latch and clear-evidence counter**. It is disabled by default and only writes its own helpers and creates notifications. It never clears the hold, dismisses a notification or controls a device. These helpers belong to the example, not the integration. This is a trial of the evidence rule, not working property protection.

The file is a [Home Assistant package](https://www.home-assistant.io/docs/configuration/packages/), unlike the single automation above. Only after approval, load it as a package, check the three radar IDs, and match its radius, threshold and maximum-age variables to the integration options. Keep display units at `%` and `km`; other units are rejected. If the automation entity is renamed, also update its self-enable trigger. Change `initial_state: false` to `true` only when the notification trial is approved. Don't install both examples unless you want both sets of notifications.

| Helper | Purpose |
| --- | --- |
| `input_boolean.hail_shadow_hold` | Restored request latch. The example can turn it on, never off. Startup/reload/enable also latches it conservatively; this is not a claim that hail was observed. |
| `input_number.hail_shadow_clear_count` | Restored evidence count: `-1` requires recovery, `0` waits for a new observation, `1..7` counts consecutive clear observations. |
| `input_datetime.hail_shadow_last_clear` | Last counted observation, or the reset/recovery boundary before which no observation may count. |
| `input_text.hail_shadow_last_signature` | Timestamp, health, coverage, detection and POH of the counted observation, to reject changed same-time reports. No coordinates. |
| `input_text.hail_shadow_settings` | Example radius/threshold/age signature, so changed example settings invalidate old evidence. |

None of the helpers has an `initial` override; HA restores their values. The automation deliberately discards restored clear evidence at startup and reasserts the hold if its state is absent/off. Helpers are not a transactional protection store, and the package cannot protect anything while HA is down or before its automation runs. A lost or uncertain restored value must not be interpreted as release.

A fresh qualifying observation requests the shadow hold without a delay, including qualifying partial coverage. Clear samples require complete `ok` data below threshold, a fresh nonfuture five-minute timestamp and matching attributes across entities. Seven distinct samples at five-minute intervals span 30 minutes. Cached repeats don't advance the count; changed same-time reports, out-of-order timestamps, gaps, unusable data and unit mismatches invalidate it. Unchanged same-time source revisions cannot be distinguished from cache because no checksum entity is exposed, but neither counts as a new observation.

Startup, automation reload/enable, home-location changes and changed example settings reset the sequence. Saving integration options normally reloads its entities; the package captures the resulting `unavailable`/removal event, even if a queued run sees recovered current states. The real HA test confirms this options-reload path. **It cannot read or verify the integration options themselves.** Keep both configurations matched, reload the example after changes, and stop the trial if an entry reload fails rather than trusting old evidence.

After an outage or gap, the first usable clear report only establishes a recovery boundary. It doesn't count; the package needs seven newer observations. A reset likewise excludes cached pre-reset samples. A helper-write interruption loses the count before replacing its timestamp/signature. At seven samples, the package rechecks freshness and current data after helper writes, then only notifies **“clear evidence satisfied”**. The hold stays on. Later invalid data cannot release it, and old notifications are historical reports, not current clearance. Check their timestamps.

The minute trigger also checks age when nothing changes. It isn't a continuity clock: no number of timer runs can manufacture observations. A missing five-minute timestamp is rejected when the next observation arrives; stale expiry and unhealthy entity events also break the sequence. There is no archive backfill. Review queue limits, helper restoration, actual entity IDs and recovery behavior in an approved shadow trial before considering any separate physical protection design.

## Later 30-minute clear requirement

The shadow package exercises this rule and only reports satisfaction. **Integration protection and physical release remain unimplemented.** Don't replace the rule with `off` for 30 minutes or a timer that advances on every poll.

- A fresh qualifying observation requests protection immediately, including qualifying partial coverage. Persist the active hold separately from the integration's weather state. The example's restored helper is only a shadow request; a physical protection design must review storage/recovery and device behavior separately. A notification is not hold storage.
- A clear sample must have `health == ok`, `coverage_complete == true`, detection `off`, a valid POH below the configured threshold and a nonfuture observation within the age limit. All inputs must describe the same observation and entry.
- Count strictly increasing, **distinct observation timestamps**, not state events, cached polls, download times or revisions of the same timestamp. Require consecutive five-minute clear observations spanning at least 30 minutes: for example, 12:00, 12:05, …, 12:30 UTC (seven samples). Each must be fresh when accepted, and the final sample must still be fresh when considering release. Don't backfill missed samples from the archive after an outage.
- Repeated cached data contributes no time and no samples. A missing five-minute observation, stale/future/missing/error/partial data, unavailable entity, clock reversal or qualifying observation breaks the clear sequence. A conflicting correction to a counted observation invalidates it; a benign same-time correction still doesn't add evidence. Reset the sequence on location, threshold, radius or freshness-option changes.
- On Home Assistant restart, automation reload or outage recovery, preserve any active hold but discard the clear sequence. Require new post-recovery observations spanning the full 30 minutes; pre-restart data cannot finish it. If restored hold state is absent or uncertain, require protective/manual review rather than defaulting to released.
- At the final observation, recheck health, freshness, coverage and continuity. Only then may the later implementation report that its clear-evidence rule is satisfied. Missing data **never releases protection**, and release must not restore old device positions automatically.

An outage can occur while a cached observation still appears fresh. Track recovery and observation gaps explicitly; freshness alone cannot prove continuity. The example has isolated helper/schema/sequence tests, not live protection validation. Physical protection still needs its own persistence, startup and device-failure tests. Keep existing weather protection until a separate approved cutover.

## Later shadow test

After approval, run notification/logging only over storm events and quiet periods. Log the approved software revision, receipt time, observation time, radius (km), inclusive threshold (%), observed maximum POH (%), nearest qualifying distance (km), calculated age (minutes), health, coverage and the report decision. Record unknown values as unknown. Exclude home coordinates, addresses and private entity names; review exported logs for location information.

Group records by observation timestamp so repeated cache reads and source corrections aren't counted as new observations. Include source gaps, partial coverage, off-season behavior, restart/recovery and stale expiry. Compare the computed decisions with the recorded inputs and note missed or delayed updates. This checks computation and operation; it is **not meteorological validation**, evidence of property-level protection or a latency guarantee. No such live trial has been performed here.

## Later HACS installation

As checked on 2026-09-09, the fork has no published release or tag containing hail. Upstream `v0.1.3` is the rain-only baseline; unchanged manifest/version text in this working tree is not a hail release identifier.

After the reviewed changes are pushed and installation is approved, HACS can download a public branch or commit **without a release, merge or default-branch change**:

1. Confirm that HACS tracks `https://github.com/tma/hass-meteoswiss-rain-radar`, type **Integration**, through [Custom repositories](https://www.hacs.xyz/docs/faq/custom_repositories/). The integration name remains **MeteoSwiss Rain Radar**. A branch must belong to the tracked repository: targeting the upstream update entity cannot install this fork's branch. Use the repository URL, not a `/tree/feature/hail-reporting` URL.
2. In **Developer tools → Actions**, select `update.install` and target the actual HACS update entity belonging to this fork. Set **Version** to the approved **full commit SHA** for an exact snapshot, or `feature/hail-reporting` after verifying its current SHA. `APPROVED_FULL_COMMIT_SHA` is a placeholder for the reviewed, pushed revision, not a literal Version value; no future commit is assumed here. HACS documents this advanced [Install action](https://www.hacs.xyz/docs/use/entities/update/#install-action) for public branches and full SHAs as well as tags.
3. Execute the download only when approved, then restart Home Assistant yourself. **Keep the existing integration entry; don't remove and recreate it.** Open the entry's **Options** and confirm both expanded sections, **Rain** and **Hail**, with units and help on all six fields. New entries use the same form at initial setup. Saving options reloads that entry and retains its entity IDs.
4. Record the installed SHA and keep automatic updates disabled for this integration. Installing a branch downloads its current snapshot; it does **not** establish persistent branch tracking or an immutable revision lock. Later normal updates can replace the snapshot with `main`, so review updates before accepting them. If HACS rejects a revision or reports incompatibility, stop rather than substituting “latest”.

An approved release/tag remains an optional route. HACS documents **Download / Redownload → Need a different version?** for [available versions](https://www.hacs.xyz/docs/use/repositories/dashboard/#downloading-a-specific-version-of-a-repository); that normal dialog isn't an arbitrary branch/SHA picker. Record the full SHA and verify the tag-to-SHA mapping, since ordinary tags can move. Neither route requires removing the integration entry. These are user-run instructions, not live Home Assistant actions performed by development tests.

## Sources and attribution

- [MeteoSwiss hail product documentation](https://opendatadocs.meteoswiss.ch/d-radar-data/d3-hail-radar-products)
- [Official STAC collection](https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-radar-hail) and [browser](https://data.geo.admin.ch/browser/index.html#/collections/ch.meteoschweiz.ogd-radar-hail)
- [ODIM HDF5 2.4](https://www.eumetnet.eu/wp-content/uploads/2021/07/ODIM_H5_v2.4.pdf), especially Tables 5/16 and section 5.2
- [MeteoSwiss terms](https://opendatadocs.meteoswiss.ch/general/terms-of-use) and [FSDI terms](https://www.geo.admin.ch/en/general-terms-of-use-fsdi)

**Source: MeteoSwiss.** Data is free, without registration, under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); the STAC license label is `CC-BY`. Retain attribution and license/terms links when sharing data or derived reports. The bundled national fixtures are unchanged; conversion to percent, radius thresholding, distances and health classifications are integration adaptations, not MeteoSwiss warnings. Neither MeteoSwiss nor the upstream author endorses protective use of this fork.

The source offers no guarantee of correctness, freshness, completeness or availability. Respect caching and the terms against high-frequency repeat downloads; faster polling is not faster radar production. Software licensing is separate: the upstream [MIT copyright and license](../LICENSE) remain intact. Exact fixture URLs, timestamps, SHA256 values and unit evidence are in [provenance](../tests/fixtures/hail/provenance.json), summarized in [development and verification](development.md).
