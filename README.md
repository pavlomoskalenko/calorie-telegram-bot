# Telegram Calorie & Macro Tracker Bot

A multi-user Telegram bot for daily calorie and macronutrient tracking. Each user connects their own Google Sheet — just send your service account key and spreadsheet title in Telegram.

Send a meal in any language and the bot will:

- extract ingredients with calories and macros (protein, fat, carbs) via OpenAI,
- write ingredient rows in **Ukrainian** to a `log` tab in your Google Sheet,
- keep a `daily` tab in sync with running totals, target, and deficit.

## Per-user setup

Each user brings their own Google Sheet. No shared credentials are needed.

1. **Create a service account** in Google Cloud (IAM & Admin -> Service Accounts -> Create -> Keys -> Add key -> JSON). Download the `.json` key file.
2. **Share your spreadsheet** with the `client_email` from the JSON (Editor access).
3. **Send the JSON file** to the bot in Telegram as a file attachment (paperclip -> File).
4. **Run `/setsheet <exact title>`** with your spreadsheet title as shown in Google Sheets.

The bot creates `log` and `daily` tabs automatically if they don't exist. Run `/setup` in the bot for step-by-step instructions.

## Commands

| Command | Description |
|---|---|
| _meal text_ | Log a meal, e.g. `2 eggs, toast with butter, coffee`. Works in any language. |
| `/today` | Today's total kcal, target, deficit, and macros (P/F/C). |
| `/settarget <kcal>` | Set calorie target for today. Add a date for another day: `/settarget 2200 2026-04-14`. |
| `/setup` | Per-user Google Sheets setup guide. |
| `/setsheet <title>` | Link your spreadsheet after uploading credentials. |
| `/status` | Check whether your Google Sheets connection is configured. |
| `/help` | Show available commands. |

## How estimation works

The bot instructs the model to follow a strict 4-step procedure for every ingredient:

1. **Extract** each distinct food component from the message.
2. **Estimate grams** — convert pieces, ml, spoons, cups, or vague portions to total grams (using standard weights like 1 egg = 60 g).
3. **Look up per-100 g values** — `kcal_per_100g`, `protein_per_100g`, `fat_per_100g`, `carbs_per_100g` from standard nutrition tables.
4. **Compute** — `value = round(per_100g * grams / 100, 1)` for each metric.

As a safety net, the bot **recomputes** all values server-side from the model's `grams` and `*_per_100g` fields, so arithmetic errors in the model output are corrected before reaching the sheet.

## Sheet layout

**Tab `log`** (source of truth): `logged_at`, `date`, `entry_id`, `ingredient`, `amount`, `unit`, `kcal`, `protein`, `fat`, `carbs`, `raw_input`, `note`

**Tab `daily`** (derived): `date`, `total_kcal`, `target_kcal`, `deficit`, `total_protein`, `total_fat`, `total_carbs`

The `daily` tab is fully recomputed from `log` on every meal log, `/today`, and `/settarget`.

## Running locally

```bash
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN and OPENAI_API_KEY
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

When `WEBHOOK_URL` is empty, the bot uses long-polling — no public URL needed. User configs are stored on disk under `user_data/` by default.

## Environment variables

See `.env.example` for the full list. Key ones:

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | Telegram bot token from @BotFather |
| `OPENAI_API_KEY` | yes | — | OpenAI API key (billed to the bot host) |
| `OPENAI_MODEL` | no | `gpt-4o-mini` | Model for nutrition extraction |
| `TIMEZONE` | no | `UTC` | Default timezone for new users |
| `DEFAULT_TARGET_KCAL` | no | `2000` | Default daily calorie target |
| `USER_CONFIG_BACKEND` | no | auto | `firestore` or `filesystem`. Auto-detects GCP if empty. |
| `FIRESTORE_COLLECTION` | no | `user_configs` | Firestore collection name |
| `USER_DATA_DIR` | no | `user_data` | Directory for filesystem backend |
| `WEBHOOK_URL` | no | — | Cloud Run service URL. Empty = polling mode. |
| `WEBHOOK_SECRET` | no | random | Secret for validating Telegram webhooks |

## Architecture

```
Telegram --> bot.py --> OpenAI API (nutrition extraction)
                    \-> Google Sheets API (per-user logging)
                    \-> Firestore (per-user config persistence)
```

- **Polling mode** for local development (`WEBHOOK_URL` empty).
- **Webhook mode** for Cloud Run (`WEBHOOK_URL` set).
- **Config backend** is pluggable: Firestore for production (survives cold starts), filesystem for local dev. Auto-detected based on environment.

## Notes

- Calorie and macro estimates come from the model; treat as approximate.
- Ingredient names in the sheet are always Ukrainian regardless of input language.
- OpenAI usage is billed to whoever deploys the bot. Only Google Sheets access is per-user.
- The bot overwrites header row 1 if it doesn't match the expected layout. If upgrading from an older version, start a fresh sheet or manually add missing columns.
