import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.enums import ChatMemberStatus

from bot.config import MarketSettings
from bot.market_handlers import _inline_offer_result, _offer_text, _user_has_market_access


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

    def test_inline_offer_title_and_message_use_original_currency_labels(self) -> None:
        for currency, label in (("GRAM", "GRAM"), ("STARS", "Stars")):
            with self.subTest(currency=currency):
                result = _inline_offer_result(
                    {**self.offer, "currency": currency}, "abcdef123456"
                )

                self.assertIn(label, result.title)
                self.assertIn(label, result.input_message_content.message_text)
                self.assertNotIn("⭐", result.title)
                self.assertNotIn("⭐", result.input_message_content.message_text)
                self.assertEqual(result.input_message_content.parse_mode, "HTML")


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


if __name__ == "__main__":
    unittest.main()
