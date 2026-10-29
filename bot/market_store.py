from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .market_database import MarketDatabase


class MarketStore:
    """Persistent offer and gift records with a process-local static catalog."""

    def __init__(self, database_url: str) -> None:
        self.database = MarketDatabase(database_url)
        self._items: list[dict[str, Any]] = []
        self._load_static_catalog()

    async def initialize(self) -> None:
        await self.database.initialize()

    async def close(self) -> None:
        await self.database.close()

    def _load_static_catalog(self) -> None:
        root = Path(__file__).parent.parent
        catalog_path = root / "catalog.json"
        if not catalog_path.is_file():
            catalog_path = root / "netlify-site" / "catalog.json"
        if not catalog_path.is_file():
            return
        try:
            payload = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in payload.get("items", []):
            if item.get("category") not in {"username", "number", "gift"}:
                continue
            self._items.append({
                "id": len(self._items) + 1,
                "category": str(item["category"]),
                "title": str(item.get("title", "")),
                "collection": str(item.get("collection", "")),
                "item_url": str(item.get("item_url", "")),
                "image_url": str(item.get("image_url", "")),
                "price_grams": Decimal(str(item.get("price_grams", "0"))),
                "status": "for_sale",
            })

    async def create_offer(
        self,
        buyer_id: int,
        buyer_label: str,
        nft_url: str,
        price: Decimal,
        language: str = "en",
        currency: str = "GRAM",
    ) -> dict[str, Any]:
        return await self.database.create_offer(
            buyer_id, buyer_label, nft_url, price, language, currency
        )

    async def get_offer(self, offer_id: str) -> dict[str, Any] | None:
        return await self.database.get_offer(offer_id)

    async def offers_for_buyer(self, buyer_id: int, limit: int = 30) -> list[dict[str, Any]]:
        return await self.database.offers_for_buyer(buyer_id, limit)

    async def public_offers(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self.database.public_offers(limit)

    async def seed_gift_links(self) -> int:
        gift_count = sum(item["category"] == "gift" for item in self._items)
        if gift_count:
            return gift_count

        links_path = Path(__file__).with_name("quest_market_gift_links.txt")
        if not links_path.is_file():
            return 0
        urls = [line.strip() for line in links_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for index, url in enumerate(urls, start=1):
            parsed = urlsplit(url)
            slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            match = re.fullmatch(r"([A-Za-z0-9]+)-(\d+)", slug)
            if not match or parsed.scheme != "https" or parsed.netloc != "t.me":
                continue
            collection, serial = match.groups()
            name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", collection)
            if collection == "BDayCandle":
                name = "Birthday Candle"
            self._items.append({
                "id": len(self._items) + 1,
                "category": "gift",
                "title": f"{name} #{serial}",
                "collection": collection,
                "item_url": url,
                "image_url": "",
                "price_grams": Decimal(3 + ((index - 1) * 37 % 198)),
                "status": "for_sale",
            })
        return sum(item["category"] == "gift" for item in self._items)

    async def catalog_items(
        self,
        category: str,
        search: str = "",
        collection: str = "",
        sort: str = "recent",
        limit: int = 36,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
        needle = search[:80].casefold()
        rows = [
            item for item in self._items
            if item["category"] == category
            and item["status"] == "for_sale"
            and (not collection or item["collection"] == collection)
            and (not needle or needle in item["title"].casefold() or needle in item["collection"].casefold())
        ]
        if sort == "price_asc":
            rows.sort(key=lambda item: (item["price_grams"], item["id"]))
        elif sort == "price_desc":
            rows.sort(key=lambda item: (item["price_grams"], item["id"]), reverse=True)
        elif sort == "featured" and category == "gift":
            rows.sort(key=lambda item: (bool(item["image_url"]), item["id"]), reverse=True)
        else:
            rows.sort(key=lambda item: item["id"], reverse=True)
        gifts = [item for item in self._items if item["category"] == "gift" and item["status"] == "for_sale"]
        counts: dict[str, int] = {}
        for item in gifts:
            counts[item["collection"]] = counts.get(item["collection"], 0) + 1
        collections = [
            {"collection": name, "item_count": count}
            for name, count in sorted(counts.items())
        ]
        selected = [dict(item) for item in rows[offset:offset + limit]]
        return selected, len(rows), collections

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
