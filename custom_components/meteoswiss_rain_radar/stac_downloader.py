"""Bounded STAC discovery and download shared by the official radar products.

Filenames are never guessed. MeteoSwiss names five-minute grids
``PPPyyjjjHHMMKK.XYZ.h5``, where ``KK`` follows radar-site availability and has
changed in service (``nl``, ``vl``, ``ul``). Only the catalogue knows the
published name, so every request uses a listed asset href.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from .http_client import async_create_client

STAC_URL = "https://data.geo.admin.ch/api/stac/v1/collections/"
DATA_HOST = "data.geo.admin.ch"
MAX_PAGES = 16
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_DISCOVERY_BYTES = 32 * 1024 * 1024
REQUEST_ATTEMPTS = 3
DISCOVERY_SECONDS = 45
REQUEST_SECONDS = 15
_CHECKSUM = re.compile(r"1220([0-9a-f]{64})", re.I)


@dataclass(frozen=True, slots=True)
class StacProduct:
    """One collection, its file naming and how far back it is worth looking."""

    collection: str
    prefix: str  # Product code, "bzc" for POH or "rzc" for rain rate.
    grid: str  # Reserved grid identifier pattern in PPPyyjjjHHMMKK.XYZ.h5.
    history_days: int  # Daily items consulted per discovery, newest first.
    cutoff_days: int  # Oldest observation this product can still use.
    cold_listing: bool  # One bounded archive listing when no daily item helps.
    filename: re.Pattern[str] = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        pattern = (
            rf"{self.prefix}(\d{{2}})(\d{{3}})(\d{{2}})(\d{{2}})"
            rf"[a-z0-9]{{2}}\.{self.grid}\.h5"
        )
        object.__setattr__(self, "filename", re.compile(pattern, re.I))

    @property
    def collection_url(self) -> str:
        return f"{STAC_URL}{self.collection}"

    @property
    def items_url(self) -> str:
        return f"{self.collection_url}/items?limit=100"

    @property
    def asset_path(self) -> str:
        return f"/{self.collection}/"


@dataclass(frozen=True, slots=True)
class StacAsset:
    url: str
    checksum: str
    observation: datetime

    @property
    def identity(self) -> tuple[str, str]:
        return self.url, self.checksum


@dataclass(frozen=True, slots=True)
class Discovery:
    asset: StacAsset | None
    health: str = "missing"
    observation: datetime | None = None


def _official_url(url: str, path: str, *, allow_query: bool = False) -> bool:
    parsed = urlsplit(url)
    decoded_path = unquote(parsed.path)
    normalized_path = posixpath.normpath(decoded_path)
    path_matches = (
        decoded_path.startswith(path)
        if path.endswith("/")
        else decoded_path == path or decoded_path.startswith(f"{path}/")
    )
    return (
        parsed.scheme == "https"
        and parsed.netloc == DATA_HOST
        and (allow_query or not parsed.query)
        and not parsed.fragment
        and "\\" not in decoded_path
        and decoded_path == parsed.path == normalized_path
        and path_matches
    )


def _asset_timestamp(product: StacProduct, item_id: str, url: str) -> datetime | None:
    """Do not use STAC item datetime: it is not the observation timestamp."""
    if not re.fullmatch(r"\d{8}-ch", item_id):
        return None
    parsed = urlsplit(url)
    match = product.filename.fullmatch(parsed.path.rsplit("/", 1)[-1])
    if not match or not _official_url(url, f"{product.asset_path}{item_id}/"):
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
    candidates: tuple[StacAsset, ...]
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
    product: StacProduct,
    content: bytes,
    url: str,
    item_id: str | None,
    etag: str | None,
    modified: str | None,
) -> _Metadata:
    """Decode, validate and scan STAC in a worker, never on the HA event loop."""
    page = json.loads(content)
    if not isinstance(page, dict):
        raise ValueError(f"Invalid {product.prefix} STAC response")
    if item_id is not None:
        if page.get("type") != "Feature" or page.get("id") != item_id:
            raise ValueError(f"Invalid {product.prefix} STAC daily item")
        items = [page]
        links = []
    else:
        if not isinstance(page.get("features"), list) or not isinstance(
            page.get("links"), list
        ):
            raise ValueError(f"Invalid {product.prefix} STAC feature collection")
        items, links = page["features"], page["links"]
    candidates = []
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("assets"), dict)
        ):
            raise ValueError(f"Invalid {product.prefix} STAC item")
        for asset in item["assets"].values():
            if not isinstance(asset, dict) or not isinstance(asset.get("href"), str):
                raise ValueError(f"Invalid {product.prefix} STAC asset")
            href = asset["href"]
            # Other products, aggregates and foreign hrefs are skipped, not fatal.
            observation = _asset_timestamp(product, item["id"], href)
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
                StacAsset(href, checksum[1].lower() if checksum else "", observation)
            )
    next_links = []
    for link in links:
        if (
            not isinstance(link, dict)
            or not isinstance(link.get("rel"), str)
            or not isinstance(link.get("href"), str)
        ):
            raise ValueError(f"Invalid {product.prefix} STAC link")
        if link["rel"] == "next":
            next_links.append(link["href"])
    if len(next_links) > 1:
        raise ValueError(f"Ambiguous {product.prefix} STAC pagination")
    return _Metadata(
        tuple(candidates),
        urljoin(url, next_links[0]) if next_links else None,
        len(content),
        etag,
        modified,
    )


def _select_candidates(
    metadata: _Metadata, now: datetime, cutoff_days: int = 14
) -> Discovery:
    """Recheck time eligibility on every response, including a cached 304."""
    latest = None
    future = None
    cutoff = now - timedelta(days=cutoff_days)
    for candidate in metadata.candidates:
        if candidate.observation > now:
            future = (
                min(future, candidate.observation) if future else candidate.observation
            )
        elif candidate.observation >= cutoff:
            if not candidate.checksum:
                raise ValueError("Radar asset lacks a supported SHA256 checksum")
            # Same-time duplicates are ordered by href only, to stay deterministic
            # without claiming that one site suffix is better than another.
            if latest is None or (candidate.observation, candidate.url) > (
                latest.observation,
                latest.url,
            ):
                latest = candidate
    if latest:
        return Discovery(latest, "ok", latest.observation)
    return Discovery(None, "future" if future else "missing", future)


def _newer_result(first: Discovery, second: Discovery) -> Discovery:
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


class StacDownloader:
    """Discovery and download for one product; geometry stays with its reader."""

    def __init__(
        self,
        product: StacProduct,
        client: httpx.AsyncClient | None = None,
        *,
        async_add_executor_job: Callable[..., Awaitable[Any]] = asyncio.to_thread,
    ):
        self.product = product
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
                raise RuntimeError(f"{self.product.prefix} downloader is closed")
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
            if self._closed:
                raise asyncio.CancelledError
            try:
                async with client.stream(
                    "GET",
                    url,
                    headers=headers,
                    timeout=REQUEST_SECONDS,
                    follow_redirects=False,
                ) as response:
                    if response.status_code == 304:
                        return b"", response.headers, 304
                    response.raise_for_status()
                    if int(response.headers.get("Content-Length", 0)) > limit:
                        raise ValueError("Radar response exceeds byte limit")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunks) + len(chunk) > limit:
                            raise ValueError("Radar response exceeds byte limit")
                        chunks.extend(chunk)
                    return bytes(chunks), response.headers, response.status_code
            except (httpx.TransportError, httpx.HTTPStatusError) as err:
                if self._closed:
                    raise asyncio.CancelledError from err
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
                raise ValueError("Unexpected STAC 304 without cache")
            return cached
        metadata = await self._executor(
            _parse_metadata,
            self.product,
            content,
            url,
            item_id,
            headers.get("ETag"),
            headers.get("Last-Modified"),
        )
        if item_id is not None and not self._closed:
            self._items[url] = metadata
        return metadata

    async def _daily(self, day: datetime) -> _Metadata | None:
        item_id = day.strftime("%Y%m%d-ch")
        url = f"{self.product.collection_url}/items/{item_id}"
        try:
            return await self._metadata(url, item_id)
        except httpx.HTTPStatusError as err:
            if err.response.status_code != 404:
                raise
            # Only an exhausted daily-item 404 means absence, never cached weather.
            self._items.pop(url, None)
            return None

    async def _historical_listing(self, now: datetime, total_bytes: int) -> Discovery:
        """One bounded cold fallback; a partial listing cannot identify the latest."""
        self._historical_fallback_attempted = True
        collection_path = urlsplit(self.product.collection_url).path
        url: str | None = self.product.items_url
        visited: set[str] = set()
        result = Discovery(None)
        while url:
            if (
                url in visited
                or len(visited) >= MAX_PAGES
                or not _official_url(url, f"{collection_path}/items", allow_query=True)
            ):
                raise ValueError("Invalid or excessive STAC pagination")
            visited.add(url)
            metadata = await self._metadata(url)
            total_bytes += metadata.size
            if total_bytes > MAX_DISCOVERY_BYTES:
                raise ValueError("STAC listing exceeds byte limit")
            selected = await self._executor(
                _select_candidates, metadata, now, self.product.cutoff_days
            )
            result = _newer_result(result, selected)
            url = metadata.next_url
        return result

    async def discover(self, now: datetime) -> Discovery:
        """Poll today's complete item; consult older days only without a past asset."""
        now = now.astimezone(UTC)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        history = range(self.product.history_days)
        days = [today - timedelta(days=offset) for offset in history]
        retained_urls = {
            f"{self.product.collection_url}/items/{day:%Y%m%d}-ch" for day in days
        }
        self._items = {
            url: metadata
            for url, metadata in self._items.items()
            if url in retained_urls
        }
        async with asyncio.timeout(DISCOVERY_SECONDS):
            result = Discovery(None)
            total_bytes = 0
            for offset, day in enumerate(days):
                if (
                    offset == 2
                    and self.product.cold_listing
                    and not self._historical_fallback_attempted
                ):
                    return _newer_result(
                        result, await self._historical_listing(now, total_bytes)
                    )
                metadata = await self._daily(day)
                if metadata is None:
                    continue
                total_bytes += metadata.size
                if total_bytes > MAX_DISCOVERY_BYTES:
                    raise ValueError("STAC discovery exceeds byte limit")
                selected = await self._executor(
                    _select_candidates, metadata, now, self.product.cutoff_days
                )
                result = _newer_result(result, selected)
                if result.asset:
                    return result
            return result

    async def fetch(self, asset: StacAsset, *, force: bool = False) -> bytes:
        if (
            not force
            and asset.identity == self._cached_identity
            and self._cached_bytes is not None
        ):
            return self._cached_bytes
        if not _official_url(asset.url, self.product.asset_path):
            raise ValueError("Unofficial radar asset URL")
        async with asyncio.timeout(DISCOVERY_SECONDS):
            content, _, status = await self._get(asset.url, MAX_FILE_BYTES)
        if status == 304:
            raise ValueError("Unexpected radar asset 304 without matching cache")
        if hashlib.sha256(content).hexdigest() != asset.checksum:
            raise ValueError("Radar asset SHA256 mismatch")
        if not self._closed:
            self._cached_identity, self._cached_bytes = asset.identity, content
        return content
