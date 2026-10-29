
# Quest Market

Telegram collectible marketplace bot and Mini App.

## Deploy on Render

Create a Render Blueprint from this private repository. The Blueprint starts the webhook service and serves the Mini App from the same URL. Add these environment variables in Render:

- `MARKET_BOT_TOKEN` — BotFather token for Quest Market.
- `MARKET_DATABASE_URL` — internal connection URL for a Render PostgreSQL database. The bot refuses to start on Render with SQLite because the service filesystem is not persistent.
- `MARKET_ALLOWED_USER_IDS` — optional comma-separated Telegram IDs that always have access.
- `MARKET_ACCESS_CHAT_ID` — Telegram chat whose members can create and publish offers; the bot must be an administrator there.
- `BANK_USERNAME` — receiving bank account username, without `@` (default: `questbankk`).
- `OFFERS_GROUP_ID` — Telegram group ID used for gift verification and transfer notifications.
- `GIFT_NOTIFICATION_RETRY_SECONDS` — retry interval for failed group notifications (default: `60`).

Create the PostgreSQL database in Render, then copy its **internal** database URL into `MARKET_DATABASE_URL` in the web service's Environment settings. Do not commit or share the URL because it contains database credentials. Redeploy after setting it. Local development may use `MARKET_DATABASE_URL=sqlite+aiosqlite:///./data/quest_market.db`.

The `market_offers` and `market_gifts` tables are created safely at startup. Offers and gift records use PostgreSQL in production; the static catalog remains loaded from the project files. Each gift stores the Telegram sender, received time, status, Telegram metadata, and (when determinable) an immutable snapshot of the accepted offer and its creator. Telegram-owned gift IDs and source chat/message IDs are unique, so webhook retries do not create duplicate gift records. If multiple accepted offers match a sender, the bot leaves the gift unlinked rather than guessing.

Offers created before PostgreSQL is configured were held only in process memory and cannot be migrated; offers created after the database is configured persist across restarts.

### Register and verify gifts

Only users allowed to use inline offers can register gifts by sending the actual Telegram gift service message to the bot in a private chat; an image or a sticker that merely depicts a gift is not a gift update. Telegram's Bot API exposes different metadata for regular and unique gifts; any unavailable model, symbol, backdrop, or link is left empty rather than inferred. If a single accepted offer cannot be matched, the seller can link it with `/linkgift GIFT_RECORD_ID OFFER_ID`.

Telegram does not provide this bot with a reliable event proving that a gift was transferred from a person's account to the ordinary user account `@questbankk`. The inline “I've transferred” button is only a seller report and does not change the stored gift status. After an admin checks the gift in the bank account, an admin of `OFFERS_GROUP_ID` can use `/pendinggifts` and then `/confirmgift GIFT_RECORD_ID`. Only that explicit group-admin confirmation changes the gift from `received` to `transferred` and queues the group log.

Failed group sends are retained in a database-backed notification queue and retried. Repeated confirmations do not enqueue another notification. Telegram's send-message API has no idempotency key, so a process crash after Telegram accepts a message but before the database records its message ID can exceptionally cause a retry duplicate.

No reliable price source is configured, so notifications show `Немає оцінки` rather than converting the offer price or inventing a UAH value. The transfer log includes the creator's username, or their display name and Telegram ID when no username was available. Gift links are included only when the Bot API provides enough unique-gift data to construct the canonical link.

The bot must be an admin in `MARKET_ACCESS_CHAT_ID` for member checks and an admin with send-message permission in `OFFERS_GROUP_ID` so it can check which group admins may confirm transfers. Admins running `/confirmgift` must be Telegram admins of that exact group.

Free Render web services may sleep after inactivity, so the first request can take time to wake the service.

Offer messages display currency labels as plain text without emoji: `GRAM` or `Stars`.

## Run locally

```sh
python3 -m pip install -r requirements-market.txt
export MARKET_BOT_TOKEN="your-bot-token"
export MARKET_DATABASE_URL="sqlite+aiosqlite:///./data/quest_market.db"
export MARKET_ACCESS_CHAT_ID="-1004447182501"
export OFFERS_GROUP_ID="-1004447182501"
python3 -m bot.market_main
```

In webhook mode, also set `RENDER_EXTERNAL_URL` or `WEBAPP_URL`; for local polling, leave `BOT_MODE=polling`.
