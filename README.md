
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

The bot creates the `market_offers`, `market_offer_tokens`, `market_gifts`, `market_business_connections`, and `market_business_recipients` tables safely at startup. Offers, one-time offer codes, gift records, and Telegram Business connections use PostgreSQL in production; the static catalog remains loaded from the project files. Each gift stores the Telegram sender, received time, status, Telegram metadata, and (when determinable) an immutable snapshot of the accepted offer and its creator. Telegram-owned gift IDs and source chat/message IDs are unique, so webhook retries do not create duplicate gift records. If multiple accepted offers match a sender, the bot leaves the gift unlinked rather than guessing.

### Send offers through Telegram Business

Enable Business Mode for the bot in `@BotFather`, then connect it from the Telegram Business account that will send offers and allow it to reply to chats. The connected account needs Telegram Premium for the requested custom emojis. The bot stores Business connection updates and uses the offer creator's own enabled connection; the bot cannot send as a different creator's account.

Create an offer in a private chat with the bot using the recipient's Telegram username or numeric user ID:

```text
.buy @username https://t.me/nft/Collectible-123 12.5 gram
.buy 123456789 https://t.me/nft/Collectible-123 500 stars
```

You can avoid entering recipient details by creating a one-time command:

```text
.buy https://t.me/nft/Collectible-123 12.5 gram
```

The bot returns a command such as `.qm <unique-code>`. Paste it into the intended recipient's private chat with the connected Business account. The bot removes the command message after receiving it, then sends the offer with the usual Accept/Decline buttons. The first other person whose message reaches the bot through an active Business connection belonging to the offer creator becomes the recipient. Codes are tied to the creator's Telegram account, expire after 24 hours, cannot be used by the creator, and are consumed once. Do not share the code with anyone except the intended recipient. The Bot API must deliver the pasted command as a Business message; if Telegram does not deliver that update, no offer is created.

When an incoming message in a connected Business chat contains a username, the bot remembers the username-to-user-ID association for that Business account. Thus, `@username` works after that person has messaged the connected Business account and the bot received the update. Telegram does not let the Bot API look up a random private user's ID from a username, so an unseen username cannot be resolved; in that case ask them to message the Business account first or provide their numeric ID. Usernames can change, so the bot refreshes the mapping only when it observes a new message from that user. The recipient must be reachable in the connected Business account's chats and Telegram must permit that account to reply. If Telegram rejects delivery, the bot reports the failure and does not fall back to inline mode. Existing inline offer messages can still have their buttons processed, but new inline offers are disabled. Disable inline mode in `@BotFather` as well if you want to remove its Telegram UI entry entirely.

Offers created before PostgreSQL is configured were held only in process memory and cannot be migrated; offers created after the database is configured persist across restarts.

### Register and verify gifts

Only users allowed to create offers can register gifts by sending the actual Telegram gift service message to the bot in a private chat; an image or a sticker that merely depicts a gift is not a gift update. Telegram's Bot API exposes different metadata for regular and unique gifts; any unavailable model, symbol, backdrop, or link is left empty rather than inferred. If a single accepted offer cannot be matched, the seller can link it with `/linkgift GIFT_RECORD_ID OFFER_ID`.

Telegram does not provide this bot with a reliable event proving that a gift was transferred from a person's account to the ordinary user account `@questbankk`. The “I've transferred” button is only a seller report and does not change the stored gift status. After an admin checks the gift in the bank account, an admin of `OFFERS_GROUP_ID` can use `/pendinggifts` and then `/confirmgift GIFT_RECORD_ID`. Only that explicit group-admin confirmation changes the gift from `received` to `transferred` and queues the group log.

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
