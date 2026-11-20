# Telegram control bot

Install with `python -m pip install -r requirements.txt` in a virtual environment.

Keep your existing `credentials.json` beside `api.py` and `bot.py`. If you do not have one, create it as a JSON object using the setting names below as keys and your existing values as strings. No environment variables are required; any that are already set override the corresponding file values. Keep `credentials.json` private: Git ignores it. On Linux/macOS, use `chmod 600 credentials.json`.

| Setting | Service | Value |
| --- | --- | --- |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | API | Telegram application credentials |
| `ENCRYPTION_KEY_HEX` | API | 32-byte key encoded as 64 hex characters |
| `BACKEND_API_TOKEN` | Both | Same high-entropy secret on both services |
| `TELEGRAM_BOT_TOKEN` | Bot | BotFather token |
| `BOT_OWNER_IDS` | Bot | Comma-separated numeric Telegram user IDs, e.g. `"123456789,987654321"` |
| `BOT_OWNER_ID` | Bot | Optional legacy single owner ID; combined with `BOT_OWNER_IDS` |
| `BACKEND_URL` | Bot | Defaults to `http://127.0.0.1:11584` |

Run `python api.py` and `python bot.py` in separate terminals. The API binds to loopback by default. All endpoints except `/health` require `Authorization: Bearer <BACKEND_API_TOKEN>`.

The previously hardcoded bot token and encryption key should be treated as exposed. Rotate the bot token and revoke affected Telegram sessions. A fresh encryption key cannot decrypt sessions encrypted with the old key; keep any required database backup outside the repository and re-register accounts with new sessions.

Run offline regression tests with `python -m unittest discover -s tests`.

Run the API with **one worker**: live Telegram sessions and voice connections are process-local. `DB_PATH` optionally selects the database path. Backup writes a separate `<database>.backup` snapshot on the same volume. Restore reloads the active database; to use the snapshot for disaster recovery, stop the API and copy the snapshot over the active file first. Restore interrupts active voice calls. Logout removes the locally saved session; revoke it in Telegram's Devices settings if server-side revocation is needed. The bot tries to delete messages containing an OTP or 2FA password, but Telegram private bot chats are not end-to-end encrypted and deletion does not undo prior delivery.

The bot runs one command at a time across all owners. The owner who started it can finish it or use `/cancel`. Each command has a 15-minute deadline from when it starts, including time waiting for OTPs or confirmations. When the deadline expires, start the command again.
