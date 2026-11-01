from __future__ import annotations

from decimal import Decimal
from typing import Any

from .market_database import MarketDatabase


class MarketStore:
    """Persistent offer and gift records."""

    def __init__(self, database_url: str) -> None:
        self.database = MarketDatabase(database_url)

    async def initialize(self) -> None:
        await self.database.initialize()

    async def close(self) -> None:
        await self.database.close()

    async def save_business_connection(
        self,
        *,
        connection_id: str,
        business_user_id: int,
        user_chat_id: int,
        is_enabled: bool,
        can_reply: bool,
    ) -> None:
        await self.database.save_business_connection(
            connection_id=connection_id,
            business_user_id=business_user_id,
            user_chat_id=user_chat_id,
            is_enabled=is_enabled,
            can_reply=can_reply,
        )

    async def get_business_connection(self, business_user_id: int) -> dict[str, Any] | None:
        return await self.database.get_business_connection(business_user_id)

    async def get_business_offer_context(
        self,
        *,
        business_user_id: int,
        username: str | None = None,
    ) -> tuple[dict[str, Any] | None, int | None]:
        return await self.database.get_business_offer_context(
            business_user_id=business_user_id,
            username=username,
        )

    async def set_gift_recipient(self, *, user_id: int, username: str) -> None:
        await self.database.set_gift_recipient(user_id=user_id, username=username)

    async def get_gift_recipient(self, user_id: int) -> str | None:
        return await self.database.get_gift_recipient(user_id)

    async def record_business_recipient(
        self,
        *,
        connection_id: str,
        username: str,
        user_id: int,
    ) -> bool:
        return await self.database.record_business_recipient(
            connection_id=connection_id,
            username=username,
            user_id=user_id,
        )

    async def resolve_business_recipient(
        self,
        *,
        connection_id: str,
        username: str,
    ) -> int | None:
        return await self.database.resolve_business_recipient(
            connection_id=connection_id,
            username=username,
        )

    async def create_offer_token(self, **offer: Any) -> str:
        return await self.database.create_offer_token(**offer)

    async def claim_offer_token(
        self,
        *,
        token: str,
        connection_id: str,
        recipient_id: int,
    ) -> dict[str, Any] | None:
        return await self.database.claim_offer_token(
            token=token,
            connection_id=connection_id,
            recipient_id=recipient_id,
        )

    async def create_offer(
        self,
        buyer_id: int,
        buyer_label: str,
        nft_url: str,
        price: Decimal,
        language: str = "en",
        currency: str = "GRAM",
        recipient_id: int | None = None,
    ) -> dict[str, Any]:
        return await self.database.create_offer(
            buyer_id, buyer_label, nft_url, price, language, currency, recipient_id
        )

    async def get_offer(self, offer_id: str) -> dict[str, Any] | None:
        return await self.database.get_offer(offer_id)

    async def offers_for_buyer(self, buyer_id: int, limit: int = 30) -> list[dict[str, Any]]:
        return await self.database.offers_for_buyer(buyer_id, limit)

    async def accept_offer(self, offer_id: str, seller_id: int) -> dict[str, Any] | None:
        return await self.database.change_offer(
            offer_id, seller_id, "pending", "accepted", disallow_buyer=True
        )

    async def reject_offer(self, offer_id: str, seller_id: int) -> dict[str, Any] | None:
        return await self.database.change_offer(
            offer_id, seller_id, "pending", "rejected", disallow_buyer=True
        )

    async def mark_gift_sent(self, offer_id: str, seller_id: int) -> dict[str, Any] | None:
        return await self.database.change_offer(
            offer_id, seller_id, "accepted", "gift_sent", require_accepted_by=True
        )

    async def record_gift(self, **gift: Any) -> tuple[dict[str, Any], bool]:
        return await self.database.record_gift(**gift)

    async def link_gift_to_offer(
        self, record_id: str, offer_id: str, sender_id: int
    ) -> dict[str, Any] | None:
        return await self.database.link_gift_to_offer(record_id, offer_id, sender_id)

    async def pending_transfer_gifts(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self.database.pending_transfer_gifts(limit)

    async def confirm_gift_transfer(self, record_id: str) -> tuple[dict[str, Any] | None, bool]:
        return await self.database.confirm_gift_transfer(record_id)

    async def claim_pending_notifications(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self.database.claim_pending_notifications(limit)

    async def finish_notification(
        self,
        record_id: str,
        *,
        message_id: int | None = None,
        error: str | None = None,
    ) -> None:
        await self.database.finish_notification(record_id, message_id=message_id, error=error)
