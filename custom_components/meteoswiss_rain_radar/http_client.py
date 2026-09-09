"""Off-loop client construction for standalone downloaders only."""

import asyncio

import httpx


async def async_create_client() -> httpx.AsyncClient:
    """Finish SSL initialization and close the owned client if its caller cancels."""
    task = asyncio.create_task(asyncio.to_thread(httpx.AsyncClient))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        client = await task
        await client.aclose()
        raise
