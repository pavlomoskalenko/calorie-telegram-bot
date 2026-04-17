# Telegram Calorie Tracker Bot

A Telegram bot that:

- reads your meal message,
- uses OpenAI to estimate calories per ingredient,
- appends rows to a Google Sheet `log` tab,
- keeps a `daily` tab in sync (totals from log, deficit vs target).

## Design: log vs daily

- **`log` is the source of truth** (every ingredient row with `kcal`).
- **`daily` is derived**: for each date, `total_kcal` is the sum of `kcal` in `log` for that calendar `date` (column B in `log`). `deficit = target_kcal - total_kcal`.

After each meal, the bot **recomputes and upserts** that day’s row in `daily` so numbers stay current.

Additionally, a **scheduled job** runs once per day (default 00:05 in `TIMEZONE`) and refreshes **yesterday and today** in `daily`. That covers late-night entries, manual edits in `log`, or edge cases where you want an end-of-day reconciliation without sending another message.

Adjust schedule with `DAILY_SYNC_HOUR` and `DAILY_SYNC_MINUTE` in `.env`.

## Sheet layout

**Tab `log`:** `logged_at`, `date`, `entry_id`, `ingredient`, `amount`, `unit`, `kcal`, `raw_input`, `note`

**Tab `daily`:** `date`, `total_kcal`, `target_kcal`, `deficit`

## 1) Google Cloud and Sheet

1. Create a spreadsheet with tabs named exactly `log` and `daily`.
2. In Google Cloud, enable Google Sheets API and Google Drive API.
3. Create a service account and download its JSON key as `credentials.json` in this project folder.
4. Share the spreadsheet with the service account email (Editor).

## 2) Telegram bot

1. Chat with [@BotFather](https://t.me/BotFather), create a bot, copy the token.

## 3) Environment

```bash
cp .env.example .env
```

Fill:

- `TELEGRAM_BOT_TOKEN`
- `OPENAI_API_KEY`
- `GOOGLE_SHEET_NAME` (exact spreadsheet title)
- `GOOGLE_SHEETS_CREDENTIALS_FILE` (default `credentials.json`)
- `TIMEZONE` (example: `Europe/Kyiv`)
- `DEFAULT_TARGET_KCAL` (used for new days until you set a row or use `/settarget`)
- `DAILY_SYNC_HOUR`, `DAILY_SYNC_MINUTE` (nightly refresh of `daily`)

## 4) Install and run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

## Commands

- Send meal text: e.g. `Lunch: 2 eggs, toast, coffee` (rows go to `log`, then `daily` for today updates).
- `/today` – total, target, deficit for today.
- `/settarget <kcal>` – set target for today (`/settarget 2200 2026-04-14` for a specific date).

## Notes

- Calorie estimates come from the model; treat as approximate.
- The bot may write header row 1 if it does not match the expected headers (see layout above).
