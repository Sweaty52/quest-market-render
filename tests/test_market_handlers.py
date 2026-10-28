import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.market_handlers import _currency_emoji_alts, _inline_offer_result, _offer_text


class OfferCurrencyEmojiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.offer = {
            "status": "pending",
            "price": Decimal("500"),
            "nft_url": "https://t.me/nft/LootBag-4767",
            "buyer_label": "@buyer",
        }

    def test_offer_uses_custom_emoji_without_currency_names(self) -> None:
        for currency, emoji_id, sticker_emoji in (
            ("GRAM", "5264766603584641330", "🪙"),
            ("STARS", "5220029283631672529", "🌟"),
        ):
            with self.subTest(currency=currency):
                offer = {**self.offer, "currency": currency}
                text = _offer_text(offer, emoji_alts={emoji_id: sticker_emoji})

                self.assertIn(f'<tg-emoji emoji-id="{emoji_id}">{sticker_emoji}</tg-emoji>', text)
                self.assertNotIn("GRAM", text)
                self.assertNotIn("STARS", text)

    def test_inline_offer_has_standard_emoji_fallback(self) -> None:
        for currency, fallback in (("GRAM", "💎"), ("STARS", "⭐")):
            with self.subTest(currency=currency):
                offer = {**self.offer, "currency": currency}
                result = _inline_offer_result(
                    offer, "abcdef123456", {}, custom_emoji=False
                )

                self.assertIn(f"500 {fallback}", result.input_message_content.message_text)
                self.assertNotIn("<tg-emoji", result.input_message_content.message_text)


class CurrencyEmojiMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_the_alt_emoji_for_each_custom_emoji_id(self) -> None:
        expected = {
            "5264766603584641330": "🪙",
            "5220029283631672529": "🌟",
        }
        bot = SimpleNamespace(
            get_custom_emoji_stickers=AsyncMock(
                return_value=[
                    SimpleNamespace(custom_emoji_id=emoji_id, emoji=emoji)
                    for emoji_id, emoji in expected.items()
                ]
            )
        )

        with patch("bot.market_handlers._CURRENCY_EMOJI_ALTS", None):
            emoji_alts = await _currency_emoji_alts(bot)

        self.assertEqual(emoji_alts, expected)
        bot.get_custom_emoji_stickers.assert_awaited_once_with(
            custom_emoji_ids=list(expected)
        )


if __name__ == "__main__":
    unittest.main()
