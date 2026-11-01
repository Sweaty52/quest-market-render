from __future__ import annotations

import asyncio
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
_LANGUAGE_TOKEN = r"(?:ru|rus|en|eng|zh|cn|ch|rh)"
_BUY_DRAFT_RE = re.compile(
    rf"^(?:\.buy|/buy)\s+(?:({_LANGUAGE_TOKEN})\s+)?(?:(gram|grams|star|stars)\s+)?(\S+)\s+(\S+)(?:\s+(gram|grams|star|stars))?(?:\s+({_LANGUAGE_TOKEN}))?\s*$",
    re.IGNORECASE,
)
_BUY_RE = re.compile(
    rf"^(?:\.buy|/buy)\s+(?:({_LANGUAGE_TOKEN})\s+)?(?:(gram|grams|star|stars)\s+)?({_TARGET_RE.pattern})\s+(\S+)\s+(\S+)(?:\s+(gram|grams|star|stars))?(?:\s+({_LANGUAGE_TOKEN}))?\s*$",
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


def _normalize_offer_language(value: str | None) -> str:
    if not value:
        return "ru"
    normalized = value.casefold()
    if normalized in {"ru", "rus"}:
        return "ru"
    if normalized in {"zh", "cn", "ch", "rh"}:
        return "zh"
    return "en"


def _selected_offer_language(prefix: str | None, suffix: str | None) -> str | None:
    if prefix and suffix:
        normalized_prefix = _normalize_offer_language(prefix)
        normalized_suffix = _normalize_offer_language(suffix)
        if normalized_prefix != normalized_suffix:
            return None
        return normalized_prefix
    return _normalize_offer_language(prefix or suffix)


_OFFER_TRANSLATIONS = {
    "ru": {
        "first": "<b>{buyer} предлагает вам</b>",
        "pending": "<b>{price} {currency}</b> за подарок {gift}.\n\nСрок действия офера — {hours} ч.",
        "accepted": "\n<b>Офер принят.</b> Переведите подарок на аккаунт, указанный автором офера, затем нажмите «Я перевёл подарок».",
        "rejected": "\nПродавец отклонил офер.",
        "gift_sent": "\nПродавец сообщил о переводе. Передача будет завершена после проверки администратором.",
        "decline": "Отклонить",
        "accept": "Принять",
        "transfer": "Перевести подарок",
        "transferred": "Я перевёл подарок",
        "how": "Как это работает",
        "help": "Переведите подарок пользователю {recipient}, затем нажмите «Я перевёл подарок». Подтверждайте только после завершения перевода.",
        "invalid": "Недействительный офер.",
        "transfer_unavailable": "Ссылка для перевода не настроена. Укажите MARKET_ACCOUNT_URL.",
        "unknown_action": "Неизвестное действие.",
        "own_offer": "Нельзя принять или отклонить собственный офер.",
        "already_accepted": "Этот офер уже принял другой продавец.",
        "already_transferred": "Этот офер уже отмечен как переданный.",
        "already_declined": "Этот офер уже отклонён.",
        "unavailable": "Это действие недоступно.",
        "accepted_success": "Офер принят.",
        "declined_success": "Офер отклонён.",
        "reported_success": "Перевод отмечен. Администратору банка ещё нужно подтвердить получение.",
        "update_failed": "{success} Статус сохранён, но Telegram не удалось обновить сообщение.",
    },
    "en": {
        "first": "<b>User {buyer} offers you</b>",
        "pending": "<b>{price} {currency}</b> for the gift {gift}.\n\nOffer expires in {hours} hour{plural}.",
        "accepted": "\n<b>Offer accepted.</b> Transfer the gift to the account configured by the offer creator, then tap “I've transferred”.",
        "rejected": "\nThe seller declined this offer.",
        "gift_sent": "\nThe seller reported the transfer. It is not complete until an admin verifies receipt in the bank account.",
        "decline": "Decline",
        "accept": "Accept",
        "transfer": "Transfer gift",
        "transferred": "I've transferred",
        "how": "How it works",
        "help": "Transfer the collectible to {recipient}, then tap “I've transferred”. Only confirm after the transfer is complete.",
        "invalid": "Invalid offer.",
        "transfer_unavailable": "The transfer link is not configured. Set MARKET_ACCOUNT_URL.",
        "unknown_action": "Unknown action.",
        "own_offer": "You can't accept or decline your own offer.",
        "already_accepted": "Another seller has already accepted this offer.",
        "already_transferred": "This offer is already marked as transferred.",
        "already_declined": "This offer has already been declined.",
        "unavailable": "This action is unavailable.",
        "accepted_success": "Offer accepted.",
        "declined_success": "Offer declined.",
        "reported_success": "Transfer reported. A bank admin must still verify receipt.",
        "update_failed": "{success} The offer status was saved, but Telegram could not update its message.",
    },
    "zh": {
        "first": "<b>{buyer} 向你发出报价</b>",
        "pending": "<b>{price} {currency}</b>，交换礼物 {gift}。\n\n报价将在 {hours} 小时后过期。",
        "accepted": "\n<b>报价已接受。</b>请将礼物转给报价创建者指定的账号，然后点击“我已转赠”。",
        "rejected": "\n卖家已拒绝此报价。",
        "gift_sent": "\n卖家已报告转赠。管理员核实收到礼物后才算完成。",
        "decline": "拒绝",
        "accept": "接受",
        "transfer": "转赠礼物",
        "transferred": "我已转赠",
        "how": "使用说明",
        "help": "请将礼物转给 {recipient}，然后点击“我已转赠”。只有完成转赠后才能确认。",
        "invalid": "报价无效。",
        "transfer_unavailable": "转赠链接尚未配置，请设置 MARKET_ACCOUNT_URL。",
        "unknown_action": "未知操作。",
        "own_offer": "不能接受或拒绝自己创建的报价。",
        "already_accepted": "此报价已被其他卖家接受。",
        "already_transferred": "此报价已标记为已转赠。",
        "already_declined": "此报价已被拒绝。",
        "unavailable": "此操作不可用。",
        "accepted_success": "报价已接受。",
        "declined_success": "报价已拒绝。",
        "reported_success": "已报告转赠，仍需银行管理员核实收到礼物。",
        "update_failed": "{success} 状态已保存，但 Telegram 无法更新消息。",
    },
}


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
    language = _normalize_offer_language(offer.get("language", "en"))
    text = _OFFER_TRANSLATIONS[language]
    price = _format_price(offer["price"])
    currency_label = _currency_label(offer.get("currency", "GRAM"))
    if language == "ru" and offer.get("currency") == "STARS":
        currency_label = "звёзд"
    elif language == "zh" and offer.get("currency") == "STARS":
        currency_label = "星"
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

    first = text["first"].format(buyer=buyer_label)
    pending = text["pending"].format(
        price=price,
        currency=currency if use_business_emojis else currency_label,
        gift=gift_link,
        hours=hours_left,
        plural="" if hours_left == 1 else "s",
    )
    body = pending if current_status == "pending" else text.get(current_status, "")
    return f"{first}\n{body}"


def _pending_keyboard(offer_id: str, language: str = "en") -> InlineKeyboardMarkup:
    text = _OFFER_TRANSLATIONS[_normalize_offer_language(language)]
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text["decline"], callback_data=f"qm:r:{offer_id}"),
        InlineKeyboardButton(text=text["accept"], callback_data=f"qm:a:{offer_id}"),
    ]])


