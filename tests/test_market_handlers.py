import unittest
from decimal import Decimal

from bot.market_handlers import _inline_offer_result, _offer_text


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


if __name__ == "__main__":
    unittest.main()
