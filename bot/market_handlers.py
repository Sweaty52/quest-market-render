from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from urllib.parse import urlsplit

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
    Message,
    WebAppInfo,
)
from aiogram.utils.formatting import Bold, CustomEmoji, Text, TextLink

from .config import MarketSettings
from .market_store import MarketStore

router = Router(name="quest-market")
log = logging.getLogger(__name__)
_PRICE_RE = re.compile(r"^(?:0|[1-9]\d{0,20})(?:[.,]\d{1,9})?$")
_STARS_PRICE_RE = re.compile(r"^[1-9]\d{0,20}$")
_OFFER_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_CURRENCY_CUSTOM_EMOJI_IDS = {
    "GRAM": "5264766603584641330",
    "STARS": "5220029283631672529",
}
_CURRENCY_FALLBACK_EMOJIS = {"GRAM": "💎", "STARS": "⭐"}
_CURRENCY_EMOJI_ALTS: dict[str, str] | None = None
_BUY_RE = re.compile(
    r"^(?:\.buy|/buy)\s+(?:eng\s+)?(?:(gram|grams|star|stars)\s+)?(\S+)\s+(\S+)(?:\s+(gram|grams|star|stars))?\s*$",
    re.IGNORECASE,
)


def _format_price(value: Decimal | str) -> str:
    price = Decimal(value).normalize()
    return format(price, ",f")


async def _currency_emoji_alts(bot: Bot) -> dict[str, str]:
    global _CURRENCY_EMOJI_ALTS
    if _CURRENCY_EMOJI_ALTS is not None:
        return _CURRENCY_EMOJI_ALTS
    try:
        stickers = await bot.get_custom_emoji_stickers(
            custom_emoji_ids=list(_CURRENCY_CUSTOM_EMOJI_IDS.values())
        )
    except TelegramAPIError as exc:
        log.warning("Could not retrieve currency custom emoji metadata (%s)", type(exc).__name__)
        return {}
    emoji_alts = {
        str(sticker.custom_emoji_id): sticker.emoji
        for sticker in stickers
        if sticker.custom_emoji_id and sticker.emoji
    }
    missing_ids = set(_CURRENCY_CUSTOM_EMOJI_IDS.values()) - emoji_alts.keys()
    if missing_ids:
        log.warning("Telegram returned no metadata for currency custom emoji IDs: %s", ", ".join(sorted(missing_ids)))
        return emoji_alts
    _CURRENCY_EMOJI_ALTS = emoji_alts
    return emoji_alts


def _currency_emoji(
    currency: str, emoji_alts: dict[str, str], *, custom: bool = True
) -> str:
    key = "STARS" if currency == "STARS" else "GRAM"
    fallback = _CURRENCY_FALLBACK_EMOJIS[key]
    if not custom:
        return fallback
    custom_emoji_id = _CURRENCY_CUSTOM_EMOJI_IDS[key]
    alt = escape(emoji_alts.get(custom_emoji_id, fallback))
    return f'<tg-emoji emoji-id="{custom_emoji_id}">{alt}</tg-emoji>'


def _valid_nft_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and len(value) <= 1500
        )
    except ValueError:
        return False


def _gift_label(nft_url: str) -> str:
    slug = urlsplit(nft_url).path.rstrip("/").rsplit("/", 1)[-1]
    match = re.fullmatch(r"([A-Za-z0-9]+)-(\d+)", slug)
    if not match:
        return "Telegram collectible"
    collection, serial = match.groups()
    overrides = {"BDayCandle": "Birthday Candle", "Icecream": "Ice Cream", "UFCStrike": "UFC Strike"}
    name = overrides.get(collection) or re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", collection)
    return f"{name} #{serial}"


