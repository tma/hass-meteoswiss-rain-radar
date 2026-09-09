"""Deterministic STAC and HTTP tests, including archive/time edges."""

import hashlib
import threading
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from custom_components.meteoswiss_rain_radar.hail_downloader import (
    ASSET_PATH,
    COLLECTION_URL,
    ITEMS_URL,
    HailAsset,
    HailDownloader,
    _parse_metadata,
    _select_candidates,
)

from .hail_helpers import OBSERVATION

MODULE = "custom_components.meteoswiss_rain_radar.hail_downloader"


def daily_url(day=OBSERVATION):
    return f"{COLLECTION_URL}/items/{day:%Y%m%d}-ch"


TODAY_URL = daily_url()


def item(*assets, day=OBSERVATION):
    return {
        "type": "Feature",
        "id": day.strftime("%Y%m%d-ch"),
        "assets": {asset["href"].split("/")[-1]: asset for asset in assets},
    }


def empty_days(httpx_mock, now=OBSERVATION, start=0, stop=2):
    for offset in range(start, stop):
        day = now - timedelta(days=offset)
        httpx_mock.add_response(url=daily_url(day), json=item(day=day))


def stac_asset(observation=OBSERVATION, content=b"hail", filename=None):
    name = filename or observation.strftime("bzc%y%j%H%Mvl.845.h5")
    item_id = observation.strftime("%Y%m%d-ch")
    return {
        "href": f"https://data.geo.admin.ch{ASSET_PATH}{item_id}/{name}",
        "type": "application/x-hdf5",
        "file:checksum": "1220" + hashlib.sha256(content).hexdigest(),
    }


def page(*assets, next_url=None):
    items = {}
    for asset in assets:
        item_id = asset["href"].split("/")[-2]
        item = items.setdefault(
            item_id,
            {
                "id": item_id,
                "properties": {"datetime": "2099-01-01T00:00:00Z"},
                "assets": {},
            },
        )
        item["assets"][asset["href"].split("/")[-1]] = asset
    return {
        "features": list(items.values()),
        "links": [{"rel": "next", "href": next_url}] if next_url else [],
    }


@pytest.fixture
async def downloader():
    downloader = HailDownloader()
    yield downloader
    await downloader.close()


async def test_pagination_selects_latest_past_and_ignores_item_datetime(
    downloader, httpx_mock
):
    empty_days(httpx_mock)
    older = stac_asset(OBSERVATION - timedelta(minutes=5))
    latest = stac_asset()
    future = stac_asset(OBSERVATION + timedelta(minutes=5))
    daily = [
        stac_asset(filename=f"bzc26252{hour}vl.845.h5")
        for hour in ("2400", "3000", "0826")
    ]
    meshs = stac_asset(filename="mzc262520825vl.850.h5")
    next_url = f"{COLLECTION_URL}/items?cursor=second"
    httpx_mock.add_response(url=ITEMS_URL, json=page(older, next_url=next_url))
    httpx_mock.add_response(url=next_url, json=page(latest, future, meshs, *daily))
    result = await downloader.discover(OBSERVATION + timedelta(minutes=1))
    assert result.asset.url == latest["href"]
    assert result.observation == OBSERVATION


@pytest.mark.parametrize(
    "now",
    [
        datetime(2027, 1, 1, 0, 1, tzinfo=UTC),
        datetime(2024, 3, 1, 0, 1, tzinfo=UTC),
        datetime(2026, 9, 10, 0, 1, tzinfo=UTC),
    ],
)
async def test_day_year_and_leap_rollover(downloader, httpx_mock, now):
    previous = now.replace(hour=0, minute=0) - timedelta(minutes=5)
    asset = stac_asset(previous, filename=previous.strftime("bzc%y%j%H%Mnl.123.h5"))
    httpx_mock.add_response(
        url=daily_url(now), json=item(stac_asset(now.replace(minute=5)), day=now)
    )
    httpx_mock.add_response(url=daily_url(previous), json=item(asset, day=previous))
    result = await downloader.discover(now)
    assert result.observation == previous


@pytest.mark.parametrize(
    "filename",
    [
        "bzc263660825vl.845.h5",
        "bzc252520825vl.845.h5",
        "bzc262520860vl.845.h5",
        "bzc262520824vl.845.h5",
    ],
)
async def test_reject_invalid_filename_or_item_date(downloader, httpx_mock, filename):
    httpx_mock.add_response(url=TODAY_URL, json=item(stac_asset(filename=filename)))
    empty_days(httpx_mock, start=1)
    httpx_mock.add_response(url=ITEMS_URL, json=page())
    assert (await downloader.discover(OBSERVATION)).health == "missing"