def _accepted_keyboard(
    offer_id: str,
    transfer_url: str = "",
    language: str = "en",
) -> InlineKeyboardMarkup:
    text = _OFFER_TRANSLATIONS[_normalize_offer_language(language)]
    transfer_button = (
        InlineKeyboardButton(text=text["transfer"], url=transfer_url)
        if transfer_url
        else InlineKeyboardButton(text=text["transfer"], callback_data=f"qm:t:{offer_id}")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [transfer_button, InlineKeyboardButton(text=text["transferred"], callback_data=f"qm:s:{offer_id}")],
        [InlineKeyboardButton(text=text["how"], callback_data=f"qm:h:{offer_id}")],
    ])


def _current_keyboard(
    offer: dict,
    settings: MarketSettings,
    transfer_url: str | None = None,
) -> InlineKeyboardMarkup | None:
    if offer["status"] == "pending":
        return _pending_keyboard(offer["id"], offer.get("language", "en"))
    if offer["status"] == "accepted":
        return _accepted_keyboard(
            offer["id"],
            transfer_url if transfer_url is not None else settings.market_account_url,
            offer.get("language", "en"),
        )
    return InlineKeyboardMarkup(inline_keyboard=[])


@router.message(Command("myid"))
async def show_user_id(message: Message) -> None:
    if message.from_user:
        await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


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
        await message.answer("Эта настройка доступна только пользователям с разрешённым доступом к Quest Market.")
        return

    parts = (message.text or "").split()
    if len(parts) == 1:
        username = await store.get_gift_recipient(user.id)
        if username:
            await message.answer(f"Получатель подарков: <code>@{escape(username)}</code>", parse_mode="HTML")
        else:
            await message.answer(
                "Личный получатель подарков не задан. В оферах будет использоваться аккаунт по умолчанию. "
                "Задайте его командой <code>/giftrecipient @username</code>.",
                parse_mode="HTML",
            )
        return
    if len(parts) != 2 or not _TARGET_USERNAME_RE.fullmatch(parts[1]):
        await message.answer(
            "Формат: <code>/giftrecipient @username</code>. Укажите действительный username Telegram.",
            parse_mode="HTML",
        )
        return

    await store.set_gift_recipient(user_id=user.id, username=parts[1])
    await message.answer(
        f"Получатель подарков сохранён: <code>{escape(parts[1])}</code>. "
        "Кнопка перевода подарка в ваших оферах будет открывать этот аккаунт.",
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
                text="Код офера недействителен, истёк, уже использован или относится к другому Business-аккаунту.",
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
                reply_markup=_pending_keyboard(offer["id"], offer.get("language", "en")),
            )
        except TelegramAPIError:
            log.exception("Could not deliver Business offer %s", offer["id"])
            try:
                await bot.send_message(
                    chat_id=offer["buyer_id"],
                    text=(
                        f"Офер <code>{offer['id']}</code> создан для пользователя Telegram "
                        f"<code>{user.id}</code>, но Business-доставка не удалась. "
                        "Одноразовый код уже использован; создайте новый код для повтора."
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
        await message.answer("Регистрировать подарки могут только пользователи с доступом к Quest Market.")
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
            await message.answer("Этот уникальный подарок Telegram уже зарегистрирован.")
            return
        await message.answer(
            f"Этот подарок уже сохранён. ID записи: <code>{gift['record_id']}</code>",
            parse_mode="HTML",
        )
        return

    if gift["offer_id"]:
        offer_text = f"Связанный офер: <code>{gift['offer_id']}</code>."
    else:
        offer_text = (
            "Не удалось однозначно связать подарок с принятым офером. "
            "Если подарок относится к оферу, свяжите его командой "
            f"<code>/linkgift {gift['record_id']} OFFER_ID</code>."
        )
    await message.answer(
        f"Подарок сохранён со статусом <b>получен</b>.\n"
        f"ID записи: <code>{gift['record_id']}</code>\n{offer_text}\n\n"
        "Администратор банка должен проверить подарок в аккаунте, прежде чем отмечать его переданным.",
        parse_mode="HTML",
    )


@router.message(Command("start"))
async def start(message: Message, bot: Bot, settings: MarketSettings) -> None:
    user_id = message.from_user.id if message.from_user else None
    has_market_access = user_id is not None and await _user_has_market_access(user_id, bot, settings)
    if has_market_access:
        name = escape(message.from_user.first_name, quote=True)
        text = (
            f"Привет, {name}! Добро пожаловать в <b>Quest Market</b>.\n\n"
            "Создайте офер через подключённый аккаунт Telegram Business:\n"
            "<code>.buy ru @username https://t.me/nft/Collectible-123 12.5 gram</code>\n"
            "<code>.buy eng @username https://t.me/nft/Collectible-123 500 stars</code>\n"
            "<code>.buy zh @username https://t.me/nft/Collectible-123 500 stars</code>\n"
            "Язык офера: ru, eng или zh; если не указан — русский.\n"
            "Для одноразового кода вставьте ссылку и цену без получателя:\n"
            "<code>.buy ru https://t.me/nft/Collectible-123 12.5 gram</code>\n\n"
            "Чтобы определить получателя по username, он должен сначала написать вашему Business-аккаунту. "
            "Одноразовый код определяет его по чату, в котором код был использован.\n\n"
            "Задайте аккаунт для получения подарков командой <code>/giftrecipient @username</code>; "
            "посмотреть текущий — <code>/giftrecipient</code>."
        )
        await message.answer(text, parse_mode="HTML")
    elif settings.public_webapp_url:
        name = escape(message.from_user.first_name if message.from_user else "there", quote=True)
        text = (
            f"Добро пожаловать, {name} 👋\n\n"
            "<b>QUEST MARKET</b>\n"
            "Коллекционные предметы Telegram в одном месте.\n\n"
            "🎁 Уникальные подарки\n"
            "✦ Редкие username\n"
            "＋ Коллекционные номера +888\n\n"
            "Откройте каталог и выберите лот, чтобы посмотреть подробности."
        )
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Открыть маркет", web_app=WebAppInfo(url=settings.public_webapp_url))
            ], [
                InlineKeyboardButton(text="Как это работает", callback_data="market:how")
            ]]),
        )
    elif not has_market_access:
        await message.answer("Ссылка на маркет пока не настроена.")


