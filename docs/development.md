# Development and verification

This records the development checks for the hail development branch `feature/hail-reporting`, based on rain-only commit `de8e7731aa922856dbfafbe5804b1324a7c81f33`. Tests check computational behavior, not meteorological quality, source latency or live Home Assistant operation. See the [hail guide](hail.md) for reporting semantics and the separate future protection specification.

## Test environment

The working isolated runtime is Python 3.12.14 on musl Linux aarch64, with **Home Assistant 2025.1.4** and **pytest-homeassistant-custom-component 0.13.205**. These match the HA/plugin versions in the inspected successful [upstream CI run](https://github.com/deltaecho07/hass-meteoswiss-rain-radar/actions/runs/34089720029), not a claim of compatibility with current Home Assistant.

The environment installed the unchanged `requirements_test.txt` with only these two extra constraints:

```text
homeassistant==2025.1.4
pytest-homeassistant-custom-component==0.13.205
```

The working installation used `/usr/bin/uv pip install --python /workspace/.venv/bin/python --prerelease allow -r /workspace/requirements_test.txt -c /workspace/.venv/ci-constraints.txt`, with `SKIP_CYTHON=1` and each UV download/build/install concurrency set to 1. Genuine pure-Python builds of `fnv-hash-fast` and `ulid-transform` were used; no HA modules were replaced. Both `uv pip check` and `python -m pip check` passed. There were no global installations or TLS-verification bypasses.

For a **new development checkout only**, create an isolated environment rather than changing system Python:

```sh
python3.12 -m venv .venv
printf '%s\n' 'homeassistant==2025.1.4' \
  'pytest-homeassistant-custom-component==0.13.205' > .venv/ci-constraints.txt
# With uv already available; do not reinstall an existing working environment.
SKIP_CYTHON=1 UV_CONCURRENT_DOWNLOADS=1 UV_CONCURRENT_BUILDS=1 \
  UV_CONCURRENT_INSTALLS=1 uv pip install --python .venv/bin/python \
  --prerelease allow -r requirements_test.txt -c .venv/ci-constraints.txt
.venv/bin/python -m pip check
```

This is not a complete dependency lock. A future resolver run can choose different transitive versions or lack wheels for a target device. The initial unconstrained scout attempt selected HA 2024.12.5 and failed building `ciso8601` without a compiler; a later scout install was killed with exit 137. The subsequent `.venv` installation above succeeded. Don't disable TLS or substitute fake dependencies to get a passing result.

## Commands and results

Run from the repository root. In the reviewed `/workspace` environment, use `. /workspace/.venv/runtime-env.sh` **inside a subshell** before Python checks. That local helper keeps caches, temporary files and HOME under `.venv` and disables bytecode/user-site imports. It isn't a committed project dependency. Run Git outside that subshell because the isolated HOME changes Git's environment.

The mounted workspace required `--capture=sys`; default pytest file-descriptor capture failed on a temporary-file operation before any tests ran. Console scripts on that mount weren't executable, so the commands use `python -m pytest`. Ordinary development filesystems may not need these workarounds.

```sh
(
  . /workspace/.venv/runtime-env.sh  # This development workspace only.
  .venv/bin/python -m pytest -q --capture=sys -p no:cacheprovider \
    --basetemp=/workspace/.venv/pytest-formfix-focused-final \
    tests/test_hail_config_flow.py tests/test_hail_setup.py \
    tests/test_hail_documentation.py
  .venv/bin/python -m pytest -q --capture=sys -p no:cacheprovider \
    --basetemp=/workspace/.venv/pytest-formfix-originals-01 \
    tests/test_coordinator.py tests/test_radar_data.py tests/test_radar_downloader.py
  .venv/bin/python -m pytest -q --capture=sys -p no:cacheprovider \
    --basetemp=/workspace/.venv/pytest-formfix-full-final
)
git diff --check
```

Results after source remediation, the rain stop guard and the form-rendering fix:

| Check | Result |
| --- | --- |
| Form/setup/documentation tests | 201 passed |
| Original rain tests | 24 passed; all three original test files unchanged |
| Reader tests | 36 passed in the full suite |
| Simple notification example tests | 36 passed in the full suite |
| Shadow-hold package tests | 34 passed in the full suite |
| Full suite | **364 passed**, including all 24 unchanged original rain tests |
| Full repository Ruff lint | Passed |
| Ruff format check, four Python files touched by the form fix | Passed |
| Full repository format check | Four unchanged baseline files would be reformatted; not a pass |
| Scoped mypy, changed `config_flow.py` module | One existing HA subclass typing error with imports skipped; also reproduced on unchanged HEAD |
| Broader mypy attempt | Killed, exit 137; no broad type-check pass |
| Hassfest / HACS validation | Not run for this work |

The implementation-only suite had 140 passing tests before the 70 example tests were added. Source remediation added 27 regressions; the rain stop guard added three more. The form follow-up adds 124, including real configuration/options HTTP requests and translation loading. The first client/lifecycle run exposed two test assumptions: this HTTPX version loads certificates through `SSLContext.load_verify_locations`, and HA's initial one-shot stop listeners disappear when fired. The corrected tests exercise actual worker functions and SSL loading, not inline Mock executor targets. A documentation-phase coordinator/setup rerun had timed out; later runs completed. The pinned pytest-asyncio plugin emits an unset `asyncio_default_fixture_loop_scope` deprecation warning. Mocked HTTPX request logs in tests are not network access.

### Lint, formatting and types

On a normal isolated environment, use its Ruff binary. On this mount the executable was the isolated scout's `/tmp/meteoswiss-hail-scout-XXEiLJCb/venv/bin/ruff`; no global tool was installed. Set `RUFF` to a working isolated executable:

```sh
RUFF=.venv/bin/ruff
# In the reviewed mounted workspace instead:
# RUFF=/tmp/meteoswiss-hail-scout-XXEiLJCb/venv/bin/ruff
"$RUFF" check --no-cache
"$RUFF" format --check --no-cache \
  custom_components/meteoswiss_rain_radar/config_flow.py \
  tests/{test_hail_config_flow,test_hail_setup,test_hail_documentation}.py
"$RUFF" format --check --no-cache
(
  . /workspace/.venv/runtime-env.sh  # This development workspace only.
  .venv/bin/python -m mypy --check-untyped-defs --follow-imports=skip \
    --ignore-missing-imports \
    custom_components/meteoswiss_rain_radar/config_flow.py
)
```

The four format exceptions are `detector.py`, `geo.py`, `models.py` and `radar.py` under `custom_components/meteoswiss_rain_radar/`. They were left untouched rather than mixing baseline formatting changes into hail work. Brace expansion in these command blocks requires Bash.

The earlier broader mypy command used `--follow-imports=silent --ignore-missing-imports` on the original three hail modules and exited 137. Earlier `--follow-imports=skip` checks passed on seven implementation modules that excluded `config_flow.py`; they don't verify imported dependency types or the full HA API. The form follow-up checks `config_flow.py` alone and reports an unexpected `domain` argument to `__init_subclass__` because HA's base-class types are skipped. The unchanged HEAD version reproduces that same error. No suppression or broad rerun was added, and no passing type check is claimed for this module.

### Source remediation checks

Daily-item tests cover conditional 304 reuse, same-time corrections on each poll, UTC/day/year/leap rollover, future eligibility, the inclusive 14-day cutoff, cache eviction, bounded historical pagination and daily history after the single cold fallback. Failures cannot reuse cached weather or trigger a fallback. Daily-item absence and transient asset-file 404s have separate tests.

Client tests verify real certificate loading off-loop, cancellation during owned-client construction, shared HA client reuse, per-request timeout/redirect settings and unchanged shared defaults. One entry's unload leaves the other entry's client open. STAC parsing and selection use real executor functions; HDF5/geometry worker coverage remains intact.

Expiry tests retain the actual scheduled timer handle and deadline, preserve coordinator error state, and confirm the next request fires at its original time. Data remains fresh at the age limit and becomes unknown one microsecond later. Shutdown barriers during rain refresh, hail discovery and platform forwarding cancel setup and leave no entry-owned client reference, timer or mapping. The setup failure handler owns cleanup; it doesn't create a shared cleanup task that could await itself.

Rain unload barriers hold a scheduled HEAD miss or successful GET until after the real entry unloads. Both requests can finish, but neither can rearm a timer. Advancing past the retry and normal polling deadlines produces no requests, and HA's shared client stays open. A separate callback captured before `stop()` does nothing when awaited afterward. All three tests failed before the stop guard and passed after it. The guard sets the stopped flag before the first await; it doesn't cancel in-flight rain requests or change normal polling times.

### Form rendering and settings follow-up

The earlier 240 passing tests validated option schemas but never serialized the displayed form. A regression added before the fix reproduced HTTP **500** at `/api/config/config_entries/options/flow` and failed in HA's actual `_prepare_result_json` path: the custom `_finite` callable cannot be encoded by `voluptuous_serialize`. The installed serializer raised `TypeError`; the reported production trace raised `ValueError` for the same unsupported callable. After the fix, both initial and options endpoints return **200** with expanded native Rain/Hail sections.

[`tests/test_hail_config_flow.py`](../tests/test_hail_config_flow.py) uses the real HA flow manager, HTTP views, `cv.custom_serializer` and strict JSON checks on rendered forms. Tests verify all six numeric fields, defaults, bounds and expanded sections; strings `nan`, `inf` and `-inf` through both HTTP flows; and Python nonfinite numbers directly through both flow managers. Nonfinite numeric tokens aren't legal JSON, so they aren't used to claim backend validation through the HTTP decoder. The installed Voluptuous range validator rejects hail NaN before the step; a separate test simulates permissive range behavior and proves post-submission finite validation still rejects it. Rain has the same finite check, without new ranges.

Range/type/section-shape errors rejected by HA before the step return HTTP 400; post-submission validation returns an error form. Both paths remain serializable, keep finite saved/default values, and allow correction on the same flow. Tests retain old flat version-1 entry fixtures, check options precedence on reopening, reload one entry without changing IDs or another entry, and prove initial nondefault hail radius/threshold/age/poll values reach the real coordinator and reader. Initial data contains only legacy rain settings and coordinates; initial hail settings live in flat options, which the hail coordinator already reads. No coordinator fallback or rain parser change was needed.

The translation tests in `test_hail_documentation.py` load English text through HA and verify both section names, every label's unit, help and errors for initial setup and options. This is backend serialization and translation coverage, not a browser screenshot test. HTTP tests use an isolated loopback server; source downloads are mocked. A test-only threaded DNS resolver avoids the installed pycares version's process-wide cleanup thread. The first matrix run exposed an overly strict distance assertion and assumptions about HA's invalid-JSON handling; corrected tests compare projected distance with tolerance and send Python nonfinite values directly to the flow manager.

Final verification is **364 passed**, full repository Ruff lint, formatting of the four touched Python files, local documentation links/contracts and unchanged original rain-file hashes. Reduced mypy reports the one existing HA subclass error described above, not a pass. Runtime remains Python 3.12.14 / HA 2025.1.4 / plugin 0.13.205. **Python 3.14 and the reported installation's exact HA release were not tested.** The local report `.venv/form-fix-report.md` records commands and fail-before/pass-after logs; it is not a committed artifact.

The HACS guidance was also corrected against its [Install action documentation](https://www.hacs.xyz/docs/use/entities/update/#install-action): an approved public branch or full commit SHA in the tracked repository can be installed without a release. This is snapshot selection, not persistent branch tracking. No HACS install, restart or live Home Assistant action was performed by these checks.

### Example contract tests

[`tests/test_hail_documentation.py`](../tests/test_hail_documentation.py) loads the [actual example YAML](examples/hail-notification.yaml) using repository-relative paths and the real HA test plugin. It validates automation, trigger, condition and action schemas; renders variables, the condition and notification payload; and validates that payload against HA's registered notification service schema. It never installs the automation, executes a script or calls a service. Service calls are guarded by a failing mock.

Cases include inclusive age/POH/radius boundaries, datetime and ISO timestamp attributes, qualifying partial data, all nonusable health states, missing/invalid/future timestamps, unknown/nonfinite values, unexpected units, mismatched observations, repeated cache checks and stale expiry. A mocked integration setup confirms the example's default IDs and templates against real HA entities. Tests assert that the sole action is notification creation and that a fresh `off` sample produces no action decision. This simple example has no hold or clear counter.

The temporary check first exposed a datetime/string equality mistake in the draft example. The YAML now compares parsed timestamps across entities. Initial validation-helper errors were fixed before the final pass; no integration source change was needed. These checks are now retained in the repository rather than only in `.venv`.

[`tests/test_hail_shadow_example.py`](../tests/test_hail_shadow_example.py) validates the [second package](examples/hail-shadow-hold.yaml) with the real HA schemas and restored helpers. It runs the action sequence only in the isolated test runtime: a strict service allowlist permits writes to the five example helpers, and notifications are schema-checked and intercepted. No device action or hold-off action is permitted. A separate check loads the automation disabled, then simulates enabling that isolated test entity to verify its self-enable reset and clean up its listeners.

The package tests cover immediate qualifying alarms, restored active hold, startup evidence discard, seven distinct five-minute clear observations, cached repeats, corrections, gaps, out-of-order and unusable data, recovery, option changes and final freshness rechecks during helper writes. The real integration's options reload emits `unavailable`; this event is captured as an interruption even if current states have recovered. Missing or uncertain restored hold state is latched conservatively. Tests verify clear satisfaction only notifies and leaves the hold on. These are helper/sequence tests, not physical protection or live outage validation. Frozen clock jumps can produce asyncio slow-task warnings; they aren't measured processing delays.

A separate documentation check resolved local Markdown links/anchors and compared option defaults, entity names/suffixes, health values and attributes with the implementation. Official source, standard, terms and license URLs returned HTTP 200. HACS version-selection instructions were checked against its public documentation; read-only GitHub API queries confirmed no fork releases/tags and upstream `v0.1.3` on 2026-09-09. These are documentation checks, not HACS or hassfest validation. The HA packages documentation URL couldn't be network-verified because its TLS certificate was reported as not yet valid; TLS verification was not bypassed. Package schemas and behavior were tested locally as described above.

## Fixture provenance

**Source: MeteoSwiss.** The two bundled fixtures are unchanged official national grids, not clipped to a home location. The [provenance file](../tests/fixtures/hail/provenance.json) records exact URLs, retrieval/observation times, sizes, SHA256 values, STAC checksums, unit evidence, geometry, float encoding, attribution and [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) terms.

Both observations are **2026-09-09T08:25:00Z**, covering 08:20–08:25 UTC:

| Fixture | Official URL | Retrieved (UTC) | Bytes |
| --- | --- | --- | --- |
| `bzc262520825vl.845.h5` (POH) | [POH file](https://data.geo.admin.ch/ch.meteoschweiz.ogd-radar-hail/20260909-ch/bzc262520825vl.845.h5) | 2026-09-09T08:28:34.479581Z | 25,010 |
| `mzc262520825vl.850.h5` (MESHS) | [MESHS file](https://data.geo.admin.ch/ch.meteoschweiz.ogd-radar-hail/20260909-ch/mzc262520825vl.850.h5) | 2026-09-09T08:28:34.571247Z | 23,817 |

SHA256:

```text
POH    d604554e99f8d1f17af21dd22f531f3283e3c1f188ec7cf2a8321d6838d99765
MESHS  74067e4d1f51250bfecb665de5714e0c0486d393192edf906069790b5294d074
```

The tests read these local files and verify their bytes/checksums; they don't download them. The rolling archive may later remove the original URLs. Retrieval time is not observation time and this snapshot is not a latency benchmark. See [official sources and terms](hail.md#sources-and-attribution) for reuse obligations. Derived percentages and distances are integration calculations, not an endorsement or official warning.

## Remaining limits

- No tagged hail release. Development checks performed no live installation, restart, storm trial or device action. Later [HACS snapshot selection](hail.md#later-hacs-installation) and [shadow testing](hail.md#later-shadow-test) need approval.
- No integration protection hold, physical release logic or MESHS entity. The [shadow package](hail.md#shadow-hold-package) implements a restored local request and the 30-minute evidence rule only; it never releases its hold. Operator settings must match the integration, and failed reloads/queue errors require stopping the trial for review.
- No proof of compatibility with newer HA releases or of dependency wheel availability on the eventual host. Test there before an approved installation.
- Hassfest and HACS jobs in [the workflow](../.github/workflows/tests.yml) remain to be run for this work; the development runtime had no Docker. Upstream CI results don't validate this fork's hail change.
- Existing packaging inconsistencies remain: `LICENSE` is MIT while `pyproject.toml` says Apache-2.0; `const.py` reports 0.1.0 while the manifest/project report 0.1.3; the project name contains spaces. The MIT notice and upstream attribution were preserved, not silently relicensed or changed as documentation cleanup. Resolve release metadata in a separately reviewed publication change.
