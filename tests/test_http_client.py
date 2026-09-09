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
from custom_components.meteoswiss_rain_radar.hail_downloader import HailDownloader
from custom_components.meteoswiss_rain_radar.radar_downloader import RadarDownloader

from .hail_helpers import OBSERVATION
from .test_hail_downloader import TODAY_URL, item, stac_asset
from .test_hail_setup import make_entry

MODULE = "custom_components.meteoswiss_rain_radar.http_client"


@pytest.mark.parametrize("downloader_type", [RadarDownloader, HailDownloader])
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


@pytest.mark.parametrize("downloader_type", [RadarDownloader, HailDownloader])
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
    rain, hail = RadarDownloader(client), HailDownloader(client)
    _, rain_url = rain.build_url(OBSERVATION)
    httpx_mock.add_response(url=rain_url, method="HEAD")
    httpx_mock.add_response(url=rain_url, method="GET", content=b"rain")
    httpx_mock.add_response(url=TODAY_URL, json=item(stac_asset()))
    httpx_mock.add_response(
        url=TODAY_URL, status_code=302, headers={"Location": "https://example.com"}
    )
    httpx_mock.add_response(
        url=rain_url,
        method="HEAD",
        status_code=302,
        headers={"Location": "https://example.com"},
    )
    httpx_mock.add_response(
        url=rain_url,
        method="GET",
        status_code=302,
        headers={"Location": "https://example.com"},
    )
    defaults = dict(client.headers)
    try:
        assert await rain.radar_exists(OBSERVATION)
        assert (await rain.fetch_radar(OBSERVATION)).read() == b"rain"
        assert (await hail.discover(OBSERVATION)).health == "ok"
        with pytest.raises(httpx.HTTPStatusError):
            await hail.discover(OBSERVATION)
        assert not await rain.radar_exists(OBSERVATION)
        with pytest.raises(httpx.HTTPStatusError):
            await rain.fetch_radar(OBSERVATION)
        for request in httpx_mock.get_requests():
            timeout = 15 if str(request.url) == TODAY_URL else 30
            assert set(request.extensions["timeout"].values()) == {timeout}
            assert request.headers["X-Shared"] == "unchanged"
            if timeout == 30:
                assert request.headers["Cache-Control"] == "no-cache"
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