def _offer_text(
    offer: dict,
    status: str | None = None,
    emoji_alts: dict[str, str] | None = None,
    *,
    custom_emoji: bool = True,
) -> Text:
    current_status = status or offer["status"]
    price = _format_price(offer["price"])
    currency_key = "STARS" if offer.get("currency") == "STARS" else "GRAM"
    custom_emoji_id = _CURRENCY_CUSTOM_EMOJI_IDS[currency_key]
    emoji = emoji_alts.get(custom_emoji_id, _CURRENCY_FALLBACK_EMOJIS[currency_key]) if emoji_alts else _CURRENCY_FALLBACK_EMOJIS[currency_key]
    currency = (
        CustomEmoji(emoji, custom_emoji_id=custom_emoji_id)
        if custom_emoji
        else emoji
    )
    buyer_label = str(offer.get("buyer_label") or "A user")
    gift_label = _gift_label(str(offer["nft_url"]))
    expires_at = offer.get("expires_at")
    seconds_left = max(1, int((expires_at - datetime.now(timezone.utc)).total_seconds())) if expires_at else 6 * 3600
    hours_left = max(1, (seconds_left + 3599) // 3600)
    first = Bold(f"User {buyer_label} offers you")

    if current_status == "pending":
        return Text(
            first,
            "\n",
            Bold(price, " ", currency),
            " for the gift ",
            TextLink(gift_label, url=str(offer["nft_url"])),
            ".\n\nOffer expires in ",
            str(hours_left),
            " hour",
            "s" if hours_left != 1 else "",
            ".",
        )
    if current_status == "accepted":
        body = Text(
            Bold("Offer accepted."),
            " Transfer the gift to Quest Market (@questbankk), then tap “I've transferred”.",
        )
    elif current_status == "rejected":
        body = Text("The seller declined this offer.")
    elif current_status == "gift_sent":
        body = Text("The seller marked the collectible as transferred. Quest Market will verify receipt.")
    else:
        body = Text()
    return Text(first, "\n", body)


def _pending_keyboard(offer_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Decline", callback_data=f"qm:r:{offer_id}"),
        InlineKeyboardButton(text="Accept", callback_data=f"qm:a:{offer_id}"),
    ]])


def _accepted_keyboard(offer_id: str, transfer_url: str = "") -> InlineKeyboardMarkup:
    transfer_button = (
        InlineKeyboardButton(text="Transfer gift", url=transfer_url)
        if transfer_url
        else InlineKeyboardButton(text="Transfer gift", callback_data=f"qm:t:{offer_id}")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [transfer_button, InlineKeyboardButton(text="I've transferred", callback_data=f"qm:s:{offer_id}")],
        [InlineKeyboardButton(text="How it works", callback_data=f"qm:h:{offer_id}")],
    ])


def _share_keyboard(offer: dict, bot_username: str = "quest_mkt_bot") -> InlineKeyboardMarkup:
    inline_query = f"@{bot_username} {offer['id']}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Copy inline", copy_text=CopyTextButton(text=inline_query))
    ]])


def _current_keyboard(offer: dict, settings: MarketSettings) -> InlineKeyboardMarkup | None:
    if offer["status"] == "pending":
        return _pending_keyboard(offer["id"])
    if offer["status"] == "accepted":
        return _accepted_keyboard(offer["id"], settings.market_account_url)
    return InlineKeyboardMarkup(inline_keyboard=[])


@router.message(Command("myid"))
async def show_user_id(message: Message) -> None:
    if message.from_user:
        await message.answer(f"Your Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@router.message(Command("start"))
async def start(message: Message, settings: MarketSettings) -> None:
    user_id = message.from_user.id if message.from_user else None
    has_inline_access = user_id is not None and settings.user_has_market_access(user_id)
    if has_inline_access:
        name = escape(message.from_user.first_name, quote=True)
        text = (
            f"Hello, {name}! Welcome to <b>Quest Market</b>.\n\n"
            "Create an offer with one command:\n"
            "<code>.buy https://t.me/nft/Collectible-123 12.5 grams</code>\n"
            "<code>.buy https://t.me/nft/Collectible-123 500 stars</code>\n\n"
            "The bot will create the inline offer. Tap <b>Copy inline</b>, paste it in the target chat, then select the result."
        )
        await message.answer(text, parse_mode="HTML")
    elif settings.public_webapp_url:
        name = escape(message.from_user.first_name if message.from_user else "there", quote=True)
        text = (
            f"Welcome, {name} 👋\n\n"
            "<b>QUEST MARKET</b>\n"
            "Discover Telegram collectibles in one place.\n\n"
            "🎁 Unique gifts\n"
            "✦ Rare usernames\n"
            "＋ Collectible +888 numbers\n\n"
            "Browse the catalog and open any listing to see its details."
        )
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Explore the market", web_app=WebAppInfo(url=settings.public_webapp_url))
            ], [
                InlineKeyboardButton(text="How it works", callback_data="market:how")
            ]]),
        )
    elif not has_inline_access:
        await message.answer("The marketplace link is not configured yet.")


