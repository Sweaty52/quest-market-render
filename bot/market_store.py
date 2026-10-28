from __future__ import annotations

import asyncio
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class MarketStore:
    """Volatile Quest Market state; no database connection is required."""

    def __init__(self) -> None:
        self._offers: dict[str, dict[str, Any]] = {}
        self._items: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._load_static_catalog()

    def _load_static_catalog(self) -> None:
        catalog_path = Path(__file__).parent.parent / "catalog.json"
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
        async with self._lock:
            for _ in range(4):
                offer_id = secrets.token_hex(6)
                if offer_id not in self._offers:
                    now = datetime.now(timezone.utc)
                    offer = {
                        "id": offer_id,
                        "buyer_id": buyer_id,
                        "buyer_label": buyer_label[:120],
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
                    self._offers[offer_id] = offer
                    return offer.copy()
        raise RuntimeError("Could not allocate a unique market offer id")

    async def get_offer(self, offer_id: str) -> dict[str, Any] | None:
        async with self._lock:
            offer = self._offers.get(offer_id)
            if offer and offer["status"] == "pending" and offer["expires_at"] <= datetime.now(timezone.utc):
                offer["status"] = "expired"
                offer["updated_at"] = datetime.now(timezone.utc)
            return offer.copy() if offer else None

    async def offers_for_buyer(self, buyer_id: int, limit: int = 30) -> list[dict[str, Any]]:
        async with self._lock:
            rows = [offer for offer in self._offers.values() if offer["buyer_id"] == buyer_id]
            rows.sort(key=lambda offer: offer["created_at"], reverse=True)
            return [offer.copy() for offer in rows[:limit]]

    async def public_offers(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._lock:
            rows = [offer for offer in self._offers.values() if offer["status"] == "pending"]
            now = datetime.now(timezone.utc)
            for offer in rows:
                if offer["expires_at"] <= now:
                    offer["status"] = "expired"
                    offer["updated_at"] = now
            rows = [offer for offer in rows if offer["status"] == "pending"]
            rows.sort(key=lambda offer: offer["created_at"], reverse=True)
            return [offer.copy() for offer in rows[:limit]]

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
                "price_grams": Decimal(500 + ((index * 137) % 5000)),
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
        return await self._change_offer(offer_id, seller_id, "pending", "accepted", disallow_buyer=True)

    async def reject_offer(self, offer_id: str, seller_id: int) -> dict[str, Any] | None:
        return await self._change_offer(offer_id, seller_id, "pending", "rejected", disallow_buyer=True)

    async def mark_gift_sent(self, offer_id: str, seller_id: int) -> dict[str, Any] | None:
        return await self._change_offer(offer_id, seller_id, "accepted", "gift_sent", require_accepted_by=True)

    async def _change_offer(
        self,
        offer_id: str,
        user_id: int,
        expected_status: str,
        new_status: str,
        *,
        disallow_buyer: bool = False,
        require_accepted_by: bool = False,
    ) -> dict[str, Any] | None:
        async with self._lock:
            offer = self._offers.get(offer_id)
            if not offer or offer["status"] != expected_status:
                return None
            now = datetime.now(timezone.utc)
            if expected_status == "pending" and offer["expires_at"] <= now:
                offer["status"] = "expired"
                offer["updated_at"] = now
                return None
            if disallow_buyer and offer["buyer_id"] == user_id:
                return None
            if require_accepted_by and offer["accepted_by"] != user_id:
                return None
            offer["status"] = new_status
            if new_status in {"accepted", "rejected"}:
                offer["accepted_by"] = user_id
            offer["updated_at"] = now
            return offer.copy()
