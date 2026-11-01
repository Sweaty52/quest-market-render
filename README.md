
# Gift Relayer

Telegram collectible marketplace bot. The Mini App and its catalog have been removed.

## Deploy on Render

Create a Render Blueprint from this private repository. The Blueprint starts the bot webhook service and a minimal HTTP server for Telegram updates and Render health checks. Add these environment variables in Render:

- `MARKET_BOT_TOKEN` — BotFather token for Gift Relayer.
- `MARKET_DATABASE_URL` — internal connection URL for a Render PostgreSQL database. The bot refuses to start on Render with SQLite because the service filesystem is not persistent.
- `MARKET_ALLOWED_USER_IDS` — optional comma-separated Telegram IDs that always have access.
- `MARKET_ACCESS_CHAT_ID` — Telegram chat whose members can create and publish offers; the bot must be an administrator there.
- `MARKET_ACCOUNT_URL` — fallback `tg://send_gift` link for users who have not set a personal gift destination.

Create the PostgreSQL database in Render, then copy its **internal** database URL into `MARKET_DATABASE_URL` in the web service's Environment settings. Do not commit or share the URL because it contains database credentials. Redeploy after setting it. Local development may use `MARKET_DATABASE_URL=sqlite+aiosqlite:///./data/quest_market.db`.

The bot creates the `market_offers`, `market_offer_tokens`, `market_business_connections`, `market_business_recipients`, and `market_user_preferences` tables safely at startup. Offers, one-time offer codes, per-user gift-recipient preferences, and Telegram Business connections use PostgreSQL in production. On startup, the bot deletes the legacy `market_gifts` table and its records; gifts are no longer logged or verified.

### Send offers through Telegram Business

Enable Business Mode for the bot in `@BotFather`, then connect it from the Telegram Business account that will send offers and allow it to reply to chats. The connected account needs Telegram Premium for the requested custom emojis. The bot stores Business connection updates and uses the offer creator's own enabled connection; the bot cannot send as a different creator's account.

Each authorized user can configure their own gift destination in a private chat with the bot using `/giftrecipient @username`. Use `/giftrecipient` to view the saved account. This sets the recipient opened by the **Transfer gift** button on that user's accepted offers; it does not automatically transfer a gift. If no personal recipient is configured, the existing `MARKET_ACCOUNT_URL` default (`tg://send_gift?to=questbankk`) is used. The destination preference is saved per Telegram user and survives restarts.

Create an offer in a private chat with the bot using the recipient's Telegram username or numeric user ID:

```text
.buy ru @username https://t.me/nft/Collectible-123 12.5 gram
.buy eng 123456789 https://t.me/nft/Collectible-123 500 stars
.buy zh @username https://t.me/nft/Collectible-123 500 stars
```

The offer recipient sees the offer and its buttons in the selected language: `ru` (Russian), `eng` (English), or `zh` (Chinese). The language tag can also be placed at the end of the command; if omitted, Russian is used. Bot replies, command descriptions, and creator/admin controls are in Russian.

You can avoid entering recipient details by creating a one-time command:

```text
.buy ru https://t.me/nft/Collectible-123 12.5 gram
```

The bot returns a command such as `.qm <unique-code>`. Paste it into the intended recipient's private chat with the connected Business account. The bot removes the command message after receiving it, then sends the offer with the usual Accept/Decline buttons. The recipient is determined from the private chat ID, not the message sender (which may be the Business account owner for a message sent on their behalf). The Business connection must belong to the offer creator. Codes expire after 24 hours, cannot target the creator, and are consumed once. Do not share the code with anyone except the intended recipient. The Bot API must deliver the pasted command as a Business message; if Telegram does not deliver that update, no offer is created.

When an incoming message in a connected Business chat contains a username, the bot remembers the username-to-user-ID association for that Business account. Thus, `@username` works after that person has messaged the connected Business account and the bot received the update. Telegram does not let the Bot API look up a random private user's ID from a username, so an unseen username cannot be resolved; in that case ask them to message the Business account first or provide their numeric ID. Usernames can change, so the bot refreshes the mapping only when it observes a new message from that user. The recipient must be reachable in the connected Business account's chats and Telegram must permit that account to reply. If Telegram rejects delivery, the bot reports the failure and does not fall back to inline mode. Existing inline offer messages can still have their buttons processed, but new inline offers are disabled. Disable inline mode in `@BotFather` as well if you want to remove its Telegram UI entry entirely.

Offers created before PostgreSQL is configured were held only in process memory and cannot be migrated; offers created after the database is configured persist across restarts.

The bot must be an admin in `MARKET_ACCESS_CHAT_ID` for member checks.

Open [https://quest-market.onrender.com](https://quest-market.onrender.com) to request a blank page and wake the Render service. The `Wake Render` GitHub Actions workflow also requests `/health` every five minutes to reduce idle spin-downs on the free Render service. Each run logs the UTC request time, HTTP status, and response time in its GitHub Actions log. To send each result to a Telegram forum topic, add the bot to the group and configure repository Actions secrets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (the group ID, usually starting with `-100`), and `TELEGRAM_MESSAGE_THREAD_ID` (the numeric ID of the destination topic). Never put the bot token in source code or workflow files. GitHub's scheduled workflows run no more often than every five minutes and may be delayed or skipped. Render may also restart or suspend a free instance, so this is best-effort and does not guarantee 24/7 availability. Reliable always-on operation requires moving the Render web service to a paid plan. GitHub Actions must be enabled for this repository for the scheduled workflow to run.

Offer messages display currency labels as plain text without emoji: `GRAM` or `Stars`.

## Run locally

```sh
python3 -m pip install -r requirements-market.txt
export MARKET_BOT_TOKEN="your-bot-token"
export MARKET_DATABASE_URL="sqlite+aiosqlite:///./data/quest_market.db"
export MARKET_ACCESS_CHAT_ID="-1004447182501"
python3 -m bot.market_main
```

In webhook mode, Render supplies `RENDER_EXTERNAL_URL`; for a different host, set `WEBHOOK_BASE_URL` to its public HTTPS origin. For local polling, leave `BOT_MODE=polling`.
