from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    and_,
    inspect,
    insert,
    select,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.sql.schema import Column

_metadata = MetaData()

offers = Table(
    "market_offers",
    _metadata,
    Column("id", String(12), primary_key=True),
    Column("buyer_id", BigInteger, nullable=False),
    Column("recipient_id", BigInteger),
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

business_connections = Table(
    "market_business_connections",
    _metadata,
    Column("id", String(255), primary_key=True),
    Column("business_user_id", BigInteger, nullable=False),
    Column("user_chat_id", BigInteger, nullable=False),
    Column("is_enabled", Boolean, nullable=False),
    Column("can_reply", Boolean, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("ix_market_business_connections_user", "business_user_id", "is_enabled", "can_reply"),
)

business_recipients = Table(
    "market_business_recipients",
    _metadata,
    Column(
        "connection_id",
        String(255),
        ForeignKey("market_business_connections.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("username", String(32), primary_key=True),
    Column("user_id", BigInteger, nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Index("ix_market_business_recipients_user", "connection_id", "user_id"),
)

user_preferences = Table(
    "market_user_preferences",
    _metadata,
    Column("user_id", BigInteger, primary_key=True),
    Column("gift_recipient_username", String(32), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

offer_tokens = Table(
    "market_offer_tokens",
    _metadata,
    Column("token", String(32), primary_key=True),
    Column(
        "connection_id",
        String(255),
        ForeignKey("market_business_connections.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("creator_id", BigInteger, nullable=False),
    Column("buyer_label", String(120), nullable=False),
    Column("nft_url", Text, nullable=False),
    Column("price", Numeric(30, 9), nullable=False),
    Column("currency", String(8), nullable=False),
    Column("language", String(8), nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("claimed_by", BigInteger),
    Column("offer_id", String(12), ForeignKey("market_offers.id", ondelete="SET NULL")),
    CheckConstraint("status IN ('pending', 'claimed')"),
    Index("ix_market_offer_tokens_expiry", "status", "expires_at"),
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
    for field in ("expires_at", "updated_at"):
        value = result.get(field)
        if value is not None and value.tzinfo is None:
            result[field] = value.replace(tzinfo=timezone.utc)
    return result


def _offer_creator_username(offer: dict[str, Any]) -> str | None:
    label = str(offer.get("buyer_label") or "")
    return label[1:] if label.startswith("@") else None


def _add_offer_recipient_id(connection: Any) -> None:
    columns = {column["name"] for column in inspect(connection).get_columns("market_offers")}
    if "recipient_id" not in columns:
        connection.exec_driver_sql("ALTER TABLE market_offers ADD COLUMN recipient_id BIGINT")


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
        self._business_connection_lock = asyncio.Lock()
        self._business_recipient_lock = asyncio.Lock()
        self._user_preference_lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(_metadata.create_all)
            await connection.run_sync(_add_offer_recipient_id)
            await connection.exec_driver_sql("DROP TABLE IF EXISTS market_gifts")

    async def close(self) -> None:
        await self.engine.dispose()

    async def save_business_connection(
        self,
        *,
        connection_id: str,
        business_user_id: int,
        user_chat_id: int,
        is_enabled: bool,
        can_reply: bool,
    ) -> None:
        values = {
            "business_user_id": business_user_id,
            "user_chat_id": user_chat_id,
            "is_enabled": is_enabled,
            "can_reply": can_reply,
            "updated_at": datetime.now(timezone.utc),
        }
        async with self._business_connection_lock:
            async with self.engine.begin() as connection:
                updated = await connection.execute(
                    update(business_connections)
                    .where(business_connections.c.id == connection_id)
                    .values(**values)
                )
                if updated.rowcount == 0:
                    await connection.execute(
                        insert(business_connections).values(id=connection_id, **values)
                    )

    async def get_business_connection(self, business_user_id: int) -> dict[str, Any] | None:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                select(business_connections)
                .where(
                    business_connections.c.business_user_id == business_user_id,
                    business_connections.c.is_enabled.is_(True),
                    business_connections.c.can_reply.is_(True),
                )
                .order_by(business_connections.c.updated_at.desc())
                .limit(1)
            )
            row = result.mappings().first()
            return dict(row) if row is not None else None

    async def get_business_offer_context(
        self,
        *,
        business_user_id: int,
        username: str | None = None,
    ) -> tuple[dict[str, Any] | None, int | None]:
        normalized_username = username.casefold().lstrip("@") if username else None
        latest_connection_id = (
            select(business_connections.c.id)
            .where(
                business_connections.c.business_user_id == business_user_id,
                business_connections.c.is_enabled.is_(True),
                business_connections.c.can_reply.is_(True),
            )
            .order_by(business_connections.c.updated_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        recipient_join = business_connections.outerjoin(
            business_recipients,
            and_(
                business_recipients.c.connection_id == business_connections.c.id,
                business_recipients.c.username == normalized_username,
            ),
        )
        async with self.engine.connect() as connection:
            result = await connection.execute(
                select(
                    business_connections,
                    business_recipients.c.user_id.label("recipient_id"),
                )
                .select_from(recipient_join)
                .where(business_connections.c.id == latest_connection_id)
            )
            row = result.mappings().first()
            if row is None:
                return None, None
            business_connection = {
                column.name: row[column.name]
                for column in business_connections.columns
            }
            return business_connection, row["recipient_id"]

    async def set_gift_recipient(self, *, user_id: int, username: str) -> None:
        normalized_username = username.casefold().lstrip("@")
        async with self._user_preference_lock:
            async with self.engine.begin() as connection:
                updated = await connection.execute(
                    update(user_preferences)
                    .where(user_preferences.c.user_id == user_id)
                    .values(
                        gift_recipient_username=normalized_username,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                if updated.rowcount == 0:
                    await connection.execute(
                        insert(user_preferences).values(
                            user_id=user_id,
                            gift_recipient_username=normalized_username,
                            updated_at=datetime.now(timezone.utc),
                        )
                    )

    async def get_gift_recipient(self, user_id: int) -> str | None:
        async with self.engine.connect() as connection:
            return await connection.scalar(
                select(user_preferences.c.gift_recipient_username).where(
                    user_preferences.c.user_id == user_id
                )
            )

    async def record_business_recipient(
        self,
        *,
        connection_id: str,
        username: str,
        user_id: int,
    ) -> bool:
        normalized_username = username.casefold()
        values = {
            "user_id": user_id,
            "last_seen_at": datetime.now(timezone.utc),
        }
        async with self._business_recipient_lock:
            async with self.engine.begin() as connection:
                business_user_id = await connection.scalar(
                    select(business_connections.c.business_user_id).where(
                        business_connections.c.id == connection_id,
                        business_connections.c.is_enabled.is_(True),
                        business_connections.c.can_reply.is_(True),
                    )
                )
                if business_user_id is None or business_user_id == user_id:
                    return False
                updated = await connection.execute(
                    update(business_recipients)
                    .where(
                        business_recipients.c.connection_id == connection_id,
                        business_recipients.c.username == normalized_username,
                    )
                    .values(**values)
                )
                if updated.rowcount == 0:
                    await connection.execute(
                        insert(business_recipients).values(
                            connection_id=connection_id,
                            username=normalized_username,
                            **values,
                        )
                    )
        return True

    async def resolve_business_recipient(
        self,
        *,
        connection_id: str,
        username: str,
    ) -> int | None:
        normalized_username = username.casefold().lstrip("@")
        async with self.engine.connect() as connection:
            result = await connection.execute(
                select(business_recipients.c.user_id)
                .join(
                    business_connections,
                    business_connections.c.id == business_recipients.c.connection_id,
                )
                .where(
                    business_connections.c.id == connection_id,
                    business_connections.c.is_enabled.is_(True),
                    business_connections.c.can_reply.is_(True),
                    business_recipients.c.username == normalized_username,
                )
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def create_offer(
        self,
        buyer_id: int,
        buyer_label: str,
        nft_url: str,
        price: Decimal,
        language: str,
        currency: str,
        recipient_id: int | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        offer = {
            "id": uuid4().hex[:12],
            "buyer_id": buyer_id,
            "recipient_id": recipient_id,
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

    async def create_offer_token(
        self,
        *,
        connection_id: str,
        creator_id: int,
        buyer_label: str,
        nft_url: str,
        price: Decimal,
        currency: str,
        language: str,
    ) -> str:
        now = datetime.now(timezone.utc)
        token = secrets.token_urlsafe(12)
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(offer_tokens).values(
                    token=token,
                    connection_id=connection_id,
                    creator_id=creator_id,
                    buyer_label=buyer_label[:120],
                    nft_url=nft_url,
                    price=price,
                    currency=currency,
                    language=language,
                    status="pending",
                    created_at=now,
                    expires_at=now + timedelta(hours=24),
                    claimed_by=None,
                    offer_id=None,
                )
            )
        return token

    async def claim_offer_token(
        self,
        *,
        token: str,
        connection_id: str,
        recipient_id: int,
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        token_connection = business_connections.alias("token_connection")
        recipient_connection = business_connections.alias("recipient_connection")
        async with self.engine.begin() as connection:
            token_row = (
                await connection.execute(
                    select(offer_tokens)
                    .join(
                        token_connection,
                        token_connection.c.id == offer_tokens.c.connection_id,
                    )
                    .join(
                        recipient_connection,
                        recipient_connection.c.id == connection_id,
                    )
                    .where(
                        offer_tokens.c.token == token,
                        token_connection.c.business_user_id == offer_tokens.c.creator_id,
                        recipient_connection.c.business_user_id == offer_tokens.c.creator_id,
                        offer_tokens.c.status == "pending",
                        offer_tokens.c.expires_at > now,
                        recipient_connection.c.is_enabled.is_(True),
                        recipient_connection.c.can_reply.is_(True),
                    )
                    .with_for_update()
                )
            ).mappings().first()
            if token_row is None or token_row["creator_id"] == recipient_id:
                return None

            offer = {
                "id": uuid4().hex[:12],
                "buyer_id": token_row["creator_id"],
                "recipient_id": recipient_id,
                "buyer_label": token_row["buyer_label"],
                "buyer_username": _offer_creator_username({"buyer_label": token_row["buyer_label"]}),
                "nft_url": token_row["nft_url"],
                "price": token_row["price"],
                "currency": token_row["currency"],
                "language": token_row["language"],
                "status": "pending",
                "accepted_by": None,
                "created_at": now,
                "expires_at": now + timedelta(hours=6),
                "updated_at": now,
            }
            await connection.execute(insert(offers).values(**offer))
            claimed = await connection.execute(
                update(offer_tokens)
                .where(
                    offer_tokens.c.token == token,
                    offer_tokens.c.status == "pending",
                    offer_tokens.c.expires_at > now,
                )
                .values(status="claimed", claimed_by=recipient_id, offer_id=offer["id"])
            )
            if claimed.rowcount != 1:
                raise RuntimeError("Offer token was claimed concurrently.")
            offer["business_connection_id"] = connection_id
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
            if (
                expected_status == "pending"
                and offer.get("recipient_id") is not None
                and offer["recipient_id"] != user_id
            ):
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
