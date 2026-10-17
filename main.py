"""Точка входа: aiogram-бот + фоновые задачи (сканер и чекер резолюций)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from clob_trader import ClobTrader
from config import Settings, get_settings
from database import Database
from http_client import ApiError, HttpClient
from position_manager import PositionManager
from scanner import MarketScanner
from telegram_ui import broadcast, format_closed, format_signal, format_trade_failure, links_keyboard, router

log = logging.getLogger("bot")


async def scanner_loop(bot: Bot, scanner: MarketScanner, pm: PositionManager,
                       db: Database, trader: ClobTrader, settings: Settings) -> None:
    while True:
        try:
            free = await pm.free_slots()
            if free <= 0:
                log.info("Все %d слотов заняты — новые сигналы на паузе", settings.max_open_positions)
            else:
                exclude = await db.known_condition_ids()
                # Берем с запасом: часть авто-ордеров может не исполниться
                opportunities = await scanner.scan(exclude, limit=free * 3)
                for opp in opportunities:
                    position = await pm.reserve(opp)
                    if position is None:
                        break  # лимит достигнут
                    if trader.enabled:
                        result = await trader.buy(opp)
                        position = await pm.apply_trade(position, result)
                        if not result.success:
                            await broadcast(bot, settings.telegram_chat_ids,
                                            format_trade_failure(opp, result),
                                            links_keyboard(settings, opp.event_slug, opp.market_slug))
                            continue
                        text = format_signal(opp, await pm.active_count(), settings, result)
                        kb = links_keyboard(settings, opp.event_slug, opp.market_slug)
                    else:
                        text = format_signal(opp, await pm.active_count(), settings)
                        kb = links_keyboard(settings, opp.event_slug, opp.market_slug,
                                            skip_position_id=position.id if position else None)
                    await broadcast(bot, settings.telegram_chat_ids, text, kb)
        except ApiError as exc:
            log.warning("Сканер: ошибка API: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Сканер: непредвиденная ошибка")
        await asyncio.sleep(settings.scan_interval_sec)


async def resolution_loop(bot: Bot, pm: PositionManager, settings: Settings) -> None:
    while True:
        try:
            for ev in await pm.check_resolutions():
                await broadcast(bot, settings.telegram_chat_ids,
                                format_closed(ev, await pm.active_count(), settings))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Чекер резолюций: непредвиденная ошибка")
        await asyncio.sleep(settings.resolution_check_interval_sec)


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    for noisy in ("aiogram.event", "httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if settings.trading_mode == "auto" and not settings.private_key:
        log.warning("TRADING_MODE=auto, но PRIVATE_KEY не задан — работаем в режиме сигналов")

    db = Database(settings.db_path)
    await db.connect()

    http = HttpClient(
        timeout=settings.http_timeout_sec,
        max_retries=settings.http_max_retries,
        host_rates={
            settings.gamma_host.split("://", 1)[-1].rstrip("/"): settings.gamma_max_rps,
            settings.clob_host.split("://", 1)[-1].rstrip("/"): settings.clob_max_rps,
        },
    )
    await http.start()

    trader = ClobTrader(settings)
    await trader.init()

    pm = PositionManager(db, http, trader, settings)
    scanner = MarketScanner(http, settings)

    bot = Bot(settings.bot_token.get_secret_value(),
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(settings=settings, db=db, pm=pm)
    dp.include_router(router)

    tasks: list[asyncio.Task[None]] = []

    async def on_startup() -> None:
        tasks.append(asyncio.create_task(scanner_loop(bot, scanner, pm, db, trader, settings),
                                         name="scanner"))
        tasks.append(asyncio.create_task(resolution_loop(bot, pm, settings), name="resolution"))
        active = await pm.active_count()
        await broadcast(bot, settings.telegram_chat_ids,
                        f"🚀 Бот запущен · режим: <b>{pm.mode}</b> · слоты {active}/{settings.max_open_positions}")

    async def on_shutdown() -> None:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await http.close()
        await db.close()
        log.info("Бот остановлен")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, handle_signals=True,
                           allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