async def test_future_only_and_empty_listing_are_unknown(downloader, httpx_mock):
    httpx_mock.add_response(
        url=TODAY_URL, json=item(stac_asset(OBSERVATION + timedelta(minutes=5)))
    )
    empty_days(httpx_mock, start=1)
    httpx_mock.add_response(url=ITEMS_URL, json=page())
    result = await downloader.discover(OBSERVATION)
    assert result.asset is None and result.health == "future"
    assert result.observation > OBSERVATION
    empty_days(httpx_mock, stop=15)
    result = await downloader.discover(OBSERVATION)
    assert result.asset is None and result.health == "missing"


async def test_conditional_daily_items_and_url_checksum_file_cache(
    downloader, httpx_mock
):
    asset = stac_asset()
    httpx_mock.add_response(
        url=TODAY_URL, json=item(asset), headers={"ETag": '"page-version"'}
    )
    httpx_mock.add_response(url=asset["href"], content=b"hail")
    selected = (await downloader.discover(OBSERVATION)).asset
    assert await downloader.fetch(selected) == b"hail"
    httpx_mock.add_response(url=TODAY_URL, status_code=304)
    selected = (await downloader.discover(OBSERVATION + timedelta(minutes=1))).asset
    assert await downloader.fetch(selected) == b"hail"
    assert httpx_mock.get_requests()[-1].headers["If-None-Match"] == '"page-version"'
    assert len(httpx_mock.get_requests()) == 3

    corrected = stac_asset(content=b"corrected")
    httpx_mock.add_response(url=TODAY_URL, json=item(corrected))
    httpx_mock.add_response(url=asset["href"], content=b"corrected")
    selected = (await downloader.discover(OBSERVATION + timedelta(minutes=2))).asset
    assert await downloader.fetch(selected) == b"corrected"
    assert selected.observation == OBSERVATION


async def test_new_url_with_same_checksum_is_a_new_identity(downloader, httpx_mock):
    for time in (OBSERVATION, OBSERVATION + timedelta(minutes=5)):
        asset = stac_asset(time)
        httpx_mock.add_response(url=TODAY_URL, json=item(asset))
        httpx_mock.add_response(url=asset["href"], content=b"hail")
        assert (
            await downloader.fetch((await downloader.discover(time)).asset) == b"hail"
        )
    assert len(httpx_mock.get_requests()) == 4


async def test_failed_checksum_not_cached(downloader, httpx_mock):
    asset = stac_asset()
    selected = HailAsset(asset["href"], asset["file:checksum"][4:], OBSERVATION)
    httpx_mock.add_response(url=asset["href"], content=b"wrong")
    with pytest.raises(ValueError, match="SHA256"):
        await downloader.fetch(selected)
    httpx_mock.add_response(url=asset["href"], content=b"hail")
    assert await downloader.fetch(selected) == b"hail"


async def test_missing_checksum_is_error_not_clear(downloader, httpx_mock):
    asset = stac_asset()
    del asset["file:checksum"]
    httpx_mock.add_response(url=TODAY_URL, json=item(asset))
    with pytest.raises(ValueError, match="checksum"):
        await downloader.discover(OBSERVATION)


@pytest.mark.parametrize("status", [404, 429, 503])
async def test_bounded_retry_then_recovery(downloader, httpx_mock, status):
    httpx_mock.add_response(url=TODAY_URL, status_code=status)
    httpx_mock.add_response(url=TODAY_URL, json=item(stac_asset()))
    with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock) as sleep:
        assert (await downloader.discover(OBSERVATION)).health == "ok"
    sleep.assert_awaited_once_with(0.5)


async def test_transport_retries_exhaust_and_do_not_fall_back_to_cached_listing(
    downloader, httpx_mock
):
    httpx_mock.add_response(url=TODAY_URL, json=item(stac_asset()))
    await downloader.discover(OBSERVATION)
    for _ in range(3):
        httpx_mock.add_exception(httpx.ReadTimeout("timeout"), url=TODAY_URL)
    with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock) as sleep:
        with pytest.raises(httpx.ReadTimeout):
            await downloader.discover(OBSERVATION)
    assert sleep.await_count == 2


