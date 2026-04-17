# Telegram Calorie & Macro Tracker Bot

A Telegram bot that:

- reads your meal message (in any language — Russian, Ukrainian, English, etc.),
- uses OpenAI to extract ingredients with calories and macronutrients (protein, fat, carbs),
- writes ingredient names in **Ukrainian** to a Google Sheet `log` tab,
- keeps a `daily` tab in sync (totals from log, deficit vs target, macro sums).

## How calorie & macro estimation works

The bot instructs the model to follow a strict 4-step procedure for every ingredient:

1. **Extract** each distinct food component from the message.
2. **Estimate grams** — convert pieces, ml, spoons, cups, or vague portions to total grams (using standard weights like 1 egg ≈ 60 g).
3. **Look up per-100 g values** — `kcal_per_100g`, `protein_per_100g`, `fat_per_100g`, `carbs_per_100g` from standard nutrition tables.
4. **Compute** — `value = round(per_100g * grams / 100, 1)` for each metric.

As an extra safety net, the bot **recomputes** `kcal`, `protein`, `fat`, and `carbs` server-side from the model's `grams` and `*_per_100g` fields, so arithmetic errors in the model's output are corrected before they reach the sheet.

## Design: log vs daily

- **`log` is the source of truth** — every ingredient row with `kcal`, `protein`, `fat`, `carbs`.
- **`daily` is derived** — for each date, totals are the sum of the corresponding columns in `log`. `deficit = target_kcal - total_kcal`.

After each meal, the bot **recomputes and upserts** that day's row in `daily` so numbers stay current.

A **scheduled job** runs once per day (default 00:05 in `TIMEZONE`) and refreshes **yesterday and today** in `daily`. That covers late-night entries, manual edits in `log`, or edge cases where you want an end-of-day reconciliation without sending another message.

Adjust schedule with `DAILY_SYNC_HOUR` and `DAILY_SYNC_MINUTE` in `.env`.

## Sheet layout

**Tab `log`:** `logged_at`, `date`, `entry_id`, `ingredient`, `amount`, `unit`, `kcal`, `protein`, `fat`, `carbs`, `raw_input`, `note`

**Tab `daily`:** `date`, `total_kcal`, `target_kcal`, `deficit`, `total_protein`, `total_fat`, `total_carbs`

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
- `OPENAI_MODEL` (default `gpt-4o-mini`)
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

- **Send meal text** — e.g. `2 яйця, тост з маслом, кава з молоком` (rows go to `log`, then `daily` updates). Works in any language; ingredient names in the sheet are always Ukrainian.
- `/today` — total kcal, target, deficit, and macros (P/F/C) for today.
- `/settarget <kcal>` — set calorie target for today (`/settarget 2200 2026-04-14` for a specific date).

## Notes

- Calorie and macro estimates come from the model; treat as approximate.
- The bot overwrites header row 1 if it does not match the expected layout (see above). If upgrading from an older version with fewer columns, insert the new columns (`protein`, `fat`, `carbs`) in the existing sheet or start fresh.
