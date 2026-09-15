"""Rain product policy: near-real-time RZC grids, no archive search.

MeteoSwiss names these grids ``RZCyyjjjHHMMKK.XYZ.h5``. The two-character ``KK``
follows radar-site availability and has changed in service, so the published
name comes from the daily catalogue item instead of a built filename.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .stac_downloader import StacDownloader, StacProduct

RAIN_PRODUCT = StacProduct(
    collection="ch.meteoschweiz.ogd-radar-precip",
    prefix="rzc",
    grid=r"\d01",  # The reserved grid identifier; other digits are other grids.
    history_days=2,  # Today and the previous UTC day, for frames around midnight.
    cutoff_days=1,
    # A rain frame older than the age limit is never usable, so no archive walk.
    cold_listing=False,
)
COLLECTION_URL = RAIN_PRODUCT.collection_url
ASSET_PATH = RAIN_PRODUCT.asset_path


def create_radar_downloader(
    client: httpx.AsyncClient | None = None,
    *,
    async_add_executor_job: Callable[..., Awaitable[Any]] = asyncio.to_thread,
) -> StacDownloader:
    return StacDownloader(
        RAIN_PRODUCT, client, async_add_executor_job=async_add_executor_job
    )
