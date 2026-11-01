import unittest
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
from aiogram import Dispatcher

from bot.config import MarketSettings
from bot.market_http import create_http_app


class MarketHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        settings = MarketSettings(market_bot_token="123456:TEST_TOKEN", bot_mode="webhook")
        app = create_http_app(settings, SimpleNamespace(), Dispatcher())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_health_route_remains_available(self) -> None:
        response = await self.client.get("/health")

        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"status": "ok"})

    async def test_web_app_and_catalog_routes_are_removed(self) -> None:
        for path in ("/", "/api/offers", "/api/catalog"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 404)

    async def test_webhook_route_requires_telegram_secret(self) -> None:
        response = await self.client.post("/telegram/webhook", json={})

        self.assertEqual(response.status, 401)


if __name__ == "__main__":
    unittest.main()
