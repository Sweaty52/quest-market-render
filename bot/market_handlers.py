from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from urllib.parse import quote, urlsplit
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChatMemberRestricted,
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

from .config import MarketSettings
from .gift_notifications import deliver_pending_gift_notifications
from .market_store import MarketStore

router = Router(name="quest-market")
_PRICE_RE = re.compile(r"^(?:0|[1-9]\d{0,20})(?:[.,]\d{1,9})?$")
_STARS_PRICE_RE = re.compile(r"^[1-9]\d{0,20}$")
_OFFER_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_BUY_RE = re.compile(
    r"^(?:\.buy|/buy)\s+(?:eng\s+)?(?:(gram|grams|star|stars)\s+)?(\S+)\s+(\S+)(?:\s+(gram|grams|star|stars))?\s*$",
    re.IGNORECASE,
)


async def _user_has_market_access(user_id: int, bot: Bot, settings: MarketSettings) -> bool:
    if settings.user_has_market_access(user_id):
        return True
    if settings.market_access_chat_id is None:
        return False
    member = await bot.get_chat_member(settings.market_access_chat_id, user_id)
    if member.status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    }:
        return True
    return isinstance(member, ChatMemberRestricted) and member.is_member


def _format_price(value: Decimal | str) -> str:
    price = Decimal(value).normalize()
    return format(price, ",f")