@router.callback_query(F.data == "market:how")
async def market_how_it_works(callback: CallbackQuery) -> None:
    await callback.answer(
        "Browse gifts, usernames and +888 numbers. Prices are shown in GRAM. "
        "Open a listing to view its Telegram page or start an offer.",
        show_alert=True,
    )


@router.message(Command("help"))
async def help_command(message: Message, settings: MarketSettings) -> None:
    await message.answer(
        "Use <code>.buy https://t.me/nft/Collectible-123 12.5 grams</code> or "
        "<code>.buy https://t.me/nft/Collectible-123 500 stars</code>. "
        "Tap <b>Copy inline</b>, paste it in the target chat, then select the result.",
        parse_mode="HTML",
    )


@router.message(F.text.startswith(".buy") | F.text.startswith("/buy"))
async def create_buy_offer(
    message: Message,
    store: MarketStore,
    bot_username: str,
    settings: MarketSettings,
) -> None:
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("Create offers in a private chat with Quest Market.")
        return
    text = message.text or ""
    match = _BUY_RE.fullmatch(text.strip())
    if not match:
        await message.answer(
            "Format: <code>.buy eng https://t.me/nft/Collectible-123 12.5</code> for GRAM or "
            "<code>.buy eng stars https://t.me/nft/Collectible-123 500</code> for Stars.",
            parse_mode="HTML",
        )
        return

    prefix_currency, nft_url, raw_price, suffix_currency = match.groups()
    if prefix_currency and suffix_currency:
        prefix_is_stars = prefix_currency.casefold().startswith("star")
        suffix_is_stars = suffix_currency.casefold().startswith("star")
        if prefix_is_stars != suffix_is_stars:
            await message.answer("Choose one offer currency: GRAM or Stars.")
            return
    currency_token = prefix_currency or suffix_currency or "gram"
    currency = "STARS" if currency_token.casefold().startswith("star") else "GRAM"
    if not _valid_nft_url(nft_url):
        await message.answer("The collectible link must start with https://.")
        return
    valid_price = _STARS_PRICE_RE.fullmatch(raw_price) if currency == "STARS" else _PRICE_RE.fullmatch(raw_price)
    if not valid_price:
        if currency == "STARS":
            await message.answer("Enter a positive whole-number Stars price, for example <code>500</code>.", parse_mode="HTML")
        else:
            await message.answer("Enter a positive GRAM price with up to 9 decimal places, for example <code>12.5</code>.", parse_mode="HTML")
        return
    try:
        price = Decimal(raw_price.replace(",", "."))
    except InvalidOperation:
        await message.answer(f"Couldn't read the {currency} price.")
        return
    if price <= 0:
        await message.answer(f"Price must be greater than zero {currency}.")
        return

    user = message.from_user
    if user is None:
        return
    buyer_label = f"@{user.username}" if user.username else user.full_name
    offer = await store.create_offer(user.id, buyer_label, nft_url, price, currency=currency, language="en")
    success_text = "✅ <b>Offer created.</b> Tap Copy inline, paste the query in the target chat, then select the result:"
    await message.answer(
        success_text + "\n\n"
        f"<code>@{escape(bot_username)} {offer['id']}</code>",
        reply_markup=_share_keyboard(offer, bot_username),
        parse_mode="HTML",
    )


@router.inline_query()
async def inline_offer(query: InlineQuery, store: MarketStore, settings: MarketSettings) -> None:
    if not settings.user_has_market_access(query.from_user.id):
        await query.answer([], cache_time=0, is_personal=True)
        return
    offer_id = (query.query or "").strip()
    if not _OFFER_ID_RE.fullmatch(offer_id):
        await query.answer([], cache_time=0, is_personal=True)
        return
    offer = await store.get_offer(offer_id)
    if not offer or offer["buyer_id"] != query.from_user.id or offer["status"] != "pending":
        await query.answer([], cache_time=0, is_personal=True)
        return
    emoji_alts = await _currency_emoji_alts(query.bot)
    result = _inline_offer_result(offer, offer_id, emoji_alts, custom_emoji=True)
    try:
        await query.answer([result], cache_time=0, is_personal=True)
    except TelegramBadRequest as exc:
        log.warning("Telegram rejected custom emoji in inline offer; using standard emoji (%s)", exc)
        await query.answer(
            [_inline_offer_result(offer, offer_id, emoji_alts, custom_emoji=False)],
            cache_time=0,
            is_personal=True,
        )


