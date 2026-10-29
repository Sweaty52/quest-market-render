import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage

from bot.config import MarketSettings
from bot.gift_notifications import deliver_pending_gift_notifications, transfer_notification_text
from bot.market_handlers import _gift_details, receive_gift
from bot.market_store import MarketStore


class GiftDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite+aiosqlite:///{Path(self.directory.name) / 'market.db'}"
        self.store = MarketStore(self.database_url)
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.directory.cleanup()

    async def _accepted_offer(self, seller_id: int = 200) -> dict:
        offer = await self.store.create_offer(
            buyer_id=100,
            buyer_label="@offer_creator",
            nft_url="https://t.me/nft/Collectible-12",
            price=Decimal("12.5"),
            currency="GRAM",
        )
        accepted = await self.store.accept_offer(offer["id"], seller_id)
        self.assertIsNotNone(accepted)
        return offer

    async def _receive_gift(
        self,
        *,
        sender_id: int = 200,
        source_message_id: int = 501,
        owned_gift_id: str | None = "owned-unique-1",
    ) -> tuple[dict, bool]:
        return await self.store.record_gift(
            sender_id=sender_id,
            sender_username="seller",
            sender_name="Gift Seller",
            source_chat_id=sender_id,
            source_message_id=source_message_id,
            telegram_gift_id=None,
            telegram_owned_gift_id=owned_gift_id,
            gift_name="Collectible #1",
            gift_url="https://t.me/nft/Collectible-1",
            model_name="Blue",
            backdrop_name="Night",
            symbol_name="Star",
            metadata={"unique_gift": {"name": "Collectible", "number": 1}},
            received_at=datetime(2026, 10, 29, 10, 30, tzinfo=timezone.utc),
        )

    async def test_gift_is_deduplicated_and_snapshots_accepted_offer(self) -> None:
        offer = await self._accepted_offer()
        gift, created = await self._receive_gift()
        duplicate, duplicate_created = await self._receive_gift(source_message_id=502)

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(gift["record_id"], duplicate["record_id"])
        self.assertEqual(gift["status"], "received")
        self.assertEqual(gift["offer_id"], offer["id"])
        self.assertEqual(gift["offer_creator_id"], 100)
        self.assertEqual(gift["offer_creator_username"], "offer_creator")
        self.assertEqual(gift["offer_price"], Decimal("12.500000000"))
        self.assertEqual(gift["model_name"], "Blue")

    async def test_message_identity_deduplicates_non_unique_gifts(self) -> None:
        gift, created = await self._receive_gift(owned_gift_id=None)
        duplicate, duplicate_created = await self._receive_gift(owned_gift_id=None)

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(gift["record_id"], duplicate["record_id"])

    async def test_gift_requires_unambiguous_offer_or_explicit_link(self) -> None:
        first_offer = await self._accepted_offer()
        second_offer = await self.store.create_offer(
            buyer_id=101,
            buyer_label="Creator without username",
            nft_url="https://t.me/nft/Collectible-13",
            price=Decimal("500"),
            currency="STARS",
        )
        await self.store.accept_offer(second_offer["id"], 200)

        gift, _ = await self._receive_gift()
        self.assertIsNone(gift["offer_id"])

        linked = await self.store.link_gift_to_offer(gift["record_id"], first_offer["id"], 200)
        self.assertIsNotNone(linked)
        self.assertEqual(linked["offer_creator_id"], 100)
        self.assertEqual(linked["offer_creator_username"], "offer_creator")
        self.assertEqual(linked["offer_id"], first_offer["id"])

    async def test_transfer_confirmation_is_idempotent_and_notification_retriable(self) -> None:
        await self._accepted_offer()
        gift, _ = await self._receive_gift()

        confirmed, newly_confirmed = await self.store.confirm_gift_transfer(gift["record_id"])
        self.assertIsNotNone(confirmed)
        self.assertTrue(newly_confirmed)
        self.assertEqual(confirmed["status"], "transferred")
        self.assertIsNotNone(confirmed["transferred_at"])

        first_claim = await self.store.claim_pending_notifications()
        self.assertEqual([item["record_id"] for item in first_claim], [gift["record_id"]])
        repeated_confirmation, repeated = await self.store.confirm_gift_transfer(gift["record_id"])
        self.assertFalse(repeated)
        self.assertEqual(repeated_confirmation["record_id"], gift["record_id"])
        self.assertEqual(await self.store.claim_pending_notifications(), [])

        await self.store.finish_notification(gift["record_id"], error="Temporary group error")
        retry = await self.store.claim_pending_notifications()
        self.assertEqual([item["record_id"] for item in retry], [gift["record_id"]])
        await self.store.finish_notification(gift["record_id"], message_id=900)
        self.assertEqual(await self.store.claim_pending_notifications(), [])

    async def test_gift_and_offer_survive_store_restart(self) -> None:
        offer = await self._accepted_offer()
        gift, _ = await self._receive_gift()
        await self.store.close()

        reopened = MarketStore(self.database_url)
        await reopened.initialize()
        try:
            persisted_offer = await reopened.get_offer(offer["id"])
            persisted_gift, created = await reopened.record_gift(
                sender_id=200,
                sender_username="seller",
                sender_name="Gift Seller",
                source_chat_id=200,
                source_message_id=501,
                telegram_gift_id=None,
                telegram_owned_gift_id="owned-unique-1",
                gift_name="Collectible #1",
                gift_url="https://t.me/nft/Collectible-1",
                model_name="Blue",
                backdrop_name="Night",
                symbol_name="Star",
                metadata={"unique_gift": {"name": "Collectible", "number": 1}},
                received_at=datetime(2026, 10, 29, 10, 30, tzinfo=timezone.utc),
            )
        finally:
            await reopened.close()

        self.assertEqual(persisted_offer["id"], offer["id"])
        self.assertEqual(persisted_gift["record_id"], gift["record_id"])
        self.assertFalse(created)

    async def test_confirmed_transfer_is_sent_to_the_configured_group(self) -> None:
        await self._accepted_offer()
        gift, _ = await self._receive_gift()
        await self.store.confirm_gift_transfer(gift["record_id"])
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=900))
        )
        settings = MarketSettings(
            market_bot_token="test-token",
            offers_group_id=-1004447182501,
        )

        await deliver_pending_gift_notifications(bot, self.store, settings)

        bot.send_message.assert_awaited_once()
        sent = bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], -1004447182501)
        self.assertIn("Немає оцінки", sent["text"])
        self.assertEqual(await self.store.claim_pending_notifications(), [])

    async def test_failed_telegram_send_is_retried(self) -> None:
        await self._accepted_offer()
        gift, _ = await self._receive_gift()
        await self.store.confirm_gift_transfer(gift["record_id"])
        bot = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=TelegramNetworkError(
                    method=SendMessage(chat_id=-1004447182501, text="notification"),
                    message="temporary network failure",
                )
            )
        )
        settings = MarketSettings(
            market_bot_token="test-token",
            offers_group_id=-1004447182501,
        )

        with self.assertLogs("bot.gift_notifications", level="ERROR"):
            await deliver_pending_gift_notifications(bot, self.store, settings)
        bot.send_message.side_effect = None
        bot.send_message.return_value = SimpleNamespace(message_id=901)

        await deliver_pending_gift_notifications(bot, self.store, settings)

        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(await self.store.claim_pending_notifications(), [])

    def test_notification_does_not_invent_a_value(self) -> None:
        text = transfer_notification_text(
            {
                "offer_creator_username": None,
                "offer_creator_name": "Creator Name",
                "offer_creator_id": 100,
                "sender_name": "Seller",
                "sender_id": 200,
                "gift_name": "Collectible #1",
                "gift_url": None,
                "estimated_value_uah": None,
                "transferred_at": datetime(2026, 10, 29, 10, 30, tzinfo=timezone.utc),
            },
            "questbankk",
        )

        self.assertIn("Creator Name (ID: <code>100</code>)", text)
        self.assertIn("Немає оцінки", text)
        self.assertIn("Немає посилання", text)
        self.assertNotIn("≈", text)

    def test_unique_gift_metadata_and_link_use_only_telegram_fields(self) -> None:
        info = SimpleNamespace(
            gift=SimpleNamespace(
                base_name="TestGift",
                name="Test Gift",
                number=12,
                model=SimpleNamespace(name="Cloud"),
                backdrop=SimpleNamespace(name="Blue"),
                symbol=SimpleNamespace(name="Star"),
            ),
            owned_gift_id="owned-12",
            model_dump=lambda **_: {"owned_gift_id": "owned-12"},
        )
        message = SimpleNamespace(unique_gift=info, gift=None)

        details = _gift_details(message)

        self.assertEqual(details["gift_name"], "Test Gift #12")
        self.assertEqual(details["gift_url"], "https://t.me/nft/TestGift-12")
        self.assertEqual(details["model_name"], "Cloud")
        self.assertEqual(details["backdrop_name"], "Blue")
        self.assertEqual(details["symbol_name"], "Star")

    async def test_unauthorized_user_cannot_register_a_gift(self) -> None:
        settings = MarketSettings(
            market_bot_token="test-token",
            market_access_chat_id=-1004447182501,
        )
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status=ChatMemberStatus.LEFT))
        )
        store = SimpleNamespace(record_gift=AsyncMock())
        message = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.PRIVATE, id=43),
            from_user=SimpleNamespace(id=43, username="not_allowed", full_name="Not Allowed"),
            unique_gift=SimpleNamespace(gift=SimpleNamespace()),
            gift=None,
            answer=AsyncMock(),
        )

        await receive_gift(message, bot, store, settings)

        store.record_gift.assert_not_awaited()
        message.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
