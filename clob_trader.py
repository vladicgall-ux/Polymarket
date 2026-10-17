"""Работа с ордерами через py-clob-client и генерация deeplink-ссылок (Phantom / Polymarket)."""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from config import Settings
from scanner import Opportunity

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- links
def polymarket_url(settings: Settings, event_slug: str, market_slug: str) -> str:
    base = settings.polymarket_web.rstrip("/")
    if event_slug and market_slug and event_slug != market_slug:
        return f"{base}/event/{event_slug}/{market_slug}"
    if event_slug or market_slug:
        return f"{base}/event/{event_slug or market_slug}"
    return base


def phantom_browse_url(target_url: str, ref: str) -> str:
    """Universal link Phantom: открывает target_url во встроенном браузере приложения."""
    return f"https://phantom.app/ul/browse/{quote(target_url, safe='')}?ref={quote(ref, safe='')}"


# --------------------------------------------------------------------------- trading
@dataclass
class TradeResult:
    success: bool
    order_id: str | None = None
    status: str = ""           # matched / live / delayed / unmatched / error
    shares: float = 0.0        # фактически купленные акции
    cost_usdc: float = 0.0     # фактически потраченные USDC
    avg_price: float = 0.0
    error: str | None = None

    @property
    def filled(self) -> bool:
        return self.success and self.shares > 0


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class ClobTrader:
    """Тонкая async-обертка над синхронным py-clob-client (вызовы уходят в thread pool)."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._client: Any = None
        self._lock = asyncio.Lock()   # ордера отправляем строго последовательно

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def init(self) -> None:
        if not self.s.auto_trading:
            log.info("Авто-торговля выключена (режим: сигналы)")
            return
        for attempt in range(1, 6):
            try:
                self._client = await asyncio.to_thread(self._init_sync)
                log.info("CLOB-клиент инициализирован, авто-торговля включена (%s)", self.s.order_type)
                return
            except Exception as exc:  # noqa: BLE001
                delay = min(2 ** attempt, 30)
                log.error("Не удалось инициализировать CLOB-клиент (%s), попытка %d/5, ждем %ds",
                          exc, attempt, delay)
                await asyncio.sleep(delay)
        log.error("CLOB-клиент не инициализирован — бот работает в режиме сигналов")

    def _init_sync(self) -> Any:
        from py_clob_client.client import ClobClient

        assert self.s.private_key is not None
        client = ClobClient(
            self.s.clob_host,
            key=self.s.private_key.get_secret_value(),
            chain_id=self.s.chain_id,
            signature_type=self.s.signature_type,
            funder=self.s.funder_address,
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        return client

    async def buy(self, opp: Opportunity) -> TradeResult:
        if not self.enabled:
            return TradeResult(False, status="error", error="авто-торговля не активна")
        async with self._lock:
            try:
                resp = await asyncio.to_thread(self._buy_sync, opp)
            except Exception as exc:  # noqa: BLE001
                # Повторно НЕ отправляем: риск двойной покупки. Сообщаем пользователю.
                log.exception("Ошибка отправки ордера %s", opp.token_id)
                return TradeResult(False, status="error", error=str(exc)[:300])
        return self._parse_response(resp, opp)

    def _buy_sync(self, opp: Opportunity) -> Any:
        from py_clob_client.clob_types import (
            MarketOrderArgs,
            OrderArgs,
            OrderType,
            PartialCreateOrderOptions,
        )
        from py_clob_client.order_builder.constants import BUY

        options = PartialCreateOrderOptions(tick_size=opp.tick_size, neg_risk=opp.neg_risk)  # type: ignore[arg-type]
        if self.s.order_type == "FOK":
            # Маркет-ордер на $1 с худшей допустимой ценой max_price: либо целиком, либо ничего
            args = MarketOrderArgs(
                token_id=opp.token_id,
                amount=round(self.s.order_size_usdc, 2),
                side=BUY,
                price=self.s.max_price,
                order_type=OrderType.FOK,
            )
            signed = self._client.create_market_order(args, options)
            return self._client.post_order(signed, OrderType.FOK)

        # GTC: лимитный ордер по лучшему Ask на сумму не более $order_size
        price = opp.best_ask
        size = math.floor(self.s.order_size_usdc / price * 100) / 100
        args = OrderArgs(token_id=opp.token_id, price=price, size=size, side=BUY)
        signed = self._client.create_order(args, options)
        return self._client.post_order(signed, OrderType.GTC)

    def _parse_response(self, resp: Any, opp: Opportunity) -> TradeResult:
        if not isinstance(resp, dict):
            return TradeResult(False, status="error", error=f"неожиданный ответ: {resp!r}"[:300])
        order_id = resp.get("orderID") or resp.get("orderId")
        status = str(resp.get("status") or "").lower()
        if not resp.get("success", bool(order_id)) or resp.get("errorMsg"):
            return TradeResult(False, order_id=order_id, status=status or "error",
                               error=str(resp.get("errorMsg") or resp)[:300])

        if status == "matched":
            # Для BUY: makingAmount = USDC, takingAmount = акции
            cost = _f(resp.get("makingAmount")) or self.s.order_size_usdc
            shares = _f(resp.get("takingAmount")) or opp.shares
            return TradeResult(True, order_id, status, shares, cost, cost / shares if shares else 0.0)
        # live / delayed — ордер принят, но еще не исполнен (GTC) — уточним в чекере
        return TradeResult(True, order_id, status or "live")

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            data = await asyncio.to_thread(self._client.get_order, order_id)
            return data if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001
            log.warning("get_order %s: %s", order_id, exc)
            return None

    async def cancel(self, order_id: str) -> bool:
        if not self.enabled:
            return False
        try:
            resp = await asyncio.to_thread(self._client.cancel, order_id)
            return bool(resp) and order_id in (resp.get("canceled") or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel %s: %s", order_id, exc)
            return False
