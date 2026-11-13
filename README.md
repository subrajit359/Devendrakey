# DEVENDRAMODS X BYPASS Keysystem

Railway-ready Python + PostgreSQL license server with Telegram admin management.

## Files

Everything is in this one folder. There are no subfolders.

- `app.py` — FastAPI server + Telegram admin bot + PostgreSQL logic
- `requirements.txt` — Python dependencies
- `railway.json` — Railway build/start configuration
- `.env.example` — environment variable template
- `.gitignore` — ignores secrets/cache

## Railway setup

1. Create a Railway project.
2. Add a PostgreSQL service.
3. Add this project as the application service.
4. Set these variables in the application service:

```text
BOT_TOKEN=your Telegram bot token
ADMIN_IDS=your Telegram numeric user ID
API_SECRET=a long random secret
DATABASE_URL=${{Postgres.DATABASE_URL}}
```

If Railway exposes the Postgres variable under another reference name, select the PostgreSQL service's `DATABASE_URL` variable from Railway's variable/reference UI.

5. Deploy.
6. Open `/health` on the generated Railway domain. It should return:

```json
{"ok":true,"database":"ok"}
```

## Telegram commands

```text
/gen 30
/gen 30 5
/Customkey ABCD1234 30 3
/Customkey ABCD1234 30 Unlimited

/reset DEVENDRAMODS-XXXXX-XXXXX-XXXXX-XXXXX
/ban DEVENDRAMODS-XXXXX-XXXXX-XXXXX-XXXXX
/ban DEVENDRAMODS-XXXXX-XXXXX-XXXXX-XXXXX stolen
/unban DEVENDRAMODS-XXXXX-XXXXX-XXXXX-XXXXX

/extend DEVENDRAMODS-XXXXX-XXXXX-XXXXX-XXXXX 30
/delete DEVENDRAMODS-XXXXX-XXXXX-XXXXX-XXXXX
/info DEVENDRAMODS-XXXXX-XXXXX-XXXXX-XXXXX
/list
/stats
```

Only IDs in `ADMIN_IDS` can use these commands.

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
  "key": "DEVENDRAMODS-XXXXX-XXXXX-XXXXX-XXXXX",
  "device_id": "YOUR_DEVICE_FINGERPRINT"
}
```

A license can be used by the configured number of distinct device IDs. Custom keys support a numeric device limit or Unlimited. Existing keys retain a one-device limit.

The server also rejects banned and expired keys.

## Important

Do not put the bot token, database password, or API secret into a public GitHub repository.

A Lua-only device ID can be spoofed. For stronger anti-sharing protection, the client should eventually use an Android Keystore-backed proof instead of relying only on a Lua-generated fingerprint.


## Custom keys

`/Customkey <suffix> <days> <device_no|Unlimited>` creates keys in the format `DEVENDRA_<days>D_<suffix>`.

Examples:

- `/Customkey ABCD1234 30 3` — valid for 30 days, maximum 3 devices.
- `/Customkey ABCD1234 30 Unlimited` — valid for 30 days, no device-count limit.

The suffix must be 4-32 letters or digits and must be unique. The existing `/gen` command continues to create one-device licenses.