def _currency_label(currency: str) -> str:
    return "Stars" if currency == "STARS" else "GRAM"


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
) -> str:
    current_status = status or offer["status"]
    price = _format_price(offer["price"])
    currency = _currency_label(offer.get("currency", "GRAM"))
    link = escape(str(offer["nft_url"]), quote=True)
    buyer_label = escape(str(offer.get("buyer_label") or "A user"), quote=True)
    gift_label = escape(_gift_label(str(offer["nft_url"])), quote=True)
    expires_at = offer.get("expires_at")
    seconds_left = max(1, int((expires_at - datetime.now(timezone.utc)).total_seconds())) if expires_at else 6 * 3600
    hours_left = max(1, (seconds_left + 3599) // 3600)
    gift_link = f'<a href="{link}">{gift_label}</a>'

    first = f"<b>User {buyer_label} offers you</b>"
    pending = f"<b>{price} {currency}</b> for the gift {gift_link}.\n\nOffer expires in {hours_left} hour{'s' if hours_left != 1 else ''}."
    statuses = {
        "accepted": "\n<b>Offer accepted.</b> Transfer the gift to Quest Market (@questbankk), then tap “I've transferred”.",
        "rejected": "\nThe seller declined this offer.",
        "gift_sent": "\nThe seller reported the transfer. It is not complete until an admin verifies receipt in the bank account.",
    }

    body = pending if current_status == "pending" else statuses.get(current_status, "")
    return f"{first}\n{body}"


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


def _gift_details(message: Message) -> dict:
    if message.unique_gift:
        info = message.unique_gift
        gift = info.gift
        base_name = gift.base_name
        gift_url = (
            f"https://t.me/nft/{quote(base_name, safe='')}-{gift.number}"
            if base_name and gift.number is not None
            else None
        )
        return {
            "telegram_gift_id": None,
            "telegram_owned_gift_id": info.owned_gift_id,
            "gift_name": f"{gift.name} #{gift.number}" if gift.number is not None else gift.name,
            "gift_url": gift_url,
            "model_name": gift.model.name if gift.model else None,
            "backdrop_name": gift.backdrop.name if gift.backdrop else None,
            "symbol_name": gift.symbol.name if gift.symbol else None,
            "metadata": info.model_dump(mode="json", exclude_none=True),
        }

    if message.gift:
        info = message.gift
        gift = info.gift
        return {
            "telegram_gift_id": str(gift.id),
            "telegram_owned_gift_id": info.owned_gift_id,
            "gift_name": f"Telegram gift {gift.id}",
            "gift_url": None,
            "model_name": None,
            "backdrop_name": None,
            "symbol_name": None,
            "metadata": info.model_dump(mode="json", exclude_none=True),
        }
    raise ValueError("The message does not contain a Telegram gift.")


@router.message(F.gift | F.unique_gift)
async def receive_gift(message: Message, bot: Bot, store: MarketStore, settings: MarketSettings) -> None:
    if message.chat.type != ChatType.PRIVATE:
        return
    user = message.from_user
    if user is None:
        return
    if not await _user_has_market_access(user.id, bot, settings):
        await message.answer("Only authorized Quest Market users can register gifts.")
        return

    details = _gift_details(message)
    gift, created = await store.record_gift(
        sender_id=user.id,
        sender_username=user.username,
        sender_name=user.full_name,
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        received_at=message.date,
        **details,
    )
    if not created:
        if gift["sender_id"] != user.id:
            await message.answer("This unique Telegram gift has already been registered.")
            return
        await message.answer(
            f"This gift was already recorded. Record ID: <code>{gift['record_id']}</code>",
            parse_mode="HTML",
        )
        return

    if gift["offer_id"]:
        offer_text = f"Linked offer: <code>{gift['offer_id']}</code>."
    else:
        offer_text = (
            "No single accepted offer could be matched automatically. "
            "If this gift belongs to an offer, link it with "
            f"<code>/linkgift {gift['record_id']} OFFER_ID</code>."
        )
    await message.answer(
        f"Gift saved with status <b>received</b>.\n"
        f"Record ID: <code>{gift['record_id']}</code>\n{offer_text}\n\n"
        "A bank admin must verify the gift in the bank account before it is marked transferred.",
        parse_mode="HTML",
    )


@router.message(Command("start"))
async def start(message: Message, bot: Bot, settings: MarketSettings) -> None:
    user_id = message.from_user.id if message.from_user else None
    has_inline_access = user_id is not None and await _user_has_market_access(user_id, bot, settings)
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


@router.message(F.text.startswith(".buy") | F.text.startswith("/buy"))
async def create_buy_offer(
    message: Message,
    bot: Bot,
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
    if not await _user_has_market_access(user.id, bot, settings):
        await message.answer("Quest Market offers are available to members of the authorized Telegram chat.")
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


@router.message(Command("linkgift"))
async def link_gift(message: Message, bot: Bot, store: MarketStore, settings: MarketSettings) -> None:
    user = message.from_user
    if message.chat.type != ChatType.PRIVATE or user is None:
        return
    if not await _user_has_market_access(user.id, bot, settings):
        await message.answer("Only authorized Quest Market users can link gifts to offers.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not _OFFER_ID_RE.fullmatch(parts[2]):
        await message.answer("Usage: <code>/linkgift GIFT_RECORD_ID OFFER_ID</code>", parse_mode="HTML")
        return
    try:
        record_id = str(UUID(parts[1]))
    except ValueError:
        await message.answer("Invalid gift record ID.")
        return
    gift = await store.link_gift_to_offer(record_id, parts[2], user.id)
    if gift is None:
        await message.answer(
            "Could not link that gift. It must belong to you, and the offer must be accepted by you."
        )
        return
    await message.answer(f"Gift linked to offer <code>{parts[2]}</code>.", parse_mode="HTML")


async def _is_offers_group_admin(message: Message, bot: Bot, settings: MarketSettings) -> bool:
    user = message.from_user
    group_id = settings.offers_group_id
    if group_id is None or message.chat.id != group_id or user is None:
        return False
    member = await bot.get_chat_member(group_id, user.id)
    return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}


@router.message(Command("pendinggifts"))
async def pending_gifts(message: Message, bot: Bot, store: MarketStore, settings: MarketSettings) -> None:
    if not await _is_offers_group_admin(message, bot, settings):
        return
    pending = await store.pending_transfer_gifts()
    if not pending:
        await message.answer("No received gifts are waiting for transfer verification.")
        return
    lines = [
        f"• <code>{gift['record_id']}</code> — {escape(gift['gift_name'])} "
        f"(offer <code>{gift['offer_id']}</code>)"
        for gift in pending[:10]
    ]
    await message.answer(
        "Verify each gift in the bank account before confirming:\n" + "\n".join(lines)
        + f"\n\nUse <code>/confirmgift GIFT_RECORD_ID</code> only after checking @{escape(settings.bank_username)}.",
        parse_mode="HTML",
    )


@router.message(Command("confirmgift"))
async def confirm_gift(
    message: Message,
    bot: Bot,
    store: MarketStore,
    settings: MarketSettings,
) -> None:
    if not await _is_offers_group_admin(message, bot, settings):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Usage: <code>/confirmgift GIFT_RECORD_ID</code>", parse_mode="HTML")
        return
    try:
        record_id = str(UUID(parts[1]))
    except ValueError:
        await message.answer("Invalid gift record ID.")
        return
    gift, newly_confirmed = await store.confirm_gift_transfer(record_id)
    if gift is None:
        await message.answer("Gift not found, not linked to an accepted offer, or not awaiting verification.")
        return
    if not newly_confirmed:
        await message.answer("This gift was already confirmed; no duplicate notification was queued.")
        return
    await deliver_pending_gift_notifications(bot, store, settings)
    await message.answer(
        f"Transfer recorded for @{escape(settings.bank_username)}. "
        "The group notification was sent or queued for retry."
    )


@router.inline_query()
async def inline_offer(query: InlineQuery, bot: Bot, store: MarketStore, settings: MarketSettings) -> None:
    if not await _user_has_market_access(query.from_user.id, bot, settings):
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
    await query.answer(
        [_inline_offer_result(offer, offer_id)],
        cache_time=0,
        is_personal=True,
    )


def _inline_offer_result(
    offer: dict,
    offer_id: str,
) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=offer_id,
        title="Purchase offer · " + _format_price(offer["price"]) + " " + _currency_label(offer.get("currency", "GRAM")),
        description="Send the offer with Accept and Decline buttons",
        input_message_content=InputTextMessageContent(
            message_text=_offer_text(offer),
            parse_mode="HTML",
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
    await query.bot.edit_message_text(
        inline_message_id=query.inline_message_id,
        text=_offer_text(offer),
        reply_markup=_current_keyboard(offer, settings),
        parse_mode="HTML",
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
        success_text = "Transfer reported. A bank admin must still verify receipt."
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