async def test_permanent_error_not_retried(downloader, httpx_mock):
    httpx_mock.add_response(url=TODAY_URL, status_code=403)
    with pytest.raises(httpx.HTTPStatusError):
        await downloader.discover(OBSERVATION)
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize(
    "next_url",
    [ITEMS_URL, "https://example.com/items", "http://data.geo.admin.ch/items"],
)
async def test_pagination_loop_and_untrusted_links_rejected(
    downloader, httpx_mock, next_url
):
    empty_days(httpx_mock)
    httpx_mock.add_response(url=ITEMS_URL, json=page(stac_asset(), next_url=next_url))
    with pytest.raises(ValueError, match="pagination"):
        await downloader.discover(OBSERVATION)


async def test_page_limit_fails_instead_of_selecting_partial_listing(
    downloader, httpx_mock
):
    empty_days(httpx_mock)
    httpx_mock.add_response(
        url=ITEMS_URL,
        json=page(stac_asset(), next_url=f"{COLLECTION_URL}/items?next=1"),
    )
    with patch(f"{MODULE}.MAX_PAGES", 1), pytest.raises(ValueError, match="pagination"):
        await downloader.discover(OBSERVATION)


async def test_metadata_byte_limits(downloader, httpx_mock):
    httpx_mock.add_response(url=TODAY_URL, content=b"123456789")
    with (
        patch(f"{MODULE}.MAX_PAGE_BYTES", 8),
        pytest.raises(ValueError, match="byte limit"),
    ):
        await downloader.discover(OBSERVATION)
    empty_days(httpx_mock)
    httpx_mock.add_response(url=ITEMS_URL, json=page(stac_asset()))
    with (
        patch(f"{MODULE}.MAX_DISCOVERY_BYTES", 256),
        pytest.raises(ValueError, match="byte limit"),
    ):
        await downloader.discover(OBSERVATION)


async def test_unexpected_304_and_malformed_json(downloader, httpx_mock):
    httpx_mock.add_response(url=TODAY_URL, status_code=304)
    with pytest.raises(ValueError, match="304"):
        await downloader.discover(OBSERVATION)
    httpx_mock.add_response(url=TODAY_URL, content=b"{broken")
    with pytest.raises(ValueError):
        await downloader.discover(OBSERVATION)


async def test_last_modified_validator(downloader, httpx_mock):
    httpx_mock.add_response(
        url=TODAY_URL,
        json=item(stac_asset()),
        headers={"Last-Modified": "Wed, 09 Sep 2026 08:26:00 GMT"},
    )
    await downloader.discover(OBSERVATION)
    httpx_mock.add_response(url=TODAY_URL, status_code=304)
    await downloader.discover(OBSERVATION)
    assert (
        httpx_mock.get_requests()[-1].headers["If-Modified-Since"]
        == "Wed, 09 Sep 2026 08:26:00 GMT"
    )


async def test_total_discovery_deadline(downloader):
    import asyncio

    real_timeout = asyncio.timeout

    async def blocked(*args):
        await asyncio.Event().wait()

    with (
        patch.object(downloader, "_get", side_effect=blocked),
        patch(f"{MODULE}.asyncio.timeout", side_effect=lambda _: real_timeout(0.001)),
        pytest.raises(TimeoutError),
    ):
        await downloader.discover(OBSERVATION)


@pytest.mark.parametrize(
    "payload",
    [[], {}, {"features": [None], "links": []}, {"features": [], "links": [None]}],
)
async def test_malformed_stac_schema(downloader, httpx_mock, payload):
    httpx_mock.add_response(url=TODAY_URL, json=payload)
    with pytest.raises(ValueError, match="STAC"):
        await downloader.discover(OBSERVATION)


async def test_file_byte_bound_and_unexpected_asset_304(downloader, httpx_mock):
    asset = stac_asset()
    selected = HailAsset(asset["href"], asset["file:checksum"][4:], OBSERVATION)
    httpx_mock.add_response(url=selected.url, content=b"hail")
    with (
        patch(f"{MODULE}.MAX_FILE_BYTES", 3),
        pytest.raises(ValueError, match="byte limit"),
    ):
        await downloader.fetch(selected)
    httpx_mock.add_response(url=selected.url, status_code=304)
    with pytest.raises(ValueError, match="304"):
        await downloader.fetch(selected)


