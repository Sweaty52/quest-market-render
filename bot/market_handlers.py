from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from urllib.parse import quote, urlsplit
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    BusinessConnection,
    CallbackQuery,
    ChatMemberRestricted,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    WebAppInfo,
)

from .config import MarketSettings
from .gift_notifications import deliver_pending_gift_notifications
from .market_store import MarketStore

log = logging.getLogger(__name__)
router = Router(name="quest-market")
_PRICE_RE = re.compile(r"^(?:0|[1-9]\d{0,20})(?:[.,]\d{1,9})?$")
_STARS_PRICE_RE = re.compile(r"^[1-9]\d{0,20}$")
_OFFER_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_TARGET_USERNAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")
_TARGET_RE = re.compile(r"(?:@[A-Za-z][A-Za-z0-9_]{4,31}|[1-9]\d{0,15})")
_BUY_DRAFT_RE = re.compile(
    r"^(?:\.buy|/buy)\s+(?:(gram|grams|star|stars)\s+)?(\S+)\s+(\S+)(?:\s+(gram|grams|star|stars))?\s*$",
    re.IGNORECASE,
)
_BUY_RE = re.compile(
    rf"^(?:\.buy|/buy)\s+(?:eng\s+)?(?:(gram|grams|star|stars)\s+)?({_TARGET_RE.pattern})\s+(\S+)\s+(\S+)(?:\s+(gram|grams|star|stars))?\s*$",
    re.IGNORECASE,
)
_OFFER_TOKEN_COMMAND_RE = re.compile(r"^\.qm\s+([A-Za-z0-9_-]{16})$")
_GRAM_CUSTOM_EMOJI_ID = "5264766603584641330"
_STARS_CUSTOM_EMOJI_ID = "5220029283631672529"


async def _delete_business_command(message: Message, bot: Bot) -> bool:
    try:
        deleted = await bot.delete_message(
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
        if deleted:
            return True
        log.info(
            "Telegram did not delete offer-code message %s in chat %s; trying Business API",
            message.message_id,
            message.chat.id,
        )
    except TelegramAPIError:
        log.info(
            "Standard deletion failed for offer-code message %s in chat %s; trying Business API",
            message.message_id,
            message.chat.id,
            exc_info=True,
        )
    if not message.business_connection_id:
        return False
    try:
        deleted = await bot.delete_business_messages(
            business_connection_id=message.business_connection_id,
            message_ids=[message.message_id],
        )
        if deleted:
            return True
    except TelegramAPIError:
        log.info(
            "Business deletion failed for offer-code message %s in chat %s",
            message.message_id,
            message.chat.id,
            exc_info=True,
        )
    log.warning(
        "Telegram could not delete offer-code message %s in chat %s",
        message.message_id,
        message.chat.id,
    )
    return False


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
    *,
    use_business_emojis: bool = False,
) -> str:
    current_status = status or offer["status"]
    price = _format_price(offer["price"])
    if use_business_emojis:
        if offer.get("currency") == "STARS":
            currency = f'<tg-emoji emoji-id="{_STARS_CUSTOM_EMOJI_ID}">⭐</tg-emoji>'
        else:
            currency = f'<tg-emoji emoji-id="{_GRAM_CUSTOM_EMOJI_ID}">🪙</tg-emoji>'
    else:
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
        "accepted": "\n<b>Offer accepted.</b> Transfer the gift to the account configured by the offer creator, then tap “I've transferred”.",
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


def _current_keyboard(
    offer: dict,
    settings: MarketSettings,
    transfer_url: str | None = None,
) -> InlineKeyboardMarkup | None:
    if offer["status"] == "pending":
        return _pending_keyboard(offer["id"])
    if offer["status"] == "accepted":
        return _accepted_keyboard(
            offer["id"],
            transfer_url if transfer_url is not None else settings.market_account_url,
        )
    return InlineKeyboardMarkup(inline_keyboard=[])


