"""Canvas REST client that rides on a Playwright request context.

Because ``APIRequestContext`` shares cookie storage with the browser
context, every call here is authenticated by the logged-in session — no
token. We talk to the documented ``/api/v1`` JSON API and never scrape
HTML.

Handles the two things that silently break naive Canvas clients:

* Pagination via RFC 5988 ``Link`` headers (``per_page`` caps at 100).
* The cost-based leaky-bucket rate limiter: bounded concurrency, a short
  pause as the quota drops, and exponential backoff on a 403 throttle.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from playwright.async_api import APIRequestContext, APIResponse

from app.config import Settings, get_settings

_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
_MAX_RETRIES = 5


class CanvasAPIError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"Canvas API {status} for {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


def parse_next_link(link_header: str | None) -> str | None:
    """Return the ``rel="next"`` URL from a Canvas ``Link`` header, if any."""
    if not link_header:
        return None
    m = _NEXT_RE.search(link_header)
    return m.group(1) if m else None


class CanvasClient:
    def __init__(
        self,
        request: APIRequestContext,
        settings: Settings | None = None,
    ):
        self.settings = settings or get_settings()
        self._request = request
        self._base = self.settings.canvas_base_url
        self._sem = asyncio.Semaphore(self.settings.request_max_concurrency)

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base}/api/v1/{path.lstrip('/')}"

    async def _get(
        self, url: str, params: dict[str, Any] | None
    ) -> APIResponse:
        for attempt in range(_MAX_RETRIES):
            async with self._sem:
                resp = await self._request.get(url, params=params or {})
            if resp.status == 403 and "rate limit exceeded" in (
                await resp.text()
            ).lower():
                await asyncio.sleep(2**attempt)
                continue
            await self._respect_rate_limit(resp)
            return resp
        raise CanvasAPIError(429, "still rate limited after retries", url)

    async def _respect_rate_limit(self, resp: APIResponse) -> None:
        raw = resp.headers.get("x-rate-limit-remaining")
        if raw is None:
            return
        try:
            remaining = float(raw)
        except ValueError:
            return
        if remaining < self.settings.rate_limit_floor:
            await asyncio.sleep(1.0)

    async def get_json(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        url = self._url(path)
        resp = await self._get(url, params)
        if not resp.ok:
            raise CanvasAPIError(resp.status, await resp.text(), url)
        return await resp.json()

    async def get_paginated(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        merged = {"per_page": 100, **(params or {})}
        url: str | None = self._url(path)
        out: list[dict[str, Any]] = []
        first = True
        while url:
            resp = await self._get(url, merged if first else None)
            if not resp.ok:
                raise CanvasAPIError(resp.status, await resp.text(), url)
            payload = await resp.json()
            if isinstance(payload, list):
                out.extend(payload)
            elif isinstance(payload, dict):
                out.append(payload)
            url = parse_next_link(resp.headers.get("link"))
            first = False
        return out

    # -- convenience endpoints --------------------------------------------

    async def courses(self) -> list[dict[str, Any]]:
        return await self.get_paginated(
            "courses",
            {
                "enrollment_state": "active",
                "state[]": "available",
                "include[]": "term",
            },
        )

    async def assignments(self, course_id: int) -> list[dict[str, Any]]:
        return await self.get_paginated(
            f"courses/{course_id}/assignments", {"order_by": "due_at"}
        )

    async def all_assignments(
        self, course_ids: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        """Fetch assignments for many courses; concurrency bounded by the semaphore."""
        results = await asyncio.gather(
            *(self.assignments(cid) for cid in course_ids)
        )
        return dict(zip(course_ids, results))
