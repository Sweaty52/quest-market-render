import unittest
from decimal import Decimal

from bot.market_handlers import _inline_offer_result, _offer_text


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


if __name__ == "__main__":
    unittest.main()
