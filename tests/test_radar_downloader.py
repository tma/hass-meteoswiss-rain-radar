"""Tests for RadarDownloader.

Requires: pip install pytest pytest-asyncio pytest-httpx

"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

import httpx
import pytest

from custom_components.meteoswiss_rain_radar.const import METEOSWISS_API_BASE_URL
from custom_components.meteoswiss_rain_radar.radar_downloader import RadarDownloader

# A fixed timestamp used across tests so the generated filename/url is deterministic.
TEST_DT = datetime(2024, 3, 5, 14, 30, tzinfo=UTC)  # 2024, day-of-year 065 (%j), 14:30
EXPECTED_FILENAME = "rzc240651430vl.001.h5"
EXPECTED_URL = f"{METEOSWISS_API_BASE_URL}20240305-ch/{EXPECTED_FILENAME}"


@pytest.fixture
async def downloader():
    """Provide a RadarDownloader instance and clean up its client afterwards."""
    dl = RadarDownloader()
    yield dl
    await dl.close()


# ---------------------------------------------------------------------------
# Pure helper methods - no HTTP involved
# ---------------------------------------------------------------------------


def test_build_filename():
    filename = RadarDownloader.build_filename(TEST_DT)
    assert filename == EXPECTED_FILENAME


def test_build_url():
    filename, url = RadarDownloader.build_url(TEST_DT)
    assert filename == EXPECTED_FILENAME
    assert url == EXPECTED_URL


# ---------------------------------------------------------------------------
# radar_exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_radar_exists_true(downloader, httpx_mock):
    httpx_mock.add_response(
        method="HEAD",
        url=EXPECTED_URL,
        status_code=200,
    )

    exists = await downloader.radar_exists(TEST_DT)

    assert exists is True
    request = httpx_mock.get_requests()[0]
    assert request.headers["Cache-Control"] == "no-cache"


@pytest.mark.asyncio
async def test_radar_exists_false(downloader, httpx_mock):
    httpx_mock.add_response(
        method="HEAD",
        url=EXPECTED_URL,
        status_code=404,
    )

    exists = await downloader.radar_exists(TEST_DT)

    assert exists is False


# ---------------------------------------------------------------------------
# fetch_radar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_radar_success(downloader, httpx_mock):
    fake_content = b"fake-h5-binary-data"
    httpx_mock.add_response(
        method="GET",
        url=EXPECTED_URL,
        status_code=200,
        content=fake_content,
    )

    result = await downloader.fetch_radar(TEST_DT)

    assert isinstance(result, BytesIO)
    assert result.read() == fake_content
    request = httpx_mock.get_requests()[0]
    assert request.headers["Cache-Control"] == "no-cache"


@pytest.mark.asyncio
async def test_fetch_radar_raises_on_http_error(downloader, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=EXPECTED_URL,
        status_code=500,
    )

    with pytest.raises(httpx.HTTPStatusError):
        await downloader.fetch_radar(TEST_DT)


# ---------------------------------------------------------------------------
# radar_exists: absence is only 404, every other status is a failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500, 503])
async def test_radar_exists_raises_on_other_statuses(downloader, httpx_mock, status):
    httpx_mock.add_response(method="HEAD", url=EXPECTED_URL, status_code=status)

    with pytest.raises(httpx.HTTPStatusError):
        await downloader.radar_exists(TEST_DT)


@pytest.mark.asyncio
async def test_radar_exists_raises_on_unexpected_success_status(downloader, httpx_mock):
    httpx_mock.add_response(method="HEAD", url=EXPECTED_URL, status_code=204)

    with pytest.raises(httpx.HTTPStatusError):
        await downloader.radar_exists(TEST_DT)


@pytest.mark.asyncio
async def test_radar_exists_propagates_transport_errors(downloader, httpx_mock):
    httpx_mock.add_exception(httpx.ReadTimeout("timeout"), method="HEAD")

    with pytest.raises(httpx.ReadTimeout):
        await downloader.radar_exists(TEST_DT)
