from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiohttp import web
from pydantic import ValidationError

from .config import MarketSettings
from .market_store import MarketStore

_INDEX = Path(__file__).parent.parent / "webapp" / "index.html"


def _telegram_user(init_data: str, bot_token: str) -> dict | None:
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        values: dict[str, str] = {}
        for key, value in pairs:
            if key in values:
                return None
            values[key] = value
        received_hash = values.pop("hash", "")
        values.pop("signature", None)
        auth_date = int(values["auth_date"])
        now = int(time.time())
        if not received_hash or auth_date > now + 30 or now - auth_date > 86400:
            return None
        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(received_hash, expected_hash):
            return None
        user = json.loads(values["user"])
        return user if isinstance(user, dict) and isinstance(user.get("id"), int) else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _init_data(request: web.Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("tma "):
        return auth_header[4:]
    return request.headers.get("X-Telegram-Init-Data", "")


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(_INDEX)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def telegram_webhook(request: web.Request) -> web.Response:
    settings: MarketSettings = request.app["settings"]
    if not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""),
        settings.webhook_secret,
    ):
        raise web.HTTPUnauthorized(text="Invalid webhook secret.")
    bot: Bot = request.app["bot"]
    dispatcher: Dispatcher = request.app["dispatcher"]
    try:
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": bot})
    except (ValueError, ValidationError):
        raise web.HTTPBadRequest(text="Invalid Telegram update.") from None
    await dispatcher.feed_update(bot, update)
    return web.Response(text="ok")


async def api_offers(request: web.Request) -> web.Response:
    settings: MarketSettings = request.app["settings"]
    user = _telegram_user(_init_data(request), settings.market_bot_token)
    if not user:
        raise web.HTTPUnauthorized(text="Open Quest Market inside Telegram.")
    store: MarketStore = request.app["store"]
    offers = await store.public_offers()
    result = [
        {
            "id": offer["id"],
            "nft_url": offer["nft_url"],
            "price": str(offer["price"]),
            "currency": offer.get("currency", "GRAM"),
            "created_at": offer["created_at"].isoformat(),
        }
        for offer in offers
    ]
    return web.json_response({"offers": result, "user_id": user["id"]})


async def api_catalog(request: web.Request) -> web.Response:
    category = request.query.get("category", "username")
    if category not in {"username", "number", "gift"}:
        raise web.HTTPBadRequest(text="Unknown catalog category.")
    try:
        page = max(1, min(1000, int(request.query.get("page", "1"))))
        limit = max(1, min(60, int(request.query.get("limit", "36"))))
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid page number.") from None
    store: MarketStore = request.app["store"]
    items, total, collections = await store.catalog_items(
        category=category,
        search=request.query.get("search", ""),
        collection=request.query.get("collection", ""),
        sort=request.query.get("sort", "recent"),
        limit=limit,
        offset=(page - 1) * limit,
    )
    return web.json_response({
        "items": [
            {
                "id": item["id"],
                "category": item["category"],
                "title": item["title"],
                "collection": item["collection"],
                "item_url": item["item_url"],
                "image_url": item["image_url"],
                "price_grams": str(item["price_grams"]),
                "status": item["status"],
            }
            for item in items
        ],
        "total": total,
        "page": page,
        "limit": limit,
        "collections": collections,
    })


def create_web_app(
    settings: MarketSettings,
    store: MarketStore,
    bot: Bot,
    dispatcher: Dispatcher,
) -> web.Application:
    app = web.Application(client_max_size=16 * 1024)
    app["settings"] = settings
    app["store"] = store
    app["bot"] = bot
    app["dispatcher"] = dispatcher
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/offers", api_offers)
    app.router.add_get("/api/catalog", api_catalog)
    if settings.bot_mode == "webhook":
        app.router.add_post("/telegram/webhook", telegram_webhook)
    return app


async def start_web_app(
    settings: MarketSettings,
    store: MarketStore,
    bot: Bot,
    dispatcher: Dispatcher,
) -> web.AppRunner:
    runner = web.AppRunner(create_web_app(settings, store, bot, dispatcher), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.webapp_host, settings.listen_port)
    await site.start()
    return runner
