from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .config import MarketSettings
from .market_store import MarketStore

log = logging.getLogger(__name__)
_KYIV = ZoneInfo("Europe/Kyiv")


def transfer_notification_text(gift: dict, bank_username: str) -> str:
    username = gift.get("offer_creator_username")
    if username:
        creator = f"@{escape(str(username))}"
    else:
        name = escape(str(gift.get("offer_creator_name") or gift["sender_name"]))
        creator = f"{name} (ID: <code>{gift.get('offer_creator_id') or gift['sender_id']}</code>)"

    gift_name = escape(str(gift["gift_name"]))
    gift_url = gift.get("gift_url")
    gift_line = f'<a href="{escape(str(gift_url), quote=True)}">{gift_name}</a>' if gift_url else gift_name
    estimated_value = gift.get("estimated_value_uah")
    if estimated_value is None:
        price_line = "Немає оцінки"
    else:
        amount = Decimal(estimated_value)
        price_line = f"≈ {amount:,.0f}".replace(",", " ") + " грн"

    transferred_at: datetime = gift["transferred_at"]
    transferred_at = transferred_at.astimezone(_KYIV)
    return (
        "🎁 Подарунок передано в банк\n\n"
        f"👤 Хто створив офер: {creator}\n"
        f"🎁 Подарунок: {gift_line}\n"
        f"🔗 Посилання: {escape(str(gift_url), quote=True) if gift_url else 'Немає посилання'}\n"
        f"💰 Орієнтовна вартість: {price_line}\n"
        f"📅 Дата передачі: {transferred_at:%d.%m.%Y, %H:%M}\n"
        f"🏦 Підтверджено для @{escape(bank_username)}"
    )


async def deliver_pending_gift_notifications(
    bot: Bot,
    store: MarketStore,
    settings: MarketSettings,
) -> None:
    if settings.offers_group_id is None:
        return
    pending = await store.claim_pending_notifications()
    for gift in pending:
        try:
            message = await bot.send_message(
                chat_id=settings.offers_group_id,
                text=transfer_notification_text(gift, settings.bank_username),
                parse_mode="HTML",
            )
        except TelegramAPIError as exc:
            await store.finish_notification(gift["record_id"], error=str(exc))
            log.exception("Could not notify offers group about gift %s", gift["record_id"])
        else:
            await store.finish_notification(gift["record_id"], message_id=message.message_id)


async def gift_notification_worker(
    bot: Bot,
    store: MarketStore,
    settings: MarketSettings,
) -> None:
    while True:
        await deliver_pending_gift_notifications(bot, store, settings)
        await asyncio.sleep(settings.gift_notification_retry_seconds)