@router.message(Command("myid"))
async def show_user_id(message: Message) -> None:
    if message.from_user:
        await message.answer(f"Your Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


def _gift_recipient_url(username: str | None, settings: MarketSettings) -> str:
    if not username:
        return settings.market_account_url
    return f"tg://send_gift?to={quote(username, safe='')}"


@router.message(Command("giftrecipient"))
async def configure_gift_recipient(
    message: Message,
    bot: Bot,
    store: MarketStore,
    settings: MarketSettings,
) -> None:
    user = message.from_user
    if message.chat.type != ChatType.PRIVATE or user is None:
        return
    if not await _user_has_market_access(user.id, bot, settings):
        await message.answer("This setting is available only to authorized Quest Market users.")
        return

    parts = (message.text or "").split()
    if len(parts) == 1:
        username = await store.get_gift_recipient(user.id)
        if username:
            await message.answer(f"Gift recipient: <code>@{escape(username)}</code>", parse_mode="HTML")
        else:
            await message.answer(
                "No personal gift recipient set. Offers use the default configured account. "
                "Set one with <code>/giftrecipient @username</code>.",
                parse_mode="HTML",
            )
        return
    if len(parts) != 2 or not _TARGET_USERNAME_RE.fullmatch(parts[1]):
        await message.answer(
            "Usage: <code>/giftrecipient @username</code> using a valid Telegram username.",
            parse_mode="HTML",
        )
        return

    await store.set_gift_recipient(user_id=user.id, username=parts[1])
    await message.answer(
        f"Gift recipient saved as <code>{escape(parts[1])}</code>. "
        "The Transfer gift button in your offers will open this account.",
        parse_mode="HTML",
    )


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


@router.business_connection()
async def update_business_connection(
    connection: BusinessConnection,
    store: MarketStore,
) -> None:
    can_reply = bool(
        connection.can_reply
        or (connection.rights and connection.rights.can_reply)
    )
    await store.save_business_connection(
        connection_id=connection.id,
        business_user_id=connection.user.id,
        user_chat_id=connection.user_chat_id,
        is_enabled=connection.is_enabled,
        can_reply=can_reply,
    )
    log.info(
        "Business connection updated for user %s (enabled=%s, can_reply=%s)",
        connection.user.id,
        connection.is_enabled,
        can_reply,
    )


@router.business_message()
async def remember_business_recipient(
    message: Message,
    bot: Bot,
    store: MarketStore,
) -> None:
    if message.chat.type != ChatType.PRIVATE or not message.business_connection_id:
        return
    user = message.from_user
    if user is None:
        return

    token_match = _OFFER_TOKEN_COMMAND_RE.fullmatch((message.text or "").strip())
    if token_match:
        await _delete_business_command(message, bot)
        recipient_id = message.chat.id
        if recipient_id <= 0:
            log.warning(
                "Ignoring offer code in non-user Business chat %s",
                message.chat.id,
            )
            return
        offer = await store.claim_offer_token(
            token=token_match.group(1),
            connection_id=message.business_connection_id,
            recipient_id=recipient_id,
        )
        if offer is None:
            await bot.send_message(
                chat_id=message.chat.id,
                business_connection_id=message.business_connection_id,
                text="This offer code is invalid, expired, already used, or belongs to another Business account.",
            )
            return
        try:
            await bot.send_message(
                chat_id=recipient_id,
                business_connection_id=message.business_connection_id,
                text=_offer_text(offer, use_business_emojis=True),
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(
                    is_disabled=False,
                    url=offer["nft_url"],
                    prefer_large_media=True,
                    show_above_text=True,
                ),
                reply_markup=_pending_keyboard(offer["id"]),
            )
        except TelegramAPIError:
            log.exception("Could not deliver Business offer %s", offer["id"])
            try:
                await bot.send_message(
                    chat_id=offer["buyer_id"],
                    text=(
                        f"Offer <code>{offer['id']}</code> was created for Telegram user "
                        f"<code>{user.id}</code>, but Business delivery failed. "
                        "The one-time code was consumed; create a new code to retry."
                    ),
                    parse_mode="HTML",
                )
            except TelegramAPIError:
                log.exception("Could not notify offer creator %s about delivery failure", offer["buyer_id"])
            return
        log.info("Offer %s created from a one-time Business code", offer["id"])
        return

    if not user.username:
        return
    recorded = await store.record_business_recipient(
        connection_id=message.business_connection_id,
        username=user.username,
        user_id=user.id,
    )
    if recorded:
        log.info(
            "Observed recipient @%s through a Business chat",
            user.username,
        )


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
    has_market_access = user_id is not None and await _user_has_market_access(user_id, bot, settings)
    if has_market_access:
        name = escape(message.from_user.first_name, quote=True)
        text = (
            f"Hello, {name}! Welcome to <b>Quest Market</b>.\n\n"
            "Send an offer through your connected Telegram Business account:\n"
            "<code>.buy @username https://t.me/nft/Collectible-123 12.5 gram</code>\n"
            "<code>.buy @username https://t.me/nft/Collectible-123 500 stars</code>\n"
            "Or create a one-time code to paste into the recipient's chat:\n"
            "<code>.buy https://t.me/nft/Collectible-123 12.5 gram</code>\n\n"
            "The recipient must have messaged your connected Business account so the bot can resolve "
            "their username. A one-time code instead identifies the recipient from the Business chat "
            "where the code is used.\n\n"
            "Set your gift destination with <code>/giftrecipient @username</code>; "
            "check it with <code>/giftrecipient</code>."
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
    elif not has_market_access:
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
    settings: MarketSettings,
) -> None:
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("Create offers in a private chat with Quest Market.")
        return
    text = message.text or ""
    match = _BUY_RE.fullmatch(text.strip())
    if not match:
        draft_match = _BUY_DRAFT_RE.fullmatch(text.strip())
        if draft_match:
            prefix_currency, nft_url, raw_price, suffix_currency = draft_match.groups()
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
                await message.answer(
                    "Enter a positive whole-number Stars price, for example <code>500</code>."
                    if currency == "STARS"
                    else "Enter a positive GRAM price with up to 9 decimal places, for example <code>12.5</code>.",
                    parse_mode="HTML",
                )
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
            business_connection = await store.get_business_connection(user.id)
            if business_connection is None:
                await message.answer(
                    "Connect this bot to your Telegram Business account and allow it to reply to chats "
                    "before creating an offer code."
                )
                return

            buyer_label = f"@{user.username}" if user.username else user.full_name
            token = await store.create_offer_token(
                connection_id=business_connection["id"],
                creator_id=user.id,
                buyer_label=buyer_label,
                nft_url=nft_url,
                price=price,
                currency=currency,
                language="en",
            )
            await message.answer(
                "Offer code created. Paste this exact command into the recipient's private chat "
                "with your connected Business account. The first other person to use it becomes "
                "the recipient; it expires in 24 hours and works once:\n"
                f"<code>.qm {escape(token)}</code>",
                parse_mode="HTML",
            )
            return
        await message.answer(
            "Format: <code>.buy @username https://t.me/nft/Collectible-123 12.5 gram</code> "
            "or <code>.buy 123456789 https://t.me/nft/Collectible-123 500 stars</code>.\n"
            "To generate a one-time recipient command, use "
            "<code>.buy https://t.me/nft/Collectible-123 12.5 gram</code>.",
            parse_mode="HTML",
        )
        return

    prefix_currency, target_text, nft_url, raw_price, suffix_currency = match.groups()
    if target_text.startswith("@"):
        if not _TARGET_USERNAME_RE.fullmatch(target_text):
            await message.answer("Recipient must be a valid Telegram username such as <code>@username</code>.", parse_mode="HTML")
            return
        target_user_id = None
    elif target_text.isdecimal():
        target_user_id = int(target_text)
    else:
        await message.answer(
            "Recipient must be a Telegram username such as <code>@username</code> or a numeric user ID.",
            parse_mode="HTML",
        )
        return
    if target_user_id is not None and target_user_id > 2**52 - 1:
        await message.answer("Recipient ID is outside Telegram's supported range.")
        return
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
    if target_user_id == user.id:
        await message.answer("You cannot send an offer to yourself.")
        return
    business_connection = await store.get_business_connection(user.id)
    if business_connection is None:
        await message.answer(
            "Connect this bot to your Telegram Business account and allow it to reply to chats "
            "before sending business offers."
        )
        return

    if target_user_id is None:
        target_user_id = await store.resolve_business_recipient(
            connection_id=business_connection["id"],
            username=target_text[1:],
        )
        if target_user_id is None:
            await message.answer(
                f"I don't know <code>{escape(target_text)}</code> from your Business chats yet. "
                "Ask them to message your connected Business account first, then retry the offer.",
                parse_mode="HTML",
            )
            return

    buyer_label = f"@{user.username}" if user.username else user.full_name
    offer = await store.create_offer(
        user.id,
        buyer_label,
        nft_url,
        price,
        currency=currency,
        language="en",
        recipient_id=target_user_id,
    )
    try:
        await bot.send_message(
            chat_id=target_user_id,
            business_connection_id=business_connection["id"],
            text=_offer_text(offer, use_business_emojis=True),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                is_disabled=False,
                url=offer["nft_url"],
                prefer_large_media=True,
                show_above_text=True,
            ),
            reply_markup=_pending_keyboard(offer["id"]),
        )
    except TelegramAPIError:
        log.exception(
            "Could not deliver business offer %s to Telegram user %s",
            offer["id"],
            target_user_id,
        )
        await message.answer(
            "Offer was saved, but Telegram could not deliver it to that recipient. "
            "Check that the Business account can reply in their chat. "
            f"Offer ID: <code>{offer['id']}</code>",
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"✅ Offer <code>{offer['id']}</code> sent to Telegram user "
        f"<code>{target_user_id}</code> from your Business account.",
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


async def _edit_inline_message(
    query: CallbackQuery,
    offer: dict,
    settings: MarketSettings,
    transfer_url: str | None = None,
) -> None:
    if not query.inline_message_id:
        return
    await query.bot.edit_message_text(
        inline_message_id=query.inline_message_id,
        text=_offer_text(offer),
        reply_markup=_current_keyboard(offer, settings, transfer_url),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(
            is_disabled=False,
            url=offer["nft_url"],
            prefer_large_media=True,
            show_above_text=True,
        ),
    )


async def _edit_offer_message(
    query: CallbackQuery,
    offer: dict,
    settings: MarketSettings,
    store: MarketStore,
) -> None:
    recipient_username = await store.get_gift_recipient(offer["buyer_id"])
    transfer_url = _gift_recipient_url(recipient_username, settings)
    if query.inline_message_id:
        await _edit_inline_message(query, offer, settings, transfer_url)
        return
    if not isinstance(query.message, Message):
        return

    business_connection_id = query.message.business_connection_id
    await query.bot.edit_message_text(
        chat_id=query.message.chat.id,
        message_id=query.message.message_id,
        business_connection_id=business_connection_id,
        text=_offer_text(offer, use_business_emojis=bool(business_connection_id)),
        reply_markup=_current_keyboard(offer, settings, transfer_url),
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
        offer = await store.get_offer(offer_id)
        creator_username = (
            await store.get_gift_recipient(offer["buyer_id"])
            if offer is not None
            else None
        )
        recipient = f"@{creator_username}" if creator_username else "the account configured by the offer creator"
        await query.answer(
            f"Transfer the collectible to {recipient}, then tap “I've transferred”. Only confirm after the transfer is complete.",
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
        try:
            await _edit_offer_message(query, offer, settings, store)
        except TelegramAPIError:
            log.exception("Could not update Telegram message for offer %s", offer_id)
            await query.answer(
                f"{success_text} The offer status was saved, but Telegram could not update its message.",
                show_alert=True,
            )
            return
        await query.answer(success_text)
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
