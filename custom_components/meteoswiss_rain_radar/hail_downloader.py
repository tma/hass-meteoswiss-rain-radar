"""Bounded asynchronous discovery of official five-minute POH assets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .http_client import async_create_client

COLLECTION_URL = (
    "https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-radar-hail"
)
ITEMS_URL = f"{COLLECTION_URL}/items?limit=100"
ASSET_PATH = "/ch.meteoschweiz.ogd-radar-hail/"
MAX_PAGES = 16
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_DISCOVERY_BYTES = 32 * 1024 * 1024
REQUEST_ATTEMPTS = 3
_FILENAME = re.compile(r"bzc(\d{2})(\d{3})(\d{2})(\d{2})[a-z0-9]{2}\.\d{3}\.h5", re.I)
_CHECKSUM = re.compile(r"1220([0-9a-f]{64})", re.I)


@dataclass(frozen=True, slots=True)
class HailAsset:
    url: str
    checksum: str
    observation: datetime

    @property
    def identity(self) -> tuple[str, str]:
        return self.url, self.checksum


@dataclass(frozen=True, slots=True)
class HailDiscovery:
    asset: HailAsset | None
    health: str = "missing"
    observation: datetime | None = None


def _official_url(url: str, path: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "data.geo.admin.ch"
        and parsed.path.startswith(path)
        and not parsed.fragment
    )


def _asset_timestamp(item_id: str, url: str) -> datetime | None:
    """Do not use STAC item datetime: it is not the observation timestamp."""
    if not re.fullmatch(r"\d{8}-ch", item_id):
        return None
    parsed = urlsplit(url)
    match = _FILENAME.fullmatch(parsed.path.rsplit("/", 1)[-1])
    if not match or not _official_url(url, f"{ASSET_PATH}{item_id}/"):
        return None
    year, day, hour, minute = map(int, match.groups())
    if hour >= 24 or minute >= 60 or minute % 5:
        return None  # Includes the 2400 and 3000 daily aggregates.
    try:
        date = datetime.strptime(item_id[:8], "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    if date.year % 100 != year or date.timetuple().tm_yday != day:
        return None
    return date.replace(hour=hour, minute=minute)


@dataclass(frozen=True, slots=True)
class _Metadata:
    candidates: tuple[HailAsset, ...]
    next_url: str | None
    size: int
    etag: str | None = None
    modified: str | None = None

    def validators(self) -> dict[str, str]:
        if self.etag:
            return {"If-None-Match": self.etag}
        if self.modified:
            return {"If-Modified-Since": self.modified}
        return {}


def _parse_metadata(
    content: bytes,
    url: str,
    item_id: str | None,
    etag: str | None,
    modified: str | None,
) -> _Metadata:
    """Decode, validate and scan STAC in a worker, never on the HA event loop."""
    page = json.loads(content)
    if not isinstance(page, dict):
        raise ValueError("Invalid hail STAC response")
    if item_id is not None:
        if page.get("type") != "Feature" or page.get("id") != item_id:
            raise ValueError("Invalid hail STAC daily item")
        items = [page]
        links = []
    else:
        if not isinstance(page.get("features"), list) or not isinstance(
            page.get("links"), list
        ):
            raise ValueError("Invalid hail STAC feature collection")
        items, links = page["features"], page["links"]
    candidates = []
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("assets"), dict)
        ):
            raise ValueError("Invalid hail STAC item")
        for asset in item["assets"].values():
            if not isinstance(asset, dict) or not isinstance(asset.get("href"), str):
                raise ValueError("Invalid hail STAC asset")
            href = asset["href"]
            observation = _asset_timestamp(item["id"], href)
            if observation is None:
                continue
            raw_checksum = asset.get("file:checksum")
            checksum = (
                _CHECKSUM.fullmatch(raw_checksum)
                if isinstance(raw_checksum, str)
                else None
            )
            # Missing/unsupported checksums fail only when time-eligible.
            candidates.append(
                HailAsset(href, checksum[1].lower() if checksum else "", observation)
            )
    next_links = []
    for link in links:
        if (
            not isinstance(link, dict)
            or not isinstance(link.get("rel"), str)
            or not isinstance(link.get("href"), str)
        ):
            raise ValueError("Invalid hail STAC link")
        if link["rel"] == "next":
            next_links.append(link["href"])
    if len(next_links) > 1:
        raise ValueError("Ambiguous hail STAC pagination")
    return _Metadata(
        tuple(candidates),
        urljoin(url, next_links[0]) if next_links else None,
        len(content),
        etag,
        modified,
    )


def _select_candidates(metadata: _Metadata, now: datetime) -> HailDiscovery:
    """Recheck time eligibility on every response, including a cached 304."""
    latest = None
    future = None
    cutoff = now - timedelta(days=14)
    for candidate in metadata.candidates:
        if candidate.observation > now:
            future = (
                min(future, candidate.observation) if future else candidate.observation
            )
        elif candidate.observation >= cutoff:
            if not candidate.checksum:
                raise ValueError("POH asset lacks a supported SHA256 checksum")
            if latest is None or (candidate.observation, candidate.url) > (
                latest.observation,
                latest.url,
            ):
                latest = candidate
    if latest:
        return HailDiscovery(latest, "ok", latest.observation)
    return HailDiscovery(None, "future" if future else "missing", future)


def _newer_result(first: HailDiscovery, second: HailDiscovery) -> HailDiscovery:
    if first.asset and second.asset:
        if (first.asset.observation, first.asset.url) > (
            second.asset.observation,
            second.asset.url,
        ):
            return first
        return second
    if first.asset or second.asset:
        return first if first.asset else second
    if first.observation and second.observation:
        return first if first.observation < second.observation else second
    return first if first.observation else second


class HailDownloader:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        async_add_executor_job: Callable[..., Awaitable[Any]] = asyncio.to_thread,
    ):
        self._client = client
        self._owns_client = client is None
        self._client_lock = asyncio.Lock()
        self._closed = False
        self._executor = async_add_executor_job
        self._items: dict[str, _Metadata] = {}
        self._historical_fallback_attempted = False
        self._cached_identity: tuple[str, str] | None = None
        self._cached_bytes: bytes | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._closed:
                raise RuntimeError("Hail downloader is closed")
            if self._client is None:
                self._client = await async_create_client()
            return self._client

    async def close(self):
        async with self._client_lock:
            self._closed = True
            if self._client and self._owns_client:
                await self._client.aclose()
            self._client = None
        self._items.clear()
        self._cached_bytes = None
        self._cached_identity = None

    async def _get(
        self, url: str, limit: int, headers: dict[str, str] | None = None
    ) -> tuple[bytes, httpx.Headers, int]:
        client = await self._get_client()
        for attempt in range(REQUEST_ATTEMPTS):
            try:
                async with client.stream(
                    "GET", url, headers=headers, timeout=15, follow_redirects=False
                ) as response:
                    if response.status_code == 304:
                        return b"", response.headers, 304
                    response.raise_for_status()
                    if int(response.headers.get("Content-Length", 0)) > limit:
                        raise ValueError("Hail response exceeds byte limit")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunks) + len(chunk) > limit:
                            raise ValueError("Hail response exceeds byte limit")
                        chunks.extend(chunk)
                    return bytes(chunks), response.headers, response.status_code
            except (httpx.TransportError, httpx.HTTPStatusError) as err:
                if isinstance(err, httpx.HTTPStatusError) and (
                    err.response.status_code not in (404, 408, 429)
                    and err.response.status_code < 500
                ):
                    raise
                if attempt == REQUEST_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(0.5 * 2**attempt)
        raise AssertionError("Unreachable retry state")

    async def _metadata(self, url: str, item_id: str | None = None) -> _Metadata:
        cached = self._items.get(url)
        content, headers, status = await self._get(
            url, MAX_PAGE_BYTES, cached.validators() if cached else None
        )
        if status == 304:
            if cached is None:
                raise ValueError("Unexpected hail STAC 304 without cache")
            return cached
        metadata = await self._executor(
            _parse_metadata,
            content,
            url,
            item_id,
            headers.get("ETag"),
            headers.get("Last-Modified"),
        )
        if item_id is not None:
            self._items[url] = metadata
        return metadata

    async def _daily(self, day: datetime) -> _Metadata | None:
        item_id = day.strftime("%Y%m%d-ch")
        url = f"{COLLECTION_URL}/items/{item_id}"
        try:
            return await self._metadata(url, item_id)
        except httpx.HTTPStatusError as err:
            if err.response.status_code != 404:
                raise
            # Only an exhausted daily-item 404 means absence, never cached weather.
            self._items.pop(url, None)
            return None

    async def _historical_listing(
        self, now: datetime, total_bytes: int
    ) -> HailDiscovery:
        """One bounded cold fallback; a partial listing cannot identify the latest."""
        self._historical_fallback_attempted = True
        url: str | None = ITEMS_URL
        visited: set[str] = set()
        result = HailDiscovery(None)
        while url:
            if (
                url in visited
                or len(visited) >= MAX_PAGES
                or not _official_url(url, urlsplit(COLLECTION_URL).path + "/items")
            ):
                raise ValueError("Invalid or excessive hail STAC pagination")
            visited.add(url)
            metadata = await self._metadata(url)
            total_bytes += metadata.size
            if total_bytes > MAX_DISCOVERY_BYTES:
                raise ValueError("Hail STAC listing exceeds byte limit")
            selected = await self._executor(_select_candidates, metadata, now)
            result = _newer_result(result, selected)
            url = metadata.next_url
        return result

    async def discover(self, now: datetime) -> HailDiscovery:
        """Poll today's complete item; consult older days only without a past asset."""
        now = now.astimezone(UTC)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        days = [today - timedelta(days=offset) for offset in range(15)]
        retained_urls = {f"{COLLECTION_URL}/items/{day:%Y%m%d}-ch" for day in days}
        self._items = {
            url: metadata
            for url, metadata in self._items.items()
            if url in retained_urls
        }
        async with asyncio.timeout(45):
            result = HailDiscovery(None)
            total_bytes = 0
            for offset, day in enumerate(days):
                if offset == 2 and not self._historical_fallback_attempted:
                    return _newer_result(
                        result, await self._historical_listing(now, total_bytes)
                    )
                metadata = await self._daily(day)
                if metadata is None:
                    continue
                total_bytes += metadata.size
                if total_bytes > MAX_DISCOVERY_BYTES:
                    raise ValueError("Hail STAC discovery exceeds byte limit")
                selected = await self._executor(_select_candidates, metadata, now)
                result = _newer_result(result, selected)
                if result.asset:
                    return result
            return result

    async def fetch(self, asset: HailAsset) -> bytes:
        if asset.identity == self._cached_identity and self._cached_bytes is not None:
            return self._cached_bytes
        if not _official_url(asset.url, ASSET_PATH):
            raise ValueError("Unofficial hail asset URL")
        async with asyncio.timeout(45):
            content, _, status = await self._get(asset.url, MAX_FILE_BYTES)
        if status == 304:
            raise ValueError("Unexpected hail asset 304 without matching cache")
        if hashlib.sha256(content).hexdigest() != asset.checksum:
            raise ValueError("Hail asset SHA256 mismatch")
        self._cached_identity, self._cached_bytes = asset.identity, content
        return content
