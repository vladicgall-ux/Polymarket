"""Общий асинхронный HTTP-клиент: ретраи, экспоненциальный backoff, rate-limit (token bucket)."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger(__name__)

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimiter:
    """Token bucket: не более `rate` запросов в секунду с burst до `burst`."""

    def __init__(self, rate: float, burst: int | None = None) -> None:
        self.rate = max(rate, 0.1)
        self.capacity = float(burst or max(1, int(rate)))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)

    def penalize(self, seconds: float) -> None:
        """После 429 «обнуляем» бакет, чтобы все запросы к хосту подождали."""
        self.tokens = -seconds * self.rate


def _clean_params(params: dict[str, Any] | None) -> dict[str, str] | None:
    if not params:
        return None
    out: dict[str, str] = {}
    for k, v in params.items():
        if v is None:
            continue
        out[k] = ("true" if v else "false") if isinstance(v, bool) else str(v)
    return out


class HttpClient:
    def __init__(
        self,
        timeout: float = 20.0,
        max_retries: int = 5,
        host_rates: dict[str, float] | None = None,
        default_rate: float = 5.0,
    ) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_retries = max_retries
        self._default_rate = default_rate
        self._limiters: dict[str, RateLimiter] = {
            host: RateLimiter(rate) for host, rate in (host_rates or {}).items()
        }
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"User-Agent": "polymarket-underdog-bot/1.0", "Accept": "application/json"},
                connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300),
                trust_env=True,
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _limiter(self, url: str) -> RateLimiter:
        host = urlsplit(url).netloc
        if host not in self._limiters:
            self._limiters[host] = RateLimiter(self._default_rate)
        return self._limiters[host]

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        allow_404: bool = False,
    ) -> Any:
        await self.start()
        assert self._session is not None
        limiter = self._limiter(url)
        last_error: str = ""

        for attempt in range(self._max_retries + 1):
            await limiter.acquire()
            delay = min(2 ** attempt, 30) + random.uniform(0, 0.5)
            try:
                async with self._session.request(
                    method, url, params=_clean_params(params), json=json_body
                ) as resp:
                    if resp.status == 404 and allow_404:
                        return None
                    if resp.status in RETRY_STATUSES:
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            try:
                                delay = max(delay, float(retry_after))
                            except ValueError:
                                pass
                        if resp.status == 429:
                            limiter.penalize(delay)
                        last_error = f"HTTP {resp.status}"
                        log.warning("%s %s -> %s, retry %d/%d через %.1fs",
                                    method, url, resp.status, attempt + 1, self._max_retries, delay)
                    elif resp.status >= 400:
                        text = (await resp.text())[:300]
                        raise ApiError(f"{method} {url} -> HTTP {resp.status}: {text}", resp.status)
                    else:
                        return await resp.json(content_type=None)
            except ApiError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("%s %s ошибка сети (%s), retry %d/%d через %.1fs",
                            method, url, last_error, attempt + 1, self._max_retries, delay)

            if attempt < self._max_retries:
                await asyncio.sleep(delay)

        raise ApiError(f"{method} {url}: исчерпаны попытки ({last_error})")

    async def get(self, url: str, **kwargs: Any) -> Any:
        return await self.request_json("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        return await self.request_json("POST", url, **kwargs)