@router.callback_query(F.data == "market:how")
async def market_how_it_works(callback: CallbackQuery) -> None:
    await callback.answer(
        "Просматривайте подарки, username и номера +888. Цены указаны в GRAM. "
        "Откройте лот, чтобы перейти на его страницу в Telegram или создать офер.",
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
        await message.answer("Создавайте оферы в личном чате с Quest Market.")
        return
    text = message.text or ""
    match = _BUY_RE.fullmatch(text.strip())
    if not match:
        draft_match = _BUY_DRAFT_RE.fullmatch(text.strip())
        if draft_match:
            (
                prefix_language,
                prefix_currency,
                nft_url,
                raw_price,
                suffix_currency,
                suffix_language,
            ) = draft_match.groups()
            language = _selected_offer_language(prefix_language, suffix_language)
            if language is None:
                await message.answer("Укажите один язык офера: ru, eng или zh.")
                return
            if prefix_currency and suffix_currency:
                prefix_is_stars = prefix_currency.casefold().startswith("star")
                suffix_is_stars = suffix_currency.casefold().startswith("star")
                if prefix_is_stars != suffix_is_stars:
                    await message.answer("Выберите одну валюту офера: GRAM или Stars.")
                    return
            currency_token = prefix_currency or suffix_currency or "gram"
            currency = "STARS" if currency_token.casefold().startswith("star") else "GRAM"
            if not _valid_nft_url(nft_url):
                await message.answer("Ссылка на коллекционный подарок должна начинаться с https://.")
                return
            valid_price = _STARS_PRICE_RE.fullmatch(raw_price) if currency == "STARS" else _PRICE_RE.fullmatch(raw_price)
            if not valid_price:
                await message.answer(
                    "Укажите целое количество Stars больше нуля, например <code>500</code>."
                    if currency == "STARS"
                    else "Укажите сумму в GRAM больше нуля, например <code>12.5</code>.",
                    parse_mode="HTML",
                )
                return
            try:
                price = Decimal(raw_price.replace(",", "."))
            except InvalidOperation:
                await message.answer(f"Не удалось прочитать цену в {currency}.")
                return
            if price <= 0:
                await message.answer(f"Цена должна быть больше нуля ({currency}).")
                return

            user = message.from_user
            if user is None:
                return
            has_access, (business_connection, _) = await asyncio.gather(
                _user_has_market_access(user.id, bot, settings),
                store.get_business_offer_context(business_user_id=user.id),
            )
            if not has_access:
                await message.answer("Создавать оферы могут только участники разрешённого Telegram-чата.")
                return
            if business_connection is None:
                await message.answer(
                    "Подключите бота к аккаунту Telegram Business и разрешите отвечать в чатах, "
                    "чтобы создать код офера."
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
                language=language,
            )
            await message.answer(
                "Код офера создан. Отправьте эту команду в личный чат получателя через подключённый "
                "аккаунт Business. Первый другой пользователь, который применит код, станет получателем. "
                "Код действует 24 часа и используется один раз:\n"
                f"<code>.qm {escape(token)}</code>",
                parse_mode="HTML",
            )
            return
        await message.answer(
            "Формат: <code>.buy ru @username https://t.me/nft/Collectible-123 12.5 gram</code> "
            "или <code>.buy zh 123456789 https://t.me/nft/Collectible-123 500 stars</code>.\n"
            "Язык можно указать как ru, eng или zh; по умолчанию используется русский. "
            "Чтобы создать одноразовый код получателя, используйте "
            "<code>.buy ru https://t.me/nft/Collectible-123 12.5 gram</code>.",
            parse_mode="HTML",
        )
        return

    (
        prefix_language,
        prefix_currency,
        target_text,
        nft_url,
        raw_price,
        suffix_currency,
        suffix_language,
    ) = match.groups()
    language = _selected_offer_language(prefix_language, suffix_language)
    if language is None:
        await message.answer("Укажите один язык офера: ru, eng или zh.")
        return
    if target_text.startswith("@"):
        if not _TARGET_USERNAME_RE.fullmatch(target_text):
            await message.answer("Укажите действительный username Telegram, например <code>@username</code>.", parse_mode="HTML")
            return
        target_user_id = None
    elif target_text.isdecimal():
        target_user_id = int(target_text)
    else:
        await message.answer(
            "Укажите username Telegram, например <code>@username</code>, или числовой ID пользователя.",
            parse_mode="HTML",
        )
        return
    if target_user_id is not None and target_user_id > 2**52 - 1:
        await message.answer("ID получателя выходит за поддерживаемый Telegram диапазон.")
        return
    if prefix_currency and suffix_currency:
        prefix_is_stars = prefix_currency.casefold().startswith("star")
        suffix_is_stars = suffix_currency.casefold().startswith("star")
        if prefix_is_stars != suffix_is_stars:
            await message.answer("Выберите одну валюту офера: GRAM или Stars.")
            return
    currency_token = prefix_currency or suffix_currency or "gram"
    currency = "STARS" if currency_token.casefold().startswith("star") else "GRAM"
    if not _valid_nft_url(nft_url):
        await message.answer("Ссылка на коллекционный подарок должна начинаться с https://.")
        return
    valid_price = _STARS_PRICE_RE.fullmatch(raw_price) if currency == "STARS" else _PRICE_RE.fullmatch(raw_price)
    if not valid_price:
        if currency == "STARS":
            await message.answer("Укажите целое количество Stars больше нуля, например <code>500</code>.", parse_mode="HTML")
        else:
            await message.answer("Укажите сумму в GRAM больше нуля, например <code>12.5</code>.", parse_mode="HTML")
        return
    try:
        price = Decimal(raw_price.replace(",", "."))
    except InvalidOperation:
        await message.answer(f"Не удалось прочитать цену в {currency}.")
        return
    if price <= 0:
        await message.answer(f"Цена должна быть больше нуля ({currency}).")
        return

    user = message.from_user
    if user is None:
        return
    if target_user_id == user.id:
        await message.answer("Нельзя отправить офер самому себе.")
        return
    username = target_text[1:] if target_text.startswith("@") else None
    has_access, (business_connection, resolved_recipient_id) = await asyncio.gather(
        _user_has_market_access(user.id, bot, settings),
        store.get_business_offer_context(
            business_user_id=user.id,
            username=username,
        ),
    )
    if not has_access:
        await message.answer("Создавать оферы могут только участники разрешённого Telegram-чата.")
        return
    if business_connection is None:
        await message.answer(
            "Подключите бота к аккаунту Telegram Business и разрешите отвечать в чатах, "
            "чтобы отправлять оферы."
        )
        return

    if target_user_id is None:
        target_user_id = resolved_recipient_id
        if target_user_id is None:
            await message.answer(
                f"Пользователь <code>{escape(target_text)}</code> пока не найден среди чатов вашего Business-аккаунта. "
                "Попросите его сначала написать этому аккаунту, затем повторите отправку офера.",
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
        language=language,
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
            reply_markup=_pending_keyboard(offer["id"], offer.get("language", "en")),
        )
    except TelegramAPIError:
        log.exception(
            "Could not deliver business offer %s to Telegram user %s",
            offer["id"],
            target_user_id,
        )
        await message.answer(
            "Офер сохранён, но Telegram не смог доставить его получателю. "
            "Проверьте, что Business-аккаунт может отвечать в этом чате. "
            f"ID офера: <code>{offer['id']}</code>",
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"✅ Офер <code>{offer['id']}</code> отправлен пользователю Telegram "
        f"<code>{target_user_id}</code> от имени вашего Business-аккаунта.",
        parse_mode="HTML",
    )


@router.message(Command("linkgift"))
async def link_gift(message: Message, bot: Bot, store: MarketStore, settings: MarketSettings) -> None:
    user = message.from_user
    if message.chat.type != ChatType.PRIVATE or user is None:
        return
    if not await _user_has_market_access(user.id, bot, settings):
        await message.answer("Связывать подарки с оферами могут только пользователи с доступом к Quest Market.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not _OFFER_ID_RE.fullmatch(parts[2]):
        await message.answer("Формат: <code>/linkgift GIFT_RECORD_ID OFFER_ID</code>", parse_mode="HTML")
        return
    try:
        record_id = str(UUID(parts[1]))
    except ValueError:
        await message.answer("Неверный ID записи подарка.")
        return
    gift = await store.link_gift_to_offer(record_id, parts[2], user.id)
    if gift is None:
        await message.answer(
            "Не удалось связать подарок. Он должен принадлежать вам, а офер должен быть принят вами."
        )
        return
    await message.answer(f"Подарок связан с офером <code>{parts[2]}</code>.", parse_mode="HTML")


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
        await message.answer("Нет полученных подарков, ожидающих проверки передачи.")
        return
    lines = [
        f"• <code>{gift['record_id']}</code> — {escape(gift['gift_name'])} "
        f"(offer <code>{gift['offer_id']}</code>)"
        for gift in pending[:10]
    ]
    await message.answer(
        "Перед подтверждением проверьте каждый подарок в аккаунте банка:\n" + "\n".join(lines)
        + f"\n\nИспользуйте <code>/confirmgift GIFT_RECORD_ID</code> только после проверки @{escape(settings.bank_username)}.",
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
        await message.answer("Формат: <code>/confirmgift GIFT_RECORD_ID</code>", parse_mode="HTML")
        return
    try:
        record_id = str(UUID(parts[1]))
    except ValueError:
        await message.answer("Неверный ID записи подарка.")
        return
    gift, newly_confirmed = await store.confirm_gift_transfer(record_id)
    if gift is None:
        await message.answer("Подарок не найден, не связан с принятым офером или уже не ожидает проверки.")
        return
    if not newly_confirmed:
        await message.answer("Этот подарок уже подтверждён; повторное уведомление не создавалось.")
        return
    await deliver_pending_gift_notifications(bot, store, settings)
    await message.answer(
        f"Передача подарка на @{escape(settings.bank_username)} подтверждена. "
        "Уведомление отправлено в группу или поставлено в очередь на повторную отправку."
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
        await query.answer("Недействительный офер.", show_alert=True)
        return
    _, action, offer_id = parts
    user_id = query.from_user.id

    if action == "h":
        offer = await store.get_offer(offer_id)
        language = _normalize_offer_language(offer.get("language", "en")) if offer else "ru"
        text = _OFFER_TRANSLATIONS[language]
        creator_username = (
            await store.get_gift_recipient(offer["buyer_id"])
            if offer is not None
            else None
        )
        recipient = (
            f"@{creator_username}"
            if creator_username
            else (
                "аккаунт, настроенный автором офера"
                if language == "ru"
                else "the account configured by the offer creator"
                if language == "en"
                else "报价创建者设置的账号"
            )
        )
        await query.answer(
            text["help"].format(recipient=recipient),
            show_alert=True,
        )
        return
    if action == "t":
        offer = await store.get_offer(offer_id)
        language = _normalize_offer_language(offer.get("language", "en")) if offer else "ru"
        await query.answer(
            _OFFER_TRANSLATIONS[language]["transfer_unavailable"],
            show_alert=True,
        )
        return

    offer = None
    if action == "a":
        offer = await store.accept_offer(offer_id, user_id)
    elif action == "r":
        offer = await store.reject_offer(offer_id, user_id)
    elif action == "s":
        offer = await store.mark_gift_sent(offer_id, user_id)
    else:
        await query.answer("Неизвестное действие.", show_alert=True)
        return

    if offer:
        language = _normalize_offer_language(offer.get("language", "en"))
        text = _OFFER_TRANSLATIONS[language]
        success_text = {
            "a": text["accepted_success"],
            "r": text["declined_success"],
            "s": text["reported_success"],
        }[action]
        try:
            await _edit_offer_message(query, offer, settings, store)
        except TelegramAPIError:
            log.exception("Could not update Telegram message for offer %s", offer_id)
            await query.answer(
                text["update_failed"].format(success=success_text),
                show_alert=True,
            )
            return
        await query.answer(success_text)
        return

    current = await store.get_offer(offer_id)
    language = _normalize_offer_language(current.get("language", "en")) if current else "ru"
    text = _OFFER_TRANSLATIONS[language]
    if current and current["buyer_id"] == user_id:
        await query.answer(text["own_offer"], show_alert=True)
    elif current and current["status"] == "accepted" and current["accepted_by"] != user_id:
        await query.answer(text["already_accepted"], show_alert=True)
    elif current and current["status"] == "gift_sent":
        await query.answer(text["already_transferred"], show_alert=True)
    elif current and current["status"] == "rejected":
        await query.answer(text["already_declined"], show_alert=True)
    else:
        await query.answer(text["unavailable"], show_alert=True)