async def test_close_releases_client_and_caches(downloader, httpx_mock):
    httpx_mock.add_response(url=TODAY_URL, json=item(stac_asset()))
    await downloader.discover(OBSERVATION)
    client = downloader._client
    await downloader.close()
    assert client.is_closed
    assert downloader._client is None
    assert downloader._items == {}
    assert downloader._cached_identity is None


async def test_daily_304_reuses_immutable_metadata_and_rechecks_future_in_executor(
    hass, httpx_mock
):
    loop_thread = threading.get_ident()
    parses, selections = [], []

    def parse(*args):
        assert threading.get_ident() != loop_thread
        parses.append(args[1])
        return _parse_metadata(*args)

    def select(*args):
        assert threading.get_ident() != loop_thread
        selections.append(args[0])
        return _select_candidates(*args)

    downloader = HailDownloader(async_add_executor_job=hass.async_add_executor_job)
    future = OBSERVATION + timedelta(minutes=5)
    httpx_mock.add_response(
        url=TODAY_URL,
        json=item(stac_asset(), stac_asset(future)),
        headers={"ETag": '"daily"'},
    )
    httpx_mock.add_response(url=TODAY_URL, status_code=304)
    try:
        with (
            patch(f"{MODULE}._parse_metadata", new=parse),
            patch(f"{MODULE}._select_candidates", new=select),
        ):
            assert (await downloader.discover(OBSERVATION)).observation == OBSERVATION
            assert (await downloader.discover(future)).observation == future
        assert parses == [TODAY_URL]
        assert selections[0] is selections[1] is downloader._items[TODAY_URL]
        with pytest.raises(FrozenInstanceError):
            selections[0].etag = "changed"
        assert httpx_mock.get_requests()[-1].headers["If-None-Match"] == '"daily"'
    finally:
        await downloader.close()


async def test_one_cold_listing_then_daily_history_newest_first_and_corrections(
    downloader, httpx_mock
):
    historic = OBSERVATION - timedelta(days=3)
    previous = stac_asset(historic)
    empty_days(httpx_mock)
    next_url = f"{COLLECTION_URL}/items?cursor=old"
    httpx_mock.add_response(url=ITEMS_URL, json=page(next_url=next_url))
    httpx_mock.add_response(url=next_url, json=page(previous))
    assert (await downloader.discover(OBSERVATION)).observation == historic

    for poll in range(3):
        empty_days(httpx_mock, stop=3)
        httpx_mock.add_response(
            url=daily_url(historic),
            json=item(stac_asset(historic, content=str(poll).encode()), day=historic),
            headers={"ETag": f'"history-{poll}"'},
        )
        result = await downloader.discover(OBSERVATION + timedelta(minutes=poll))
        assert result.observation == historic
        assert result.asset.checksum == hashlib.sha256(str(poll).encode()).hexdigest()
    requests = httpx_mock.get_requests()
    assert sum(str(request.url) == ITEMS_URL for request in requests) == 1
    assert [str(request.url) for request in requests[4:8]] == [
        daily_url(OBSERVATION - timedelta(days=offset)) for offset in range(4)
    ]
    assert requests[-1].headers["If-None-Match"] == '"history-1"'
    assert set(downloader._items) == {
        daily_url(OBSERVATION - timedelta(days=offset)) for offset in range(4)
    }


async def test_exact_14_day_cutoff_on_304_and_cache_eviction(downloader, httpx_mock):
    cutoff = OBSERVATION - timedelta(days=14)
    empty_days(httpx_mock)
    httpx_mock.add_response(url=ITEMS_URL, json=page(stac_asset(cutoff)))
    assert (await downloader.discover(OBSERVATION)).observation == cutoff
    empty_days(httpx_mock, stop=14)
    httpx_mock.add_response(
        url=daily_url(cutoff),
        json=item(stac_asset(cutoff), day=cutoff),
        headers={"ETag": '"cutoff"'},
    )
    assert (await downloader.discover(OBSERVATION)).observation == cutoff
    cached = downloader._items[daily_url(cutoff)]
    empty_days(httpx_mock, stop=14)
    httpx_mock.add_response(url=daily_url(cutoff), status_code=304)
    result = await downloader.discover(OBSERVATION + timedelta(microseconds=1))
    assert result.health == "missing"
    assert downloader._items[daily_url(cutoff)] is cached
    tomorrow = OBSERVATION + timedelta(days=1)
    httpx_mock.add_response(
        url=daily_url(tomorrow), json=item(stac_asset(tomorrow), day=tomorrow)
    )
    await downloader.discover(tomorrow)
    assert daily_url(cutoff) not in downloader._items


