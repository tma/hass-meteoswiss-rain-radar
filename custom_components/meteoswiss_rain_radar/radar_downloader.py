from __future__ import annotations

import asyncio
from datetime import datetime
from io import BytesIO

import httpx

from .const import METEOSWISS_API_BASE_URL
from .http_client import async_create_client


class RadarDownloader:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._owns_client = client is None
        self._client_lock = asyncio.Lock()
        self._closed = False

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._closed:
                raise RuntimeError("Radar downloader is closed")
            if self._client is None:
                self._client = await async_create_client()
            return self._client

    async def close(self):
        async with self._client_lock:
            self._closed = True
            if self._client and self._owns_client:
                await self._client.aclose()
            self._client = None

    @staticmethod
    def build_filename(dt: datetime) -> str:
        year = dt.strftime("%y")
        day = dt.strftime("%j")
        time = dt.strftime("%H%M")

        return f"rzc{year}{day}{time}vl.001.h5"

    @staticmethod
    def build_url(
        timestamp: datetime,
    ) -> tuple[str, str]:
        folder = timestamp.strftime("%Y%m%d")
        filename = RadarDownloader.build_filename(timestamp)
        url = METEOSWISS_API_BASE_URL + folder + "-ch/" + filename
        return filename, url

    async def radar_exists(
        self,
        timestamp: datetime,
    ) -> bool:
        """404 means not published yet; other statuses are failures, not absence."""
        _, url = self.build_url(timestamp)
        client = await self._get_client()
        response = await client.head(
            url,
            headers={"Cache-Control": "no-cache"},
            timeout=30,
            follow_redirects=False,
        )
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        response.raise_for_status()
        raise httpx.HTTPStatusError(
            f"Unexpected rain radar status {response.status_code} for {url}",
            request=response.request,
            response=response,
        )

    async def fetch_radar(
        self,
        timestamp: datetime,
    ) -> BytesIO:
        _, url = self.build_url(timestamp)
        client = await self._get_client()
        response = await client.get(
            url,
            headers={"Cache-Control": "no-cache"},
            timeout=30,
            follow_redirects=False,
        )
        response.raise_for_status()
        return BytesIO(response.content)
