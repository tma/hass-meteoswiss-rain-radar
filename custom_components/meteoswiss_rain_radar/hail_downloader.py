"""Hail product policy: POH grids with the bounded 14-day archive fallback."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .stac_downloader import StacDownloader, StacProduct

HAIL_PRODUCT = StacProduct(
    collection="ch.meteoschweiz.ogd-radar-hail",
    prefix="bzc",
    grid=r"\d{3}",
    history_days=15,
    cutoff_days=14,
    # Hail reports the latest archived grid, so one cold listing is worthwhile.
    cold_listing=True,
)
COLLECTION_URL = HAIL_PRODUCT.collection_url
ITEMS_URL = HAIL_PRODUCT.items_url
ASSET_PATH = HAIL_PRODUCT.asset_path


def create_hail_downloader(
    client: httpx.AsyncClient | None = None,
    *,
    async_add_executor_job: Callable[..., Awaitable[Any]] = asyncio.to_thread,
) -> StacDownloader:
    return StacDownloader(
        HAIL_PRODUCT, client, async_add_executor_job=async_add_executor_job
    )