async def test_daily_404_exhaustion_means_absence_not_cached_weather(
    downloader, httpx_mock
):
    httpx_mock.add_response(
        url=TODAY_URL, json=item(stac_asset()), headers={"ETag": '"old"'}
    )
    await downloader.discover(OBSERVATION)
    for _ in range(3):
        httpx_mock.add_response(url=TODAY_URL, status_code=404)
    yesterday = OBSERVATION - timedelta(days=1)
    httpx_mock.add_response(
        url=daily_url(yesterday), json=item(stac_asset(yesterday), day=yesterday)
    )
    with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock) as sleep:
        assert (await downloader.discover(OBSERVATION)).observation == yesterday
    assert sleep.await_count == 2
    assert TODAY_URL not in downloader._items
    assert not downloader._historical_fallback_attempted


async def test_asset_404_remains_retriable_not_absence(downloader, httpx_mock):
    metadata = stac_asset()
    asset = HailAsset(metadata["href"], metadata["file:checksum"][4:], OBSERVATION)
    for _ in range(3):
        httpx_mock.add_response(url=asset.url, status_code=404)
    with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock) as sleep:
        with pytest.raises(httpx.HTTPStatusError):
            await downloader.fetch(asset)
    assert sleep.await_count == 2
    assert downloader._cached_bytes is None


@pytest.mark.parametrize("failure", ["transport", "schema", "checksum"])
async def test_daily_failure_cannot_trigger_cold_historical_fallback(
    downloader, httpx_mock, failure
):
    if failure == "transport":
        for _ in range(3):
            httpx_mock.add_exception(httpx.ReadTimeout("timeout"), url=TODAY_URL)
    else:
        payload = item(stac_asset())
        if failure == "schema":
            payload["assets"] = []
        else:
            next(iter(payload["assets"].values()))["file:checksum"] = "bad"
        httpx_mock.add_response(url=TODAY_URL, json=payload)
    with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises((httpx.ReadTimeout, ValueError)):
            await downloader.discover(OBSERVATION)
    assert not downloader._historical_fallback_attempted
    assert all(str(request.url) == TODAY_URL for request in httpx_mock.get_requests())


async def test_failed_cold_fallback_is_not_repeated(downloader, httpx_mock):
    empty_days(httpx_mock)
    httpx_mock.add_response(url=ITEMS_URL, json={"features": [None], "links": []})
    with pytest.raises(ValueError, match="STAC"):
        await downloader.discover(OBSERVATION)
    empty_days(httpx_mock, stop=15)
    assert (await downloader.discover(OBSERVATION)).health == "missing"
    assert (
        sum(str(request.url) == ITEMS_URL for request in httpx_mock.get_requests()) == 1
    )


async def test_daily_selection_uses_utc_not_local_date(downloader, httpx_mock):
    from datetime import timezone

    local = OBSERVATION.astimezone(timezone(timedelta(hours=-12)))
    assert local.date() != OBSERVATION.date()
    httpx_mock.add_response(url=TODAY_URL, json=item(stac_asset()))
    assert (await downloader.discover(local)).observation == OBSERVATION


async def test_daily_history_total_byte_bound(downloader, httpx_mock):
    for offset in range(2):
        day = OBSERVATION - timedelta(days=offset)
        payload = item(day=day)
        payload["padding"] = "x" * 600
        httpx_mock.add_response(url=daily_url(day), json=payload)
    with (
        patch(f"{MODULE}.MAX_DISCOVERY_BYTES", 1000),
        pytest.raises(ValueError, match="byte limit"),
    ):
        await downloader.discover(OBSERVATION)
    assert not downloader._historical_fallback_attempted


@pytest.mark.parametrize(
    "payload",
    [[], {}, {"features": [None], "links": []}, {"features": [], "links": [None]}],
)
async def test_malformed_historical_collection(downloader, httpx_mock, payload):
    empty_days(httpx_mock)
    httpx_mock.add_response(url=ITEMS_URL, json=payload)
    with pytest.raises(ValueError, match="STAC"):
        await downloader.discover(OBSERVATION)
