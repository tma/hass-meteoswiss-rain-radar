"""Rain discovery from the official daily catalogue item, never a guessed name."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from custom_components.meteoswiss_rain_radar.radar_downloader import (
    ASSET_PATH,
    COLLECTION_URL,
    RAIN_PRODUCT,
    create_radar_downloader,
)
from custom_components.meteoswiss_rain_radar.stac_downloader import StacAsset

MODULE = "custom_components.meteoswiss_rain_radar.stac_downloader"
# A 2026-09-15 slot whose guessed rzc...vl.001.h5 name returned 403 while the
# catalogue listed rzc...ul.001.h5 and served it with 200.
OBSERVATION = datetime(2026, 9, 15, 9, 10, tzinfo=UTC)
NOW = OBSERVATION + timedelta(seconds=50)


def daily_url(day=OBSERVATION):
    return f"{COLLECTION_URL}/items/{day:%Y%m%d}-ch"


def rain_asset(observation=OBSERVATION, *, suffix="ul", content=b"rzc", filename=None):
    name = filename or observation.strftime(f"rzc%y%j%H%M{suffix}.001.h5")
    item_id = observation.strftime("%Y%m%d-ch")
    return {
        "href": f"https://data.geo.admin.ch{ASSET_PATH}{item_id}/{name}",
        "type": "application/x-hdf5",
        "file:checksum": "1220" + hashlib.sha256(content).hexdigest(),
    }


def item(*assets, day=OBSERVATION):
    return {
        "type": "Feature",
        "id": day.strftime("%Y%m%d-ch"),
        "properties": {"datetime": "2099-01-01T00:00:00Z"},
        "assets": {asset["href"].rsplit("/", 1)[-1]: asset for asset in assets},
    }


def empty_days(httpx_mock, now=NOW, start=0, stop=2):
    for offset in range(start, stop):
        day = now - timedelta(days=offset)
        httpx_mock.add_response(url=daily_url(day), json=item(day=day))


@pytest.fixture
async def downloader():
    downloader = create_radar_downloader()
    yield downloader
    await downloader.close()


@pytest.mark.parametrize("suffix", ["nl", "vl", "ul", "rl", "5l"])
async def test_published_suffix_is_used_instead_of_a_guessed_name(
    downloader, httpx_mock, suffix
):
    asset = rain_asset(suffix=suffix)
    httpx_mock.add_response(url=daily_url(), json=item(asset))
    httpx_mock.add_response(url=asset["href"], content=b"rzc")

    discovery = await downloader.discover(NOW)

    assert discovery.health == "ok"
    assert discovery.observation == OBSERVATION
    assert discovery.asset.url == asset["href"]
    assert discovery.asset.url.endswith(f"rzc262580910{suffix}.001.h5")
    assert await downloader.fetch(discovery.asset) == b"rzc"
    assert str(httpx_mock.get_requests()[-1].url) == asset["href"]


async def test_latest_frame_wins_and_future_frames_are_not_used(downloader, httpx_mock):
    older = rain_asset(OBSERVATION - timedelta(minutes=5), suffix="vl")
    latest = rain_asset()
    future = rain_asset(OBSERVATION + timedelta(minutes=5), suffix="rl")
    httpx_mock.add_response(url=daily_url(), json=item(older, latest, future))

    discovery = await downloader.discover(NOW)

    assert discovery.asset.url == latest["href"]
    assert discovery.observation == OBSERVATION


@pytest.mark.parametrize(
    "filename",
    [
        "cpc262580910ul.001.h5",  # Another product in the same daily item.
        "rzc262580910ul.845.h5",  # Another product grid identifier.
        "rzc262580910ul.002.h5",  # Not a reserved x01 grid.
        "rzc262580910u.001.h5",  # Truncated site suffix.
        "rzc262582400ul.001.h5",  # Daily aggregate.
        "rzc262583000ul.001.h5",
        "rzc262580912ul.001.h5",  # Not a five-minute slot.
        "rzc263660910ul.001.h5",  # Day of year does not match the item date.
        "rzc252580910ul.001.h5",  # Year does not match the item date.
        "rzc262580910ul.001.h5.tmp",
    ],
)
async def test_unrelated_products_grids_and_aggregates_are_ignored(
    downloader, httpx_mock, filename
):
    httpx_mock.add_response(url=daily_url(), json=item(rain_asset(filename=filename)))
    empty_days(httpx_mock, start=1)

    assert (await downloader.discover(NOW)).health == "missing"


@pytest.mark.parametrize(
    "href",
    [
        "http://data.geo.admin.ch/ch.meteoschweiz.ogd-radar-precip/"
        "20260915-ch/rzc262580910ul.001.h5",
        "https://example.invalid/ch.meteoschweiz.ogd-radar-precip/"
        "20260915-ch/rzc262580910ul.001.h5",
        "https://data.geo.admin.ch/ch.meteoschweiz.ogd-radar-hail/"
        "20260915-ch/rzc262580910ul.001.h5",
        "https://data.geo.admin.ch/ch.meteoschweiz.ogd-radar-precip/"
        "20260914-ch/rzc262580910ul.001.h5",
        "https://data.geo.admin.ch/ch.meteoschweiz.ogd-radar-precip/"
        "20260915-ch/rzc262580910ul.001.h5?download=1",
        "https://data.geo.admin.ch/ch.meteoschweiz.ogd-radar-precip/"
        "20260915-ch/rzc262580910ul.001.h5#fragment",
        "https://data.geo.admin.ch/ch.meteoschweiz.ogd-radar-precip/"
        "20260915-ch/../../other/rzc262580910ul.001.h5",
        "https://data.geo.admin.ch/ch.meteoschweiz.ogd-radar-precip/"
        "20260915-ch/%2e%2e/%2e%2e/other/rzc262580910ul.001.h5",
    ],
)
async def test_foreign_or_mismatched_hrefs_are_ignored(downloader, httpx_mock, href):
    asset = rain_asset()
    asset["href"] = href
    httpx_mock.add_response(url=daily_url(), json=item(asset))
    empty_days(httpx_mock, start=1)

    assert (await downloader.discover(NOW)).health == "missing"


async def test_unofficial_asset_url_is_never_downloaded(downloader):
    asset = StacAsset(
        "https://example.invalid/rzc262580910ul.001.h5",
        hashlib.sha256(b"rzc").hexdigest(),
        OBSERVATION,
    )

    with pytest.raises(ValueError, match="Unofficial"):
        await downloader.fetch(asset)


async def test_previous_utc_day_covers_frames_around_midnight(downloader, httpx_mock):
    now = datetime(2026, 9, 16, 0, 2, tzinfo=UTC)
    previous = datetime(2026, 9, 15, 23, 55, tzinfo=UTC)
    httpx_mock.add_response(url=daily_url(now), json=item(day=now))
    httpx_mock.add_response(
        url=daily_url(previous), json=item(rain_asset(previous), day=previous)
    )

    discovery = await downloader.discover(now)

    assert discovery.observation == previous
    assert discovery.asset.url.endswith("rzc262582355ul.001.h5")
    assert set(downloader._items) == {daily_url(now), daily_url(previous)}


async def test_search_is_two_days_with_no_archive_listing(downloader, httpx_mock):
    empty_days(httpx_mock)

    assert (await downloader.discover(NOW)).health == "missing"

    assert [str(request.url) for request in httpx_mock.get_requests()] == [
        daily_url(NOW),
        daily_url(NOW - timedelta(days=1)),
    ]
    assert RAIN_PRODUCT.cold_listing is False


async def test_older_frame_is_reported_with_its_own_source_time(downloader, httpx_mock):
    older = OBSERVATION - timedelta(hours=3)
    httpx_mock.add_response(url=daily_url(), json=item(rain_asset(older)))

    discovery = await downloader.discover(NOW)

    # Discovery reports what is published; the coordinator decides freshness.
    assert discovery.health == "ok" and discovery.observation == older


async def test_daily_item_404_is_absence_and_403_is_an_error(downloader, httpx_mock):
    for _ in range(3):
        httpx_mock.add_response(url=daily_url(), status_code=404)
    httpx_mock.add_response(
        url=daily_url(NOW - timedelta(days=1)),
        json=item(day=NOW - timedelta(days=1)),
    )
    with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
        assert (await downloader.discover(NOW)).health == "missing"
    assert daily_url() not in downloader._items

    httpx_mock.add_response(url=daily_url(), status_code=403)
    with pytest.raises(httpx.HTTPStatusError):
        await downloader.discover(NOW)


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_transient_daily_statuses_are_errors_not_absence(
    downloader, httpx_mock, status
):
    for _ in range(3):
        httpx_mock.add_response(url=daily_url(), status_code=status)
    with (
        patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await downloader.discover(NOW)


@pytest.mark.parametrize("status", [301, 302, 401, 403])
async def test_permanent_daily_statuses_fail_without_a_retry(
    downloader, httpx_mock, status
):
    httpx_mock.add_response(url=daily_url(), status_code=status)

    with pytest.raises(httpx.HTTPStatusError):
        await downloader.discover(NOW)

    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize("payload", [b"{broken", b"[]", b'{"type": "Collection"}'])
async def test_malformed_daily_item_is_an_error(downloader, httpx_mock, payload):
    httpx_mock.add_response(url=daily_url(), content=payload)

    with pytest.raises(ValueError):
        await downloader.discover(NOW)


async def test_conditional_revalidation_and_same_time_correction(
    downloader, httpx_mock
):
    asset = rain_asset()
    httpx_mock.add_response(
        url=daily_url(), json=item(asset), headers={"ETag": '"daily"'}
    )
    httpx_mock.add_response(url=asset["href"], content=b"rzc")
    first = (await downloader.discover(NOW)).asset
    assert await downloader.fetch(first) == b"rzc"

    httpx_mock.add_response(url=daily_url(), status_code=304)
    again = (await downloader.discover(NOW + timedelta(seconds=60))).asset
    assert again.identity == first.identity
    assert await downloader.fetch(again) == b"rzc"  # Served from the file cache.
    assert httpx_mock.get_requests()[-1].headers["If-None-Match"] == '"daily"'
    assert len(httpx_mock.get_requests()) == 3

    corrected = rain_asset(content=b"corrected")
    httpx_mock.add_response(url=daily_url(), json=item(corrected))
    httpx_mock.add_response(url=corrected["href"], content=b"corrected")
    latest = (await downloader.discover(NOW + timedelta(seconds=120))).asset
    assert latest.observation == OBSERVATION
    assert latest.identity != first.identity  # Same URL, new checksum, new identity.
    assert await downloader.fetch(latest) == b"corrected"


async def test_cache_keeps_at_most_the_two_requested_days(downloader, httpx_mock):
    empty_days(httpx_mock)
    await downloader.discover(NOW)
    assert set(downloader._items) == {
        daily_url(NOW),
        daily_url(NOW - timedelta(days=1)),
    }

    tomorrow = NOW + timedelta(days=1)
    httpx_mock.add_response(url=daily_url(tomorrow), json=item(day=tomorrow))
    httpx_mock.add_response(url=daily_url(NOW), status_code=304)
    await downloader.discover(tomorrow)

    assert set(downloader._items) == {daily_url(tomorrow), daily_url(NOW)}


async def test_missing_checksum_is_an_error_not_a_missing_frame(downloader, httpx_mock):
    asset = rain_asset()
    del asset["file:checksum"]
    httpx_mock.add_response(url=daily_url(), json=item(asset))

    with pytest.raises(ValueError, match="checksum"):
        await downloader.discover(NOW)


async def test_file_checksum_mismatch_and_403(downloader, httpx_mock):
    asset = rain_asset()
    selected = StacAsset(asset["href"], asset["file:checksum"][4:], OBSERVATION)
    httpx_mock.add_response(url=selected.url, content=b"wrong")
    with pytest.raises(ValueError, match="SHA256"):
        await downloader.fetch(selected)
    assert downloader._cached_bytes is None

    httpx_mock.add_response(url=selected.url, status_code=403)
    with pytest.raises(httpx.HTTPStatusError):
        await downloader.fetch(selected)


async def test_response_byte_limits(downloader, httpx_mock):
    httpx_mock.add_response(url=daily_url(), content=b"123456789")
    with (
        patch(f"{MODULE}.MAX_PAGE_BYTES", 8),
        pytest.raises(ValueError, match="byte limit"),
    ):
        await downloader.discover(NOW)

    asset = rain_asset()
    selected = StacAsset(asset["href"], asset["file:checksum"][4:], OBSERVATION)
    httpx_mock.add_response(url=selected.url, content=b"rzc")
    with (
        patch(f"{MODULE}.MAX_FILE_BYTES", 2),
        pytest.raises(ValueError, match="byte limit"),
    ):
        await downloader.fetch(selected)


async def test_close_releases_the_owned_client_and_caches(downloader, httpx_mock):
    asset = rain_asset()
    httpx_mock.add_response(url=daily_url(), json=item(asset))
    httpx_mock.add_response(url=asset["href"], content=b"rzc")
    discovery = await downloader.discover(NOW)
    await downloader.fetch(discovery.asset)
    client = downloader._client

    await downloader.close()

    assert client.is_closed
    assert downloader._client is None
    assert downloader._items == {}
    assert downloader._cached_bytes is None and downloader._cached_identity is None
    with pytest.raises(RuntimeError, match="closed"):
        await downloader.discover(NOW)


async def test_close_stops_retry_after_an_inflight_failure(downloader, httpx_mock):
    asset = rain_asset()
    selected = StacAsset(asset["href"], asset["file:checksum"][4:], OBSERVATION)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def failed_request(_request):
        entered.set()
        await release.wait()
        return httpx.Response(503)

    httpx_mock.add_callback(failed_request, url=selected.url)
    task = asyncio.create_task(downloader.fetch(selected))
    await entered.wait()
    await downloader.close()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(httpx_mock.get_requests()) == 1
    assert downloader._cached_bytes is None


async def test_close_during_retry_backoff_stops_the_next_request(
    downloader, httpx_mock
):
    asset = rain_asset()
    selected = StacAsset(asset["href"], asset["file:checksum"][4:], OBSERVATION)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_sleep(_delay):
        entered.set()
        await release.wait()

    httpx_mock.add_response(url=selected.url, status_code=503)
    with patch(f"{MODULE}.asyncio.sleep", side_effect=blocked_sleep):
        task = asyncio.create_task(downloader.fetch(selected))
        await entered.wait()
        await downloader.close()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(httpx_mock.get_requests()) == 1
    assert downloader._cached_bytes is None


async def test_borrowed_client_is_not_closed(httpx_mock):
    client = httpx.AsyncClient()
    borrowed = create_radar_downloader(client)
    empty_days(httpx_mock)

    assert (await borrowed.discover(NOW)).health == "missing"
    await borrowed.close()

    assert not client.is_closed
    await client.aclose()
