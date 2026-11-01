import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from sqlalchemy import inspect

from bot.market_store import MarketStore


class MarketDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite+aiosqlite:///{Path(self.directory.name) / 'market.db'}"
        self.store = MarketStore(self.database_url)
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.directory.cleanup()

    async def _create_offer(self, recipient_id: int | None = None) -> dict:
        return await self.store.create_offer(
            buyer_id=100,
            buyer_label="@offer_creator",
            nft_url="https://t.me/nft/Collectible-12",
            price=Decimal("12.5"),
            currency="GRAM",
            recipient_id=recipient_id,
        )

    async def test_startup_drops_gift_logs_and_preserves_offers(self) -> None:
        offer = await self._create_offer()
        async with self.store.database.engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE market_gifts (record_id TEXT PRIMARY KEY, gift_name TEXT NOT NULL)"
            )
            await connection.exec_driver_sql(
                "INSERT INTO market_gifts (record_id, gift_name) VALUES ('old-record', 'Old gift')"
            )

        await self.store.initialize()

        async with self.store.database.engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names()
            )
        self.assertNotIn("market_gifts", tables)
        self.assertEqual((await self.store.get_offer(offer["id"]))["id"], offer["id"])

    async def test_targeted_offer_can_only_be_accepted_by_its_recipient(self) -> None:
        offer = await self._create_offer(recipient_id=200)

        self.assertIsNone(await self.store.accept_offer(offer["id"], 201))
        accepted = await self.store.accept_offer(offer["id"], 200)

        self.assertIsNotNone(accepted)
        self.assertEqual(accepted["recipient_id"], 200)

    async def test_startup_adds_recipient_column_to_existing_offers_table(self) -> None:
        async with self.store.database.engine.begin() as connection:
            await connection.exec_driver_sql("ALTER TABLE market_offers DROP COLUMN recipient_id")
        await self.store.initialize()

        async with self.store.database.engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"] for column in inspect(sync_connection).get_columns("market_offers")
                }
            )
        self.assertIn("recipient_id", columns)

    async def test_business_connection_and_recipient_lookup_survive_restart(self) -> None:
        await self.store.save_business_connection(
            connection_id="business-connection-1",
            business_user_id=900,
            user_chat_id=900,
            is_enabled=True,
            can_reply=True,
        )
        await self.store.record_business_recipient(
            connection_id="business-connection-1",
            username="KnownBuyer",
            user_id=901,
        )
        await self.store.close()
        self.store = MarketStore(self.database_url)
        await self.store.initialize()

        connection = await self.store.get_business_connection(900)
        self.assertEqual(connection["id"], "business-connection-1")
        offer_connection, recipient_id = await self.store.get_business_offer_context(
            business_user_id=900,
            username="@KNOWNBUYER",
        )
        self.assertEqual(offer_connection["id"], "business-connection-1")
        self.assertEqual(recipient_id, 901)

    async def test_gift_recipient_preference_survives_restart_and_updates(self) -> None:
        await self.store.set_gift_recipient(user_id=900, username="@QuestBank")
        self.assertEqual(await self.store.get_gift_recipient(900), "questbank")

        await self.store.close()
        self.store = MarketStore(self.database_url)
        await self.store.initialize()

        self.assertEqual(await self.store.get_gift_recipient(900), "questbank")
        await self.store.set_gift_recipient(user_id=900, username="NewBank")
        self.assertEqual(await self.store.get_gift_recipient(900), "newbank")

    async def test_offer_token_is_connection_bound_and_one_time(self) -> None:
        await self.store.save_business_connection(
            connection_id="business-connection-1",
            business_user_id=900,
            user_chat_id=900,
            is_enabled=True,
            can_reply=True,
        )
        token = await self.store.create_offer_token(
            connection_id="business-connection-1",
            creator_id=900,
            buyer_label="@creator",
            nft_url="https://t.me/nft/Collectible-12",
            price=Decimal("12.5"),
            currency="GRAM",
            language="en",
        )

        self.assertIsNone(
            await self.store.claim_offer_token(
                token=token,
                connection_id="another-connection",
                recipient_id=901,
            )
        )
        claimed = await self.store.claim_offer_token(
            token=token,
            connection_id="business-connection-1",
            recipient_id=901,
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["recipient_id"], 901)
        self.assertIsNone(
            await self.store.claim_offer_token(
                token=token,
                connection_id="business-connection-1",
                recipient_id=902,
            )
        )

    async def test_unrecognized_business_connection_cannot_add_recipient(self) -> None:
        recorded = await self.store.record_business_recipient(
            connection_id="not-connected",
            username="untrusted",
            user_id=901,
        )

        self.assertFalse(recorded)
