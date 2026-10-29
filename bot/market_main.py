from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo
from pydantic import ValidationError

from .config import MarketSettings
from .gift_notifications import gift_notification_worker
from .market_handlers import router
from .market_store import MarketStore
from .market_webapp import start_web_app

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = MarketSettings()
    except ValidationError as exc:
        missing = ", ".join("".join(str(part) for part in error["loc"]) for error in exc.errors())
        raise SystemExit(f"Missing or invalid Quest Market settings ({missing}). Check .env.") from None

    if settings.public_webapp_url and settings.market_database_url.startswith("sqlite"):
        raise SystemExit(
            "Render requires a persistent PostgreSQL database. Set MARKET_DATABASE_URL before deploying."
        )

    store = MarketStore(settings.market_database_url)
    bot = Bot(settings.market_bot_token)
    dispatcher = Dispatcher()
    dispatcher["settings"] = settings
    dispatcher["store"] = store
    dispatcher.include_router(router)
    web_runner = None
    notification_task: asyncio.Task[None] | None = None
    try:
        await store.initialize()
        gift_count = await store.seed_gift_links()
        me = await bot.get_me()
        dispatcher["bot_username"] = me.username or "quest_mkt_bot"
        await bot.set_my_commands([
            BotCommand(command="start", description="Open Quest Market"),
            BotCommand(command="myid", description="Show your Telegram ID"),
            BotCommand(command="linkgift", description="Link a gift to an accepted offer"),
            BotCommand(command="pendinggifts", description="List gifts awaiting bank verification"),
            BotCommand(command="confirmgift", description="Confirm a verified bank transfer"),
        ])
        if settings.public_webapp_url:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Quest Market", web_app=WebAppInfo(url=settings.public_webapp_url))
            )
        web_runner = await start_web_app(settings, store, bot, dispatcher)
        log.info(
            "Quest Market started as @%s with %d linked gifts; persistent offer and gift storage is active; Mini App listening on %s:%d",
            me.username,
            gift_count,
            settings.webapp_host,
            settings.listen_port,
        )
        if settings.offers_group_id is None:
            log.warning("OFFERS_GROUP_ID is not configured; transfer notifications are disabled.")
        else:
            notification_task = asyncio.create_task(
                gift_notification_worker(bot, store, settings),
                name="gift-notification-worker",
            )
        if settings.bot_mode == "webhook":
            if not settings.public_webapp_url:
                raise SystemExit("RENDER_EXTERNAL_URL or WEBAPP_URL is required in webhook mode.")
            await bot.set_webhook(
                url=f"{settings.public_webapp_url}/telegram/webhook",
                secret_token=settings.webhook_secret,
                allowed_updates=dispatcher.resolve_used_update_types(),
                drop_pending_updates=False,
            )
            log.info("Telegram webhook configured at %s/telegram/webhook", settings.public_webapp_url)
            await asyncio.Event().wait()
        else:
            await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        if notification_task is not None:
            notification_task.cancel()
            try:
                await notification_task
            except asyncio.CancelledError:
                pass
        if web_runner is not None:
            await web_runner.cleanup()
        await bot.session.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
