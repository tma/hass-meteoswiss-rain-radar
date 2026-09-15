"""Client ownership and real SSL thread boundaries for rain and hail."""

import asyncio
import ssl
import threading
from unittest.mock import patch

import httpx
import pytest
from homeassistant.helpers.httpx_client import get_async_client

from custom_components.meteoswiss_rain_radar.coordinator import (
    MeteoSwissRainRadarCoordinator,
)
from custom_components.meteoswiss_rain_radar.hail_coordinator import (
    MeteoSwissHailCoordinator,
)
from custom_components.meteoswiss_rain_radar.hail_downloader import (
    create_hail_downloader,
)
from custom_components.meteoswiss_rain_radar.radar_downloader import (
    create_radar_downloader,
)

from .hail_helpers import OBSERVATION
from .test_hail_downloader import TODAY_URL, item, stac_asset
from .test_hail_setup import make_entry
from .test_radar_downloader import NOW, rain_asset
from .test_radar_downloader import daily_url as rain_daily_url
from .test_radar_downloader import item as rain_item

MODULE = "custom_components.meteoswiss_rain_radar.http_client"


@pytest.mark.parametrize(
    "downloader_type", [create_radar_downloader, create_hail_downloader]
)
async def test_owned_lazy_client_ssl_is_off_loop_and_reused(downloader_type):
    downloader = downloader_type()
    loop_thread = threading.get_ident()
    ssl_threads = []
    load_certificates = ssl.SSLContext.load_verify_locations

    def context(*args, **kwargs):
        ssl_threads.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return load_certificates(*args, **kwargs)

    assert downloader._client is None
    try:
        with patch.object(ssl.SSLContext, "load_verify_locations", new=context):
            first, second = await asyncio.gather(
                downloader._get_client(), downloader._get_client()
            )
        assert first is second
        assert ssl_threads
        assert not first.is_closed
    finally:
        await downloader.close()
    assert first.is_closed
    assert downloader._client is None
    await downloader.close()
    with pytest.raises(RuntimeError, match="closed"):
        await downloader._get_client()


@pytest.mark.parametrize(
    "downloader_type", [create_radar_downloader, create_hail_downloader]
)
async def test_cancellation_during_owned_construction_closes_late_client(
    downloader_type,
):
    downloader = downloader_type()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    clients = []
    client_class = httpx.AsyncClient

    def create_client():
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        client = client_class()
        clients.append(client)
        return client

    try:
        with patch(f"{MODULE}.httpx.AsyncClient", new=create_client):
            task = asyncio.create_task(downloader._get_client())
            await entered.wait()
            task.cancel()
            close = asyncio.create_task(downloader.close())
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await close
        assert len(clients) == 1
        assert clients[0].is_closed
        assert downloader._client is None
    finally:
        release.set()
        await downloader.close()


async def test_borrowed_client_request_settings_do_not_mutate_defaults(httpx_mock):
    client = await asyncio.to_thread(
        httpx.AsyncClient,
        timeout=77,
        follow_redirects=True,
        headers={"X-Shared": "unchanged"},
    )
    rain = create_radar_downloader(client)
    hail = create_hail_downloader(client)
    published = rain_asset(content=b"rain")
    rain_url = rain_daily_url()
    httpx_mock.add_response(url=rain_url, json=rain_item(published))
    httpx_mock.add_response(url=published["href"], content=b"rain")
    httpx_mock.add_response(url=TODAY_URL, json=item(stac_asset()))
    for url in (TODAY_URL, rain_url):
        httpx_mock.add_response(
            url=url, status_code=302, headers={"Location": "https://example.com"}
        )
    defaults = dict(client.headers)
    try:
        discovery = await rain.discover(NOW)
        assert discovery.health == "ok"
        assert await rain.fetch(discovery.asset) == b"rain"
        assert (await hail.discover(OBSERVATION)).health == "ok"
        # An unfollowed redirect is a failure, not an absent radar file.
        for downloader, moment in ((hail, OBSERVATION), (rain, NOW)):
            with pytest.raises(httpx.HTTPStatusError):
                await downloader.discover(moment)
        for request in httpx_mock.get_requests():
            assert set(request.extensions["timeout"].values()) == {15}
            assert request.headers["X-Shared"] == "unchanged"
        assert client.timeout == httpx.Timeout(77)
        assert client.follow_redirects is True
        assert dict(client.headers) == defaults
        await rain.close()
        await hail.close()
        assert not client.is_closed
    finally:
        await rain.close()
        await hail.close()
        await client.aclose()


async def test_coordinators_borrow_ha_client_without_ssl_initialization(hass):
    def unexpected_ssl(*args, **kwargs):
        pytest.fail("Coordinators must reuse HA's initialized SSL context")

    with (
        patch("ssl.create_default_context", new=unexpected_ssl),
        patch.object(ssl.SSLContext, "load_verify_locations", new=unexpected_ssl),
    ):
        entry = make_entry(hass)
        rain = MeteoSwissRainRadarCoordinator(hass, entry)
        hail = MeteoSwissHailCoordinator(hass, entry)
        shared = get_async_client(hass)
        assert rain.downloader._client is hail.downloader._client is shared
        await hail.stop()
        await rain.stop()
        assert not shared.is_closed
