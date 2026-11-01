import asyncio
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

from bot.config import MarketSettings
from bot.market_handlers import (
    _BUY_DRAFT_RE,
    _BUY_RE,
    _delete_business_command,
    _edit_offer_message,
    _gift_recipient_url,
    _offer_text,
    _pending_keyboard,
    _user_has_market_access,
    configure_gift_recipient,
    create_buy_offer,
    offer_action,
    remember_business_recipient,
    update_business_connection,
)


class OfferCurrencyLabelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.offer = {
            "status": "pending",
            "price": Decimal("500"),
            "nft_url": "https://t.me/nft/LootBag-4767",
            "buyer_label": "@buyer",
        }

    def test_offer_text_uses_original_currency_labels(self) -> None:
        for currency, label in (("GRAM", "GRAM"), ("STARS", "Stars")):
            with self.subTest(currency=currency):
                text = _offer_text({**self.offer, "currency": currency})

                self.assertIn(f"500 {label}", text)
                self.assertNotIn("⭐", text)
                self.assertNotIn("<tg-emoji", text)

    def test_offer_text_is_translated_for_russian_english_and_chinese(self) -> None:
        expected = {
            "ru": ("предлагает вам", "Принять", "Отклонить"),
            "en": ("offers you", "Accept", "Decline"),
            "zh": ("向你发出报价", "接受", "拒绝"),
        }
        for language, (message, accept, decline) in expected.items():
            with self.subTest(language=language):
                offer = {**self.offer, "currency": "STARS", "language": language}
                text = _offer_text(offer)
                keyboard = _pending_keyboard("abcdef123456", language)

                self.assertIn(message, text)
                self.assertEqual(keyboard.inline_keyboard[0][0].text, decline)
                self.assertEqual(keyboard.inline_keyboard[0][1].text, accept)

    def test_business_offer_uses_the_configured_custom_emoji(self) -> None:
        expected_ids = {
            "GRAM": ("5264766603584641330", "🪙", "GRAM"),
            "STARS": ("5220029283631672529", "⭐", "Stars"),
        }
        for currency, (emoji_id, fallback, label) in expected_ids.items():
            with self.subTest(currency=currency):
                text = _offer_text(
                    {**self.offer, "currency": currency},
                    use_business_emojis=True,
                )

                self.assertIn(f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>', text)
                self.assertNotIn(f"500 {label}", text)

    def test_personal_gift_recipient_builds_telegram_send_gift_link(self) -> None:
        settings = MarketSettings(market_bot_token="test-token")

        self.assertEqual(
            _gift_recipient_url("chosen_account", settings),
            "tg://send_gift?to=chosen_account",
        )
        self.assertEqual(
            _gift_recipient_url(None, settings),
            settings.market_account_url,
        )

    def test_buy_command_accepts_a_username_or_numeric_recipient_id(self) -> None:
        match = _BUY_RE.fullmatch(
            ".buy @some_user https://t.me/nft/Collectible-123 12.5 gram"
        )
        self.assertIsNotNone(match)
        self.assertEqual(
            match.groups(),
            (
                None,
                None,
                "@some_user",
                "https://t.me/nft/Collectible-123",
                "12.5",
                "gram",
                None,
            ),
        )
        self.assertIsNotNone(
            _BUY_RE.fullmatch(".buy 123456789 https://t.me/nft/Collectible-123 12.5 gram")
        )
        self.assertIsNone(
            _BUY_RE.fullmatch(".buy @bad!name https://t.me/nft/Collectible-123 12.5 gram")
        )
        self.assertIsNotNone(
            _BUY_RE.fullmatch(".buy zh @some_user https://t.me/nft/Collectible-123 12.5 gram")
        )
        self.assertIsNotNone(
            _BUY_RE.fullmatch(".buy @some_user https://t.me/nft/Collectible-123 12.5 gram eng")
        )
        self.assertIsNotNone(
            _BUY_DRAFT_RE.fullmatch(".buy ru https://t.me/nft/Collectible-123 12.5 gram")
        )


class MarketChatAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = MarketSettings(
            market_bot_token="test-token",
            market_allowed_user_ids="42",
            market_access_chat_id=-1004447182501,
        )
        self.bot = SimpleNamespace(get_chat_member=AsyncMock())

    async def test_allowlisted_user_has_access_without_chat_lookup(self) -> None:
        self.assertTrue(await _user_has_market_access(42, self.bot, self.settings))
        self.bot.get_chat_member.assert_not_awaited()

    async def test_chat_member_has_access(self) -> None:
        self.bot.get_chat_member.return_value = SimpleNamespace(status=ChatMemberStatus.MEMBER)

        self.assertTrue(await _user_has_market_access(43, self.bot, self.settings))
        self.bot.get_chat_member.assert_awaited_once_with(-1004447182501, 43)

    async def test_non_member_does_not_have_access(self) -> None:
        self.bot.get_chat_member.return_value = SimpleNamespace(status=ChatMemberStatus.LEFT)

        self.assertFalse(await _user_has_market_access(43, self.bot, self.settings))


class BusinessOfferTests(unittest.IsolatedAsyncioTestCase):
    async def test_how_it_works_says_buyer_without_username(self) -> None:
        query = SimpleNamespace(
            data="qm:h:abcdef123456",
            from_user=SimpleNamespace(id=42),
            answer=AsyncMock(),
        )
        store = SimpleNamespace(
            get_offer=AsyncMock(return_value={
                "id": "abcdef123456",
                "buyer_id": 42,
                "language": "ru",
            }),
        )

        await offer_action(query, store, MarketSettings(market_bot_token="test-token"))

        self.assertIn("покупателю", query.answer.await_args.args[0])
        self.assertNotIn("@", query.answer.await_args.args[0])
        store.get_offer.assert_awaited_once_with("abcdef123456")

    async def test_authorized_user_can_set_gift_recipient(self) -> None:
        settings = MarketSettings(
            market_bot_token="test-token",
            market_allowed_user_ids="42",
        )
        store = SimpleNamespace(set_gift_recipient=AsyncMock())
        message = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.PRIVATE),
            text="/giftrecipient @chosen_account",
            from_user=SimpleNamespace(id=42, username="user"),
            answer=AsyncMock(),
        )

        await configure_gift_recipient(message, SimpleNamespace(), store, settings)

        store.set_gift_recipient.assert_awaited_once_with(
            user_id=42,
            username="@chosen_account",
        )
        self.assertIn("@chosen_account", message.answer.await_args.args[0])

    async def test_accepted_offer_uses_creator_personal_gift_recipient(self) -> None:
        settings = MarketSettings(market_bot_token="test-token")
        offer = {
            "id": "abcdef123456",
            "buyer_id": 42,
            "status": "accepted",
            "price": Decimal("12.5"),
            "currency": "GRAM",
            "nft_url": "https://t.me/nft/Collectible-123",
            "buyer_label": "@creator",
        }
        bot = SimpleNamespace(edit_message_text=AsyncMock())
        query = SimpleNamespace(
            bot=bot,
            inline_message_id="inline-message-id",
            message=None,
        )
        store = SimpleNamespace(get_gift_recipient=AsyncMock(return_value="chosen_account"))

        await _edit_offer_message(query, offer, settings, store)

        store.get_gift_recipient.assert_awaited_once_with(42)
        markup = bot.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].url, "tg://send_gift?to=chosen_account")

    async def test_unauthorized_user_cannot_set_gift_recipient(self) -> None:
        settings = MarketSettings(
            market_bot_token="test-token",
            market_allowed_user_ids="42",
            market_access_chat_id=-1004447182501,
        )
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status=ChatMemberStatus.LEFT))
        )
        store = SimpleNamespace(set_gift_recipient=AsyncMock())
        message = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.PRIVATE),
            text="/giftrecipient @chosen_account",
            from_user=SimpleNamespace(id=43, username="user"),
            answer=AsyncMock(),
        )

        await configure_gift_recipient(message, bot, store, settings)

        store.set_gift_recipient.assert_not_awaited()
        message.answer.assert_awaited_once()

    async def test_invalid_gift_recipient_username_is_rejected(self) -> None:
        settings = MarketSettings(
            market_bot_token="test-token",
            market_allowed_user_ids="42",
        )
        store = SimpleNamespace(set_gift_recipient=AsyncMock())
        message = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.PRIVATE),
            text="/giftrecipient @bad!name",
            from_user=SimpleNamespace(id=42, username="user"),
            answer=AsyncMock(),
        )

        await configure_gift_recipient(message, SimpleNamespace(), store, settings)

        store.set_gift_recipient.assert_not_awaited()
        self.assertIn("Формат:", message.answer.await_args.args[0])

    async def test_business_code_message_is_deleted_before_claiming(self) -> None:
        bot = SimpleNamespace(
            delete_message=AsyncMock(return_value=True),
            delete_business_messages=AsyncMock(),
            send_message=AsyncMock(),
        )
        store = SimpleNamespace(
            claim_offer_token=AsyncMock(return_value=None),
            record_business_recipient=AsyncMock(),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.PRIVATE, id=900),
            business_connection_id="business-connection-1",
            message_id=321,
            text=".qm 0123456789abcdef",
            from_user=SimpleNamespace(id=900, username="recipient"),
        )

        await remember_business_recipient(message, bot, store)

        bot.delete_message.assert_awaited_once_with(chat_id=900, message_id=321)
        bot.delete_business_messages.assert_not_awaited()
        bot.send_message.assert_awaited_once()

    async def test_business_code_deletion_falls_back_to_business_delete(self) -> None:
        bot = SimpleNamespace(
            delete_message=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=SimpleNamespace(),
                    message="not deletable",
                )
            ),
            delete_business_messages=AsyncMock(return_value=True),
        )
        message = SimpleNamespace(
            business_connection_id="business-connection-1",
            message_id=321,
            chat=SimpleNamespace(id=900),
        )

        self.assertTrue(await _delete_business_command(message, bot))
        bot.delete_business_messages.assert_awaited_once_with(
            business_connection_id="business-connection-1",
            message_ids=[321],
        )

    async def test_business_connection_reads_reply_right_from_rights_object(self) -> None:
        store = SimpleNamespace(save_business_connection=AsyncMock())
        connection = SimpleNamespace(
            id="business-connection-1",
            user=SimpleNamespace(id=42),
            user_chat_id=42,
            is_enabled=True,
            can_reply=None,
            rights=SimpleNamespace(can_reply=True),
        )

        await update_business_connection(connection, store)

        store.save_business_connection.assert_awaited_once_with(
            connection_id="business-connection-1",
            business_user_id=42,
            user_chat_id=42,
            is_enabled=True,
            can_reply=True,
        )

    async def test_offer_is_sent_as_the_creators_business_connection(self) -> None:
        settings = MarketSettings(
            market_bot_token="test-token",
            market_allowed_user_ids="42",
        )
        offer = {
            "id": "abcdef123456",
            "status": "pending",
            "price": Decimal("12.5"),
            "currency": "GRAM",
            "language": "zh",
            "nft_url": "https://t.me/nft/Collectible-123",
            "buyer_label": "@creator",
        }
        bot = SimpleNamespace(send_message=AsyncMock())
        store = SimpleNamespace(
            get_business_offer_context=AsyncMock(
                return_value=({"id": "business-connection-1"}, 123456789)
            ),
            create_offer=AsyncMock(return_value=offer),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.PRIVATE),
            text=".buy zh @some_user https://t.me/nft/Collectible-123 12.5 gram",
            from_user=SimpleNamespace(id=42, username="creator", full_name="Creator"),
            answer=AsyncMock(),
        )

        await create_buy_offer(message, bot, store, settings)

        sent = bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], 123456789)
        self.assertEqual(sent["business_connection_id"], "business-connection-1")
        self.assertIn('emoji-id="5264766603584641330"', sent["text"])
        self.assertIn("向你发出报价", sent["text"])
        self.assertEqual(sent["reply_markup"].inline_keyboard[0][1].text, "接受")
        self.assertEqual(sent["reply_markup"].inline_keyboard[0][1].callback_data, "qm:a:abcdef123456")
        self.assertEqual(
            store.create_offer.await_args.kwargs["recipient_id"],
            123456789,
        )
        self.assertEqual(store.create_offer.await_args.kwargs["language"], "zh")
        store.get_business_offer_context.assert_awaited_once_with(
            business_user_id=42,
            username="some_user",
        )
        message.answer.assert_awaited_once()

    async def test_offer_access_and_business_context_checks_run_concurrently(self) -> None:
        settings = MarketSettings(
            market_bot_token="test-token",
            market_access_chat_id=-1004447182501,
        )
        both_started = asyncio.Event()
        started: set[str] = set()

        async def wait_for_both(name: str) -> None:
            started.add(name)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.5)

        async def check_membership(*_: object) -> SimpleNamespace:
            await wait_for_both("membership")
            return SimpleNamespace(status=ChatMemberStatus.MEMBER)

        async def get_offer_context(**_: object) -> tuple[dict, None]:
            await wait_for_both("database")
            return {"id": "business-connection-1"}, None

        bot = SimpleNamespace(
            get_chat_member=AsyncMock(side_effect=check_membership),
            send_message=AsyncMock(),
        )
        store = SimpleNamespace(
            get_business_offer_context=AsyncMock(side_effect=get_offer_context),
            create_offer=AsyncMock(return_value={
                "id": "abcdef123456",
                "status": "pending",
                "price": Decimal("12.5"),
                "currency": "GRAM",
                "language": "ru",
                "nft_url": "https://t.me/nft/Collectible-123",
                "buyer_label": "@creator",
            }),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.PRIVATE),
            text=".buy 123456 https://t.me/nft/Collectible-123 12.5 gram",
            from_user=SimpleNamespace(id=42, username="creator", full_name="Creator"),
            answer=AsyncMock(),
        )

        await create_buy_offer(message, bot, store, settings)

        self.assertEqual(started, {"membership", "database"})
        store.create_offer.assert_awaited_once()
        bot.send_message.assert_awaited_once()

    async def test_unknown_username_does_not_create_an_offer(self) -> None:
        settings = MarketSettings(
            market_bot_token="test-token",
            market_allowed_user_ids="42",
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(),
            delete_message=AsyncMock(return_value=True),
            delete_business_messages=AsyncMock(),
        )
        store = SimpleNamespace(
            get_business_offer_context=AsyncMock(
                return_value=({"id": "business-connection-1"}, None)
            ),
            create_offer=AsyncMock(),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.PRIVATE),
            text=".buy @unknown https://t.me/nft/Collectible-123 12.5 gram",
            from_user=SimpleNamespace(id=42, username="creator", full_name="Creator"),
            answer=AsyncMock(),
        )

        await create_buy_offer(message, bot, store, settings)

        store.create_offer.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        message.answer.assert_awaited_once()

    async def test_buy_without_recipient_creates_a_one_time_business_command(self) -> None:
        settings = MarketSettings(
            market_bot_token="test-token",
            market_allowed_user_ids="42",
        )
        store = SimpleNamespace(
            get_business_offer_context=AsyncMock(
                return_value=({"id": "business-connection-1"}, None)
            ),
            create_offer_token=AsyncMock(return_value="0123456789abcdef"),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.PRIVATE),
            text=".buy https://t.me/nft/Collectible-123 12.5 gram",
            from_user=SimpleNamespace(id=42, username="creator", full_name="Creator"),
            answer=AsyncMock(),
        )

        await create_buy_offer(message, SimpleNamespace(), store, settings)

        store.create_offer_token.assert_awaited_once_with(
            connection_id="business-connection-1",
            creator_id=42,
            buyer_label="@creator",
            nft_url="https://t.me/nft/Collectible-123",
            price=Decimal("12.5"),
            currency="GRAM",
            language="ru",
        )
        self.assertIn(".qm 0123456789abcdef", message.answer.await_args.args[0])

    async def test_business_code_uses_private_chat_id_as_recipient(self) -> None:
        offer = {
            "id": "abcdef123456",
            "buyer_id": 42,
            "status": "pending",
            "price": Decimal("12.5"),
            "currency": "GRAM",
            "nft_url": "https://t.me/nft/Collectible-123",
            "buyer_label": "@creator",
        }
        bot = SimpleNamespace(
            send_message=AsyncMock(),
            delete_business_messages=AsyncMock(),
            delete_message=AsyncMock(return_value=True),
        )
        store = SimpleNamespace(
            claim_offer_token=AsyncMock(return_value=offer),
            record_business_recipient=AsyncMock(),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.PRIVATE, id=900),
            business_connection_id="business-connection-1",
            message_id=321,
            text=".qm 0123456789abcdef",
            from_user=SimpleNamespace(id=42, username="creator"),
        )

        await remember_business_recipient(message, bot, store)

        store.claim_offer_token.assert_awaited_once_with(
            token="0123456789abcdef",
            connection_id="business-connection-1",
            recipient_id=900,
        )
        store.record_business_recipient.assert_not_awaited()
        bot.delete_message.assert_awaited_once_with(
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
        sent = bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], 900)
        self.assertEqual(sent["business_connection_id"], "business-connection-1")
        self.assertEqual(sent["reply_markup"].inline_keyboard[0][1].callback_data, "qm:a:abcdef123456")


if __name__ == "__main__":
    unittest.main()
