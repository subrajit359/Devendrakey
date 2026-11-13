# DEVENDRAMODS X BYPASS Keysystem V2

Railway-ready FastAPI + PostgreSQL license server with a Telegram admin dashboard.

## What's new in V3

- Premium dark-style Telegram message layout with consistent headings, separators and status indicators
- Button-first admin dashboard with live license counters
- Clickable paginated key manager and compact license detail cards
- Consistent compact key cards for `/gen`, `/Customkey`, and `/info`
- Device overview dashboard section

## What's new in V2

- Inline Telegram admin dashboard (`/start` and `/help`)
- Key manager with 8-key pages and Previous/Next navigation
- Key search using `/search <key-or-suffix>`
- Expiry watchlist using `/expiring` (next 7 days)
- Dashboard statistics and recent admin activity
- Audit log table for key generation, reset, ban/unban, extend and delete
- Existing key commands and license verification API retained
- Database tables and indexes are initialized automatically at startup

## Railway setup

1. Add a PostgreSQL service to your Railway project.
2. Deploy this project as the application service.
3. Set these variables on the application service:

```text
BOT_TOKEN=your Telegram bot token
ADMIN_IDS=your Telegram numeric user ID
API_SECRET=a long random secret
DATABASE_URL=${{Postgres.DATABASE_URL}}
```

Use Railway's variable-reference picker if your PostgreSQL service has a different name.

4. Deploy and open `/health` on your Railway domain. Expected response:

```json
{"ok":true,"database":"ok"}
```

5. Open your Telegram bot and send `/start` using an account listed in `ADMIN_IDS`.

## Telegram commands

```text
/gen <days> [count]
/Customkey <suffix> <days> <device_no|Unlimited>
/reset <key>
/ban <key> [reason]
/unban <key>
/extend <key> <days>
/delete <key>
/info <key>
/search <key-or-suffix>
/list
/expiring
/stats
```

Only numeric Telegram user IDs in `ADMIN_IDS` can use admin commands and dashboard callbacks.

## Device limits

- `/gen <days> [count]` creates FREE keys with the default one-device limit.
- `/Customkey ABCD1234 30 3` creates a custom key with up to 3 devices.
- `/Customkey ABCD1234 30 Unlimited` creates a custom key without a device-count limit.
- `/reset <key>` clears all registered devices for that key.

## License verification API

Endpoint:

```text
POST /api/verify
```

Header:

```text
X-API-Secret: YOUR_API_SECRET
```

JSON:

```json
{
  "key": "YOUR_LICENSE_KEY",
  "device_id": "YOUR_DEVICE_FINGERPRINT"
}
```

The server rejects invalid, banned, expired and over-limit licenses. Device registrations are stored in PostgreSQL.

## Important deployment notes

- Keep `BOT_TOKEN`, `DATABASE_URL`, and `API_SECRET` private. Never commit real secrets.
- Run only one active polling instance for a given Telegram bot token.
- V2 source has been syntax-checked. A live Telegram/PostgreSQL integration test still needs to be performed in your Railway environment.
- A client-generated device fingerprint can be spoofed; stronger binding requires an app-side design using Android Keystore-backed proof.

## V3 dashboard

Send `/start` to open the button dashboard. Use the dashboard buttons to generate keys, create custom keys, browse licenses, search, inspect devices, view statistics, check expiring keys, and review admin activity. Select a row in All keys to open its compact license card. Long-press the displayed key text in Telegram to copy it.


## V4 Premium management updates

- Fixed Telegram message line breaks in dashboard and license manager text.
- License manager supports multi-select across pages and select/deselect current page.
- Bulk actions: ban, unban, pause, resume, and delete (with confirmation).
- Individual license detail view includes ban/unban/pause/resume actions.
- Paused licenses are rejected by the verification API with HTTP 403.
- Existing databases are migrated automatically with a `paused` column at startup.
