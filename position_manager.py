"""Учет сделок, жесткий лимит активных позиций и автоматическая проверка резолюций."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from clob_trader import ClobTrader, TradeResult
from config import Settings
from database import (
    STATUS_CANCELLED,
    STATUS_LOSE,
    STATUS_OPEN,
    STATUS_WIN,
    Database,
    Position,
)
from http_client import ApiError, HttpClient
from scanner import Opportunity, parse_json_list

log = logging.getLogger(__name__)


@dataclass
class ClosedEvent:
    position: Position
    status: str
    payout: float
    reason: str


class PositionManager:
    def __init__(self, db: Database, http: HttpClient, trader: ClobTrader, settings: Settings) -> None:
        self.db = db
        self.http = http
        self.trader = trader
        self.s = settings
        # Проверка лимита + вставка должны быть атомарны относительно друг друга
        self._slot_lock = asyncio.Lock()

    @property
    def mode(self) -> str:
        return "auto" if self.trader.enabled else "signals"

    async def active_count(self) -> int:
        return await self.db.count_open()

    async def free_slots(self) -> int:
        return max(0, self.s.max_open_positions - await self.active_count())

    # ------------------------------------------------------------------ slots
    async def reserve(self, opp: Opportunity) -> Position | None:
        """Занимает слот под сделку. None — если лимит исчерпан или токен уже в работе."""
        async with self._slot_lock:
            if await self.db.count_open() >= self.s.max_open_positions:
                return None
            try:
                pid = await self.db.insert_position(
                    market_id=opp.market_id,
                    token_id=opp.token_id,
                    condition_id=opp.condition_id,
                    outcome=opp.outcome,
                    entry_price=round(opp.avg_price, 6),
                    end_date=opp.end_date,
                    event_title=opp.event_title,
                    question=opp.question,
                    event_slug=opp.event_slug,
                    market_slug=opp.market_slug,
                    # В режиме сигналов фиксируем «виртуальную» покупку на $1 для статистики
                    shares=round(opp.shares, 4),
                    cost_usdc=self.s.order_size_usdc,
                    mode=self.mode,
                )
            except Exception as exc:  # noqa: BLE001 (в т.ч. UNIQUE на token_id)
                log.warning("Не удалось зарезервировать слот под %s: %s", opp.token_id, exc)
                return None
        return await self.db.get_position(pid)

    async def apply_trade(self, position: Position, result: TradeResult) -> Position | None:
        """Обновляет позицию по результату авто-ордера; неуспешный ордер освобождает слот."""
        if not result.success:
            await self.db.update_position(position.id, order_id=result.order_id, shares=0, cost_usdc=0)
            await self.db.close_position(position.id, STATUS_CANCELLED, 0.0,
                                         note=f"order failed: {result.error}")
        elif result.filled:
            await self.db.update_position(
                position.id,
                order_id=result.order_id,
                shares=round(result.shares, 4),
                cost_usdc=round(result.cost_usdc, 6),
                entry_price=round(result.avg_price, 6),
            )
        else:
            # GTC-ордер стоит в стакане: слот занят, объем уточнит фоновый чекер
            await self.db.update_position(position.id, order_id=result.order_id, shares=0, cost_usdc=0,
                                          note=f"order {result.status}")
        return await self.db.get_position(position.id)

    async def cancel_position(self, position_id: int, reason: str) -> Position | None:
        pos = await self.db.get_position(position_id)
        if pos is None or pos.status != STATUS_OPEN:
            return None
        if pos.mode == "auto" and pos.order_id and pos.shares > 0:
            # Уже купленные акции не «отменить» — закрывать вручную нельзя, ждем резолюцию
            return None
        if pos.mode == "auto" and pos.order_id:
            await self.trader.cancel(pos.order_id)
        if await self.db.close_position(position_id, STATUS_CANCELLED, 0.0, note=reason):
            await self.db.update_position(position_id, cost_usdc=0, shares=0)
        return await self.db.get_position(position_id)

    # ------------------------------------------------------------------ resolution
    async def _resolution_from_clob(self, pos: Position) -> float | None:
        data = await self.http.get(f"{self.s.clob_host}/markets/{pos.condition_id}", allow_404=True)
        if not isinstance(data, dict) or not data.get("closed"):
            return None
        tokens = data.get("tokens") or []
        if not any(t.get("winner") for t in tokens):
            return None  # закрыт, но победитель еще не объявлен (UMA / спор)
        for t in tokens:
            if str(t.get("token_id")) == pos.token_id:
                return 1.0 if t.get("winner") else 0.0
        return None

    async def _resolution_from_gamma(self, pos: Position) -> float | None:
        data = await self.http.get(
            f"{self.s.gamma_host}/markets", params={"condition_ids": pos.condition_id}
        )
        markets: list[dict[str, Any]] = data if isinstance(data, list) else []
        for m in markets:
            if m.get("conditionId") != pos.condition_id:
                continue
            tokens = [str(t) for t in parse_json_list(m.get("clobTokenIds"))]
            prices = parse_json_list(m.get("outcomePrices"))
            if pos.token_id not in tokens or len(prices) != len(tokens):
                return None
            try:
                values = [float(p) for p in prices]
            except (TypeError, ValueError):
                return None
            resolved = str(m.get("umaResolutionStatus") or "").lower() == "resolved"
            final_prices = m.get("closed") and all(v in (0.0, 0.5, 1.0) for v in values)
            if resolved or final_prices:
                return values[tokens.index(pos.token_id)]
        return None

    async def _fetch_final_price(self, pos: Position) -> float | None:
        try:
            price = await self._resolution_from_clob(pos)
            if price is not None:
                return price
        except ApiError as exc:
            log.warning("CLOB резолюция %s: %s", pos.condition_id, exc)
        try:
            return await self._resolution_from_gamma(pos)
        except ApiError as exc:
            log.warning("Gamma резолюция %s: %s", pos.condition_id, exc)
        return None

    async def _sync_order(self, pos: Position) -> Position:
        """Для авто-позиций с незаполненным GTC-ордером уточняем исполнение."""
        if pos.mode != "auto" or not pos.order_id or pos.shares > 0:
            return pos
        order = await self.trader.get_order(pos.order_id)
        if not order:
            return pos
        matched = float(order.get("size_matched") or 0)
        price = float(order.get("price") or pos.entry_price)
        status = str(order.get("status") or "").upper()
        if matched > 0:
            await self.db.update_position(pos.id, shares=matched, cost_usdc=round(matched * price, 6),
                                          entry_price=price)
        elif status in ("CANCELED", "CANCELLED", "INVALID", "UNMATCHED"):
            await self.db.close_position(pos.id, STATUS_CANCELLED, 0.0, note=f"order {status}")
        return await self.db.get_position(pos.id) or pos

    async def check_resolutions(self) -> list[ClosedEvent]:
        closed: list[ClosedEvent] = []
        for pos in await self.db.get_open_positions():
            try:
                pos = await self._sync_order(pos)
                if pos.status != STATUS_OPEN:
                    closed.append(ClosedEvent(pos, pos.status, 0.0, "ордер отменен"))
                    continue
                final_price = await self._fetch_final_price(pos)
                if final_price is None:
                    continue

                if pos.mode == "auto" and pos.shares <= 0:
                    # Ордер так и не исполнился до резолюции
                    if pos.order_id:
                        await self.trader.cancel(pos.order_id)
                    status, payout, reason = STATUS_CANCELLED, 0.0, "рынок разрешен, ордер не исполнен"
                else:
                    payout = round(pos.shares * final_price, 6)
                    status = STATUS_WIN if payout > pos.cost_usdc else STATUS_LOSE
                    reason = f"финальная цена исхода {final_price:g}"

                if await self.db.close_position(pos.id, status, payout, note=reason):
                    updated = await self.db.get_position(pos.id) or pos
                    closed.append(ClosedEvent(updated, status, payout, reason))
                    log.info("Позиция #%d закрыта: %s (payout %.2f)", pos.id, status, payout)
            except Exception:  # noqa: BLE001 — одна битая позиция не должна ломать цикл
                log.exception("Ошибка проверки позиции #%d", pos.id)
        return closed
