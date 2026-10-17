"""Сканер Polymarket: Gamma API (события/рынки) + CLOB API (проверка стакана)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import Settings
from database import from_iso, utcnow
from http_client import ApiError, HttpClient

log = logging.getLogger(__name__)

GAMMA_PAGE_SIZE = 100
CLOB_BOOKS_CHUNK = 50


@dataclass(frozen=True)
class Opportunity:
    market_id: str
    condition_id: str
    token_id: str
    outcome: str
    question: str
    event_title: str
    event_slug: str
    market_slug: str
    end_date: datetime
    best_ask: float
    avg_price: float          # средняя цена исполнения ордера на $order_size по стакану
    shares: float             # сколько акций купим на $order_size
    ask_liquidity_usdc: float  # объем Ask (в USDC) по цене <= max_price
    tick_size: str
    neg_risk: bool

    @property
    def payout(self) -> float:
        """Выплата при победе: 1 акция = $1."""
        return self.shares

    @property
    def multiplier(self) -> float:
        return 1.0 / self.avg_price if self.avg_price > 0 else 0.0


@dataclass(frozen=True)
class _Candidate:
    market: dict[str, Any]
    event: dict[str, Any]
    token_id: str
    outcome: str
    end_date: datetime


def parse_json_list(value: Any) -> list[Any]:
    """Gamma отдает outcomes / outcomePrices / clobTokenIds как JSON-строки."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        if len(value) == 10:  # YYYY-MM-DD
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        return from_iso(value)
    except ValueError:
        return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class MarketScanner:
    def __init__(self, http: HttpClient, settings: Settings) -> None:
        self.http = http
        self.s = settings

    # ------------------------------------------------------------------ Gamma
    async def fetch_events(self, now: datetime) -> list[dict[str, Any]]:
        start = now + timedelta(minutes=self.s.min_time_to_end_minutes)
        end = now + timedelta(hours=self.s.max_time_to_end_hours)
        events: list[dict[str, Any]] = []
        for page in range(self.s.max_event_pages):
            params = {
                "active": True,
                "closed": False,
                "archived": False,
                "end_date_min": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_date_max": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "order": "endDate",
                "ascending": True,
                "limit": GAMMA_PAGE_SIZE,
                "offset": page * GAMMA_PAGE_SIZE,
            }
            batch = await self.http.get(f"{self.s.gamma_host}/events", params=params)
            if not isinstance(batch, list):
                log.warning("Gamma /events вернул неожиданный ответ: %r", type(batch))
                break
            events.extend(batch)
            if len(batch) < GAMMA_PAGE_SIZE:
                break
        return events

    def extract_candidates(self, events: list[dict[str, Any]], now: datetime,
                           exclude_conditions: set[str]) -> list[_Candidate]:
        min_end = now + timedelta(minutes=self.s.min_time_to_end_minutes)
        max_end = now + timedelta(hours=self.s.max_time_to_end_hours)
        out: list[_Candidate] = []

        for event in events:
            if event.get("closed") or event.get("archived") or event.get("active") is False:
                continue
            for m in event.get("markets") or []:
                if (
                    m.get("closed")
                    or m.get("archived")
                    or m.get("active") is False
                    or m.get("acceptingOrders") is False      # торговля заморожена
                    or m.get("enableOrderBook") is False      # нет CLOB-стакана
                ):
                    continue
                condition_id = m.get("conditionId")
                if not condition_id or condition_id in exclude_conditions:
                    continue
                end_date = parse_dt(m.get("endDate")) or parse_dt(event.get("endDate"))
                if end_date is None or not (min_end <= end_date <= max_end):
                    continue

                outcomes = parse_json_list(m.get("outcomes"))
                prices = parse_json_list(m.get("outcomePrices"))
                tokens = parse_json_list(m.get("clobTokenIds"))
                if not tokens or len(tokens) != len(outcomes):
                    continue
                for i, token_id in enumerate(tokens):
                    # Предфильтр по цене Gamma; если цены нет — проверим стакан в любом случае
                    price = _to_float(prices[i], -1.0) if i < len(prices) else -1.0
                    if price > self.s.prefilter_price:
                        continue
                    out.append(_Candidate(m, event, str(token_id), str(outcomes[i]), end_date))
        return out

    # ------------------------------------------------------------------ CLOB
    async def fetch_books(self, token_ids: list[str]) -> dict[str, dict[str, Any]]:
        books: dict[str, dict[str, Any]] = {}
        for i in range(0, len(token_ids), CLOB_BOOKS_CHUNK):
            chunk = token_ids[i:i + CLOB_BOOKS_CHUNK]
            try:
                data = await self.http.post(
                    f"{self.s.clob_host}/books", json_body=[{"token_id": t} for t in chunk]
                )
                for book in data or []:
                    tid = str(book.get("asset_id") or book.get("token_id") or "")
                    if tid:
                        books[tid] = book
            except ApiError as exc:
                log.warning("POST /books не удался (%s), запрашиваю стаканы по одному", exc)
                for tid in chunk:
                    try:
                        book = await self.http.get(
                            f"{self.s.clob_host}/book", params={"token_id": tid}, allow_404=True
                        )
                    except ApiError as e:
                        log.warning("GET /book %s: %s", tid, e)
                        continue
                    if book:
                        books[tid] = book
        return books

    def analyze_book(self, book: dict[str, Any]) -> tuple[float, float, float, float] | None:
        """Возвращает (best_ask, avg_price, shares, liquidity_usdc) или None, если объема не хватает.

        Учитываются только Ask-уровни с ценой <= max_price. Ордер на $order_size должен
        полностью исполниться в этом диапазоне.
        """
        asks: list[tuple[float, float]] = []
        for lvl in book.get("asks") or []:
            p, sz = _to_float(lvl.get("price")), _to_float(lvl.get("size"))
            if 0 < p <= self.s.max_price + 1e-12 and sz > 0:
                asks.append((p, sz))
        if not asks:
            return None
        asks.sort(key=lambda x: x[0])

        liquidity = sum(p * sz for p, sz in asks)
        remaining = self.s.order_size_usdc
        shares = 0.0
        for p, sz in asks:
            take = min(remaining, p * sz)
            shares += take / p
            remaining -= take
            if remaining <= 1e-9:
                break
        if remaining > 1e-9:
            return None
        avg_price = self.s.order_size_usdc / shares
        return asks[0][0], avg_price, shares, liquidity

    # ------------------------------------------------------------------ main
    async def scan(self, exclude_conditions: set[str], limit: int) -> list[Opportunity]:
        now = utcnow()
        events = await self.fetch_events(now)
        candidates = self.extract_candidates(events, now, exclude_conditions)
        log.info("Gamma: событий %d, кандидатов-токенов (предфильтр) %d", len(events), len(candidates))
        if not candidates:
            return []

        books = await self.fetch_books(list({c.token_id for c in candidates}))
        opportunities: list[Opportunity] = []
        seen_conditions: set[str] = set()

        for c in candidates:
            condition_id = str(c.market["conditionId"])
            if condition_id in seen_conditions:
                continue
            book = books.get(c.token_id)
            if not book:
                continue
            analysis = self.analyze_book(book)
            if analysis is None:
                continue
            best_ask, avg_price, shares, liquidity = analysis
            seen_conditions.add(condition_id)
            opportunities.append(
                Opportunity(
                    market_id=str(c.market.get("id", "")),
                    condition_id=condition_id,
                    token_id=c.token_id,
                    outcome=c.outcome,
                    question=str(c.market.get("question") or ""),
                    event_title=str(c.event.get("title") or c.market.get("question") or ""),
                    event_slug=str(c.event.get("slug") or ""),
                    market_slug=str(c.market.get("slug") or ""),
                    end_date=c.end_date,
                    best_ask=best_ask,
                    avg_price=avg_price,
                    shares=shares,
                    ask_liquidity_usdc=liquidity,
                    tick_size=str(book.get("tick_size") or c.market.get("orderPriceMinTickSize") or "0.01"),
                    neg_risk=bool(book.get("neg_risk", c.market.get("negRisk", False))),
                )
            )

        # Сначала рынки, которые разрешатся быстрее всего — слоты освобождаются раньше
        opportunities.sort(key=lambda o: (o.end_date, -o.ask_liquidity_usdc))
        log.info("Найдено возможностей с ликвидностью: %d", len(opportunities))
        return opportunities[:limit]
