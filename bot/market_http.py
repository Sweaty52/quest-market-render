from __future__ import annotations

import hmac

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiohttp import web
from pydantic import ValidationError

from .config import MarketSettings

_SETTINGS_KEY: web.AppKey[MarketSettings] = web.AppKey("settings", MarketSettings)
_BOT_KEY: web.AppKey[Bot] = web.AppKey("bot", Bot)
_DISPATCHER_KEY: web.AppKey[Dispatcher] = web.AppKey("dispatcher", Dispatcher)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def telegram_webhook(request: web.Request) -> web.Response:
    settings = request.app[_SETTINGS_KEY]
    if not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""),
        settings.webhook_secret,
    ):
        raise web.HTTPUnauthorized(text="Invalid webhook secret.")
    bot = request.app[_BOT_KEY]
    dispatcher = request.app[_DISPATCHER_KEY]
    try:
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": bot})
    except (ValueError, ValidationError):
        raise web.HTTPBadRequest(text="Invalid Telegram update.") from None
    await dispatcher.feed_update(bot, update)
    return web.Response(text="ok")


def create_http_app(settings: MarketSettings, bot: Bot, dispatcher: Dispatcher) -> web.Application:
    app = web.Application(client_max_size=16 * 1024)
    app[_SETTINGS_KEY] = settings
    app[_BOT_KEY] = bot
    app[_DISPATCHER_KEY] = dispatcher
    app.router.add_get("/health", health)
    if settings.bot_mode == "webhook":
        app.router.add_post("/telegram/webhook", telegram_webhook)
    return app


async def start_http_server(
    settings: MarketSettings,
    bot: Bot,
    dispatcher: Dispatcher,
) -> web.AppRunner:
    runner = web.AppRunner(create_http_app(settings, bot, dispatcher), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.http_host, settings.listen_port)
    await site.start()
    return runner
