from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    and_,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.schema import Column

_metadata = MetaData()

offers = Table(
    "market_offers",
    _metadata,
    Column("id", String(12), primary_key=True),
    Column("buyer_id", BigInteger, nullable=False),
    Column("buyer_label", String(120), nullable=False),
    Column("buyer_username", String(64)),
    Column("nft_url", Text, nullable=False),
    Column("price", Numeric(30, 9), nullable=False),
    Column("currency", String(8), nullable=False),
    Column("language", String(8), nullable=False),
    Column("status", String(24), nullable=False),
    Column("accepted_by", BigInteger),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("status IN ('pending', 'accepted', 'rejected', 'gift_sent', 'expired')"),
    CheckConstraint("price > 0"),
    Index("ix_market_offers_buyer_created", "buyer_id", "created_at"),
    Index("ix_market_offers_status_expiry", "status", "expires_at"),
    Index("ix_market_offers_seller_status", "accepted_by", "status"),
)

gifts = Table(
    "market_gifts",
    _metadata,
    Column("record_id", String(36), primary_key=True),
    Column("telegram_owned_gift_id", String(255), unique=True),
    Column("telegram_gift_id", String(255)),
    Column("source_chat_id", BigInteger, nullable=False),
    Column("source_message_id", BigInteger, nullable=False),
    Column("sender_id", BigInteger, nullable=False),
    Column("sender_username", String(64)),
    Column("sender_name", String(255), nullable=False),
    Column("gift_name", String(255), nullable=False),
    Column("gift_url", Text),
    Column("model_name", String(255)),
    Column("backdrop_name", String(255)),
    Column("symbol_name", String(255)),
    Column("metadata", JSON, nullable=False),
    Column("offer_id", String(12), ForeignKey("market_offers.id", ondelete="SET NULL")),
    Column("offer_creator_id", BigInteger),
    Column("offer_creator_username", String(64)),
    Column("offer_creator_name", String(255)),
    Column("offer_price", Numeric(30, 9)),
    Column("offer_currency", String(8)),
    Column("offer_created_at", DateTime(timezone=True)),
    Column("status", String(24), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("transferred_at", DateTime(timezone=True)),
    Column("notification_status", String(24), nullable=False),
    Column("notification_attempts", Integer, nullable=False),
    Column("notification_claimed_at", DateTime(timezone=True)),
    Column("notified_at", DateTime(timezone=True)),
    Column("notification_message_id", BigInteger),
    Column("last_notification_error", Text),
    Column("estimated_value_uah", Numeric(14, 2)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("status IN ('received', 'transferred')"),
    CheckConstraint("notification_status IN ('not_required', 'pending', 'processing', 'sent')"),
    Index("uq_market_gifts_source_message", "source_chat_id", "source_message_id", unique=True),
    Index("ix_market_gifts_sender_received", "sender_id", "received_at"),
    Index("ix_market_gifts_offer", "offer_id"),
    Index("ix_market_gifts_notification", "notification_status", "notification_claimed_at"),
)


def _async_database_url(value: str) -> str:
    if value.startswith("postgres://"):
        return "postgresql+asyncpg://" + value.removeprefix("postgres://")
    if value.startswith("postgresql://"):
        return "postgresql+asyncpg://" + value.removeprefix("postgresql://")
    if value.startswith("sqlite:///") and not value.startswith("sqlite+aiosqlite:///"):
        return "sqlite+aiosqlite:///" + value.removeprefix("sqlite:///")
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    if result.get("created_at") and result["created_at"].tzinfo is None:
        result["created_at"] = result["created_at"].replace(tzinfo=timezone.utc)
    for field in ("expires_at", "updated_at", "received_at", "transferred_at", "offer_created_at"):
        value = result.get(field)
        if value is not None and value.tzinfo is None:
            result[field] = value.replace(tzinfo=timezone.utc)
    return result


def _offer_creator_username(offer: dict[str, Any]) -> str | None:
    label = str(offer.get("buyer_label") or "")
    return label[1:] if label.startswith("@") else None


class MarketDatabase:
    def __init__(self, database_url: str) -> None:
        normalized_url = _async_database_url(database_url)
        parsed_url = make_url(normalized_url)
        database_path = parsed_url.database
        if (
            parsed_url.drivername.startswith("sqlite")
            and database_path not in (None, "", ":memory:")
        ):
            Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(normalized_url, pool_pre_ping=True)
        self._notification_lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(_metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def create_offer(
        self,
        buyer_id: int,
        buyer_label: str,
        nft_url: str,
        price: Decimal,
        language: str,
        currency: str,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        offer = {
            "id": uuid4().hex[:12],
            "buyer_id": buyer_id,
            "buyer_label": buyer_label[:120],
            "buyer_username": _offer_creator_username({"buyer_label": buyer_label}),
            "nft_url": nft_url,
            "price": price,
            "currency": currency,
            "language": language,
            "status": "pending",
            "accepted_by": None,
            "created_at": now,
            "expires_at": now + timedelta(hours=6),
            "updated_at": now,
        }
        async with self.engine.begin() as connection:
            await connection.execute(insert(offers).values(**offer))
        return offer

    async def get_offer(self, offer_id: str) -> dict[str, Any] | None:
        async with self.engine.begin() as connection:
            result = await connection.execute(select(offers).where(offers.c.id == offer_id))
            row = result.mappings().first()
            if row is None:
                return None
            offer = _row_dict(row)
            if offer["status"] == "pending" and offer["expires_at"] <= datetime.now(timezone.utc):
                now = datetime.now(timezone.utc)
                await connection.execute(
                    update(offers).where(offers.c.id == offer_id).values(status="expired", updated_at=now)
                )
                offer["status"] = "expired"
                offer["updated_at"] = now
            return offer

    async def offers_for_buyer(self, buyer_id: int, limit: int) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                select(offers)
                .where(offers.c.buyer_id == buyer_id)
                .order_by(offers.c.created_at.desc())
                .limit(limit)
            )
            return [_row_dict(row) for row in result.mappings()]

    async def public_offers(self, limit: int) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as connection:
            await connection.execute(
                update(offers)
                .where(and_(offers.c.status == "pending", offers.c.expires_at <= now))
                .values(status="expired", updated_at=now)
            )
            result = await connection.execute(
                select(offers)
                .where(offers.c.status == "pending")
                .order_by(offers.c.created_at.desc())
                .limit(limit)
            )
            return [_row_dict(row) for row in result.mappings()]

    async def change_offer(
        self,
        offer_id: str,
        user_id: int,
        expected_status: str,
        new_status: str,
        *,
        disallow_buyer: bool = False,
        require_accepted_by: bool = False,
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    select(offers).where(offers.c.id == offer_id).with_for_update()
                )
            ).mappings().first()
            if row is None:
                return None
            offer = _row_dict(row)
            if offer["status"] != expected_status:
                return None
            if expected_status == "pending" and offer["expires_at"] <= now:
                await connection.execute(
                    update(offers).where(offers.c.id == offer_id).values(status="expired", updated_at=now)
                )
                return None
            if disallow_buyer and offer["buyer_id"] == user_id:
                return None
            if require_accepted_by and offer["accepted_by"] != user_id:
                return None
            values: dict[str, Any] = {"status": new_status, "updated_at": now}
            if new_status in {"accepted", "rejected"}:
                values["accepted_by"] = user_id
            await connection.execute(update(offers).where(offers.c.id == offer_id).values(**values))
            offer.update(values)
            return offer

    async def record_gift(
        self,
        *,
        sender_id: int,
        sender_username: str | None,
        sender_name: str,
        source_chat_id: int,
        source_message_id: int,
        telegram_gift_id: str | None,
        telegram_owned_gift_id: str | None,
        gift_name: str,
        gift_url: str | None,
        model_name: str | None,
        backdrop_name: str | None,
        symbol_name: str | None,
        metadata: dict[str, Any],
        received_at: datetime,
    ) -> tuple[dict[str, Any], bool]:
        lookup: list[ColumnElement[bool]] = [
            and_(
                gifts.c.source_chat_id == source_chat_id,
                gifts.c.source_message_id == source_message_id,
            )
        ]
        if telegram_owned_gift_id:
            lookup.append(gifts.c.telegram_owned_gift_id == telegram_owned_gift_id)
        now = datetime.now(timezone.utc)
        try:
            async with self.engine.begin() as connection:
                existing = (
                    await connection.execute(select(gifts).where(or_(*lookup)).limit(1))
                ).mappings().first()
                if existing is not None:
                    return _row_dict(existing), False

                accepted = (
                    await connection.execute(
                        select(offers)
                        .where(
                            offers.c.accepted_by == sender_id,
                            offers.c.status.in_(("accepted", "gift_sent")),
                        )
                        .order_by(offers.c.created_at.desc())
                        .limit(2)
                    )
                ).mappings().all()
                linked_offer = _row_dict(accepted[0]) if len(accepted) == 1 else None
                gift = {
                    "record_id": str(uuid4()),
                    "telegram_owned_gift_id": telegram_owned_gift_id,
                    "telegram_gift_id": telegram_gift_id,
                    "source_chat_id": source_chat_id,
                    "source_message_id": source_message_id,
                    "sender_id": sender_id,
                    "sender_username": sender_username,
                    "sender_name": sender_name,
                    "gift_name": gift_name[:255],
                    "gift_url": gift_url,
                    "model_name": model_name,
                    "backdrop_name": backdrop_name,
                    "symbol_name": symbol_name,
                    "metadata": metadata,
                    "offer_id": linked_offer["id"] if linked_offer else None,
                    "offer_creator_id": linked_offer["buyer_id"] if linked_offer else None,
                    "offer_creator_username": linked_offer["buyer_username"] if linked_offer else None,
                    "offer_creator_name": linked_offer["buyer_label"] if linked_offer else None,
                    "offer_price": linked_offer["price"] if linked_offer else None,
                    "offer_currency": linked_offer["currency"] if linked_offer else None,
                    "offer_created_at": linked_offer["created_at"] if linked_offer else None,
                    "status": "received",
                    "received_at": received_at,
                    "transferred_at": None,
                    "notification_status": "not_required",
                    "notification_attempts": 0,
                    "notification_claimed_at": None,
                    "notified_at": None,
                    "notification_message_id": None,
                    "last_notification_error": None,
                    "estimated_value_uah": None,
                    "created_at": now,
                }
                await connection.execute(insert(gifts).values(**gift))
                return gift, True
        except IntegrityError:
            async with self.engine.connect() as connection:
                existing = (
                    await connection.execute(select(gifts).where(or_(*lookup)).limit(1))
                ).mappings().first()
                if existing is None:
                    raise
                return _row_dict(existing), False

    async def link_gift_to_offer(self, record_id: str, offer_id: str, sender_id: int) -> dict[str, Any] | None:
        async with self.engine.begin() as connection:
            gift_row = (
                await connection.execute(
                    select(gifts).where(gifts.c.record_id == record_id).with_for_update()
                )
            ).mappings().first()
            offer_row = (
                await connection.execute(
                    select(offers).where(offers.c.id == offer_id).with_for_update()
                )
            ).mappings().first()
            if gift_row is None or offer_row is None:
                return None
            gift = _row_dict(gift_row)
            offer = _row_dict(offer_row)
            if gift["sender_id"] != sender_id or offer["accepted_by"] != sender_id:
                return None
            if offer["status"] not in {"accepted", "gift_sent"} or gift["status"] != "received":
                return None
            if gift["offer_id"] not in (None, offer_id):
                return None
            values = {
                "offer_id": offer_id,
                "offer_creator_id": offer["buyer_id"],
                "offer_creator_username": offer["buyer_username"],
                "offer_creator_name": offer["buyer_label"],
                "offer_price": offer["price"],
                "offer_currency": offer["currency"],
                "offer_created_at": offer["created_at"],
            }
            await connection.execute(
                update(gifts).where(gifts.c.record_id == record_id).values(**values)
            )
            gift.update(values)
            return gift

    async def pending_transfer_gifts(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                select(gifts)
                .where(gifts.c.status == "received", gifts.c.offer_id.is_not(None))
                .order_by(gifts.c.received_at.asc())
                .limit(limit)
            )
            return [_row_dict(row) for row in result.mappings()]

    async def confirm_gift_transfer(self, record_id: str) -> tuple[dict[str, Any] | None, bool]:
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    select(gifts).where(gifts.c.record_id == record_id).with_for_update()
                )
            ).mappings().first()
            if row is None:
                return None, False
            gift = _row_dict(row)
            if gift["status"] == "transferred":
                return gift, False
            if gift["status"] != "received" or not gift["offer_id"]:
                return None, False
            offer_row = (
                await connection.execute(
                    select(offers).where(offers.c.id == gift["offer_id"]).with_for_update()
                )
            ).mappings().first()
            if offer_row is None or offer_row["accepted_by"] != gift["sender_id"]:
                return None, False
            values = {
                "status": "transferred",
                "transferred_at": now,
                "notification_status": "pending",
                "notification_claimed_at": None,
                "last_notification_error": None,
            }
            await connection.execute(
                update(gifts).where(gifts.c.record_id == record_id).values(**values)
            )
            gift.update(values)
            return gift, True

    async def claim_pending_notifications(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._notification_lock:
            now = datetime.now(timezone.utc)
            retry_before = now - timedelta(minutes=5)
            async with self.engine.begin() as connection:
                rows = (
                    await connection.execute(
                        select(gifts)
                        .where(
                            gifts.c.status == "transferred",
                            or_(
                                gifts.c.notification_status == "pending",
                                and_(
                                    gifts.c.notification_status == "processing",
                                    gifts.c.notification_claimed_at <= retry_before,
                                ),
                            ),
                        )
                        .order_by(gifts.c.transferred_at.asc())
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).mappings().all()
                claimed: list[dict[str, Any]] = []
                for row in rows:
                    await connection.execute(
                        update(gifts)
                        .where(gifts.c.record_id == row["record_id"])
                        .values(
                            notification_status="processing",
                            notification_attempts=gifts.c.notification_attempts + 1,
                            notification_claimed_at=now,
                        )
                    )
                    item = _row_dict(row)
                    item.update(
                        notification_status="processing",
                        notification_attempts=item["notification_attempts"] + 1,
                        notification_claimed_at=now,
                    )
                    claimed.append(item)
                return claimed

    async def finish_notification(
        self,
        record_id: str,
        *,
        message_id: int | None = None,
        error: str | None = None,
    ) -> None:
        if error is None:
            values: dict[str, Any] = {
                "notification_status": "sent",
                "notified_at": datetime.now(timezone.utc),
                "notification_message_id": message_id,
                "notification_claimed_at": None,
                "last_notification_error": None,
            }
        else:
            values = {
                "notification_status": "pending",
                "notification_claimed_at": None,
                "last_notification_error": error[:2000],
            }
        async with self.engine.begin() as connection:
            await connection.execute(
                update(gifts).where(gifts.c.record_id == record_id).values(**values)
            )
