
# Quest Market

Telegram collectible marketplace bot and Mini App.

## Deploy on Render

Create a Render Blueprint from this private repository. The Blueprint starts the webhook service and serves the Mini App from the same URL. Add these environment variables in Render:

- `MARKET_BOT_TOKEN` — BotFather token for Quest Market.
- `MARKET_ALLOWED_USER_IDS` — optional comma-separated Telegram IDs that always have access.
- `MARKET_ACCESS_CHAT_ID` — Telegram chat whose members can create and publish offers; the bot must be an administrator there.

The catalog is demo inventory; offer prices are entered in GRAM or Telegram Stars. Gift transfers are handled manually through `tg://send_gift?to=questbankk`. Free Render web services may sleep after inactivity, so the first request can take time to wake the service.

Offer messages display currency labels as plain text without emoji: `GRAM` or `Stars`.
