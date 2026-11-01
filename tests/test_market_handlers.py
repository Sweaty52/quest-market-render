import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.enums import ChatMemberStatus, ChatType

from bot.config import MarketSettings
from bot.market_handlers import (
    _BUY_RE,
    _offer_text,
    _user_has_market_access,
    create_buy_offer,
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

    def test_buy_command_accepts_a_username_or_numeric_recipient_id(self) -> None:
        match = _BUY_RE.fullmatch(
            ".buy @some_user https://t.me/nft/Collectible-123 12.5 gram"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.groups(), (None, "@some_user", "https://t.me/nft/Collectible-123", "12.5", "gram"))
        self.assertIsNotNone(
            _BUY_RE.fullmatch(".buy 123456789 https://t.me/nft/Collectible-123 12.5 gram")
        )
        self.assertIsNone(
            _BUY_RE.fullmatch(".buy @bad!name https://t.me/nft/Collectible-123 12.5 gram")
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
            "nft_url": "https://t.me/nft/Collectible-123",
            "buyer_label": "@creator",
        }
        bot = SimpleNamespace(send_message=AsyncMock())
        store = SimpleNamespace(
            get_business_connection=AsyncMock(
                return_value={"id": "business-connection-1"}
            ),
            resolve_business_recipient=AsyncMock(return_value=123456789),
            create_offer=AsyncMock(return_value=offer),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.PRIVATE),
            text=".buy @some_user https://t.me/nft/Collectible-123 12.5 gram",
            from_user=SimpleNamespace(id=42, username="creator", full_name="Creator"),
            answer=AsyncMock(),
        )

        await create_buy_offer(message, bot, store, settings)

        sent = bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], 123456789)
        self.assertEqual(sent["business_connection_id"], "business-connection-1")
        self.assertIn('emoji-id="5264766603584641330"', sent["text"])
        self.assertEqual(sent["reply_markup"].inline_keyboard[0][1].callback_data, "qm:a:abcdef123456")
        self.assertEqual(
            store.create_offer.await_args.kwargs["recipient_id"],
            123456789,
        )
        store.resolve_business_recipient.assert_awaited_once_with(
            connection_id="business-connection-1",
            username="some_user",
        )
        message.answer.assert_awaited_once()

    async def test_unknown_username_does_not_create_an_offer(self) -> None:
        settings = MarketSettings(
            market_bot_token="test-token",
            market_allowed_user_ids="42",
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        store = SimpleNamespace(
            get_business_connection=AsyncMock(return_value={"id": "business-connection-1"}),
            resolve_business_recipient=AsyncMock(return_value=None),
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
            get_business_connection=AsyncMock(return_value={"id": "business-connection-1"}),
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
            language="en",
        )
        self.assertIn(".qm 0123456789abcdef", message.answer.await_args.args[0])

    async def test_business_code_claim_sends_offer_to_the_sender_and_is_not_remembered_as_username(self) -> None:
        offer = {
            "id": "abcdef123456",
            "buyer_id": 42,
            "status": "pending",
            "price": Decimal("12.5"),
            "currency": "GRAM",
            "nft_url": "https://t.me/nft/Collectible-123",
            "buyer_label": "@creator",
        }
        bot = SimpleNamespace(send_message=AsyncMock())
        store = SimpleNamespace(
            claim_offer_token=AsyncMock(return_value=offer),
            record_business_recipient=AsyncMock(),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.PRIVATE, id=900),
            business_connection_id="business-connection-1",
            text=".qm 0123456789abcdef",
            from_user=SimpleNamespace(id=900, username="recipient"),
        )

        await remember_business_recipient(message, bot, store)

        store.claim_offer_token.assert_awaited_once_with(
            token="0123456789abcdef",
            connection_id="business-connection-1",
            recipient_id=900,
        )
        store.record_business_recipient.assert_not_awaited()
        sent = bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], 900)
        self.assertEqual(sent["business_connection_id"], "business-connection-1")
        self.assertEqual(sent["reply_markup"].inline_keyboard[0][1].callback_data, "qm:a:abcdef123456")


if __name__ == "__main__":
    unittest.main()
