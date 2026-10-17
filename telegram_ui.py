"""Telegram-интерфейс: команды, карточки сигналов, рассылка с учетом flood-лимитов."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from clob_trader import TradeResult, phantom_browse_url, polymarket_url
from config import Settings
from database import STATUS_CANCELLED, STATUS_WIN, Database, Position, utcnow
from position_manager import ClosedEvent, PositionManager
from scanner import Opportunity

log = logging.getLogger(__name__)
router = Router(name="main")


# --------------------------------------------------------------------------- helpers
def fmt_price(p: float) -> str:
    return f"${p:.4f}".rstrip("0").rstrip(".") if p < 0.01 else f"${p:.2f}"


def fmt_left(end: datetime) -> str:
    seconds = int((end - utcnow()).total_seconds())
    if seconds <= 0:
        return "⌛ ожидает резолюции"
    h, rem = divmod(seconds, 3600)
    return f"⏳ {h}ч {rem // 60:02d}м"


def fmt_time(end: datetime, settings: Settings) -> str:
    local = end.astimezone(settings.tz)
    today = utcnow().astimezone(settings.tz).date()
    day = "" if local.date() == today else local.strftime(" %d.%m")
    return f"{local:%H:%M}{day} ({settings.timezone})"


def outcome_label(outcome: str) -> str:
    return outcome.upper() if outcome.lower() in ("yes", "no") else outcome


def links_keyboard(settings: Settings, event_slug: str, market_slug: str,
                   skip_position_id: int | None = None) -> InlineKeyboardMarkup:
    url = polymarket_url(settings, event_slug, market_slug)
    rows = [[
        InlineKeyboardButton(text="⚡ Открыть в Phantom",
                             url=phantom_browse_url(url, settings.polymarket_web)),
        InlineKeyboardButton(text="🌐 Polymarket", url=url),
    ]]
    if skip_position_id is not None:
        rows.append([InlineKeyboardButton(text="🚫 Не вошёл — освободить слот",
                                          callback_data=f"skip:{skip_position_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_signal(opp: Opportunity, active_count: int, settings: Settings,
                  trade: TradeResult | None = None) -> str:
    title = escape(opp.event_title)
    question = escape(opp.question)
    lines = [
        f"🎯 <b>СИГНАЛ 100x+ [Слот {active_count}/{settings.max_open_positions}]</b>",
        "",
        f"• <b>Событие:</b> {title}",
    ]
    if question and question != title:
        lines.append(f"• <b>Рынок:</b> {question}")
    lines += [
        f"• <b>Исход:</b> {escape(outcome_label(opp.outcome))}",
        f"• <b>Цена входа:</b> {fmt_price(opp.avg_price)} (Кэф ~{opp.multiplier:.0f}x)",
        f"• <b>Риск:</b> ${settings.order_size_usdc:.2f} USDC ➔ "
        f"<b>Потенциал:</b> ${opp.payout:.2f} USDC",
        f"• <b>Дедлайн:</b> {fmt_time(opp.end_date, settings)} · {fmt_left(opp.end_date)}",
        f"• <b>Стакан:</b> Объем доступен (${opp.ask_liquidity_usdc:.2f} по ≤ {fmt_price(settings.max_price)})",
    ]
    if trade is not None:
        lines.append("")
        if trade.filled:
            lines.append(
                f"🤖 <b>Авто-ордер исполнен:</b> {trade.shares:.2f} акций за ${trade.cost_usdc:.2f} "
                f"(ср. {fmt_price(trade.avg_price)})"
            )
        elif trade.success:
            lines.append(f"🤖 <b>Авто-ордер выставлен</b> ({escape(trade.status)}), ожидает исполнения")
        if trade.order_id:
            lines.append(f"<code>{escape(trade.order_id)}</code>")
    return "\n".join(lines)


def format_trade_failure(opp: Opportunity, trade: TradeResult) -> str:
    return (
        "⚠️ <b>Авто-ордер не исполнен</b> — слот освобожден\n"
        f"• <b>Событие:</b> {escape(opp.event_title)}\n"
        f"• <b>Исход:</b> {escape(outcome_label(opp.outcome))} @ {fmt_price(opp.best_ask)}\n"
        f"• <b>Причина:</b> <code>{escape(trade.error or trade.status)}</code>\n"
        "Если ошибка сетевая — проверьте ордера в аккаунте Polymarket вручную."
    )


def format_closed(ev: ClosedEvent, active_count: int, settings: Settings) -> str:
    p = ev.position
    if ev.status == STATUS_CANCELLED:
        head = "🚫 <b>Сделка отменена</b>"
    elif ev.status == STATUS_WIN:
        head = f"✅ <b>ПОБЕДА!</b> +${p.pnl:.2f}"
    else:
        head = f"❌ <b>Проигрыш</b> -${abs(p.pnl):.2f}"
    return (
        f"{head} · #{p.id}\n"
        f"• {escape(p.event_title)}\n"
        f"• Исход: {escape(outcome_label(p.outcome))} @ {fmt_price(p.entry_price)}\n"
        f"• Причина: {escape(ev.reason)}\n"
        f"Слоты: {active_count}/{settings.max_open_positions}"
    )


async def broadcast(bot: Bot, chat_ids: list[int], text: str,
                    reply_markup: InlineKeyboardMarkup | None = None) -> None:
    for chat_id in chat_ids:
        for attempt in range(3):
            try:
                await bot.send_message(chat_id, text, reply_markup=reply_markup,
                                       disable_web_page_preview=True)
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramForbiddenError:
                log.warning("Бот заблокирован в чате %s", chat_id)
                break
            except TelegramAPIError as exc:
                log.warning("Ошибка отправки в %s: %s (попытка %d)", chat_id, exc, attempt + 1)
                await asyncio.sleep(2 ** attempt)
        await asyncio.sleep(0.05)  # ~20 msg/s — ниже лимитов Telegram


# --------------------------------------------------------------------------- access
class AllowedChat(BaseFilter):
    async def __call__(self, event: TelegramObject, settings: Settings) -> bool:
        chat_id = None
        user_id = None
        if isinstance(event, Message):
            chat_id, user_id = event.chat.id, event.from_user.id if event.from_user else None
        elif isinstance(event, CallbackQuery):
            chat_id = event.message.chat.id if event.message else None
            user_id = event.from_user.id
        allowed = set(settings.telegram_chat_ids)
        return chat_id in allowed or user_id in allowed


router.message.filter(AllowedChat())
router.callback_query.filter(AllowedChat())


# --------------------------------------------------------------------------- handlers
@router.message(CommandStart())
async def cmd_start(message: Message, pm: PositionManager, settings: Settings) -> None:
    active = await pm.active_count()
    full = active >= settings.max_open_positions
    mode = "🤖 Авто-торговля (" + settings.order_type + ")" if pm.mode == "auto" else "📡 Сигналы"
    await message.answer(
        "🟢 <b>Polymarket Underdog Bot</b> работает\n\n"
        f"• <b>Режим:</b> {mode}\n"
        f"• <b>Открытые сделки:</b> {active}/{settings.max_open_positions}"
        f"{' — лимит исчерпан, новые сигналы на паузе' if full else ''}\n"
        f"• <b>Фильтр:</b> цена ≤ {fmt_price(settings.max_price)} (≥{1 / settings.max_price:.0f}x), "
        f"экспирация {settings.min_time_to_end_minutes}м – {settings.max_time_to_end_hours:g}ч\n"
        f"• <b>Размер сделки:</b> ${settings.order_size_usdc:.2f} USDC\n\n"
        "/positions — активные сделки\n/stats — статистика\n"
        "/cancel &lt;id&gt; — освободить слот (сигнал без входа / неисполненный ордер)"
    )


@router.message(Command("positions"))
async def cmd_positions(message: Message, db: Database, settings: Settings) -> None:
    positions = await db.get_open_positions()
    if not positions:
        await message.answer(f"📭 Активных сделок нет (0/{settings.max_open_positions})")
        return
    await message.answer(
        f"📊 <b>Активные сделки: {len(positions)}/{settings.max_open_positions}</b>"
    )
    for p in positions:
        await message.answer(_format_position(p, settings),
                             reply_markup=links_keyboard(settings, p.event_slug, p.market_slug),
                             disable_web_page_preview=True)
        await asyncio.sleep(0.05)


def _format_position(p: Position, settings: Settings) -> str:
    potential = p.shares if p.shares > 0 else settings.order_size_usdc / max(p.entry_price, 1e-9)
    fill = ""
    if p.mode == "auto" and p.shares <= 0:
        fill = "\n• ⏱ Ордер ожидает исполнения"
    return (
        f"<b>#{p.id}</b> · {escape(p.event_title)}\n"
        f"• Исход: <b>{escape(outcome_label(p.outcome))}</b> @ {fmt_price(p.entry_price)}\n"
        f"• Риск ${p.cost_usdc or settings.order_size_usdc:.2f} ➔ потенциал ${potential:.2f}\n"
        f"• Дедлайн: {fmt_time(p.end_date, settings)} · {fmt_left(p.end_date)}"
        f"{fill}"
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database, pm: PositionManager, settings: Settings) -> None:
    st = await db.stats()
    sign = "+" if st.pnl >= 0 else "-"
    note = "\n<i>В режиме сигналов PnL считается так, будто каждый сигнал исполнен на $1.</i>" \
        if pm.mode == "signals" else ""
    await message.answer(
        "📈 <b>Статистика</b>\n\n"
        f"• Всего сделок: {st.total}\n"
        f"• Открыто: {st.open}/{settings.max_open_positions}\n"
        f"• Выиграно: {st.wins}\n"
        f"• Проиграно: {st.losses}\n"
        f"• Отменено: {st.cancelled}\n"
        f"• Win rate: {st.win_rate:.1f}%\n"
        f"• Вложено (закрытые): ${st.invested:.2f}\n"
        f"• Выплаты: ${st.payout:.2f}\n"
        f"• <b>PnL: {sign}${abs(st.pnl):.2f}</b>{note}"
    )


async def _cancel(pm: PositionManager, position_id: int) -> tuple[bool, str]:
    pos = await pm.cancel_position(position_id, "отменено пользователем")
    if pos is None:
        return False, f"Позицию #{position_id} нельзя отменить (не найдена, уже закрыта или ордер исполнен)"
    return True, f"🚫 Позиция #{position_id} отменена. Слоты: {await pm.active_count()}/{pm.s.max_open_positions}"


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, command: CommandObject, pm: PositionManager) -> None:
    if not command.args or not command.args.strip().lstrip("#").isdigit():
        await message.answer("Использование: /cancel &lt;id&gt; (id из /positions)")
        return
    _, text = await _cancel(pm, int(command.args.strip().lstrip("#")))
    await message.answer(text)


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(callback: CallbackQuery, pm: PositionManager) -> None:
    try:
        position_id = int((callback.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректные данные", show_alert=True)
        return
    ok, text = await _cancel(pm, position_id)
    await callback.answer(text if len(text) < 190 else text[:190], show_alert=not ok)
    if ok and callback.message is not None and isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.reply(text)
        except TelegramAPIError:
            pass
