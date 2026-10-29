import hashlib
import json
import os
from typing import Any, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from urllib.parse import urlsplit


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)

    bot_token: str
    database_url: str
    owner_ids: Any
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    telegram_api_id: int | None = None
    telegram_api_hash: str = ""
    telethon_session_path: str = "./data/telegram_account"
    reminder_poll_seconds: int = 60
    ai_concurrency: int = 4
    ai_requests_per_minute: int = 20
    inbound_queue_size: int = 500

    @field_validator("owner_ids", mode="before")
    @classmethod
    def parse_owner_ids(cls, value):
        if isinstance(value, str):
            raw = value.strip()
            if raw.startswith("["):
                value = json.loads(raw)
            else:
                value = [item.strip() for item in raw.split(",") if item.strip()]
        if isinstance(value, (list, tuple)):
            return [int(item) for item in value]
        return value

    @property
    def has_telegram_api_credentials(self) -> bool:
        return bool(self.telegram_api_id and self.telegram_api_hash)


class MarketSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)

    market_bot_token: str
    market_account_url: str = "tg://send_gift?to=questbankk"
    market_allowed_user_ids: str = ""
    market_access_chat_id: int | None = None
    bot_mode: Literal["polling", "webhook"] = "polling"
    webapp_url: str = ""
    webapp_host: str = "0.0.0.0"
    webapp_port: int = 8080

    @property
    def public_webapp_url(self) -> str:
        return (os.getenv("RENDER_EXTERNAL_URL") or self.webapp_url).rstrip("/")

    @property
    def listen_port(self) -> int:
        return int(os.getenv("PORT") or self.webapp_port)

    @property
    def webhook_secret(self) -> str:
        return hashlib.sha256(b"quest-market-webhook:" + self.market_bot_token.encode()).hexdigest()

    @field_validator("market_allowed_user_ids")
    @classmethod
    def validate_market_allowed_user_ids(cls, value: str) -> str:
        raw = value.strip()
        if not raw:
            return ""
        if raw.startswith("["):
            try:
                values = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("MARKET_ALLOWED_USER_IDS must be comma-separated IDs or a JSON array") from exc
            if not isinstance(values, list):
                raise ValueError("MARKET_ALLOWED_USER_IDS JSON value must be an array")
        else:
            values = [part.strip() for part in raw.split(",") if part.strip()]
        try:
            ids = list(dict.fromkeys(int(item) for item in values))
        except (TypeError, ValueError) as exc:
            raise ValueError("MARKET_ALLOWED_USER_IDS must contain only numeric Telegram IDs") from exc
        return ",".join(str(user_id) for user_id in ids)

    @property
    def allowed_user_ids(self) -> set[int]:
        """Telegram accounts allowed to use the market, configured as comma-separated IDs."""
        raw = self.market_allowed_user_ids.strip()
        if not raw:
            return set()
        return {int(value) for value in raw.split(",")}

    def user_has_market_access(self, user_id: int) -> bool:
        return user_id in self.allowed_user_ids

    @field_validator("market_account_url")
    @classmethod
    def validate_market_account_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        is_telegram_send_gift = parsed.scheme == "tg" and parsed.netloc == "send_gift" and bool(parsed.query)
        is_https = parsed.scheme == "https" and bool(parsed.hostname)
        if not (is_telegram_send_gift or is_https):
            raise ValueError("MARKET_ACCOUNT_URL must be a Telegram send_gift link or an HTTPS URL")
        return value

    @field_validator("webapp_url")
    @classmethod
    def validate_webapp_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("WEBAPP_URL must be a public HTTPS URL")
        return value
