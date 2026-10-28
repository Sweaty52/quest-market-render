
# Quest Market

Telegram collectible marketplace bot and Mini App.

## Deploy on Render

Create a Render Blueprint from this private repository. The Blueprint starts the webhook service and serves the Mini App from the same URL. Add these environment variables in Render:

- `MARKET_BOT_TOKEN` — BotFather token for Quest Market.
- `MARKET_ALLOWED_USER_IDS` — comma-separated Telegram IDs allowed to publish inline offers.

The catalog is demo inventory; offer prices are entered in GRAM or Telegram Stars. Gift transfers are handled manually through `tg://send_gift?to=questbankk`. Free Render web services may sleep after inactivity, so the first request can take time to wake the service.

Offer messages display GRAM (`5264766603584641330`) and Stars (`5220029283631672529`) as Telegram custom emoji, without currency-name text. The bot retrieves each custom emoji's associated Unicode emoji from Telegram and uses it as the entity's fallback character. If Telegram rejects the custom emoji entity, the bot retries the offer with a standard emoji.