def _inline_offer_result(
    offer: dict,
    offer_id: str,
    emoji_alts: dict[str, str],
    *,
    custom_emoji: bool = True,
) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=offer_id,
        title="Purchase offer · " + _format_price(offer["price"]) + " " + _currency_emoji(
            offer.get("currency", "GRAM"), emoji_alts, custom=False
        ),
        description="Send the offer with Accept and Decline buttons",
        input_message_content=InputTextMessageContent(
            **_offer_text(offer, emoji_alts=emoji_alts, custom_emoji=custom_emoji).as_kwargs(
                text_key="message_text"
            ),
            link_preview_options=LinkPreviewOptions(
                is_disabled=False,
                url=offer["nft_url"],
                prefer_large_media=True,
                show_above_text=True,
            ),
        ),
        reply_markup=_pending_keyboard(offer_id),
    )


async def _edit_inline_message(query: CallbackQuery, offer: dict, settings: MarketSettings) -> None:
    if not query.inline_message_id:
        return
    emoji_alts = await _currency_emoji_alts(query.bot)
    try:
        await query.bot.edit_message_text(
            inline_message_id=query.inline_message_id,
            **_offer_text(offer, emoji_alts=emoji_alts).as_kwargs(),
            reply_markup=_current_keyboard(offer, settings),
            link_preview_options=LinkPreviewOptions(
                is_disabled=False,
                url=offer["nft_url"],
                prefer_large_media=True,
                show_above_text=True,
            ),
        )
    except TelegramBadRequest as exc:
        log.warning("Telegram rejected custom emoji while editing offer; using standard emoji (%s)", exc)
        await query.bot.edit_message_text(
            inline_message_id=query.inline_message_id,
            **_offer_text(offer, emoji_alts=emoji_alts, custom_emoji=False).as_kwargs(),
            reply_markup=_current_keyboard(offer, settings),
            link_preview_options=LinkPreviewOptions(
                is_disabled=False,
                url=offer["nft_url"],
                prefer_large_media=True,
                show_above_text=True,
            ),
        )


@router.callback_query(F.data.startswith("qm:"))
async def offer_action(query: CallbackQuery, store: MarketStore, settings: MarketSettings) -> None:
    parts = (query.data or "").split(":")
    if len(parts) != 3 or not _OFFER_ID_RE.fullmatch(parts[2]):
        await query.answer("Invalid offer.", show_alert=True)
        return
    _, action, offer_id = parts
    user_id = query.from_user.id

    if action == "h":
        await query.answer(
            "Transfer the collectible to Quest Market (@questbankk), then tap “I've transferred”. Only confirm after the transfer is complete.",
            show_alert=True,
        )
        return
    if action == "t":
        await query.answer("The transfer link is not configured yet. Set MARKET_ACCOUNT_URL to enable it.", show_alert=True)
        return

    offer = None
    if action == "a":
        offer = await store.accept_offer(offer_id, user_id)
        success_text = "Offer accepted. Transfer the collectible to Quest Market."
    elif action == "r":
        offer = await store.reject_offer(offer_id, user_id)
        success_text = "Offer declined."
    elif action == "s":
        offer = await store.mark_gift_sent(offer_id, user_id)
        success_text = "Marked as transferred. Quest Market must verify receipt."
    else:
        await query.answer("Unknown action.", show_alert=True)
        return

    if offer:
        await query.answer(success_text)
        try:
            await _edit_inline_message(query, offer, settings)
        except Exception:
            # The callback still updates the authoritative offer state if an old inline
            # message can no longer be edited by Telegram.
            pass
        return

    current = await store.get_offer(offer_id)
    if current and current["buyer_id"] == user_id:
        await query.answer("You can't accept or decline your own offer.", show_alert=True)
    elif current and current["status"] == "accepted" and current["accepted_by"] != user_id:
        await query.answer("Another seller has already accepted this offer.", show_alert=True)
    elif current and current["status"] == "gift_sent":
        await query.answer("This offer is already marked as transferred.", show_alert=True)
    elif current and current["status"] == "rejected":
        await query.answer("This offer has already been declined.", show_alert=True)
    else:
        await query.answer("This action is unavailable.", show_alert=True)
