"""Rain polling through the real downloader, from catalogue lookup to decoding."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meteoswiss_rain_radar.const import (
    CONF_RADIUS,
    CONF_THRESHOLD,
    DOMAIN,
)
from custom_components.meteoswiss_rain_radar.coordinator import (
    MeteoSwissRainRadarCoordinator,
)

from .test_radar_downloader import NOW, OBSERVATION, daily_url, item, rain_asset

MODULE = "custom_components.meteoswiss_rain_radar.coordinator"


@pytest.fixture
async def coordinator(hass, freezer):
    """A real coordinator and downloader; only the grid reader is replaced."""
    freezer.move_to(NOW)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_RADIUS: 5, CONF_THRESHOLD: 0.2, "latitude": 47.0, "longitude": 7.5},
        options={},
        version=1,
    )
    entry.add_to_hass(hass)
    with (
        patch(f"{MODULE}.RadarData") as radar_data,
        # Tests drive every poll explicitly; frozen clock jumps must not add any.
        patch(f"{MODULE}.async_track_point_in_utc_time"),
    ):
        radar_data.from_bytes.return_value = MagicMock(name="RadarData")
        coordinator = MeteoSwissRainRadarCoordinator(hass, entry)
        coordinator.detector = MagicMock()
        coordinator.detector.detect.return_value = (True, 2.5)
        yield coordinator
        await coordinator.stop()


def published(*observations, suffixes=(), content=b"rzc"):
    """The daily item as the source lists it, with per-frame site suffixes."""
    assets = [
        rain_asset(observation, suffix=suffix, content=content)
        for observation, suffix in zip(observations, suffixes, strict=True)
    ]
    return item(*assets), assets[-1]


async def test_changed_site_suffix_is_followed_over_several_polls(
    coordinator, httpx_mock, freezer
):
    """The published suffix moved nl -> vl -> ul in service; each poll must work."""
    assert coordinator.poll_seconds == 60 and coordinator.max_age_minutes == 10
    frames = [OBSERVATION + timedelta(minutes=step) for step in (0, 5, 10)]
    suffixes = ("nl", "vl", "ul")
    downloaded = []
    for index, frame in enumerate(frames):
        for poll in range(5):  # Five 60 second polls per five-minute frame.
            freezer.move_to(frame + timedelta(seconds=50 + 60 * poll))
            payload, asset = published(
                *frames[: index + 1], suffixes=suffixes[: index + 1]
            )
            httpx_mock.add_response(url=daily_url(), json=payload)
            if poll == 0:
                httpx_mock.add_response(url=asset["href"], content=b"rzc")
                downloaded.append(asset["href"])
            await coordinator.async_refresh()

            result = coordinator.current_result
            assert result.health == "ok"
            assert result.rain is True and result.distance_km == 2.5
            assert result.last_update == frame

    requests = [str(request.url) for request in httpx_mock.get_requests()]
    assert [url for url in requests if url.endswith(".h5")] == downloaded
    assert len({url.rsplit("/", 1)[-1][-11:-9] for url in downloaded}) == 3
    # A built name would have used one suffix for every frame.
    built = ("0910vl.001.h5", "0920vl.001.h5")
    assert not [url for url in requests if url.endswith(built)]


async def test_older_catalogue_frame_cannot_replace_newer_cached_weather(
    coordinator, httpx_mock, freezer
):
    payload, asset = published(OBSERVATION, suffixes=("ul",))
    httpx_mock.add_response(url=daily_url(), json=payload)
    httpx_mock.add_response(url=asset["href"], content=b"rzc")
    await coordinator.async_refresh()

    older = OBSERVATION - timedelta(minutes=5)
    payload, _ = published(older, suffixes=("rl",))
    httpx_mock.add_response(url=daily_url(), json=payload)
    coordinator.detector.detect.return_value = (False, None)
    freezer.move_to(NOW + timedelta(minutes=1))
    await coordinator.async_refresh()

    assert coordinator.data.health == "ok"
    assert coordinator.data.last_update == OBSERVATION
    assert coordinator.data.rain is True
    assert coordinator.detector.detect.call_count == 1


async def test_no_new_frame_keeps_the_timestamp_and_downloads_once(
    coordinator, httpx_mock, freezer
):
    payload, asset = published(OBSERVATION, suffixes=("ul",))
    httpx_mock.add_response(url=daily_url(), json=payload, headers={"ETag": '"day"'})
    httpx_mock.add_response(url=asset["href"], content=b"rzc")
    await coordinator.async_refresh()

    httpx_mock.add_response(url=daily_url(), status_code=304)
    freezer.move_to(NOW + timedelta(seconds=60))
    await coordinator.async_refresh()

    assert coordinator.data.health == "ok"
    assert coordinator.data.last_update == OBSERVATION
    assert coordinator.detector.detect.call_count == 1
    assert httpx_mock.get_requests()[-1].headers["If-None-Match"] == '"day"'


async def test_same_time_correction_is_downloaded_and_read_again(
    coordinator, httpx_mock
):
    payload, asset = published(OBSERVATION, suffixes=("ul",))
    httpx_mock.add_response(url=daily_url(), json=payload)
    httpx_mock.add_response(url=asset["href"], content=b"rzc")
    await coordinator.async_refresh()

    corrected, asset = published(OBSERVATION, suffixes=("ul",), content=b"corrected")
    httpx_mock.add_response(url=daily_url(), json=corrected)
    httpx_mock.add_response(url=asset["href"], content=b"corrected")
    coordinator.detector.detect.return_value = (False, None)
    await coordinator.async_refresh()

    assert coordinator.detector.detect.call_count == 2
    assert coordinator.data.health == "ok"
    assert coordinator.data.rain is False
    assert coordinator.data.last_update == OBSERVATION  # Source time unchanged.


async def test_asset_403_is_an_error_and_never_restores_cached_weather(
    coordinator, httpx_mock, freezer
):
    payload, asset = published(OBSERVATION, suffixes=("ul",))
    httpx_mock.add_response(url=daily_url(), json=payload)
    httpx_mock.add_response(url=asset["href"], content=b"rzc")
    await coordinator.async_refresh()
    assert coordinator.data.health == "ok"

    freezer.move_to(OBSERVATION + timedelta(minutes=5, seconds=50))
    payload, asset = published(
        OBSERVATION, OBSERVATION + timedelta(minutes=5), suffixes=("ul", "rl")
    )
    httpx_mock.add_response(url=daily_url(), json=payload)
    httpx_mock.add_response(url=asset["href"], status_code=403)
    await coordinator.async_refresh()

    assert coordinator.data.health == "error"
    assert coordinator.data.rain is None and coordinator.data.distance_km is None
    assert coordinator.data.last_update == OBSERVATION

    # A later poll that finds nothing new is no evidence of recovery.
    freezer.move_to(OBSERVATION + timedelta(minutes=6))
    httpx_mock.add_response(url=daily_url(), json=payload)
    httpx_mock.add_response(url=asset["href"], status_code=403)
    await coordinator.async_refresh()
    assert coordinator.data.health == "error"


async def test_catalogue_recovery_refetches_the_cached_frame(coordinator, httpx_mock):
    """A metadata recovery must verify the file again before clearing error."""
    payload, asset = published(OBSERVATION, suffixes=("ul",))
    httpx_mock.add_response(url=daily_url(), json=payload)
    httpx_mock.add_response(url=asset["href"], content=b"rzc")
    await coordinator.async_refresh()

    httpx_mock.add_response(url=daily_url(), status_code=403)
    await coordinator.async_refresh()
    assert coordinator.data.health == "error"

    httpx_mock.add_response(url=daily_url(), json=payload)
    httpx_mock.add_response(url=asset["href"], content=b"rzc")
    await coordinator.async_refresh()

    assert coordinator.data.health == "ok"
    assert coordinator._failed is False
    file_requests = [
        request
        for request in httpx_mock.get_requests()
        if str(request.url) == asset["href"]
    ]
    assert len(file_requests) == 2


async def test_missing_current_frame_uses_the_previous_one(coordinator, httpx_mock):
    previous = OBSERVATION - timedelta(minutes=5)
    payload, asset = published(previous, suffixes=("ul",))
    httpx_mock.add_response(url=daily_url(), json=payload)
    httpx_mock.add_response(url=asset["href"], content=b"rzc")

    await coordinator.async_refresh()

    assert coordinator.data.health == "ok"
    assert coordinator.data.last_update == previous


async def test_previous_day_frame_just_after_midnight(coordinator, httpx_mock, freezer):
    midnight = datetime(2026, 9, 16, 0, 2, tzinfo=UTC)
    previous = datetime(2026, 9, 15, 23, 55, tzinfo=UTC)
    freezer.move_to(midnight)
    asset = rain_asset(previous, suffix="ul")
    httpx_mock.add_response(url=daily_url(midnight), json=item(day=midnight))
    httpx_mock.add_response(url=daily_url(previous), json=item(asset, day=previous))
    httpx_mock.add_response(url=asset["href"], content=b"rzc")

    await coordinator.async_refresh()

    assert coordinator.data.health == "ok"
    assert coordinator.data.last_update == previous


async def test_daily_item_404_reports_missing_without_weather(coordinator, httpx_mock):
    for _ in range(3):
        httpx_mock.add_response(url=daily_url(), status_code=404)
    httpx_mock.add_response(
        url=daily_url(NOW - timedelta(days=1)),
        json=item(day=NOW - timedelta(days=1)),
    )

    with patch("asyncio.sleep"):
        await coordinator.async_refresh()

    assert coordinator.data.health == "missing"
    assert coordinator.data.rain is None
    assert coordinator.data.last_update is None
    assert coordinator.last_update_success  # Still polling, not a setup failure.


async def test_catalogue_403_is_an_error(coordinator, httpx_mock):
    httpx_mock.add_response(url=daily_url(), status_code=403)

    await coordinator.async_refresh()

    assert coordinator.data.health == "error"
    assert coordinator.data.rain is None


async def test_stale_published_frame_is_not_downloaded(coordinator, httpx_mock):
    old = OBSERVATION - timedelta(hours=2)
    payload, _ = published(old, suffixes=("ul",))
    httpx_mock.add_response(url=daily_url(), json=payload)

    await coordinator.async_refresh()

    assert coordinator.data.health == "stale"
    assert coordinator.data.rain is None
    assert coordinator.data.last_update == old
    assert coordinator.detector.detect.call_count == 0


async def test_stop_keeps_the_shared_client_open_and_stops_new_work(
    hass, coordinator, httpx_mock
):
    payload, asset = published(OBSERVATION, suffixes=("ul",))
    httpx_mock.add_response(url=daily_url(), json=payload)
    httpx_mock.add_response(url=asset["href"], content=b"rzc")
    await coordinator.async_refresh()
    requests = len(httpx_mock.get_requests())

    client = coordinator.downloader._client
    await coordinator.stop()
    await coordinator.async_refresh()

    assert len(httpx_mock.get_requests()) == requests
    assert coordinator._remove_listener is None and coordinator._cancel_expiry is None
    assert not client.is_closed  # The shared Home Assistant client stays open.
